"""Unified authentication & registration flow, plus session refresh/teardown.

The single entry point into the app: a caller either proves ownership of an email
address (the magic link, or the six-digit code printed beside it in the same email) or
authenticates with Google, and *only then* do we ask "does an account already exist for
this identity?" (`resolve_identity`, in `services.auth_service`).
If so, they're signed in immediately. If not, a temporary onboarding session is issued
and no `User` row is created until profile completion (`POST /auth/complete`) succeeds -
there are no unverified users, and there is no separate sign up flow.

A passkey assertion (`POST /auth/passkey/options`/`verify`) is the third way in, and the
one that skips that question: a credential row names its account outright, so there is
nothing to resolve and no onboarding branch to reach. Registering one is the authenticated
half of the feature and lives in `api.v1.passkeys`.

All four ways in share one funnel (`_start_onboarding_or_sign_in`), and it has a third
answer besides "signed in" and "onboard first": the identity resolves to an account inside
its deletion grace period. Nothing is signed in and nothing is changed - the caller is
handed a restore token and the purge date, and `POST /auth/restore` is the explicit click
that brings the account back. That is also why the restore endpoint lives here rather than
under `/user`, where `get_current_user` would 401 on the very accounts it serves.

`POST /auth/refresh`/`POST /auth/logout` also live here (see `DECISIONS.md`) - they
used to sit in their own `login.py`/`logout.py` modules under a stale `"login"` tag,
left over from the old password-based flow.
"""

import hmac
import logging
import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

from fastapi import APIRouter, Cookie, Depends, Request, Response
from jose import JWTError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.db.database import async_get_db, release_read_transaction
from ...core.exceptions.http_exceptions import (
    BadRequestException,
    DuplicateValueException,
    ForbiddenException,
    UnauthorizedException,
)
from ...core.schemas import OnboardingTokenData
from ...core.security import (
    TokenType,
    blacklist_token,
    blacklist_tokens,
    create_onboarding_token,
    create_restore_token,
    exchange_google_code,
    generate_secure_token,
    generate_sign_in_code,
    hash_sign_in_code,
    hash_token,
    oauth2_scheme,
    revocation_time,
    token_session_id,
    token_subject,
    verify_google_id_token,
    verify_onboarding_token,
    verify_restore_token,
    verify_token,
)
from ...core.utils.client_ip import client_ip
from ...core.utils.rate_limit import enforce_rate_limit
from ...core.utils.request_context import RequestContext
from ...crud.crud_auth_audit_events import record_auth_event
from ...crud.crud_authentication_providers import crud_authentication_providers
from ...crud.crud_authentication_requests import (
    claim_authentication_request,
    crud_authentication_requests,
    register_failed_code_attempt,
)
from ...crud.crud_invitations import accept_invitations
from ...crud.crud_user_sessions import live_session_for, revoke_session
from ...crud.crud_users import crud_users
from ...models.user import User
from ...schemas.auth import (
    AuthOutcome,
    EmailAuthRequest,
    EmailAuthRequestResponse,
    EmailCodeVerifyRequest,
    EmailVerifyRequest,
    GoogleAuthRequest,
    LinkCheckResponse,
    ProfileCompletionRequest,
    RestoreRequest,
)
from ...schemas.auth_audit_event import AuthEventType
from ...schemas.authentication_provider import AuthenticationProviderCreate
from ...schemas.authentication_request import (
    AuthenticationRequestCreate,
    AuthenticationRequestRead,
    AuthenticationRequestUpdate,
)
from ...schemas.user import UserBootstrapCreateInternal, UserCreateInternal, UserReadInternal
from ...schemas.webauthn_credential import PasskeySignInOptions, PasskeySignInVerifyRequest
from ...services.auth_service import (
    AuthenticatedUser,
    DeletionPending,
    OnboardingRequired,
    issue_tokens,
    resolve_identity,
)
from ...services.dive_form_presets import seed_default_presets
from ...services.email_service import send_magic_link_email
from ...services.passkey_service import finish_sign_in, start_sign_in
from ...services.registration_gate import admit_or_refuse, refuse_uninvited
from ...services.user_avatars import import_google_avatar

router = APIRouter(prefix="/auth", tags=["auth"])

logger = logging.getLogger(__name__)

# The one thing `POST /auth/email/verify-code` ever says about a code it won't accept.
# Wrong digits, a `request_id` naming no row, an expired or superseded request, and a code
# whose attempts ran out are all this sentence, so that nothing about the row leaks to a
# caller who has not already proven they are the browser that asked for it.
_CODE_REJECTED = "This code is invalid or has expired."

# `POST /auth/restore`'s answer to a token it will not spend. Asked twice - once before the
# row is locked and once as the `IntegrityError` a concurrent second submission raises on
# the blacklist insert - so the two must say the same thing.
_RESTORE_REJECTED = "This restore link is invalid, has expired, or has already been used."


