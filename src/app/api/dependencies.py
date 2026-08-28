import logging
import uuid as uuid_pkg
from typing import Annotated, Any, Protocol, cast

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db.database import async_get_db
from ..core.exceptions.http_exceptions import ForbiddenException, NotFoundException, UnauthorizedException
from ..core.security import TokenType, oauth2_scheme, verify_token
from ..crud.crud_users import crud_users

logger = logging.getLogger(__name__)


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)], db: Annotated[AsyncSession, Depends(async_get_db)]
) -> dict[str, Any] | None:
    """Resolve a Bearer access token to the account it was issued for.

    This single lookup is the root of the entire ownership model - `fetch_owned_or_raise`
    below compares against the `id` it returns - so it has to name an account that cannot
    change hands. It keys on the immutable `uuid` the token carries as its subject; it
    used to key on the username, which `PATCH /user` can change and release for anyone
    else to claim (see `services.auth_service.issue_tokens` and DECISIONS.md).
    """
    token_data = await verify_token(token, TokenType.ACCESS, db)
    if token_data is None:
        raise UnauthorizedException("User not authenticated.")

    user = await crud_users.get(db=db, uuid=token_data.user_uuid, is_deleted=False)
    if user:
        return user

    raise UnauthorizedException("User not authenticated.")


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
    include_deleted: bool = False,
) -> OwnedRowT:
    """Fetch a row by its public `uuid` and assert `current_user` owns it: 404 both when
    the row isn't there and when it belongs to someone else.

    Every route that names a single owned resource starts here. Two things make that
    worth centralizing rather than restating per route:

    1. **Ordering.** This has to run *before* any `@cache`-wrapped read helper, because
       `@cache` serves a cached response without re-running authorization. Each route
       used to carry its own copy of that warning, and a copy is a thing that can be
       left out.
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

    `include_deleted` exists for the routes that legitimately act on a soft-deleted row
    (restoring a certification, say) - everything else wants the default. Both it and the
    filter it controls apply only to the models that still carry the column: `Certification`
    is the last one routed through here, and `Trip`/`DiveSite`/`Course`/`GearItem`/`GearSet`
    are hard-deleted now. The check is on the model rather than unconditional because
    FastCRUD's `get_model_column` raises `ValueError` for a column the model lacks instead
    of ignoring it, so an unconditional filter would turn every `GET`/`PATCH`/`DELETE` on
    those five into a 500.

    Note the two intentional non-users: `api.v1.gear_service._owned_gear_item` and
    `api.v1.gear_sets._resolve_item_ids` answer 422 for both cases, because there the
    uuid is a reference inside a request body rather than the resource being addressed,
    and a 404 would name the wrong thing as missing - the request, not the route's
    resource.

    Not to be confused with the 403 the routes still raise for a body or query
    `user_uuid` that isn't the caller's own (`create_dive`, `read_dives`, and their
    equivalents): there the caller is naming *themselves* wrongly rather than probing
    for someone else's row, so nothing is disclosed by saying so.
    """
    filters: dict[str, Any] = {"uuid": uuid}
    if not include_deleted and hasattr(crud.model, "is_deleted"):
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
