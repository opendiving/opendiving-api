import uuid as uuid_pkg
from typing import Any

from fastcrud import FastCRUD
from sqlalchemy import JSON, ColumnElement, and_, cast, func, inspect, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..models.user import User
from ..models.user_picture import UserPicture
from ..schemas.user import (
    UNIT_FAMILIES,
    UserCreateInternal,
    UserDelete,
    UserReadInternal,
    UserUpdate,
    UserUpdateInternal,
)
from ..schemas.user_picture import PictureKind

CRUDUser = FastCRUD[User, UserCreateInternal, UserUpdate, UserUpdateInternal, UserDelete, UserReadInternal]
crud_users = CRUDUser(User)

_CROP_PARTS = ("x", "y", "width", "height")


def account_update_values(values: UserUpdate) -> dict[str, Any]:
    """`PATCH /user`'s column values. The families it names are merged into the stored
    `unit_preferences` by the UPDATE itself, so two devices changing different families at once
    both land; the deprecated `units` names every family, before the body's own families."""
    written = values.model_dump(exclude_unset=True)
    families = dict.fromkeys(UNIT_FAMILIES, written.pop("units")) if "units" in written else {}
    families |= written.pop("unit_preferences", {})
    if families:
        merged = cast(User.unit_preferences, JSONB).op("||", return_type=JSONB)(cast(literal(families, JSONB), JSONB))
        written["unit_preferences"] = cast(merged, JSON)
    return written


def avatar_of(account_id: Any) -> ColumnElement[bool]:
    """The join onto the avatar row of the account `account_id` names, for a read that
    carries another account's `avatar_sha256`."""
    return and_(UserPicture.user_id == account_id, UserPicture.kind == PictureKind.AVATAR.value)


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


async def account_for_email(db: AsyncSession, *, email: str) -> dict[str, Any] | None:
    """The account holding `email`, compared **case-insensitively**, as `crud_users.get`
    would return it: every mapped `user` column, deleted rows included.

    `lower(User.email)`, served by `ix_user_email_lower`, because a stored address may carry
    capitals - `POST /auth/complete` stores the onboarding token's address as it came, and
    Google's claim is not lowercased - while the magic link lowercases what it is sent. A raw
    equality would miss that account at sign-in and send its owner to onboarding for a
    second one.

    Two rows can differ only in case, from before this lookup existed, so the answer is
    ordered: the exact-case match first, which is the row a raw equality found, then the
    oldest.
    """
    columns = [getattr(User, attribute.key) for attribute in inspect(User).column_attrs]
    row = (
        await db.execute(
            select(*columns)
            .where(func.lower(User.email) == email.lower())
            .order_by((User.email == email).desc(), User.id)
            .limit(1)
        )
    ).one_or_none()
    return None if row is None else dict(row._mapping)
