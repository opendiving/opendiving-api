"""The two WebAuthn ceremonies, kept out of the routes.

Registration happens only inside an authenticated session, so a credential is born
linked to the account that made it; sign-in resolves that credential straight back to a
user. There is no auto-linking decision to make anywhere in here - unlike Google, a
passkey asserts no email at all, and there is no email for one to be linked *by*.
"""

import logging
import uuid as uuid_pkg
from typing import Any, cast

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, options_to_json_dict, parse_authenticator_data
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ..core.config import settings
from ..core.exceptions.http_exceptions import BadRequestException, UnauthorizedException
from ..crud.crud_users import crud_users
from ..crud.crud_webauthn_credentials import crud_webauthn_credentials, record_assertion
from ..schemas.webauthn_credential import (
    WebauthnCredentialCreateInternal,
    WebauthnCredentialReadInternal,
)
from ..services.auth_service import AuthenticatedUser
from ..services.passkey_challenges import (
    consume_registration_challenge,
    consume_sign_in_challenge,
    store_registration_challenge,
    store_sign_in_challenge,
)

logger = logging.getLogger(__name__)

# One answer for an unknown credential, a tombstoned owner, an expired or already-spent
# challenge, a wrong origin, a wrong RP ID and a bad signature alike. Every one of those
# is "this assertion does not sign anyone in", and telling them apart would answer
# questions - does this credential exist, does that account still - that a caller holding
# a failed assertion has no business asking.
_SIGN_IN_FAILED = "That passkey could not be used to sign in."

# Raised from two places - the per-user check, and the unique constraint that catches what
# that check cannot see - which must be indistinguishable to the caller.
_ALREADY_REGISTERED_DETAIL = "That passkey is already registered."

# Bounds on the one client-supplied value this app stores without a size limit in front of
# it - see `_transports_from`. The spec defines seven transports, the longest ten
# characters, so real authenticators sit far below both.
_MAX_TRANSPORTS = 10
_MAX_TRANSPORT_LENGTH = 32


def _registration_selection() -> AuthenticatorSelectionCriteria:
    """Discoverable credentials, user verification preferred.

    `resident_key="required"` is what makes the sign-in ceremony usernameless: the
    assertion carries the credential id and the credential row names the user, so there
    is one code path and no "does this email have a passkey" oracle anywhere. It costs
    pre-resident-key security keys, which cannot register - accepted, since platform
    authenticators are the audience and the alternative is an email-first
    `allowCredentials` flow that leaks exactly that.

    `user_verification="preferred"` rather than `"required"` keeps a PIN-less key usable.
    A passkey asserted without UV is a bare possession proof - precisely the strength of
    the magic link beside it - so nothing is lost against the current floor, and the UV
    flag comes back in every assertion if a stricter policy is ever wanted.
    """
    return AuthenticatorSelectionCriteria(
        resident_key=ResidentKeyRequirement.REQUIRED,
        user_verification=UserVerificationRequirement.PREFERRED,
    )


def _known_transports(transports: list[str] | None) -> list[AuthenticatorTransport] | None:
    """Drop transport hints this library has no member for.

    The list is advisory metadata the browser reported at registration, and the spec lets
    a client send values that postdate any given library. A stored `"smart-card"` from a
    newer browser must not make `excludeCredentials` - and with it the whole registration
    ceremony - raise.
    """
    if not transports:
        return None
    known = {member.value for member in AuthenticatorTransport}
    return [AuthenticatorTransport(value) for value in transports if value in known] or None


def _at_the_cap() -> HTTPException:
    """409, not 422: the request body is fine, the *account's state* is what conflicts -
    which is the line `AGENTS.md` draws between the two codes. A raw `HTTPException` for
    the same reason `PUT /dive/{uuid}/file`'s conflicts are raw ones:
    `core/exceptions/http_exceptions.py` has no class for 409.

    A factory, and so is `_already_registered` below - **never** a module-level constant,
    however tempting one looks for a message with no per-call state in it. Raising one
    exception *instance* repeatedly prepends each raise's frames to the traceback it is
    already carrying instead of replacing them, so a shared object accumulates every
    conflict's stack for the life of the process and pins each one's locals - here, a
    request's `AsyncSession`, `user` dict and attestation payload - alive with it. `raise
    ... from None` also writes `__cause__`/`__context__` onto the shared object, where a
    concurrent request can see it.
    """
    return HTTPException(
        status_code=409,
        detail=f"You already have {settings.PASSKEY_MAX_CREDENTIALS_PER_USER} passkeys. Remove one to add another.",
    )


