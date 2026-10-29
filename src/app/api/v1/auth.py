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

`POST /auth/refresh`/`POST /auth/logout` also live here (see `DECISIONS.md`).
"""

import hmac
import logging
import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

from fastapi import APIRouter, Cookie, Depends, Query, Request, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import AccountSource, settings
from ...core.db.database import async_get_db, release_read_transaction
from ...core.exceptions.http_exceptions import (
    BadRequestException,
    DuplicateValueException,
    ForbiddenException,
    RateLimitException,
    UnauthorizedException,
)
from ...core.schemas import OnboardingTokenData
from ...core.security import (
    TokenType,
    bearer_scheme,
    blacklist_token,
    blacklist_tokens,
    create_onboarding_token,
    create_restore_token,
    exchange_google_code,
    generate_secure_token,
    generate_sign_in_code,
    hash_sign_in_code,
    hash_token,
    revocation_time,
    token_session_id,
    token_subject,
    verify_google_id_token,
    verify_onboarding_token,
    verify_restore_token,
    verify_token,
)
from ...core.utils.client_ip import client_ip_bucket
from ...core.utils.rate_limit import enforce_rate_limit
from ...core.utils.request_context import RequestContext
from ...crud.crud_auth_audit_events import record_auth_event
from ...crud.crud_authentication_providers import crud_authentication_providers
from ...crud.crud_authentication_requests import (
    charge_code_attempt,
    claim_authentication_request,
    crud_authentication_requests,
    hold_sign_in_request_quota,
    invalidate_live_requests,
    sign_in_requests_since,
    spend_exhausted_code,
)
from ...crud.crud_daily_totals import count_account_created
from ...crud.crud_invitations import accept_invitations, account_exists_for, record_invite_link_sign_up
from ...crud.crud_invite_requests import delete_invite_requests
from ...crud.crud_user_sessions import live_session_for, revoke_session
from ...crud.crud_users import account_for_email, crud_users
from ...models.user import User
from ...schemas.auth import (
    AuthOutcome,
    EmailAuthRequest,
    EmailAuthRequestResponse,
    EmailCodeVerifyRequest,
    EmailVerifyRequest,
    GoogleAuthRequest,
    LinkCheckResponse,
    LogoutResponse,
    ProfileCompletionRequest,
    RefreshResponse,
    RestoreRequest,
)
from ...schemas.auth_audit_event import AuthEventType
from ...schemas.authentication_provider import AuthenticationProviderCreate
from ...schemas.authentication_request import (
    AuthenticationRequestCreate,
    AuthenticationRequestRead,
)
from ...schemas.user import UserBootstrapCreateInternal, UserCreateInternal, UserReadInternal
from ...schemas.user_session import IdleTimeout
from ...schemas.webauthn_credential import PasskeySignInOptions, PasskeySignInVerifyRequest
from ...services.auth_service import (
    AuthenticatedUser,
    DeletionPending,
    IssuedTokens,
    OnboardingRequired,
    issue_tokens,
    resolve_identity,
    set_refresh_cookie,
)
from ...services.default_tags import seed_default_tags
from ...services.dive_form_presets import seed_default_presets
from ...services.email_service import send_magic_link_email
from ...services.passkey_service import finish_sign_in, start_sign_in
from ...services.refresh_grace import held_successor, hold_successor
from ...services.registration_gate import admit_or_refuse, refuse_a_stale_join_link, refuse_uninvited
from ...services.user_pictures import import_google_avatar, seed_google_avatar

router = APIRouter(prefix="/auth", tags=["auth"])

logger = logging.getLogger(__name__)

# The one thing `POST /auth/email/verify-code` ever says about a code it won't accept.
# Wrong digits, a `request_id` naming no row, an expired or superseded request, and a code
# whose attempts ran out are all this sentence, so that nothing about the row leaks to a
# caller who has not already proven they are the browser that asked for it.
_CODE_REJECTED = "This code is invalid or has expired."

# `POST /auth/restore`'s answer to a token it will not spend. Given twice - once before the
# row is locked and once when a concurrent second submission finds the token already spent
# - so the two must say the same thing.
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
    via: str | None = None,
    idle_timeout: IdleTimeout = IdleTimeout.STANDARD,
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

    `via` is the join-channel slug or the member's invite-link token the identity arrived
    through, if any. It matters only on the onboarding branch, where it can admit an
    uninvited address and is signed into the onboarding token for the completion to count;
    an existing account signing in through either kind of link is simply signed in.

    `idle_timeout` is the window a sign-in asked for, and only the six-digit code asks: it
    is the one way in a native client can take. It reaches the first branch alone - an
    onboarding handoff mints no session, and `POST /auth/complete` starts a `standard` one.
    """
    if isinstance(outcome, AuthenticatedUser):
        tokens = await issue_tokens(
            response,
            outcome.user["uuid"],
            db=db,
            context=context,
            user_id=outcome.user["id"],
            idle_timeout=idle_timeout,
        )
        await record_auth_event(
            db,
            event_type=AuthEventType.SIGN_IN_SUCCEEDED,
            context=context,
            user_id=outcome.user["id"],
            provider=outcome.provider,
        )
        return AuthOutcome(status="authenticated", **tokens.body())

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
    await refuse_uninvited(db, email=outcome.email, via=via)

    onboarding_token = await create_onboarding_token(
        OnboardingTokenData(
            email=outcome.email,
            provider=outcome.provider,
            provider_user_id=outcome.provider_user_id,
            name=outcome.name,
            avatar=outcome.avatar,
            via=via,
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
    """Step 1 of signing in by email: sends `email` a magic link and a six-digit code.

    Anonymous, and rate limited per IP and per address. Both credentials are good for a
    window the instance sets, and a new request supersedes any earlier one still live for
    the same address.

    The response is identical whether or not an account exists for the address, so it never
    says whether someone has signed up. `request_id` identifies this request and is what
    `POST /auth/email/verify-code` takes alongside the code; it is a fresh random value
    either way. The link is redeemed at `POST /auth/email/verify` and needs no id.

    `via` is the join-channel slug or invite-link token of the page the form was on, and is
    carried through to account creation. On an invite-only instance a `via` naming neither a
    live channel nor a member's current link is refused (403) before anything is sent, which
    says whether a link is live and nothing about the address or any account; an open
    instance drops it.
    """
    # `request_id` is handed back for one reason: it is the only way to reach
    # `POST /auth/email/verify-code`, which has no email-keyed lookup at all. That keeps the
    # code's attempt budget spendable *only* by the browser that asked for it - a stranger who
    # knows an address cannot burn a diver's code, let alone their link. Not an oracle either
    # way: a row is minted for every address, so the id is random whether or not an account
    # exists.
    email = body.email.lower()
    # First, so a dead link costs nothing: no row, no audit event, no rate-limit charge.
    via = await refuse_a_stale_join_link(db, body.via)

    # Per-IP first. `enforce_rate_limit` spends a slot before it decides, so a request the
    # per-IP limit refuses would otherwise still have spent the address's own three - and
    # one caller could keep any number of addresses out of email sign-in.
    await enforce_rate_limit(
        f"auth:email-request:ip:{client_ip_bucket(request)}",
        settings.MAGIC_LINK_REQUEST_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )
    await enforce_rate_limit(
        f"auth:email-request:email:{email}",
        settings.MAGIC_LINK_REQUEST_RATE_LIMIT_PER_EMAIL,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )
    # The same per-address limit again, counted in Postgres: the Redis one fails open on an
    # outage, and the request count is what bounds guesses at an address's codes. Same
    # refusal, so it says nothing the Redis one doesn't. The lock is held to the insert's
    # commit below.
    await hold_sign_in_request_quota(db, email=email)
    recent = await sign_in_requests_since(db, email=email, window_seconds=settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS)
    if recent >= settings.MAGIC_LINK_REQUEST_RATE_LIMIT_PER_EMAIL:
        raise RateLimitException("Too many requests. Please try again later.")

    # Only one *live* link per email: invalidate any previous, still-live request(s)
    # rather than leaving them valid alongside the new one.
    await invalidate_live_requests(db, purpose="sign_in", email=email)

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
            via=via,
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
    request: Request,
    response: Response,
    token: Annotated[str, Query(description="The `token` from the magic link's URL")],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> LinkCheckResponse:
    """Whether a magic link can still be redeemed, and for which address, without redeeming it.

    Anonymous, and rate limited per IP. Changes nothing: the link stays exactly as
    redeemable as it was, so a page can ask before it shows a *Sign in* button rather than
    offer one that fails.

    `valid` is `false` for a link that was already used, superseded by a newer request,
    expired, or never issued, and the response says no more than that. When it is `true`,
    `email` is the address the link signs in as; `deletion_pending` is `true`, with
    `purge_after`, when that address's account is inside its deletion grace period, so the
    page can offer to restore the account instead of signing in - redeeming the link still
    changes nothing about the account.

    Never cacheable: the response carries `Cache-Control: private, no-store`.
    """
    # The one GET here that must not be publicly cached. Anonymous and side-effect-free is
    # exactly the shape `ClientCacheMiddleware` marks `public, max-age=60` - but the magic-link
    # token sits in the query string and the body is the account's email address, so a shared
    # cache keyed on that URL would hand both to whoever asked next. Setting the header here
    # stops the middleware from filling one in.
    #
    # `deletion_pending` is a nicety this path can afford and the other three cannot: a typed
    # code, a Google dialog and a biometric gesture have no side-effect-free
    # look-before-you-click step, and show the same outcome on a screen after the POST instead.
    response.headers["Cache-Control"] = "private, no-store"

    await enforce_rate_limit(
        f"auth:email-verify-check:ip:{client_ip_bucket(request)}",
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
    user = await account_for_email(db, email=auth_request["email"])
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
    """Step 2 of signing in by email: redeems the magic link's `token`.

    Anonymous, and rate limited per IP. The outcome is one of three: `authenticated`, with
    an access token in the body and a `refresh_token` cookie on the response, when an
    account exists for the address; `onboarding_required`, with an `onboarding_token` to
    carry to `POST /auth/complete`, when none does; or `deletion_pending`, with a
    `restore_token` for `POST /auth/restore`, when the account is inside its deletion grace
    period - in which case nothing is signed in and nothing about the account changes.

    The token is single-use. 401 for a link that was already used, superseded by a newer
    request, expired, or never issued, and `detail` says which. The link is spent even when
    the outcome is `deletion_pending` or a refusal.

    403 on an invite-only instance when the address has no account, no live invitation and
    arrived through no live join or invite link; nothing is created.
    """
    # Single-use even though resolving the identity is a pure lookup: the sign-in branch mints
    # a fresh refresh cookie good for the whole idle window, so anyone who read the mail after
    # the recipient clicked it would otherwise get a brand-new session.
    await enforce_rate_limit(
        f"auth:email-verify:ip:{client_ip_bucket(request)}",
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
    return await _start_onboarding_or_sign_in(response, outcome, db=db, context=context, via=auth_request.get("via"))


@router.post("/email/verify-code", response_model=AuthOutcome)
async def verify_email_code(
    request: Request,
    body: EmailCodeVerifyRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> AuthOutcome:
    """The other half of step 2: redeems the six-digit `code` from the sign-in email, in the
    client that asked for it.

    Anonymous, and rate limited per IP. `request_id` is the one `POST /auth/email/request`
    returned; the code is accepted against that request alone. The outcomes are the link's:
    `authenticated`, `onboarding_required` or `deletion_pending`, with the same tokens and
    the same 403 on an invite-only instance.

    `idle_timeout` chooses how long the resulting session may sit unused before it ends:
    `standard` or `extended`, each a window the instance sets. Only this operation takes it.

    Every rejection is a 401 with the same `detail`, "This code is invalid or has expired.":
    wrong digits, a `request_id` naming no request, a request that expired or was superseded
    by a newer one, a code already redeemed, and one whose guesses ran out. Guesses are
    capped per request at a number the instance sets; running out spends the code and leaves
    the link in the same email usable. A code that is not six digits, however spaced, is a
    422 and counts as no guess.

    The code and the link redeem the same request, so when both are used one wins and the
    other is refused. A code spent on reaching `deletion_pending` is spent: trying again
    takes a fresh one.
    """
    # The code exists because a link signs in *the device that opens it*, and mail is very
    # often read somewhere else.
    #
    # `request_id` is not decoration: there is deliberately no lookup by email here. The id was
    # handed only to the browser that made the request, so the only party who can spend this
    # code's attempts is the one who asked for it. A lookup by address would let anyone who
    # knew it fire wrong guesses at whatever request the victim had live - sign-in denial
    # aimed at exactly the accounts (email-only, self-hosted) whose recovery path is that
    # inbox - and would be nondeterministic besides: two tabs can each leave a live row for the
    # same address, and an `(email, live)` lookup would resolve them by an unordered
    # `.first()`.
    #
    # The link reaches the `deletion_pending` screen without spending itself only because
    # `GET /auth/email/verify/check` sits in front of it and answers while marking nothing
    # used; this path claims the request before the identity is resolved, exactly as
    # `verify_email_link` does, and has no precheck to reach.
    await enforce_rate_limit(
        f"auth:email-verify-code:ip:{client_ip_bucket(request)}",
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

    # Compared against the digest the charge hands back, never the one read above: the
    # read is a fast path for rows that are plainly dead, and every guess in flight has
    # already passed it. See `charge_code_attempt`.
    max_attempts = settings.SIGN_IN_CODE_ATTEMPTS_MAX
    code_hash = await charge_code_attempt(db, request_id=auth_request["id"], max_attempts=max_attempts)
    if code_hash is None:
        raise UnauthorizedException(_CODE_REJECTED)

    if not hmac.compare_digest(code_hash, hash_sign_in_code(body.code)):
        await spend_exhausted_code(db, request_id=auth_request["id"], max_attempts=max_attempts)
        # Commits the spend above with it, and before the raise: `async_get_db` does not
        # commit on unwind, so a row left in flight on this path is silently lost.
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
    return await _start_onboarding_or_sign_in(
        response, outcome, db=db, context=context, via=auth_request.get("via"), idle_timeout=body.idle_timeout
    )


@router.post("/google", response_model=AuthOutcome)
async def auth_with_google(
    request: Request,
    body: GoogleAuthRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> AuthOutcome:
    """Redeems a Google authorization code and signs the caller in as the Google account's
    address, or hands back an onboarding token when no account exists for it.

    Anonymous, and rate limited per IP. The browser navigates to Google's authorization
    endpoint with a PKCE challenge, returns to the instance's web app at
    `/auth/google/callback` with a single-use `code`, and posts that code here with the
    `code_verifier` and the `redirect_uri` it used; this server redeems the code with Google
    and verifies the identity Google returns, so nothing that identifies the visitor passes
    through the browser.

    The outcomes are the email flow's: `authenticated`, linking Google to the account that
    holds the address where it was not linked yet; `onboarding_required`, with an
    `onboarding_token` for `POST /auth/complete`; or `deletion_pending`, with a
    `restore_token` for `POST /auth/restore` and nothing changed. `via` is the join-channel
    slug or invite-link token the attempt started from, and 403 on an invite-only instance
    is as for the email flow.

    400 when `redirect_uri` is not the callback this instance expects; `detail` names the
    one it does. 401 when Google refuses the code or the identity it returns does not check
    out. 503 when Google could not be reached, failed, or throttled this server.
    """
    # The 400 is a diagnostic rather than a control (Google binds the code to the URI it saw,
    # and will not accept an unregistered one): it exists so a `FRONTEND_URL` disagreeing with
    # the origin the visitor reached fails here, naming the setting, rather than as a
    # `redirect_uri_mismatch` that names neither. The 503 is raised from
    # `exchange_google_code` and is deliberately not the 401: this server failing is not the
    # visitor's credential failing.
    await enforce_rate_limit(
        f"auth:google:ip:{client_ip_bucket(request)}",
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
    return await _start_onboarding_or_sign_in(response, outcome, db=db, context=context, via=body.via)


@router.post("/passkey/options", response_model=PasskeySignInOptions)
async def passkey_sign_in_options(request: Request) -> PasskeySignInOptions:
    """Step 1 of signing in with a passkey: a challenge and the assertion options the browser
    hands to `navigator.credentials.get()`.

    Anonymous, and rate limited per IP. The request names no account and takes no
    identifier: `allowCredentials` is empty, and the assertion the browser produces carries
    the credential that names one. `flow_id` identifies the challenge and goes back with the
    assertion to `POST /auth/passkey/verify`; a challenge is good for one attempt.

    503 when passkey sign-in is temporarily unavailable on this instance; `detail` says to
    use email instead, which keeps working.
    """
    # Discoverable credentials are what make the anonymity possible: the credential row names
    # the user, so no email-first step exists for a "does this address have a passkey" oracle
    # to hide in.
    #
    # The ceiling is `AUTH_REFRESH_RATE_LIMIT_PER_IP`'s, and for the same reason: conditional UI
    # arms on every signed-out page view that supports it, landing hero included, and an office
    # behind one NAT gateway is a single IP to this counter.
    #
    # The 503 is Redis being unreachable. The challenge store is the anti-replay guarantee, so
    # it fails closed where rate limiting fails open - and the magic link, being pure Postgres,
    # keeps working through exactly that outage. Three methods with independent failure domains
    # is the design, not an accident.
    await enforce_rate_limit(
        f"auth:passkey-options:ip:{client_ip_bucket(request)}",
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
    """Step 2 of signing in with a passkey: verifies the assertion and signs in the account
    that registered the credential.

    Anonymous, and rate limited per IP. `flow_id` is the one the options came with and
    `credential` is the assertion as the browser returned it. The outcome is `authenticated`,
    with an access token in the body and a `refresh_token` cookie, or `deletion_pending`,
    with a `restore_token` for `POST /auth/restore`, when the account is inside its deletion
    grace period; never `onboarding_required`, since a passkey exists only on an account.

    The challenge is spent on the attempt, success or not, so a failed attempt starts again
    from `POST /auth/passkey/options`. Every failure - an unknown credential, an expired or
    already-spent challenge, a wrong origin, a bad signature - is a 401 with the same
    `detail`. 503 when passkey sign-in is temporarily unavailable on this instance.
    """
    # Spending the challenge on the attempt is what stops a captured assertion being retried
    # against a still-live one. The uniform 401 is `passkey_service.finish_sign_in`'s; the
    # `deletion_pending` branch sits after verification there, so it tells nobody anything they
    # had not already proved. The outcome still goes through the shared funnel: that is what
    # keeps token shape, cookie mechanics and every future outcome variant in one place rather
    # than a second copy to keep in step.
    await enforce_rate_limit(
        f"auth:passkey-verify:ip:{client_ip_bucket(request)}",
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
    """Creates the account for a verified identity that had none, and signs it in.

    Takes the `onboarding_token` from an `onboarding_required` outcome, with the `name` and
    `username` the account starts with; the address is the one the token was issued for.
    Rate limited per IP. The outcome is `authenticated`, with an access token in the body
    and a `refresh_token` cookie on the response.

    401 for an onboarding token that is invalid, expired, or already used: the token is
    single-use, so a second call with it creates nothing. 422 when `username` is taken or
    an account already exists for the address.

    On an invite-only instance, 403 for an address with no live invitation unless the token
    carries a live join-channel slug or a member's live invite link, and nothing is created;
    where a member's link admitted the address, that member's invitations list the new
    account. The first account on an empty instance is admitted in either mode and is a
    superuser. Any pending invite request for the address is dropped.

    The account starts with the default dive-form presets and tags, and with the picture of
    a Google identity as its avatar, fetched once at creation and never refreshed from
    Google afterwards.
    """
    # The only place *self-service* registration creates a `User` row - the admin panel's
    # `User` view also writes one and is not registration, and bypasses the gate by
    # construction.
    #
    # Rate limited per IP because the username check is an availability oracle: someone
    # holding a single onboarding token could otherwise walk a wordlist through it and learn
    # which usernames are taken.
    #
    # The Google picture's URL rode the verified ID token into the onboarding session, and the
    # bytes are fetched and normalized here rather than stored as a link to somebody else's
    # CDN. Inline rather than a background job - one bounded fetch, once per account ever, and
    # the avatar is there on the first dashboard paint instead of a flash of initials. Non-fatal
    # in every failure mode (see `import_google_avatar`), and a one-off: signing in later never
    # re-imports, because by then the picture is the diver's to manage and overwriting it
    # because Google's changed would be Gravatar in new clothes.
    await enforce_rate_limit(
        f"auth:complete:ip:{client_ip_bucket(request)}",
        settings.AUTH_COMPLETE_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    token_data = await verify_onboarding_token(body.onboarding_token, db)
    if token_data is None:
        raise UnauthorizedException("This onboarding session is invalid or has expired.")

    if await crud_users.exists(db=db, username=body.username):
        raise DuplicateValueException("Username not available")

    if await account_exists_for(db, email=token_data.email.lower()):
        # Someone else completed onboarding for this email first - e.g. a concurrent
        # request reusing the same onboarding token/browser tab - or an account holds it
        # in another case, which the raw unique index below would let a second one beside.
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
        admission = await admit_or_refuse(db, email=token_data.email, via=token_data.via)
        bootstrap = admission.source == AccountSource.BOOTSTRAP

        user_fields = {
            "name": body.name,
            "username": body.username,
            "email": token_data.email,
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
        if avatar is not None:
            await seed_google_avatar(db, user_id=created_user.id, stored=avatar)
        # The three default dive-form presets, in the same transaction as the account:
        # a registration either creates the account with them or creates nothing. This is
        # the one place self-service registration makes a `User` row, so it is the one
        # place they can be seeded eagerly - the admin panel's generic insert is not
        # registration and reaches the same three through `POST /dive-form-presets/defaults`.
        await seed_default_presets(db, user_id=created_user.id, commit=False)
        # The default tags, on the same terms. The admin panel seeds none, and has no
        # restore to reach for: a tag is typed into being wherever one is used.
        await seed_default_tags(db, user_id=created_user.id)
        if admission.invite_link_owner_id is not None:
            # Before `accept_invitations`, so a live typed invitation from the same member is
            # the row that statement stamps rather than one this writes beside it. After the
            # `User` row is built, so the row's `accepted_at` is never earlier than the
            # account's `created_at`, which is what the listing's name lookup is bounded by.
            await record_invite_link_sign_up(
                db, email=token_data.email.lower(), inviter_id=admission.invite_link_owner_id
            )
        # In the same transaction as the account, which is the whole invariant: an address
        # whose account exists must have no live invitation left, or the gate would admit
        # it a second time. Every live invitation for it, not one - two members may each
        # have invited the same friend, and both of them should see that the friend
        # arrived. Emits no event of its own: this is the same commit as
        # `ACCOUNT_CREATED`, and the vocabulary is one event per site.
        await accept_invitations(db, email=token_data.email.lower(), commit=False)
        # The address has an account now, so a request for it has nothing left to ask for -
        # dropped here rather than left in the queue for the operator to remove by hand.
        await delete_invite_requests(db, emails=[token_data.email.lower()], commit=False)
        # The day's count for the door the gate named, and nothing about the account: the
        # total names no one, and the slug is kept nowhere else.
        await count_account_created(db, source=admission.source, now=datetime.now(UTC))
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
    return AuthOutcome(status="authenticated", **tokens.body())


@router.post("/restore", response_model=AuthOutcome)
async def restore_account(
    request: Request,
    body: RestoreRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> AuthOutcome:
    """Undoes a deletion inside its grace period and signs the account back in.

    Takes the `restore_token` from a `deletion_pending` outcome; the token names the account,
    so nothing else does. Rate limited per IP. The account stays deleted until this is
    called - signing in by any method only ever offers the restore - and the outcome is
    `authenticated`, with an access token in the body and a `refresh_token` cookie.

    401 with one `detail` for a token that is invalid, expired, or already used; the token is
    single-use. A different 401 `detail` when the grace period ran out and the account is
    permanently gone. A valid token for an account that was already restored simply signs
    it in.
    """
    # Here rather than under `/user`, and that is not filing: every `/user/*` route sits behind
    # `get_current_user`, which filters `is_deleted=False` - it would 401 on precisely the
    # accounts this exists to serve.
    #
    # Both columns are cleared, not just the flag: `is_deleted = true, deleted_at IS NULL` is a
    # row `purge_deleted_accounts` can never take and never reports as due, so leaving the
    # clock set would "restore" the account into a state that only looks alive.
    #
    # The row is taken `FOR UPDATE` first, which is what settles the race with the purge. If
    # the purge is mid-account, this waits for its transaction and then finds no row - the
    # account is gone, and the 401 says so. If this wins, the purge's guarded `DELETE` (which
    # repeats `is_deleted AND deleted_at < :cutoff`) matches nothing and logs that the account
    # came back. There is no ordering in which a restored account is destroyed or a destroyed
    # account appears restored.
    await enforce_rate_limit(
        f"auth:restore:ip:{client_ip_bucket(request)}",
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

    # Commits the restore with it - `blacklist_token` commits the session - so the account
    # coming back and its token being spent are one transaction, and the row lock above is
    # held across both. A second *token* (a fresh one from another entry point) finds the
    # account already live and simply signs in, which is the same answer either way.
    #
    # The same token submitted twice is the case the check is for, and it is the only
    # write here that a lock cannot serialize away. `verify_restore_token` reads the
    # blacklist *before* the lock is taken, so a double-click has both requests past that
    # check before either commits; the loser then wakes up holding the lock, after the
    # winner's commit, and finds the token already listed. That is the deferred half of
    # the check above, so it answers the same 401. The loser has written nothing by then:
    # it saw the account already live under the lock, so its UPDATE never ran.
    if not await blacklist_token(body.restore_token, db):
        raise UnauthorizedException(_RESTORE_REJECTED)

    context = RequestContext.from_request(request)
    await record_auth_event(db, event_type=AuthEventType.ACCOUNT_RESTORED, context=context, user_id=locked.id)

    tokens = await issue_tokens(response, user_uuid, db=db, context=context, user_id=locked.id)
    return AuthOutcome(status="authenticated", **tokens.body())


def _elapsed(since: datetime) -> str:
    """How long ago `since` was, phrased to stay readable at both ends of the range
    `_handle_revoked_refresh` has to cover.

    Milliseconds are what separate a tab race from a replay, and a replay can arrive days
    after the theft - at which point `518400.000s` is a number nobody reads at a glance.
    So: seconds under a minute, `timedelta`'s own rendering above it.
    """
    seconds = (datetime.now(UTC) - since).total_seconds()
    if seconds < 60:
        return f"{seconds:.3f}s"
    return str(timedelta(seconds=round(seconds)))


def _reuse_grace() -> timedelta:
    """How long after a token was revoked a re-presentation stops being explicable as a
    retried refresh or the documented rotation race, and starts being a replay worth acting
    on - `REFRESH_TOKEN_REUSE_GRACE_SECONDS`, read per call so a test can move it.

    Two benign things land a spent token here. The rotation race resolves in
    **milliseconds** - two tabs refreshing at the same instant, the loser presenting the
    cookie the winner has just spent. A retry takes seconds - a client whose reply was lost
    on a bad connection, presenting the same cookie again. A stolen cookie is replayed
    minutes or hours later. Treating either benign case as a replay would sign a diver out
    for their own tabs or their own boat wifi, and put a cry-wolf row in the audit table
    (`DECISIONS.md` §"A reused refresh token is a `WARNING`").

    A minute covers a retry on a twenty-second request timeout with room for one more, and
    stays far below a theft. **The `WARNING` is not conditioned on it**: that fires for every
    presentation, gap attached. What sits past the grace is the pair of consequences that
    assume replay-not-retry - the revocation and the audit row.
    """
    return timedelta(seconds=settings.REFRESH_TOKEN_REUSE_GRACE_SECONDS)


async def _handle_revoked_refresh(refresh_token: str, db: AsyncSession, context: RequestContext) -> IssuedTokens | None:
    """Answer a refresh token that failed verification *because it had been revoked*,
    which is both how a lost reply's retry arrives and the strongest evidence available
    that a refresh cookie has been stolen: log every presentation; inside the reuse grace
    hand back the pair its first presentation minted, when that pair is still good; and
    past the grace revoke the session the token belongs to and record the replay. `None`
    is the caller's 401.

    `verify_token` can't report that difference and shouldn't have to - see
    `core.security.revocation_time` for why - so the question is asked again here, on a
    path that has already failed verification. A malformed cookie is noise and stays
    silent; only a token this server actually issued and then spent gets a line.

    `WARNING` rather than `info`, and the level is load-bearing: the app configures no
    logging of its own beyond `configure_logging`, and `uvicorn` configures only its own
    loggers, so anything below `WARNING` is dropped on the floor in exactly the session
    where someone is trying to work out what happened. Same reasoning, at more length, on
    `api.dependencies.fetch_owned_or_raise`.

    **The elapsed time is the point, and it is why `token_blacklist.revoked_at` exists.**
    A retry and rotation's documented race (see `refresh_access_token`) both land here, so
    an unconditional "your token was stolen" would cry wolf on benign and not especially
    rare events. The line therefore reports the gap and leaves the reading to whoever is
    looking - nothing the server can see distinguishes the cases beyond this.

    **Inside the grace the answer is the held pair or a 401, and nothing is revoked.** See
    `_held_pair_for_a_retry` for when the pair is handed out; the race's loser, which
    arrives before the winner's pair is held, gets the 401 it always got.

    **Past the grace the session goes, and that is what actually stops the theft.**
    Rotation already made the presented token worthless; what it could not touch is the
    pair minted *from* it, which is what whichever party got there first is holding. Both
    halves of that pair carry the same `sid`, so revoking the row ends them together - the
    cookie at its next rotation, and the access token on its very next request, since
    `api.dependencies.get_current_user` asks the same question of the same row. The other
    party signs in again and gets a new session.

    The revocation is left for the `record_auth_event` below to commit rather than
    committing on its own. They are one event, `async_get_db` does not commit on unwind,
    and the caller's 401 is next - so a row failing to be written must take the revocation
    with it rather than leave a session ended with nothing saying why.
    """
    revoked_at = await revocation_time(refresh_token, db)
    if revoked_at is None:
        return None

    subject = token_subject(refresh_token)
    grace = _reuse_grace()
    logger.warning(
        "A revoked refresh token was presented (subject: %s) %s after it was revoked. Under %.0fs is "
        "a retried refresh or the rotation race POST /auth/refresh documents; a longer gap is worth "
        "investigating as a stolen cookie.",
        subject or "unknown",
        _elapsed(revoked_at),
        grace.total_seconds(),
    )

    if datetime.now(UTC) - revoked_at < grace:
        return await _held_pair_for_a_retry(refresh_token, subject, db)

    # `token_session_id` enforces the signature and the expiry and nothing else, which is
    # exactly what is wanted here: the blacklist row read above already establishes that
    # this server issued the token and then spent it, so the `sid` a valid signature
    # carries names a row this app minted for that token's own subject. There is no
    # unsigned path to another account's session, which is what lets the revoke be by
    # `sid` alone - the same way `POST /auth/logout` revokes.
    #
    # `None` is a token minted before sessions existed, or one whose `exp` passed while
    # its blacklist row waits for the hourly purge. Neither names a session that could
    # still be rotating, so the 401 is already the whole answer.
    session_uuid = token_session_id(refresh_token)
    if session_uuid is not None:
        await revoke_session(db, session_uuid=session_uuid)

    # The subject resolves to an account with one indexed read, and the cost of that read
    # is bounded by construction: it is only reached past the grace, which the benign
    # cases never are. `None` when the account has already been purged - that row satisfies
    # neither arm of the erasure and is bounded by the user-less retention tier alone.
    #
    # A deliberate amendment to the *Nothing is logged* posture of `DECISIONS.md` §"A
    # refresh token is only as alive as its account": that bullet is about a *deleted
    # account's* devices refreshing on their own schedule, which is recurring and expected.
    # A replay past the grace is neither.
    user_id: int | None = None
    if subject is not None:
        try:
            user = await crud_users.get(db=db, uuid=uuid_pkg.UUID(subject))
        except ValueError:
            user = None
        if user is not None:
            user_id = cast(dict[str, Any], user)["id"]

    # Committed before the caller raises its 401, since `async_get_db` does not commit on
    # unwind and this is the row the whole conditioning exists to write. The revocation
    # above rides that commit: one act, one transaction, and still one event - the
    # vocabulary's rule is one event per site, so this row now means "replay detected and
    # its session ended" rather than earning a second `SESSION_REVOKED` beside it.
    await record_auth_event(db, event_type=AuthEventType.REFRESH_REPLAY_DETECTED, context=context, user_id=user_id)
    return None


async def _held_pair_for_a_retry(refresh_token: str, subject: str | None, db: AsyncSession) -> IssuedTokens | None:
    """The pair a spent token's first presentation minted, if handing it out again keeps
    the session's one live refresh token the one it already has.

    Every condition the first presentation passed is asked again, because the grace must
    not outlive what it continues: the account is live, and the session the token names is
    live and the subject's - so a session revoked or signed out inside the minute stays
    ended. Then the held successor must itself still verify: once it has been presented
    and spent in turn, handing it out would give the caller a dead cookie, and the 401 says
    so honestly. A hold that is gone - never written, evicted, Redis down - is the same
    401.

    The *same* pair rather than a fresh one: minting again would spend the successor the
    first presenter may already hold, whose next refresh would then land past the grace
    and revoke the session for both.
    """
    session_uuid = token_session_id(refresh_token)
    if session_uuid is None or subject is None:
        return None
    try:
        account = await crud_users.get(db=db, uuid=uuid_pkg.UUID(subject), is_deleted=False)
    except ValueError:
        return None
    if account is None:
        return None
    user_id = cast(dict[str, Any], account)["id"]
    if await live_session_for(db, session_uuid=session_uuid, user_id=user_id) is None:
        return None

    held = await held_successor(refresh_token)
    if held is None or await verify_token(held.refresh_token, TokenType.REFRESH, db) is None:
        return None
    return held


@router.post("/refresh", response_model=RefreshResponse)
async def refresh_access_token(
    request: Request, response: Response, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> dict[str, str]:
    """Exchanges the `refresh_token` cookie for a new access token and a new refresh cookie.

    The one operation authenticated by the cookie alone: no bearer token, and the body
    carries only the new access token - the new refresh token rides the `Set-Cookie` header.
    Rate limited per IP. The session continues as itself, sliding by its own idle window.

    The presented refresh token is spent by the exchange and does not work again. A second
    presentation within a short grace window the instance sets is answered with the same
    pair the first one received, so a retry after a lost reply succeeds; later than that, a
    re-presented token ends the session it belongs to, access token included, and the
    device signs in again. Two clients refreshing the same cookie at the same instant: one
    succeeds, the other is refused.

    401 when no cookie is present. Every other failure - a token that is invalid, expired or
    already spent, an account that was deleted, a session that was revoked or ended - is a
    401 with the same `detail`, so the response never says whether a token was ever real.
    """
    # See `DECISIONS.md` for why this is the one cookie-authenticated endpoint in this flow and
    # why that is still CSRF-safe, and for the two-tab race.
    #
    # Rotation: without it a single leaked cookie stays valid for the session's whole idle
    # window with nothing to revoke it and no way to notice; with it, the theft has a much
    # shorter useful life and a later use of the same token fails outright.
    #
    # The grace: a reply lost on the way back leaves the client holding a token this server has
    # already spent, and the retry is how it finds out. The pair is held in Redis for
    # `REFRESH_TOKEN_REUSE_GRACE_SECONDS` (`services.refresh_grace`); `_held_pair_for_a_retry`
    # says when it is handed out.
    #
    # A later presentation is the loudest signal this app gets that a cookie has been stolen,
    # so it is acted on: `_handle_revoked_refresh` separates "a token we revoked, presented
    # again" from "garbage", which the 401 deliberately does not - and past the grace it revokes
    # the session that token names, so the pair rotated out of it stops working too.
    #
    # A token that verifies is not the same thing as an account that still exists, so the
    # subject is resolved before a replacement pair is minted. Nor is it the same thing as a
    # session that is still live: the `sid` has to resolve to an unrevoked, unexpired row
    # belonging to this subject, which is half of what makes "sign out my other devices" mean
    # anything. The other half is `get_current_user`, which asks the same question of the
    # access token on every authenticated request, so the revoked device is refused there
    # first. A token carrying no `sid` fails both checks.
    await enforce_rate_limit(
        f"auth:refresh:ip:{client_ip_bucket(request)}",
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
        held = await _handle_revoked_refresh(refresh_token, db, context)
        if held is None:
            raise UnauthorizedException("Invalid refresh token.")
        set_refresh_cookie(response, held.refresh_token, max_age=held.max_age)
        return held.body()

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
    # two leaves the caller signed out rather than holding two live refresh tokens. Not
    # spent by this call means another request rotated it first - the two-tab race, lost
    # after both passed `verify_token` - and one cookie must not be rotated into two. That
    # loser is refused even inside the grace: the grace answers a token already spent and
    # answered, and this one's pair may not be held yet.
    if not await blacklist_token(refresh_token, db):
        raise UnauthorizedException("Invalid refresh token.")

    # The same `sid`, deliberately: rotation replaces the credential and continues the
    # session, which is the whole difference this table makes. The session's own
    # `idle_timeout`, so an extended session slides by its own window. No audit event - an
    # ordinary rotation is once per access-token lifetime per device, which is the
    # recurring, expected class the standing rule excludes, and `last_used_at` already
    # records it.
    issued = await issue_tokens(
        response,
        user_data.user_uuid,
        db=db,
        context=context,
        user_id=user_id,
        idle_timeout=session.idle_timeout,
        session_uuid=session.uuid,
    )
    await hold_successor(refresh_token, issued)
    return issued.body()


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    request: Request,
    response: Response,
    access_token: Annotated[str, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    refresh_token: Annotated[str | None, Cookie(alias="refresh_token")] = None,
) -> LogoutResponse:
    """Ends the caller's session.

    Needs the bearer access token and the `refresh_token` cookie. Both stop working at once
    rather than at their expiry, the session leaves `GET /user/sessions` on the account's
    other devices, and the response clears the cookie. Signing out a session that was
    already ended succeeds and changes nothing.

    401 when no refresh cookie is present, or when either token is not one this instance
    issued or has expired.
    """
    # Authenticates on `bearer_scheme` alone - no `get_current_user`, so there is no
    # `current_user` dict to take an id from - and resolves its subject from the presented
    # access token the way `_handle_revoked_refresh` does. One indexed read, on a route nobody
    # calls in a loop.
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

    return LogoutResponse()
