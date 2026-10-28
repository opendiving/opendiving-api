import logging
import uuid as uuid_pkg
from datetime import UTC, datetime
from typing import Annotated, Any, Protocol, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db.database import async_get_db
from ..core.exceptions.http_exceptions import ForbiddenException, NotFoundException, UnauthorizedException
from ..core.security import TokenType, bearer_scheme, is_api_key, token_session_id, verify_token
from ..crud.crud_api_keys import live_api_key, stamp_api_key_used
from ..crud.crud_user_sessions import live_session_for
from ..crud.crud_users import read_account
from ..schemas.api_key import ApiKeyScope

logger = logging.getLogger(__name__)

# The methods a read-only API key may use. Complete because the API is REST-shaped: a safe
# method never changes stored state, and the odd write-nothing `POST` needs a write key.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def get_current_user(
    request: Request,
    token: Annotated[str, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any] | None:
    """Resolve a Bearer access token to the account it was issued for, and refuse it unless
    the session it names can still authenticate - or, for a bearer string that is an API
    key, resolve the key instead (`_account_for_api_key`).

    The account lookup is the root of the entire ownership model - `fetch_owned_or_raise`
    below compares against the `id` it returns - so it has to name an account that cannot
    change hands. It keys on the immutable `uuid` the token carries as its subject, never
    on the username, which `PATCH /user` can change and release for anyone else to claim
    (see `services.auth_service.issue_tokens` and DECISIONS.md). It is one query
    that carries both pictures' `UserRead` fields with the account (`read_account`), since
    `GET /user` returns this dict as it is.

    **The `sid` is checked against the database on every authenticated request**, which is
    what makes `DELETE /user/session/{uuid}` take effect on the revoked device's next request
    rather than at its next `/auth/refresh`. That window was up to
    `ACCESS_TOKEN_EXPIRE_MINUTES` long and is exactly the one the button exists for - a
    laptop that has just been lost is not signed out by a promise about half an hour's time.

    `live_session_for` is reused rather than restated: it already is the unrevoked-and-
    unexpired predicate `/auth/refresh` asks, and a second spelling of it is a second chance
    for the two to drift. It carries the `user_id` clause too, so a `sid` lifted onto another
    account's token resolves to nothing. It has to run *after* the account lookup, which is
    where the `user_id` it needs comes from.

    A token carrying no `sid` at all is refused here as well, with no shim. `issue_tokens` is
    the only mint site and it always names a session, so the claim is absent only on a token
    minted before sessions existed - and signing those out once is the compatibility story
    that feature already had.

    Both refusals answer the same `UnauthorizedException` as every other failure here. There
    is nothing for a caller to tell apart, and no reason to hand one an oracle for whether a
    given `sid` names a real row.

    The branch on the API-key prefix comes before `verify_token`, so a key never reaches the
    blacklist or the JWT decoder, and every other string takes the path above unchanged.
    """
    if is_api_key(token):
        return await _account_for_api_key(request, token, db)

    token_data = await verify_token(token, TokenType.ACCESS, db)
    if token_data is None:
        raise UnauthorizedException("User not authenticated.")

    user = await read_account(db, uuid=token_data.user_uuid)
    if not user:
        raise UnauthorizedException("User not authenticated.")

    session_uuid = token_data.session_uuid
    if session_uuid is None or await live_session_for(db, session_uuid=session_uuid, user_id=user["id"]) is None:
        raise UnauthorizedException("User not authenticated.")

    return user


async def _account_for_api_key(request: Request, secret: str, db: AsyncSession) -> dict[str, Any]:
    """The account an API key belongs to, as `read_account` returns it - with `is_superuser`
    false whatever the account's column says.

    **A key's principal is never a superuser**, and the dict is where that has to be true:
    `get_current_superuser` reads the flag off it and so refuses a key on its own check, and
    so does every inline read of the flag - the invitation quota's exemption today - since
    none of them can tell a key's request from a browser's. An operator's leaked key is a
    diver's key, and `GET /user` under one reports what that caller may do.

    An unknown, expired or revoked key, and one whose owner has asked for deletion, answer the
    same 401 as every other credential failure. A read key past a safe method is a 403
    instead: the caller holds a real key and is told what it cannot do. That refusal is made
    here rather than per route, so no route can forget it.
    """
    now = datetime.now(UTC)
    key = await live_api_key(db, secret=secret, now=now)
    if key is None:
        raise UnauthorizedException("User not authenticated.")

    if key.scope != ApiKeyScope.WRITE and request.method not in _SAFE_METHODS:
        raise ForbiddenException("This API key is read-only.")

    user = await read_account(db, uuid=key.owner_uuid)
    if not user:
        raise UnauthorizedException("User not authenticated.")

    await stamp_api_key_used(db, key=key, now=now)
    return {**user, "is_superuser": False}


async def refuse_api_keys(token: Annotated[str, Depends(bearer_scheme)]) -> None:
    """Refuse an API key on a route that manages credentials, the sign-in address or the
    account's existence, so a stolen key cannot lock its owner out or mint its own successor.

    A sibling of `get_current_user` rather than a change to it, as `current_session_uuid` is,
    declared on every route under the `/user/api-key`, `/user/passkey`, `/user/session`,
    `/user/checkin-link`, `/user/invite-link` and `/user/email-change` prefixes that takes a
    session, and on `DELETE /user` - `tests/test_api_keys.py` holds the set to that rule. It
    decides on the prefix alone, without verifying the key: it can only ever refuse more, and
    `get_current_user` still authenticates everything it lets through.
    """
    if is_api_key(token):
        raise ForbiddenException("This needs a signed-in browser or app, not an API key.")


