import uuid as uuid_pkg
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from fastcrud import FastCRUD
from sqlalchemy import ARRAY, ColumnElement, String, delete, func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ..core.utils.search import search_clause
from ..models.dive import Dive
from ..models.dive_tag import DiveTag
from ..models.tag import Tag, folded
from ..schemas.tag import TagCreateInternal, TagRead, TagReadInternal, TagUpdate, TagUpdateInternal

CRUDTag = FastCRUD[Tag, TagCreateInternal, TagUpdate, TagUpdateInternal, TagUpdate, TagReadInternal]
crud_tags = CRUDTag(Tag)


def _folded_value(name: str) -> ColumnElement[Any]:
    """A name as `ux_tag_user_id_name_folded` keys it, computed by Postgres - the authority
    wherever it and `str.casefold()` could differ."""
    return folded(literal(name, String))


def _dive_count() -> Any:
    """The live dives carrying the tag - exactly what `GET /dives?tag_uuid=` matches."""
    return (
        select(func.count(DiveTag.id))
        .join(Dive, Dive.id == DiveTag.dive_id)
        .where(DiveTag.tag_id == Tag.id, Dive.is_deleted.is_(False))
        .correlate(Tag)
        .scalar_subquery()
    )


def _select_tags() -> Any:
    return select(Tag.uuid, Tag.name, _dive_count().label("dive_count"), Tag.created_at, Tag.updated_at)


async def get_tags_page(
    db: AsyncSession, *, user_id: int, offset: int, limit: int, search: str | None
) -> dict[str, Any]:
    """One page of a diver's tags by name, in `get_multi`'s `{data, total_count}` shape.

    By `name` under the database's default collation, which `ix_tag_user_id_name` serves -
    not the Unicode one the uniqueness folds under, which orders by code point.
    """
    conditions: list[ColumnElement[bool]] = [Tag.user_id == user_id]
    if search:
        conditions.append(search_clause(Tag, ("name",), search))
    total_count = await db.scalar(select(func.count()).select_from(Tag).where(*conditions))
    rows = await db.execute(_select_tags().where(*conditions).order_by(Tag.name, Tag.id).offset(offset).limit(limit))
    return {"data": [TagRead.model_validate(dict(row._mapping)) for row in rows], "total_count": total_count or 0}


async def tag_name_exists(db: AsyncSession, user_id: int, name: str, exclude_id: int | None = None) -> bool:
    """Checked on the unique index's own expression, so the answer is the one the index
    would give. Names arrive trimmed."""
    stmt = select(Tag.id).where(Tag.user_id == user_id, folded(Tag.name) == _folded_value(name))
    if exclude_id is not None:
        stmt = stmt.where(Tag.id != exclude_id)
    return (await db.execute(stmt.limit(1))).first() is not None


async def resolve_tag_id_for_user(db: AsyncSession, *, tag_uuid: uuid_pkg.UUID, user_id: int) -> int | None:
    tag_id: int | None = await db.scalar(select(Tag.id).where(Tag.uuid == tag_uuid, Tag.user_id == user_id))
    return tag_id


# A multi-row insert binds four values a row, and asyncpg takes at most 32,767 per statement.
_INSERT_BATCH = 1000


async def _folds(db: AsyncSession, names: Sequence[str]) -> list[str]:
    """Each name as `ux_tag_user_id_name_folded` keys it, in order - one query however many."""
    listed = (
        func.unnest(literal(list(names), ARRAY(String())))
        .table_valued("name", with_ordinality="position")
        .render_derived()
    )
    rows = await db.execute(select(folded(listed.c.name)).order_by(listed.c.position))
    return list(rows.scalars())


async def _ids_by_key(db: AsyncSession, user_id: int, keys: Sequence[str]) -> dict[str, int]:
    rows = await db.execute(
        select(folded(Tag.name).label("key"), Tag.id).where(Tag.user_id == user_id, folded(Tag.name).in_(keys))
    )
    return {row.key: row.id for row in rows}


async def tag_ids_by_name(db: AsyncSession, *, user_id: int, names: Sequence[str]) -> dict[str, int]:
    """Each of these trimmed names' tag id, creating every one the diver lacks.

    A name is the row its fold matches - that row's spelling stands - and of several names
    folding to one, the first spelling is the one a new row takes. The fold is Postgres's, so
    the unique index decides what is one tag, never `str.casefold()`. `ON CONFLICT DO NOTHING`
    makes a concurrent write creating the same tag harmless: the re-read finds its row. Does
    not commit.
    """
    if not names:
        return {}
    keys = await _folds(db, names)
    first_spelling: dict[str, str] = {}
    for key, name in zip(keys, names, strict=True):
        first_spelling.setdefault(key, name)

    found = await _ids_by_key(db, user_id, list(first_spelling))
    missing = [name for key, name in first_spelling.items() if key not in found]
    if missing:
        now = datetime.now(UTC)
        for start in range(0, len(missing), _INSERT_BATCH):
            await db.execute(
                pg_insert(Tag)
                .values(
                    [
                        {"user_id": user_id, "name": name, "uuid": uuid7(), "created_at": now}
                        for name in missing[start : start + _INSERT_BATCH]
                    ]
                )
                .on_conflict_do_nothing()
            )
        found = await _ids_by_key(db, user_id, list(first_spelling))
    return {name: found[key] for key, name in zip(keys, names, strict=True)}


async def resolve_tag_ids(db: AsyncSession, *, user_id: int, names: Sequence[str]) -> list[int]:
    """A write's tags as ids in the diver's order, two that fold to one kept once, at the
    first position. See `tag_ids_by_name`."""
    by_name = await tag_ids_by_name(db, user_id=user_id, names=names)
    return list(dict.fromkeys(by_name[name] for name in names))


async def get_tags_for_dives(db: AsyncSession, dive_ids: Sequence[int]) -> dict[int, list[str]]:
    by_dive: dict[int, list[str]] = {dive_id: [] for dive_id in dive_ids}
    if not dive_ids:
        return by_dive
    rows = await db.execute(
        select(DiveTag.dive_id, Tag.name)
        .join(Tag, Tag.id == DiveTag.tag_id)
        .where(DiveTag.dive_id.in_(set(dive_ids)))
        .order_by(DiveTag.dive_id, DiveTag.position)
    )
    for row in rows:
        by_dive[row.dive_id].append(row.name)
    return by_dive


async def replace_tags_for_dive(db: AsyncSession, dive_id: int, tag_ids: Sequence[int], commit: bool = True) -> None:
    """Replace a dive's tags with the given ordered list: delete, then insert with positions,
    as `replace_people_for_dive` does. A tag listed twice keeps its first position."""
    await db.execute(delete(DiveTag).where(DiveTag.dive_id == dive_id))
    for position, tag_id in enumerate(dict.fromkeys(tag_ids)):
        db.add(DiveTag(dive_id=dive_id, tag_id=tag_id, position=position))
    if commit:
        await db.commit()