def _already_registered() -> HTTPException:
    """See `_at_the_cap` for why this is a factory rather than a constant."""
    return HTTPException(status_code=409, detail=_ALREADY_REGISTERED_DETAIL)


async def start_registration(*, db: AsyncSession, user: dict[str, Any]) -> dict[str, Any]:
    """Mint creation options for a signed-in user and store the challenge.

    The user handle is `user.uuid`'s bytes - immutable and non-PII, the same reasoning
    that made it the token subject. `user_name`/`user_display_name` are the email and
    name, purely cosmetic labels inside the authenticator's own UI.

    `excludeCredentials` carries every credential this account already has, which is what
    makes an authenticator say "you already have a passkey here" instead of silently
    registering a second one for the same device.
    """
    existing = await _credentials_for_user(db, user["id"])
    if len(existing) >= settings.PASSKEY_MAX_CREDENTIALS_PER_USER:
        raise _at_the_cap()

    challenge = await store_registration_challenge(user["id"])

    options = generate_registration_options(
        rp_id=settings.passkey_rp_id,
        rp_name=settings.APP_NAME,
        user_id=cast(uuid_pkg.UUID, user["uuid"]).bytes,
        user_name=user["email"],
        user_display_name=user["name"],
        challenge=challenge,
        authenticator_selection=_registration_selection(),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=row.credential_id, transports=_known_transports(row.transports))
            for row in existing
        ],
    )
    return options_to_json_dict(options)


async def finish_registration(
    *, db: AsyncSession, user: dict[str, Any], credential: dict[str, Any], name: str
) -> WebauthnCredentialReadInternal:
    """Verify an attestation and store the credential it attests to.

    The 10-credential ceiling is re-checked here as well as in `start_registration`: the
    first check is what lets the UI say so before a biometric prompt, this one is the one
    that holds when two tabs each got options while the account was at nine.
    """
    challenge = await consume_registration_challenge(user["id"])
    if challenge is None:
        raise BadRequestException("That passkey registration expired. Try again.")

    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=settings.passkey_rp_id,
            expected_origin=settings.passkey_origin,
        )
    # `WebAuthnException` rather than `InvalidRegistrationResponse`: the library raises a
    # dozen sibling types from inside verification (bad CBOR, an unsupported algorithm, an
    # unparseable public key), every one of them a malformed ceremony rather than a bug
    # here, and naming them one at a time is how the next one becomes a 500. `ValueError`
    # covers the base64url decoding underneath.
    except (WebAuthnException, ValueError) as exc:
        # `info`, not `warning`: a failed registration is overwhelmingly a user who moved
        # to another tab or a browser that changed its mind, and it is already visible to
        # the person it happened to. The assertion side is where a failure is worth a line.
        logger.info("A passkey registration for user_id %s failed verification: %s", user["id"], exc)
        raise BadRequestException("That passkey could not be registered. Try again.") from None

    existing = await _credentials_for_user(db, user["id"])
    if len(existing) >= settings.PASSKEY_MAX_CREDENTIALS_PER_USER:
        raise _at_the_cap()
    if any(row.credential_id == verified.credential_id for row in existing):
        raise _already_registered()

    try:
        created = await crud_webauthn_credentials.create(
            db=db,
            object=WebauthnCredentialCreateInternal(
                user_id=user["id"],
                credential_id=verified.credential_id,
                public_key=verified.credential_public_key,
                name=name,
                sign_count=verified.sign_count,
                transports=_transports_from(credential),
                aaguid=_aaguid_from(verified.aaguid),
                backed_up=verified.credential_backed_up,
            ),
            schema_to_select=WebauthnCredentialReadInternal,
            return_as_model=True,
        )
    except IntegrityError:
        # `credential_id` is unique across *all* users, and the check above only sees this
        # one's rows - so a collision with somebody else's credential arrives here instead.
        # Practically unreachable (authenticators mint a fresh credential per registration,
        # and an attestation cannot be replayed past the single-use challenge), but the
        # alternative to handling it is a 500, and the same conflict deserves the same 409
        # either way. Rolled back for the same reason `complete_profile` does: the session
        # is unusable afterwards otherwise.
        await db.rollback()
        raise _already_registered() from None

    return cast(WebauthnCredentialReadInternal, created)