def _has_expired(auth_request: dict[str, Any]) -> bool:
    """Whether a request's `expires_at` is in the past, tolerating a naive timestamp.

    The column is `TIMESTAMPTZ` and asyncpg hands back an aware `datetime`, but a row read
    through a driver or a test double that doesn't is a `TypeError` on the comparison
    rather than a wrong answer - so the coercion is here, once, instead of at each of the
    three sites that ask the question.
    """
    expires_at: datetime = auth_request["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at < datetime.now(UTC)


async def _start_onboarding_or_sign_in(
    response: Response,
    outcome: AuthenticatedUser | OnboardingRequired | DeletionPending,
    *,
    db: AsyncSession,
    context: RequestContext,
) -> AuthOutcome:
    """Turn a verified identity into a signed-in session, an onboarding handoff, or the
    offer of a deleted account back.

    Shared by every entry point that proves who someone is (magic link, six-digit code,
    Google, passkey), because each of them faces the same fork: a `User` row already exists
    for this identity, or it doesn't and one has to be created by `POST /auth/complete`, or
    it exists and is inside its deletion grace period. In the second case no user is created
    here - the caller gets a short-lived onboarding token carrying the verified email and
    profile, which is the only thing that lets `/auth/complete` trust them.

    **The third branch is why all four paths funnel through one function.** A restore is
    offered, never performed: no session is minted and nothing about the account changes, so
    signing in cannot silently cancel a deletion somebody deliberately asked for. What the
    caller gets is a `restore_token` and the date, and `POST /auth/restore` is the click
    that acts on them. Writing that branch here is what makes it true on every provider at
    once - including the passkey path, whose resolve site is `finish_sign_in` rather than
    `resolve_identity` and which reaches this function anyway.

    A passkey assertion can only ever take the first or third branch, since a credential
    exists only because a signed-in user registered it. It comes through here rather than
    calling `issue_tokens` directly: token shape, cookie mechanics and every future outcome
    variant then stay in one place instead of two that have to be kept in step.

    **All three audit events for a verified identity are emitted here, and sign-in-succeeded
    is emitted here *only*.** All four providers reach this function - including passkeys,
    whose resolve site is `finish_sign_in` rather than `resolve_identity`, and which funnels
    through here anyway - so emitting at the resolve sites instead would double-count, and
    emitting inside `finish_sign_in` would additionally record its `DeletionPending`
    outcome, which mints no tokens and signs nobody in. That is why `AuthenticatedUser`
    carries the provider: it is the one thing this site cannot work out for itself.
    """
    if isinstance(outcome, AuthenticatedUser):
        tokens = await issue_tokens(response, outcome.user["uuid"], db=db, context=context, user_id=outcome.user["id"])
        await record_auth_event(
            db,
            event_type=AuthEventType.SIGN_IN_SUCCEEDED,
            context=context,
            user_id=outcome.user["id"],
            provider=outcome.provider,
        )
        return AuthOutcome(status="authenticated", **tokens)

    if isinstance(outcome, DeletionPending):
        # The row's own address rather than whatever was verified to get here: on the
        # passkey path nothing was, and after an email change the two differ. Disclosing it
        # to this caller is not a leak - reaching this line took a magic link sent to that
        # inbox, a code from it, a Google identity linked to the account, or possession of a
        # registered authenticator.
        restore_token = await create_restore_token(outcome.user["uuid"])
        await record_auth_event(
            db, event_type=AuthEventType.RESTORE_OFFERED, context=context, user_id=outcome.user["id"]
        )
        return AuthOutcome(
            status="deletion_pending",
            restore_token=restore_token,
            purge_after=outcome.purge_after,
            email=outcome.user["email"],
        )

    # The gate, asked here for the person's benefit rather than for the guarantee: nobody
    # should fill in a profile form and be refused at the end of it. Above the token mint
    # and above the audit write, so a refusal costs neither - and it is not an enumeration
    # oracle, because reaching this line took proof of the address (a link delivered to it,
    # the code from that email, or Google's verified claim). The authoritative check is the
    # one inside `complete_profile`'s creating transaction; this one can be overtaken by a
    # revocation and deliberately is not the guarantee.
    #
    # No audit event on the refusal: the vocabulary's membership rule takes a site that
    # commits a row, mints a token or revokes a credential, and a refusal that does none of
    # those is neither rare nor a WARNING (`schemas/auth_audit_event.py`).
    await refuse_uninvited(db, email=outcome.email)

    onboarding_token = await create_onboarding_token(
        OnboardingTokenData(
            email=outcome.email,
            provider=outcome.provider,
            provider_user_id=outcome.provider_user_id,
            name=outcome.name,
            avatar=outcome.avatar,
        )
    )
    # "Registration-request creation": there is no registration table, so the onboarding
    # JWT *is* the registration request, and this is the one place it is minted. The row is
    # written user-less because no account exists yet - one of the genuinely pre-account
    # events, carrying only the address, and swept on the 7-day tier.
    await record_auth_event(
        db,
        event_type=AuthEventType.ONBOARDING_STARTED,
        context=context,
        email=outcome.email,
        provider=outcome.provider,
    )

    # `avatar` is deliberately not on the response, while the onboarding *token* above
    # carries it: the URL is an input to `POST /auth/complete`, which fetches the bytes and
    # stores them as the new account's avatar, and no client ever renders it. Sending it
    # out would be publishing a googleusercontent.com URL the completion form has no use
    # for - and the field it fed was declared, copied and never displayed.
    return AuthOutcome(
        status="onboarding_required",
        onboarding_token=onboarding_token,
        email=outcome.email,
        name=outcome.name,
    )


@router.post("/email/request", response_model=EmailAuthRequestResponse)
async def request_email_link(
    request: Request, body: EmailAuthRequest, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> EmailAuthRequestResponse:
    """Step 1 of the email flow: generates a magic-link token and a six-digit code, and
    emails both.

    Always returns the same generic message, whether or not `email` belongs to an
    existing account - this must never be used to check if someone has signed up.

    The `request_id` in the response is the row's public uuid, and it is handed back for
    one reason: it is the only way to reach `POST /auth/email/verify-code`, which has no
    email-keyed lookup at all. That keeps the code's attempt budget spendable *only* by
    the browser that asked for it - a stranger who knows an address cannot burn a diver's
    code, let alone their link. It is not an oracle either way: a row is minted for every
    address, so the id is a fresh random value whether or not an account exists.
    """
    email = body.email.lower()

    await enforce_rate_limit(
        f"auth:email-request:email:{email}",
        settings.MAGIC_LINK_REQUEST_RATE_LIMIT_PER_EMAIL,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )
    await enforce_rate_limit(
        f"auth:email-request:ip:{client_ip(request)}",
        settings.MAGIC_LINK_REQUEST_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    # Only one *live* link per email: invalidate any previous, still-live request(s)
    # rather than leaving them valid alongside the new one. FastCRUD's `update(...,
    # allow_multiple=True)` raises `NoResultFound` when zero rows match (the common
    # case - most emails won't have one), so check first rather than treating
    # "nothing to invalidate" as an error (mirrors the token-blacklist purge job's
    # `count()`-before-`delete()` pattern in `core.worker.functions`).
    pending_count = await crud_authentication_requests.count(db, email=email, purpose="sign_in", invalidated_at=None)
    if pending_count > 0:
        await crud_authentication_requests.update(
            db=db,
            object=AuthenticationRequestUpdate(invalidated_at=datetime.now(UTC)),
            allow_multiple=True,
            email=email,
            purpose="sign_in",
            invalidated_at=None,
        )

    raw_token = generate_secure_token()
    code = generate_sign_in_code()
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.MAGIC_LINK_TOKEN_EXPIRE_MINUTES)
    created = await crud_authentication_requests.create(
        db=db,
        object=AuthenticationRequestCreate(
            email=email,
            token_hash=hash_token(raw_token),
            code_hash=hash_sign_in_code(code),
            expires_at=expires_at,
            purpose="sign_in",
        ),
        schema_to_select=AuthenticationRequestRead,
        return_as_model=True,
    )

    # **Always user-less, and that is the whole point.** This endpoint "never even queries
    # `crud_users`" - the enumeration protection here is structural rather than a
    # response-shaping trick, and looking the address up to fill in a `user_id` would
    # reverse that guarantee verbatim, in a call no diff reviewer would connect to the
    # paragraph 11,000 lines away that it contradicts. The row carries the address and
    # nothing else, which is exactly what `authentication_request` already stores, for
    # about the same length of time.
    await record_auth_event(
        db, event_type=AuthEventType.AUTH_REQUEST_CREATED, context=RequestContext.from_request(request), email=email
    )

    magic_link_url = f"{settings.FRONTEND_URL}/auth/verify?token={raw_token}"
    await send_magic_link_email(email=email, magic_link_url=magic_link_url, code=code)

    return EmailAuthRequestResponse(request_id=created.uuid)


@router.get("/email/verify/check", response_model=LinkCheckResponse)
async def check_email_link(
    request: Request, response: Response, token: str, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> LinkCheckResponse:
    """Side-effect-free precheck used by the sign-in landing page before it shows the
    "Sign in" button - lets it show an error immediately for a link that's already
    been used, invalidated, or expired (e.g. revisited via the browser's back
    button after already signing in) rather than a misleadingly clickable button,
    and lets it display which email it's about to sign in as. Never marks anything
    used or changes any state.

    It also answers `deletion_pending` for a link into an account inside its deletion grace
    period, so the button can say *Restore my account* instead of *Sign in*. That is a
    nicety this one path can afford and the other three cannot - a typed code, a Google
    dialog and a biometric gesture have no side-effect-free look-before-you-click step, and
    show the same outcome on a screen after the POST instead. What all four share, and what
    actually carries the design, is that redeeming the credential still changes nothing
    about the account.

    The only GET in this module that must not be publicly cached. It is anonymous and
    side-effect-free, which is exactly the shape `ClientCacheMiddleware` marks
    `public, max-age=60` - but the magic-link token sits in the query string and the
    response body is the account's email address, so a shared cache keyed on that URL
    would hand both to whoever asked next. Setting the header here stops the middleware
    from filling one in.
    """
    response.headers["Cache-Control"] = "private, no-store"

    await enforce_rate_limit(
        f"auth:email-verify-check:ip:{client_ip(request)}",
        settings.MAGIC_LINK_VERIFY_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    auth_request = await crud_authentication_requests.get(db=db, token_hash=hash_token(token), purpose="sign_in")
    if auth_request is None or auth_request["invalidated_at"] is not None or auth_request["used_at"] is not None:
        return LinkCheckResponse(valid=False)

    if _has_expired(auth_request):
        return LinkCheckResponse(valid=False)

    # The one lookup this endpoint makes outside `authentication_request`, and the only
    # reason for it: the account this link opens may be inside its deletion grace period,
    # and then the button has to say *Restore my account* rather than *Sign in*. The link
    # itself is still perfectly good, so this is `valid=True` plus a flag - see
    # `LinkCheckResponse`.
    #
    # By email, matching what `verify_email_link` will resolve this same link to: it passes
    # no `provider_user_id`, so `resolve_identity` answers from its email lookup alone and
    # the precheck cannot disagree with the POST that follows it.
    #
    # Still side-effect-free, and no more of an oracle than the line above it: this
    # response already names the address, and only the holder of a live, unspent link sent
    # to that inbox ever sees either.
    user = await crud_users.get(db=db, email=auth_request["email"])
    if user is not None and user["is_deleted"]:
        pending = DeletionPending.for_row(cast(dict[str, Any], user))
        return LinkCheckResponse(
            valid=True, email=auth_request["email"], deletion_pending=True, purge_after=pending.purge_after
        )

    return LinkCheckResponse(valid=True, email=auth_request["email"])


@router.post("/email/verify", response_model=AuthOutcome)
async def verify_email_link(
    request: Request,
    body: EmailVerifyRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> AuthOutcome:
    """Step 2 of the email flow: validates the magic-link token and either signs the
    caller in (existing account) or hands back an onboarding session (new one).

    The token is strictly single-use, as the email that carries it promises: an
    already-`used_at` link is rejected, alongside one that expired or was superseded
    by a newer request (see `request_email_link` and
    `AuthenticationRequest.invalidated_at`). Replay used to be allowed here on the
    grounds that `resolve_identity` is a pure lookup - true, but beside the point,
    since the side effect that matters is `_start_onboarding_or_sign_in` minting a
    fresh refresh cookie good for `REFRESH_TOKEN_EXPIRE_DAYS`. Anyone who reads the
    mail after the recipient has clicked it got a brand-new week-long session.
    """
    await enforce_rate_limit(
        f"auth:email-verify:ip:{client_ip(request)}",
        settings.MAGIC_LINK_VERIFY_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    auth_request = await crud_authentication_requests.get(db=db, token_hash=hash_token(body.token), purpose="sign_in")
    if auth_request is None:
        raise UnauthorizedException("This sign-in link is invalid.")

    # Ordered most-specific-reason-first, matching `check_email_link`: superseded
    # beats used, used beats expired, so the caller is told the one thing that is
    # most useful to know about their link.
    if auth_request["invalidated_at"] is not None:
        raise UnauthorizedException("This sign-in link is no longer valid - a newer one was requested.")

    if auth_request["used_at"] is not None:
        raise UnauthorizedException("This sign-in link has already been used.")

    if _has_expired(auth_request):
        raise UnauthorizedException("This sign-in link has expired.")

    # The authoritative single-use gate, sitting immediately before the session gets
    # minted: the `used_at` check above is a read, and a read plus a later write is two
    # statements two concurrent submissions can both walk through. See
    # `claim_authentication_request` for why this can't be a filtered FastCRUD `update`.
    if not await claim_authentication_request(db, request_id=auth_request["id"]):
        raise UnauthorizedException("This sign-in link has already been used.")

    context = RequestContext.from_request(request)
    outcome = await resolve_identity(db=db, provider="email", email=auth_request["email"], context=context)
    return await _start_onboarding_or_sign_in(response, outcome, db=db, context=context)


@router.post("/email/verify-code", response_model=AuthOutcome)
async def verify_email_code(
    request: Request,
    body: EmailCodeVerifyRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> AuthOutcome:
    """The other half of step 2: the six-digit code from the sign-in email, typed back
    into the tab that asked for it. Signs the caller in, or hands back an onboarding
    session, exactly as the link does.

    This exists because a link signs in *the device that opens it*, and mail is very often
    read somewhere else - the one magic-link failure the explicit-click precheck never
    addressed. Both credentials ride the same row and end at the same
    `claim_authentication_request`, so a link and a code racing on one request resolve the
    way two links do: one wins, the other is told it has already been used.

    **`request_id` is not decoration.** There is deliberately no lookup by email here. The
    id was handed only to the browser that made the request, so the only party who can
    spend this code's attempts is the one who asked for it - and an earlier design without
    it let anyone who knew an address fire five wrong guesses at whatever request the
    victim had live, which, if a burnt code took its link with it, is sign-in denial aimed
    at exactly the accounts (email-only, self-hosted) whose recovery path is that inbox. It
    also removes a genuine nondeterminism: two tabs can each leave a live row for the same
    address, and an `(email, live)` lookup would resolve them by an unordered `.first()`.

    Every rejection is the same sentence (`_CODE_REJECTED`), including the case where the
    `request_id` names nothing at all.

    A code spent on reaching a `deletion_pending` screen is spent: the request is claimed
    before the identity is resolved, and the screen has to say so, because closing that tab
    costs a fresh code. The link reaches the same screen without that cost - not because
    `verify_email_link` orders those two steps any differently (it does not), but because
    the link path has `GET /auth/email/verify/check` in front of it, which answers
    `deletion_pending` while marking nothing used. The code has no such precheck to reach.
    """
    await enforce_rate_limit(
        f"auth:email-verify-code:ip:{client_ip(request)}",
        settings.MAGIC_LINK_VERIFY_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    auth_request = await crud_authentication_requests.get(db=db, uuid=body.request_id, purpose="sign_in")
    if (
        auth_request is None
        or auth_request["code_hash"] is None
        or auth_request["invalidated_at"] is not None
        or auth_request["used_at"] is not None
        or _has_expired(auth_request)
    ):
        raise UnauthorizedException(_CODE_REJECTED)

    context = RequestContext.from_request(request)

    if not hmac.compare_digest(auth_request["code_hash"], hash_sign_in_code(body.code)):
        # Charged before the 401 is raised, and atomically - the cap is the whole defense
        # of a six-digit secret, so it must not be walkable by firing guesses in parallel.
        await register_failed_code_attempt(
            db, request_id=auth_request["id"], max_attempts=settings.SIGN_IN_CODE_ATTEMPTS_MAX
        )
        # Committed before the raise, like the attempt charge above it: `async_get_db` does
        # not commit on unwind, so a row left in flight on this path is silently lost.
        # Never the code or its digest - the fact of the failure, never the artifact.
        await record_auth_event(
            db,
            event_type=AuthEventType.SIGN_IN_CODE_FAILED,
            context=context,
            email=auth_request["email"],
            provider="email",
        )
        raise UnauthorizedException(_CODE_REJECTED)

    # The authoritative single-use gate, same as the link's - see
    # `claim_authentication_request`. A correct code that loses this race lost to the link
    # in its own email, or to a second tab, and either way a session was already issued.
    if not await claim_authentication_request(db, request_id=auth_request["id"]):
        raise UnauthorizedException(_CODE_REJECTED)

    outcome = await resolve_identity(db=db, provider="email", email=auth_request["email"], context=context)
    return await _start_onboarding_or_sign_in(response, outcome, db=db, context=context)


@router.post("/google", response_model=AuthOutcome)
async def auth_with_google(
    request: Request,
    body: GoogleAuthRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> AuthOutcome:
    """Redeems a Google authorization code and either signs the caller in (existing
    account, linking the Google provider first if it hasn't been already) or hands back an
    onboarding session (new account).

    The visitor's browser never loads or runs anything of Google's. It navigates to
    Google's authorization endpoint, comes back to `{FRONTEND_URL}/auth/google/callback`
    with a single-use code, and posts that code here; this server redeems it at Google's
    token endpoint with `GOOGLE_CLIENT_SECRET` and the PKCE verifier, and verifies the ID
    token that comes back. Nothing Google issues that identifies anyone ever passes through
    the browser.

    Three distinct failures, which is the point of the shape:

    - **400** - the `redirect_uri` is not the one this instance's `FRONTEND_URL` derives.
      A diagnostic rather than a control (Google binds the code to the URI it saw, and will
      not accept an unregistered one), and it exists so a `FRONTEND_URL` disagreeing with
      the origin the visitor reached fails here, naming the setting, rather than as a
      `redirect_uri_mismatch` that names neither.
    - **401** - Google refused the code, or the ID token it returned does not check out.
    - **503** - Google could not be reached, failed, or throttled this server. Raised from
      `exchange_google_code`, and deliberately not the 401: this server failing is not the
      visitor's credential failing.
    """
    await enforce_rate_limit(
        f"auth:google:ip:{client_ip(request)}",
        settings.MAGIC_LINK_VERIFY_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    expected_redirect_uri = settings.google_redirect_uri
    if body.redirect_uri != expected_redirect_uri:
        raise BadRequestException(
            f"This instance expects Google to redirect to {expected_redirect_uri}, but the sign-in "
            f"attempt used {body.redirect_uri}. Set FRONTEND_URL to the origin visitors actually "
            "reach, and register that callback URL against your Google OAuth client."
        )

    id_token = await exchange_google_code(
        code=body.code, code_verifier=body.code_verifier, redirect_uri=body.redirect_uri
    )
    if id_token is None:
        raise UnauthorizedException("Invalid Google credential.")

    google_user = await verify_google_id_token(id_token)
    if google_user is None:
        raise UnauthorizedException("Invalid Google credential.")

    context = RequestContext.from_request(request)
    outcome = await resolve_identity(
        db=db,
        provider="google",
        email=google_user.email,
        context=context,
        provider_user_id=google_user.google_id,
        name=google_user.name,
        avatar=google_user.avatar,
    )
    return await _start_onboarding_or_sign_in(response, outcome, db=db, context=context)


@router.post("/passkey/options", response_model=PasskeySignInOptions)
async def passkey_sign_in_options(request: Request) -> PasskeySignInOptions:
    """Step 1 of signing in with a passkey: mints a challenge and the assertion options
    the browser hands to `navigator.credentials.get()`.

    Anonymous and deliberately incurious - `allowCredentials` is empty, so this request
    names no account and can reveal nothing about any. Discoverable credentials are what
    buy that: the assertion itself carries the credential id, and the credential row names
    the user, so no email-first step exists for a "does this address have a passkey"
    oracle to hide in.

    The ceiling is `AUTH_REFRESH_RATE_LIMIT_PER_IP`'s, and for the same reason: conditional
    UI arms on every signed-out page view that supports it, landing hero included, and an
    office behind one NAT gateway is a single IP to this counter.

    503 when Redis is unreachable. The challenge store is the anti-replay guarantee, so it
    fails closed where rate limiting fails open - and the magic link, being pure Postgres,
    keeps working through exactly that outage. Three methods with independent failure
    domains is the design, not an accident.
    """
    await enforce_rate_limit(
        f"auth:passkey-options:ip:{client_ip(request)}",
        settings.PASSKEY_OPTIONS_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    flow_id, options = await start_sign_in()
    return PasskeySignInOptions(flow_id=flow_id, options=options)


@router.post("/passkey/verify", response_model=AuthOutcome)
async def passkey_sign_in_verify(
    request: Request,
    body: PasskeySignInVerifyRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> AuthOutcome:
    """Step 2: verifies the assertion and signs in whoever owns the credential.

    The challenge is spent on the *attempt*, not on success, so a captured assertion can
    never be retried against a still-live one. An unknown credential, an expired or
    already-spent challenge, a wrong origin, a wrong RP ID and a bad signature are one
    indistinguishable 401 - see `passkey_service.finish_sign_in`.

    An owner whose account is pending deletion is the one case that is *not* that 401: a
    verified assertion answers `deletion_pending` and offers the account back, which used
    to be a dead end with nothing explaining it. The branch sits after verification, so it
    tells nobody anything they had not already proved.

    Never resolves to onboarding, since a credential can only exist because a signed-in
    user registered it. It goes through the shared funnel anyway: that is what makes token
    shape, cookie mechanics and every future outcome variant free here instead of a second
    copy to keep in step.
    """
    await enforce_rate_limit(
        f"auth:passkey-verify:ip:{client_ip(request)}",
        settings.PASSKEY_VERIFY_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    context = RequestContext.from_request(request)
    outcome = await finish_sign_in(db=db, flow_id=body.flow_id, credential=body.credential, context=context)
    return await _start_onboarding_or_sign_in(response, outcome, db=db, context=context)


@router.post("/complete", response_model=AuthOutcome)
async def complete_profile(
    request: Request,
    body: ProfileCompletionRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> AuthOutcome:
    """Creates the `User` row (and its `AuthenticationProvider` link) for a verified
    identity that had no account yet, then signs the new user in. This is the only place
    *self-service* registration ever creates a `User` row - two operator paths also write
    one and are not registration: `scripts/create_first_superuser.py`, which is excluded
    from the shipped image, and the admin panel's `User` view, both of which bypass the
    gate below by construction.

    **On an `invite`-mode instance this refuses (403) an address with no live
    invitation**, and creates nothing. The check is `services.registration_gate.
    admit_or_refuse` and it runs *below* `release_read_transaction`, inside the transaction
    that inserts the row, for the reason spelled out at that call - the helper rolls back,
    so a check above it decides nothing. The first account on an empty instance is admitted
    in either mode and is created with `is_superuser = true`.

    Rate limited per-IP because the username check below is an availability oracle:
    someone holding a single onboarding token could otherwise walk a wordlist through
    it and learn which usernames are taken.

    A Google sign-up arrives with a picture, and this is where it becomes the account's
    avatar: the URL rode the verified ID token into the onboarding session, and the bytes
    are fetched and normalized here rather than stored as a link to somebody else's CDN.
    Inline rather than a background job - one bounded fetch, once per account ever, and
    the avatar is there on the first dashboard paint instead of a flash of initials. It is
    non-fatal in every failure mode (see `import_google_avatar`) and it is a one-off:
    signing in later never re-imports, because by then the picture is the diver's to
    manage and overwriting it because Google's changed would be Gravatar in new clothes.

    The new account is seeded with the three default dive-form presets in the same
    transaction: a registration either creates the account with them or creates nothing.
    """
    await enforce_rate_limit(
        f"auth:complete:ip:{client_ip(request)}",
        settings.AUTH_COMPLETE_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    token_data = await verify_onboarding_token(body.onboarding_token, db)
    if token_data is None:
        raise UnauthorizedException("This onboarding session is invalid or has expired.")

    if await crud_users.exists(db=db, username=body.username):
        raise DuplicateValueException("Username not available")

    if await crud_users.exists(db=db, email=token_data.email):
        # Someone else completed onboarding for this email first - e.g. a concurrent
        # request reusing the same onboarding token/browser tab.
        raise DuplicateValueException("An account with this email already exists")

    # The duplicate checks above autobegan a transaction, and what follows is a network
    # fetch plus an image decode - exactly the idle-in-transaction hold
    # `release_read_transaction` exists to prevent. Nothing live is held across it: the
    # checks returned bare booleans and `token_data` is a Pydantic model.
    await release_read_transaction(db)
    avatar = await import_google_avatar(token_data.avatar)

    context = RequestContext.from_request(request)

    try:
        # **The gate, and it has to be here rather than above.** `release_read_transaction`
        # above *rolls back*, so a gate query, an emptiness check or an advisory lock taken
        # before it is discarded before anything is written - a check whose answer is
        # thrown away is not a check. Below it, everything down to `db.commit()` is one
        # transaction, which is what makes a revocation or a mode flip committed between
        # verification and completion honoured, and what makes "account created" and
        # "invitation accepted" a single commit.
        bootstrap = await admit_or_refuse(db, email=token_data.email)

        user_fields = {
            "name": body.name,
            "username": body.username,
            "email": token_data.email,
            "avatar_storage_key": avatar.storage_key if avatar else None,
            "avatar_sha256": avatar.sha256 if avatar else None,
        }
        user_internal = UserBootstrapCreateInternal(**user_fields) if bootstrap else UserCreateInternal(**user_fields)

        created_user = await crud_users.create(
            db=db, object=user_internal, commit=False, schema_to_select=UserReadInternal, return_as_model=True
        )
        await crud_authentication_providers.create(
            db=db,
            object=AuthenticationProviderCreate(
                user_id=created_user.id, provider=token_data.provider, provider_user_id=token_data.provider_user_id
            ),
            commit=False,
        )
        # The three default dive-form presets, in the same transaction as the account:
        # a registration either creates the account with them or creates nothing. This is
        # the one place self-service registration makes a `User` row, so it is the one
        # place they can be seeded eagerly - the other two writers (the admin panel's
        # generic insert and `scripts/create_first_superuser.py`) are not registration and
        # reach the same three through `POST /dive-form-presets/defaults` instead.
        await seed_default_presets(db, user_id=created_user.id, commit=False)
        # In the same transaction as the account, which is the whole invariant: an address
        # whose account exists must have no live invitation left, or the gate would admit
        # it a second time. Every live invitation for it, not one - two members may each
        # have invited the same friend, and both of them should see that the friend
        # arrived. Emits no event of its own: this is the same commit as
        # `ACCOUNT_CREATED`, and the vocabulary is one event per site.
        await accept_invitations(db, email=token_data.email.lower(), commit=False)
        # "Registration-request completion", in the same transaction as the account it
        # records - so an account that fails to be created leaves no event saying it was.
        # One event for the whole act: the provider row above gets none of its own, because
        # a second row would record account creation twice.
        await record_auth_event(
            db,
            event_type=AuthEventType.ACCOUNT_CREATED,
            context=context,
            user_id=created_user.id,
            provider=token_data.provider,
            commit=False,
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise DuplicateValueException("An account with this email or username already exists") from None
    except ForbiddenException:
        # The gate's refusal, and the rollback is not optional: `admit_or_refuse` takes a
        # transaction-scoped advisory lock, `async_get_db` does not end the transaction on
        # unwind, and a request that raised out of here holding that lock would block every
        # other account creation until its connection was returned to the pool.
        await db.rollback()
        raise

    # Single-use: a second `/auth/complete` call with the same onboarding token must
    # never create (or attempt to create) a second account.
    await blacklist_token(body.onboarding_token, db)

    tokens = await issue_tokens(response, created_user.uuid, db=db, context=context, user_id=created_user.id)
    return AuthOutcome(status="authenticated", **tokens)


@router.post("/restore", response_model=AuthOutcome)
async def restore_account(
    request: Request,
    body: RestoreRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> AuthOutcome:
    """Undo a deletion inside its grace period and sign the account back in.

    The second half of a `deletion_pending` outcome, and the click that makes a restore a
    decision rather than a side effect of signing in. Everything before it was
    read-only: the four entry points verify an identity, resolve it to a flagged row and
    hand back a `restore_token`, and the account stays deleted until this endpoint runs.

    **In `auth.py` rather than as `/user/restore`, and that is not filing.** Every `/user/*`
    route sits behind `get_current_user`, which filters `is_deleted=False` - it would 401
    on precisely the accounts this exists to serve. The restore token is the authority
    here, and it names the account itself, so nothing about the request has to.

    Both columns are cleared, not just the flag: `is_deleted = true, deleted_at IS NULL` is
    a row `purge_deleted_accounts` can never take and never reports as due, so leaving the
    clock set would "restore" the account into a state that only looks alive.

    The row is taken `FOR UPDATE` first, which is what settles the race with the purge. If
    the purge is mid-account, this waits for its transaction and then finds no row - the
    account is gone, and the 401 says so. If this wins, the purge's guarded `DELETE` (which
    repeats `is_deleted AND deleted_at < :cutoff`) matches nothing and logs that the account
    came back. There is no ordering in which a restored account is destroyed or a destroyed
    account appears restored.
    """
    await enforce_rate_limit(
        f"auth:restore:ip:{client_ip(request)}",
        settings.AUTH_COMPLETE_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    user_uuid = await verify_restore_token(body.restore_token, db)
    if user_uuid is None:
        raise UnauthorizedException(_RESTORE_REJECTED)

    locked = (
        await db.execute(select(User.id, User.is_deleted).where(User.uuid == user_uuid).with_for_update())
    ).one_or_none()
    if locked is None:
        # The grace period ran out while the screen was open. Said plainly rather than as
        # the generic 401 above: the caller holds a token this server signed for this
        # account, so there is nothing here they are not entitled to know, and "invalid
        # link" would send somebody hunting for a fresh one that cannot exist.
        raise UnauthorizedException("This account has already been permanently deleted and cannot be restored.")

    if locked.is_deleted:
        await db.execute(update(User).where(User.id == locked.id).values(is_deleted=False, deleted_at=None))

    # Commits the restore with it - `crud_token_blacklist.create` commits the session - so
    # the account coming back and its token being spent are one transaction, and the row
    # lock above is held across both. A second *token* (a fresh one from another entry
    # point) finds the account already live and simply signs in, which is the same answer
    # either way.
    #
    # The same token submitted twice is the case the `except` is for, and it is the only
    # write here that a lock cannot serialize away. `verify_restore_token` reads the
    # blacklist *before* the lock is taken, so a double-click has both requests past that
    # check before either commits; the loser then wakes up holding the lock and tries to
    # insert a `token_blacklist.token` that is unique and already there. That is the
    # deferred half of the check above, so it answers the same 401 rather than the 500 an
    # escaping `IntegrityError` would be. The rollback drops the loser's own UPDATE with
    # it, which is right: the winner has already restored the account.
    try:
        await blacklist_token(body.restore_token, db)
    except IntegrityError:
        await db.rollback()
        raise UnauthorizedException(_RESTORE_REJECTED) from None

    context = RequestContext.from_request(request)
    await record_auth_event(db, event_type=AuthEventType.ACCOUNT_RESTORED, context=context, user_id=locked.id)

    tokens = await issue_tokens(response, user_uuid, db=db, context=context, user_id=locked.id)
    return AuthOutcome(status="authenticated", **tokens)


def _elapsed(since: datetime) -> str:
    """How long ago `since` was, phrased to stay readable at both ends of the range
    `_warn_if_revoked` has to cover.

    Milliseconds are what separate a tab race from a replay, and a replay can arrive days
    after the theft - at which point `518400.000s` is a number nobody reads at a glance.
    So: seconds under a minute, `timedelta`'s own rendering above it.
    """
    seconds = (datetime.now(UTC) - since).total_seconds()
    if seconds < 60:
        return f"{seconds:.3f}s"
    return str(timedelta(seconds=round(seconds)))


# How long after a token was revoked a re-presentation stops being explicable as the
# documented two-tab rotation race and starts being worth an audit row.
#
# The race resolves in **milliseconds** - two tabs refreshing at the same instant, the
# loser landing on this exact branch - and is benign, documented and not especially rare
# (`DECISIONS.md` §"A reused refresh token is a `WARNING`", *The hard part: telling theft
# from the documented race*). An unconditioned audit row would therefore put that same
# cry-wolf defect straight into the audit table, which is what `token_blacklist.revoked_at`
# was added to fix in the log line.
#
# Five seconds is three orders of magnitude above the race and negligible against theft,
# which is replayed minutes or hours later. **Only the audit row is conditioned**: the
# `WARNING` still fires for every presentation, gap attached, exactly as before - so
# nothing that was visible has become invisible, and a row's mere existence now means
# replay-not-race.
_REFRESH_REPLAY_AUDIT_THRESHOLD = timedelta(seconds=5)


async def _warn_if_revoked(refresh_token: str, db: AsyncSession, context: RequestContext) -> None:
    """Log a refresh token that failed verification *because it had been revoked*, which
    is the strongest evidence available that a refresh cookie has been stolen.

    `verify_token` can't report that difference and shouldn't have to - see
    `core.security.revocation_time` for why - so the question is asked again here, on a
    path that has already decided to answer 401. A malformed cookie is noise and stays
    silent; only a token this server actually issued and then spent gets a line.

    `WARNING` rather than `info`, and the level is load-bearing: the app configures no
    logging of its own beyond `configure_logging`, and `uvicorn` configures only its own
    loggers, so anything below `WARNING` is dropped on the floor in exactly the session
    where someone is trying to work out what happened. Same reasoning, at more length, on
    `api.dependencies.fetch_owned_or_raise`.

    **The elapsed time is the point, and it is why `token_blacklist.revoked_at` exists.**
    Rotation's documented two-tab race (see `refresh_access_token`) lands the losing tab
    on this exact branch, so an unconditional "your token was stolen" would cry wolf on a
    benign and not-especially-rare event. That race resolves in milliseconds; a stolen
    cookie is replayed minutes or hours later. The line therefore reports the gap and what
    it means, and leaves the reading to whoever is looking - both are live, and nothing
    the server can see distinguishes them beyond this.
    """
    revoked_at = await revocation_time(refresh_token, db)
    if revoked_at is None:
        return

    subject = token_subject(refresh_token)
    logger.warning(
        "A revoked refresh token was presented (subject: %s) %s after it was revoked. Under a second is "
        "the two-tab rotation race POST /auth/refresh documents; a longer gap is worth investigating as "
        "a stolen cookie.",
        subject or "unknown",
        _elapsed(revoked_at),
    )

    if datetime.now(UTC) - revoked_at < _REFRESH_REPLAY_AUDIT_THRESHOLD:
        # The race, not a replay. See `_REFRESH_REPLAY_AUDIT_THRESHOLD`.
        return

    # The subject resolves to an account with one indexed read, and the cost of that read
    # is bounded by construction: it is only reached past the threshold, which the benign
    # case never is. `None` when the account has already been purged - that row satisfies
    # neither arm of the erasure and is bounded by the user-less retention tier alone.
    #
    # A deliberate amendment to the *Nothing is logged* posture of `DECISIONS.md` §"A
    # refresh token is only as alive as its account": that bullet is about a *deleted
    # account's* devices refreshing on their own schedule, which is recurring and expected.
    # A replay past this threshold is neither.
    user_id: int | None = None
    if subject is not None:
        try:
            user = await crud_users.get(db=db, uuid=uuid_pkg.UUID(subject))
        except ValueError:
            user = None
        if user is not None:
            user_id = cast(dict[str, Any], user)["id"]

    # Committed before the caller raises its 401, since `async_get_db` does not commit on
    # unwind and this is the row the whole conditioning exists to write.
    await record_auth_event(db, event_type=AuthEventType.REFRESH_REPLAY_DETECTED, context=context, user_id=user_id)


@router.post("/refresh")
async def refresh_access_token(
    request: Request, response: Response, db: AsyncSession = Depends(async_get_db)
) -> dict[str, str]:
    """Exchanges the httpOnly `refresh_token` cookie (set by `issue_tokens`) for a new
    access token *and a new refresh token*. See `DECISIONS.md` for why this is the one
    cookie-authenticated endpoint in this flow and why that's still CSRF-safe.

    The presented refresh token is blacklisted and replaced rather than reused. Without
    rotation a single leaked cookie stays valid for the whole
    `REFRESH_TOKEN_EXPIRE_DAYS` window with nothing to revoke it and no way to notice;
    with rotation, the theft has a much shorter useful life and a second use of the same
    token fails outright. The trade-off is that two tabs refreshing at the exact same
    moment will race, and the loser gets a 401 - see `DECISIONS.md`.

    That second use is also the loudest signal this app gets that a cookie has been
    stolen, so it is logged: `_warn_if_revoked` separates "a token we revoked, presented
    again" from "garbage", which the 401 deliberately does not. The *response* is
    identical either way - it must never become an oracle for whether a token was ever
    real.

    A token that verifies is not the same thing as an account that still exists, so the
    subject is resolved before a replacement pair is minted, and a deleted account 401s
    here. Same message as every other failure, for the same oracle reason.

    **Nor is it the same thing as a session that is still live.** The presented token's
    `sid` has to resolve to an unrevoked, unexpired row belonging to this subject, which is
    what makes "sign out my other devices" mean anything: revoking a row kills its refresh
    at the next rotation. A token carrying no `sid` at all - anything minted before this
    feature - fails that check and the diver signs in again. That is a one-time global
    sign-out on upgrade, and deliberately not shimmed.

    All four ways to fail here answer the same uniform 401 the endpoint already answered,
    which is the property `DECISIONS.md` pins rather than one this change gets to relax.
    """
    await enforce_rate_limit(
        f"auth:refresh:ip:{client_ip(request)}",
        settings.AUTH_REFRESH_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise UnauthorizedException("Refresh token missing.")

    context = RequestContext.from_request(request)

    user_data = await verify_token(refresh_token, TokenType.REFRESH, db)
    if not user_data:
        # Only reached on a failure, so the extra lookup costs nothing on the happy path.
        await _warn_if_revoked(refresh_token, db, context)
        raise UnauthorizedException("Invalid refresh token.")

    # Neither `verify_token` nor `issue_tokens` touches the `user` table, so without this
    # a refresh cookie outlives its own user row: `DELETE /user` blacklists only the two
    # tokens presented on that call, and every *other* signed-in device goes on rotating
    # its cookie indefinitely. `get_current_user` filters `is_deleted=False` on every
    # read, so this is the one path where "the account is gone" didn't already mean 401.
    #
    # Asked before the token is spent, so a request that answers 401 writes nothing - and
    # so a soft delete that is reversed leaves the account's other sessions intact, having
    # made them inert rather than destroyed them.
    account = await crud_users.get(db=db, uuid=user_data.user_uuid, is_deleted=False)
    if account is None:
        raise UnauthorizedException("Invalid refresh token.")
    user_id = cast(dict[str, Any], account)["id"]

    # Asked before the token is spent, for the same reason: a refresh against a revoked
    # session must leave that session revoked rather than additionally burning the cookie,
    # so nothing about the failed request is a write.
    if user_data.session_uuid is None:
        raise UnauthorizedException("Invalid refresh token.")
    session = await live_session_for(db, session_uuid=user_data.session_uuid, user_id=user_id)
    if session is None:
        raise UnauthorizedException("Invalid refresh token.")

    # Spend the presented token before minting its replacement, so a crash between the
    # two leaves the caller signed out rather than holding two live refresh tokens.
    await blacklist_token(refresh_token, db)

    # The same `sid`, deliberately: rotation replaces the credential and continues the
    # session, which is the whole difference this table makes. No audit event - an ordinary
    # rotation is once per access-token lifetime per device, which is the recurring,
    # expected class the standing rule excludes, and `last_used_at` already records it.
    return await issue_tokens(
        response, user_data.user_uuid, db=db, context=context, user_id=user_id, session_uuid=session.uuid
    )


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    access_token: str = Depends(oauth2_scheme),
    refresh_token: str | None = Cookie(None, alias="refresh_token"),
    db: AsyncSession = Depends(async_get_db),
) -> dict[str, str]:
    """End the caller's session.

    Blacklists both the access and refresh tokens, stamps `revoked_at` on the session row
    they belong to, and clears the refresh cookie - so the pair stops working immediately
    rather than remaining valid until expiry, and the row stops appearing in
    `GET /user/sessions` on the account's other devices. 401 when no refresh cookie is
    present or either token fails to decode.

    This route authenticates on `oauth2_scheme` alone - no `get_current_user`, so there is
    no `current_user` dict to take an id from - and resolves its subject from the presented
    access token the way `_warn_if_revoked` already does. One indexed read, on a route
    nobody calls in a loop.
    """
    try:
        if not refresh_token:
            raise UnauthorizedException("Refresh token not found")

        await blacklist_tokens(access_token=access_token, refresh_token=refresh_token, db=db)

        session_uuid = token_session_id(access_token)
        if session_uuid is not None:
            await revoke_session(db, session_uuid=session_uuid)

        subject = token_subject(access_token)
        user_id: int | None = None
        if subject is not None:
            try:
                user = await crud_users.get(db=db, uuid=uuid_pkg.UUID(subject))
            except ValueError:
                user = None
            if user is not None:
                user_id = cast(dict[str, Any], user)["id"]

        # Commits the revoke above with it - `record_auth_event` commits by default and
        # the revoke deliberately does not, so signing out and recording that you did are
        # one transaction.
        await record_auth_event(
            db, event_type=AuthEventType.LOGOUT, context=RequestContext.from_request(request), user_id=user_id
        )

        response.delete_cookie(key="refresh_token")

        return {"message": "Logged out successfully"}

    except JWTError:
        raise UnauthorizedException("Invalid token.")