async def current_session_uuid(token: Annotated[str, Depends(bearer_scheme)]) -> uuid_pkg.UUID | None:
    """The `user_session` row the caller's access token was minted for, or `None`.

    **A sibling of `get_current_user`, deliberately not a change to it.** That function's
    return type is the account dict every owned-resource check compares against, and
    widening it to carry a second, unrelated identifier would touch every route in the app
    to serve four. So the `sid` is asked for separately, by the handful of routes that
    need it: the two session-revoke routes, the sessions list, and `DELETE /user`.

    **Not an authorization decision, and it must never become one.** It decodes rather than
    verifying: signature and expiry still hold (`jwt.decode` enforces both), but the
    blacklist and `token_type` checks are `get_current_user`'s, running over this same
    string on the same request. A route that took this without also depending on
    `get_current_user` would be trusting a claim nothing had authorized - every current
    caller does, and `test_route_authentication.py` is what would notice if one stopped.

    `None` is unreachable beside that dependency now, and the return type keeps it anyway.
    Both roads to it - a token carrying no `sid`, and one that fails to decode - are refused
    by `get_current_user` before this runs, so every current caller gets a real uuid. The
    optional half is what keeps the two functions independent: this one still authorizes
    nothing on its own, and a route that ever reached for it without `get_current_user`
    should degrade to "I cannot tell" rather than to a claim nothing checked.
    """
    return token_session_id(token)


async def get_current_superuser(current_user: Annotated[dict, Depends(get_current_user)]) -> dict:
    if not current_user["is_superuser"]:
        raise ForbiddenException("You do not have enough privileges.")

    return current_user


class OwnedRow(Protocol):
    """Any internal read schema that carries the owning user's integer id."""

    user_id: int


async def fetch_owned_or_raise[OwnedRowT: OwnedRow](
    *,
    db: AsyncSession,
    crud: Any,
    uuid: uuid_pkg.UUID,
    current_user: dict[str, Any],
    schema: type[OwnedRowT],
    not_found_message: str,
) -> OwnedRowT:
    """Fetch a row by its public `uuid` and assert `current_user` owns it: 404 both when
    the row isn't there and when it belongs to someone else.

    Every route that names a single owned resource starts here. Two things make that
    worth centralizing rather than restating per route:

    1. **Ordering.** This has to run *before* any `@cache`-wrapped read helper, because
       `@cache` serves a cached response without re-running authorization. Stated here
       rather than at each route, where a copy is a thing that can be left out.
    2. **The one answer for both cases.** A 403 on an addressed resource confirms that
       an opaque uuid names a real row belonging to *someone*, which is a fact the caller
       has no business learning. Answering 404 either way costs the owner nothing - they
       get the row - and tells a prober nothing. Same reasoning as `_owned_gear_item`
       and `_resolve_item_ids` below, and as `GET /dives`' filters, which return an
       empty page rather than an error for a uuid that isn't the caller's.

    The distinction is kept in the log, where it is worth having when debugging a client
    and costs the caller nothing. The levels are lopsided on purpose: **wrong owner** is a
    `warning`, **genuinely absent** is a `debug`.

    Only the wrong-owner case lost information when the response stopped distinguishing
    them, so only it has to survive the default level - and it has to be `warning` to do
    that, because the app configures no logging of its own and `uvicorn` (what
    `docker compose` runs) configures only its own loggers, leaving root at `WARNING`. An
    `info` call here would be dropped on the floor in exactly the local session where
    someone is working out why a client sees a 404. `services.email_service` logs the
    magic link at `warning` for the same reason.

    The absent case stays quiet because it carries nothing the 404 doesn't, and because
    it is caller-paced: an authenticated client looping over random uuids would otherwise
    emit one `WARNING` per request. At the default level, a warning here means "wrong
    owner" and its absence beside a 404 in the access log means "absent" - the same
    distinction, without the volume. Raise the level to see both.

    A soft-deleted row reads as absent. The filter applies only to the models that carry the
    column - of those routed through here, `Dive` and `Certification`; the rest are
    hard-deleted. The check is on the model rather than unconditional because FastCRUD's
    `get_model_column` raises `ValueError` for a column the model lacks instead of ignoring
    it, so an unconditional filter would turn every `GET`/`PATCH`/`DELETE` on the others
    into a 500.

    Note the two intentional non-users: `api.v1.gear_service._owned_gear_item` and
    `api.v1.gear_sets._resolve_item_ids` answer 422 for both cases, because there the
    uuid is a reference inside a request body rather than the resource being addressed,
    and a 404 would name the wrong thing as missing - the request, not the route's
    resource.
    """
    filters: dict[str, Any] = {"uuid": uuid}
    if hasattr(crud.model, "is_deleted"):
        filters["is_deleted"] = False

    row = await crud.get(db=db, schema_to_select=schema, return_as_model=True, **filters)
    if row is None:
        logger.debug("Owned-row lookup: no %s with uuid %s exists", schema.__name__, uuid)
        raise NotFoundException(not_found_message)

    row = cast(OwnedRowT, row)
    if row.user_id != current_user["id"]:
        logger.warning(
            "Owned-row lookup: %s with uuid %s belongs to user_id %s, caller is user_id %s",
            schema.__name__,
            uuid,
            row.user_id,
            current_user["id"],
        )
        raise NotFoundException(not_found_message)

    return row
