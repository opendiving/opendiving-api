import uuid as uuid_pkg
from typing import Any

from fastcrud import FastCRUD
from sqlalchemy import and_, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..models.user import User
from ..models.user_picture import UserPicture
from ..schemas.user import UserCreateInternal, UserDelete, UserReadInternal, UserUpdate, UserUpdateInternal
from ..schemas.user_picture import PictureKind

CRUDUser = FastCRUD[User, UserCreateInternal, UserUpdate, UserUpdateInternal, UserDelete, UserReadInternal]
crud_users = CRUDUser(User)

_CROP_PARTS = ("x", "y", "width", "height")


async def read_account(db: AsyncSession, *, uuid: uuid_pkg.UUID) -> dict[str, Any] | None:
    """The live account `uuid` names: every mapped `user` column, as `crud_users.get` would
    select them, plus each picture's `UserRead` fields - in one query, joining the two
    `user_picture` rows.

    One query because this is `get_current_user`'s, the hottest read in the app, and
    `GET /user` returns its result as it is.
    """
    pictures = {kind: aliased(UserPicture, name=f"{kind.value}_picture") for kind in PictureKind}
    columns: list[Any] = [getattr(User, attribute.key) for attribute in inspect(User).column_attrs]
    for kind, picture in pictures.items():
        columns += [
            picture.rendition_sha256.label(f"{kind.value}_sha256"),
            picture.original_sha256.label(f"{kind.value}_original_sha256"),
            *(getattr(picture, f"crop_{part}").label(f"{kind.value}_crop_{part}") for part in _CROP_PARTS),
        ]
    statement = select(*columns).select_from(User)
    for kind, picture in pictures.items():
        statement = statement.outerjoin(picture, and_(picture.user_id == User.id, picture.kind == kind.value))
    statement = statement.where(User.uuid == uuid, User.is_deleted.is_(False))

    row = (await db.execute(statement)).one_or_none()
    if row is None:
        return None
    account = dict(row._mapping)
    for kind in PictureKind:
        box = {part: account.pop(f"{kind.value}_crop_{part}") for part in _CROP_PARTS}
        account[f"{kind.value}_crop"] = None if box["x"] is None else box
    return account