async def start_sign_in() -> tuple[str, dict[str, Any]]:
    """Mint assertion options for an anonymous caller: `(flow_id, options)`.

    `allowCredentials` is deliberately empty. Discoverable credentials mean the ceremony
    never asks who the user is, so this request reveals nothing about any account - there
    is no email in it to reveal anything about.
    """
    flow_id, challenge = await store_sign_in_challenge()
    options = generate_authentication_options(
        rp_id=settings.passkey_rp_id,
        challenge=challenge,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    return flow_id, options_to_json_dict(options)


async def finish_sign_in(*, db: AsyncSession, flow_id: str, credential: dict[str, Any]) -> AuthenticatedUser:
    """Verify an assertion and resolve it to the account that owns the credential.

    Every failure below raises the same 401 with the same message - see `_SIGN_IN_FAILED`.

    Does **not** go through `resolve_identity`: that function answers "which account owns
    this email, and is this provider linked to it", and an assertion carries no email. The
    credential row *is* the link, and it can only exist because a signed-in user made it.
    This is therefore a resolve site `plans/account-deletion.md`'s edit list does not know
    about; until that lands, a soft-deleted owner falls through the `is_deleted=False`
    filter into the same 401 as everything else here.
    """
    challenge = await consume_sign_in_challenge(flow_id)
    if challenge is None:
        raise UnauthorizedException(_SIGN_IN_FAILED)

    raw_id = _raw_credential_id(credential)
    if raw_id is None:
        raise UnauthorizedException(_SIGN_IN_FAILED)

    stored = await crud_webauthn_credentials.get(
        db=db, credential_id=raw_id, schema_to_select=WebauthnCredentialReadInternal, return_as_model=True
    )
    if stored is None:
        raise UnauthorizedException(_SIGN_IN_FAILED)
    stored = cast(WebauthnCredentialReadInternal, stored)

    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=settings.passkey_rp_id,
            expected_origin=settings.passkey_origin,
            credential_public_key=stored.public_key,
            credential_current_sign_count=stored.sign_count,
        )
    except (WebAuthnException, ValueError) as exc:
        _warn_if_counter_regressed(credential, stored)
        logger.info("A passkey assertion for credential %s failed verification: %s", stored.uuid, exc)
        raise UnauthorizedException(_SIGN_IN_FAILED) from None

    user = await crud_users.get(db=db, id=stored.user_id, is_deleted=False)
    if user is None:
        raise UnauthorizedException(_SIGN_IN_FAILED)

    recorded = await record_assertion(
        db,
        credential_uuid=stored.uuid,
        sign_count=verified.new_sign_count,
        backed_up=verified.credential_backed_up,
        expected_sign_count=stored.sign_count,
    )
    if not recorded:
        # Two submissions of one assertion raced and the other won. Both are the same
        # valid ceremony, so this is not worth a distinct message - but only one of them
        # gets to mint a session.
        raise UnauthorizedException(_SIGN_IN_FAILED)

    return AuthenticatedUser(user=cast(dict[str, Any], user))


def _warn_if_counter_regressed(credential: dict[str, Any], stored: WebauthnCredentialReadInternal) -> None:
    """Log the one assertion failure that means something: a signature counter that went
    backwards, which is the cloned-authenticator signal.

    py_webauthn already rejects it inside `verify_authentication_response`, so the app's
    job is only the line - and it earns it by doing the stored-vs-presented comparison
    itself rather than by parsing the library's exception message, which is a string that
    can be reworded in any release. `WARNING` for the same reason a reused refresh token
    is: the app configures no logging of its own and `uvicorn` configures only its own
    loggers, so anything below it is dropped on the floor in exactly the session where
    someone is trying to work out what happened.

    A synced passkey reports `0` forever and `0 -> 0` is not a regression - the guard is
    "we have seen this counter move at all, and it has not moved since".
    """
    presented = _presented_sign_count(credential)
    if presented is None or stored.sign_count == 0 or presented > stored.sign_count:
        return

    logger.warning(
        "Passkey %s (user_id %s) presented sign count %s against a stored count of %s. A counter that has not "
        "advanced is what a cloned authenticator looks like; the genuine device's own count is ahead, so it "
        "keeps working.",
        stored.uuid,
        stored.user_id,
        presented,
        stored.sign_count,
    )


