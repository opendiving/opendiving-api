import uuid as uuid_pkg
from typing import Annotated, Any, Protocol, cast

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db.database import async_get_db
from ..core.exceptions.http_exceptions import ForbiddenException, NotFoundException, UnauthorizedException
from ..core.security import TokenType, oauth2_scheme, verify_token
from ..crud.crud_users import crud_users


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)], db: Annotated[AsyncSession, Depends(async_get_db)]
) -> dict[str, Any] | None:
    token_data = await verify_token(token, TokenType.ACCESS, db)
    if token_data is None:
        raise UnauthorizedException("User not authenticated.")

    if "@" in token_data.username_or_email:
        user = await crud_users.get(db=db, email=token_data.username_or_email, is_deleted=False)
    else:
        user = await crud_users.get(db=db, username=token_data.username_or_email, is_deleted=False)

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
    """Fetch a row by its public `uuid` and assert `current_user` owns it: 404 if it
    isn't there, 403 if it belongs to someone else.

    Every route that names a single owned resource starts here. Two things make that
    worth centralizing rather than restating per route:

    1. **Ordering.** This has to run *before* any `@cache`-wrapped read helper, because
       `@cache` serves a cached response without re-running authorization. Each route
       used to carry its own copy of that warning, and a copy is a thing that can be
       left out.
    2. **The 404/403 split.** Answering 404 for a missing row and 403 for someone
       else's is a deliberate choice; making it in one place means it can be revisited
       in one place.

    `include_deleted` exists for the routes that legitimately act on a soft-deleted row
    (restoring a certification, say) - everything else wants the default.

    Note the two intentional non-users: `api.v1.gear_service._owned_gear_item` and
    `api.v1.gear_sets._resolve_item_ids` answer 422 for both cases, because there the
    uuid is a reference inside a request body rather than the resource being addressed,
    and a uniform answer keeps someone else's uuids unprobeable.
    """
    filters: dict[str, Any] = {"uuid": uuid}
    if not include_deleted:
        filters["is_deleted"] = False

    row = await crud.get(db=db, schema_to_select=schema, return_as_model=True, **filters)
    if row is None:
        raise NotFoundException(not_found_message)

    row = cast(OwnedRowT, row)
    if row.user_id != current_user["id"]:
        raise ForbiddenException()

    return row