def _presented_sign_count(credential: dict[str, Any]) -> int | None:
    """The counter inside the assertion's `authenticatorData`, or `None` if it cannot be
    read - a response malformed enough to fail parsing is not the case this log is about.
    """
    try:
        authenticator_data = credential["response"]["authenticatorData"]
        return parse_authenticator_data(base64url_to_bytes(authenticator_data)).sign_count
    except KeyError, TypeError, ValueError, WebAuthnException:
        return None


def _raw_credential_id(credential: dict[str, Any]) -> bytes | None:
    """The credential's raw id, decoded from the base64url the browser sends.

    `None` means the field was absent, which is the only shape worth a distinct branch:
    py_webauthn's decoder is deliberately lenient - it pads and decodes almost any string -
    so a *malformed* id becomes bytes that match no row, and arrives at the same 401 one
    lookup later.
    """
    try:
        return base64url_to_bytes(credential["rawId"])
    except KeyError, TypeError, ValueError:
        return None


def _transports_from(credential: dict[str, Any]) -> list[str] | None:
    """The client's transport hints, taken from the response as strings, deduplicated and
    bounded.

    Read off the raw credential rather than off `VerifiedRegistration`, which does not
    carry them, and *not* narrowed to values this library knows: a hint from a browser
    newer than py_webauthn is exactly what `_known_transports` filters at use time rather
    than losing at write time.

    Bounded here because this is the one place the app stores a client-supplied value with
    nothing in front of it: `credential` is unvalidated JSON (the ceremony's own parser is
    what validates it, and it ignores this field), `read_upload_within_limit` bounds every
    *upload* but no layer bounds a JSON body, and the bundled Caddy sets no
    `client_max_body_size` on the strength of that app-side enforcement. Without a cap, an
    authenticated caller could park an arbitrarily large blob in a `JSON` column that
    `start_registration` then re-reads and walks on every subsequent options call.

    The spec defines seven transports and the longest is ten characters, so the limits are
    generous enough that no real authenticator can reach them - anything that does is not a
    hint worth keeping.
    """
    transports = credential.get("response", {}).get("transports")
    if not isinstance(transports, list):
        return None

    kept: list[str] = []
    for value in transports:
        if not isinstance(value, str) or len(value) > _MAX_TRANSPORT_LENGTH or value in kept:
            continue
        kept.append(value)
        if len(kept) == _MAX_TRANSPORTS:
            break
    return kept or None


def _aaguid_from(aaguid: str) -> uuid_pkg.UUID | None:
    """py_webauthn hands the AAGUID over already formatted as a uuid string, but a
    self-attested credential reports all zeros and some authenticators report nothing
    parseable at all. Nothing reads this column in v1, so an unusable value is dropped
    rather than allowed to fail a registration.
    """
    try:
        return uuid_pkg.UUID(aaguid)
    except AttributeError, TypeError, ValueError:
        return None


async def _credentials_for_user(db: AsyncSession, user_id: int) -> list[WebauthnCredentialReadInternal]:
    """Every credential on an account, oldest first.

    Unpaginated and uncached on purpose: the ceiling is
    `PASSKEY_MAX_CREDENTIALS_PER_USER`, so this is at most ten rows, and nothing embeds a
    credential anywhere - so there is no invalidation obligation to get wrong. See the
    documented opt-outs on `OwnedResourceCache`.
    """
    rows = await crud_webauthn_credentials.get_multi(
        db=db,
        user_id=user_id,
        schema_to_select=WebauthnCredentialReadInternal,
        return_as_model=True,
        sort_columns="created_at",
        sort_orders="asc",
        limit=settings.PASSKEY_MAX_CREDENTIALS_PER_USER + 1,
    )
    return cast(list[WebauthnCredentialReadInternal], rows["data"])
