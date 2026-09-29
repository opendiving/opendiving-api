"""Tags: the record (`models/tag.py`), its routes (`api/v1/tags.py`), and a dive's tags on
the write, the read and the list.

House style per `test_people.py`: the schemas and the routes with their collaborators
stubbed, and a Postgres-guarded tail for what only the database settles - the case-folded
index, the resolution by name, the dive count, and the list's filters and order.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1 import dives as dives_module
from src.app.api.v1 import tags as tags_module
from src.app.core.exceptions.http_exceptions import DuplicateValueException
from src.app.crud.crud_tags import (
    get_tags_for_dives,
    get_tags_page,
    replace_tags_for_dive,
    resolve_tag_ids,
    tag_ids_by_name,
    tag_name_exists,
)
from src.app.models.dive import Dive
from src.app.models.dive_tag import DiveTag
from src.app.models.tag import Tag
from src.app.models.user import User
from src.app.schemas.dive import DiveCreateRequest, DiveListSort, DiveRead, DiveType, DiveUpdateRequest
from src.app.schemas.tag import TagReadInternal, TagUpdate
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_tag

USER_ID = 1


def _current_user() -> dict[str, Any]:
    return {"id": USER_ID, "uuid": uuid7(), "username": "ada", "is_superuser": False}


def _dive_body(**members: Any) -> dict[str, Any]:
    return {"dive_number": 1, "start_time": "2026-06-01T09:00:00+02:00", "duration": 1800, **members}


class TestTheWriteSchema:
    def test_a_tag_is_trimmed_of_white_space_and_nothing_else(self) -> None:
        """DiveJSON's §3 rule 8 trims Unicode White_Space. `str.strip()` also strips U+001C
        to U+001F, which are not, so it is not what trims here."""
        body = DiveCreateRequest.model_validate(_dive_body(tags=["　 night ", "\x1cwreck"]))

        assert body.tags == ["night", "\x1cwreck"]

    @pytest.mark.parametrize("tag", ["", "   ", "x" * 65])
    def test_a_blank_or_over_long_tag_is_a_422(self, tag: str) -> None:
        with pytest.raises(ValidationError):
            DiveCreateRequest.model_validate(_dive_body(tags=[tag]))

    def test_sixty_four_characters_is_a_tag(self) -> None:
        assert DiveCreateRequest.model_validate(_dive_body(tags=["x" * 64])).tags == ["x" * 64]

    def test_two_that_fold_to_one_keep_the_first_spelling_and_place(self) -> None:
        """Case folding, not lowercasing: `GROSSES RIFF` is `Großes Riff` under it."""
        body = DiveCreateRequest.model_validate(_dive_body(tags=["Großes Riff", "night", "GROSSES RIFF", "NIGHT"]))

        assert body.tags == ["Großes Riff", "night"]

    def test_an_update_leaves_the_tags_unless_it_sends_them(self) -> None:
        assert DiveUpdateRequest.model_validate({}).tags is None
        assert DiveUpdateRequest.model_validate({"tags": []}).tags == []

    def test_a_boat_name_is_trimmed_and_never_blank(self) -> None:
        assert DiveCreateRequest.model_validate(_dive_body(boat_name="  Legend ")).boat_name == "Legend"
        for name in ("", "   ", "x" * 256):
            with pytest.raises(ValidationError):
                DiveCreateRequest.model_validate(_dive_body(boat_name=name))

    def test_a_patch_clears_the_boat_name_with_a_null(self) -> None:
        body = DiveUpdateRequest.model_validate({"boat_name": None})

        assert body.model_dump(exclude_unset=True) == {"boat_name": None}

    @pytest.mark.parametrize(
        ("member", "value"),
        [("type", "gauge"), ("current", "whirlpool"), ("waves", "tsunami"), ("weather", "hail"), ("entry_type", "sky")],
    )
    def test_a_vocabulary_value_outside_the_set_is_a_422_on_a_write(self, member: str, value: str) -> None:
        with pytest.raises(ValidationError):
            DiveCreateRequest.model_validate(_dive_body(**{member: value}))

    def test_the_rating_has_no_bound_but_the_column_s(self) -> None:
        """`ck_dive_rating_range` is the bound, as `ck_dive_altitude_range` is altitude's."""
        assert DiveCreateRequest.model_validate(_dive_body(rating=6)).rating == 6

    def test_a_stored_vocabulary_value_outside_the_set_reads_back_as_itself(self) -> None:
        read = DiveRead.model_validate(
            {
                "uuid": uuid7(),
                "user_uuid": uuid7(),
                "dive_number": 1,
                "start_time": "2026-06-01T09:00:00+02:00",
                "duration": 1800,
                "created_at": datetime(2026, 6, 1, tzinfo=UTC),
                "type": "frobnicator",
                "current": "frobnicator",
                "waves": "frobnicator",
                "weather": "frobnicator",
                "entry_type": "frobnicator",
            }
        )

        assert (read.type, read.entry_type) == ("frobnicator", "frobnicator")

    def test_a_rename_is_trimmed_and_may_not_be_null(self) -> None:
        assert TagUpdate.model_validate({"name": " night dive "}).name == "night dive"
        with pytest.raises(ValidationError):
            TagUpdate.model_validate({"name": None})


def _internal_tag(**overrides: Any) -> TagReadInternal:
    values: dict[str, Any] = {
        "id": 5,
        "user_id": USER_ID,
        "uuid": uuid7(),
        "name": "night",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    values.update(overrides)
    return TagReadInternal(**values)


@pytest.fixture
def route_collaborators(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    stubs: dict[str, Any] = {
        "owned": AsyncMock(return_value=_internal_tag()),
        "exists": AsyncMock(return_value=False),
        "update": AsyncMock(),
        "delete": AsyncMock(),
        "invalidate_dive_caches": AsyncMock(),
    }
    monkeypatch.setattr(tags_module, "_get_owned_tag", stubs["owned"])
    monkeypatch.setattr(tags_module, "tag_name_exists", stubs["exists"])
    monkeypatch.setattr(tags_module.crud_tags, "update", stubs["update"])
    monkeypatch.setattr(tags_module.crud_tags, "delete", stubs["delete"])
    monkeypatch.setattr(tags_module, "invalidate_dive_caches", stubs["invalidate_dive_caches"])
    return stubs


class TestTheRoutes:
    @pytest.mark.asyncio
    async def test_renaming_onto_another_tag_is_a_422(self, route_collaborators: dict[str, Any]) -> None:
        route_collaborators["exists"].return_value = True

        with pytest.raises(DuplicateValueException):
            await tags_module.patch_tag(
                request=MagicMock(),
                uuid=uuid7(),
                values=TagUpdate.model_validate({"name": "Wreck"}),
                current_user=_current_user(),
                db=MagicMock(),
            )

        assert route_collaborators["exists"].await_args.kwargs["exclude_id"] == 5
        route_collaborators["update"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_rename_drops_the_dive_caches(self, route_collaborators: dict[str, Any]) -> None:
        """A dive read carries its tags by name, so every cached one naming this tag is
        stale the moment it is renamed."""
        await tags_module.patch_tag(
            request=MagicMock(),
            uuid=uuid7(),
            values=TagUpdate.model_validate({"name": "night dive"}),
            current_user=_current_user(),
            db=MagicMock(),
        )

        assert route_collaborators["update"].await_args.kwargs["object"] == {"name": "night dive"}
        route_collaborators["invalidate_dive_caches"].assert_awaited_once_with(USER_ID)

    @pytest.mark.asyncio
    async def test_an_empty_patch_changes_nothing(self, route_collaborators: dict[str, Any]) -> None:
        await tags_module.patch_tag(
            request=MagicMock(),
            uuid=uuid7(),
            values=TagUpdate.model_validate({}),
            current_user=_current_user(),
            db=MagicMock(),
        )

        route_collaborators["update"].assert_not_awaited()
        route_collaborators["invalidate_dive_caches"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_delete_drops_the_dive_caches(self, route_collaborators: dict[str, Any]) -> None:
        await tags_module.erase_tag(request=MagicMock(), uuid=uuid7(), current_user=_current_user(), db=MagicMock())

        route_collaborators["delete"].assert_awaited_once()
        route_collaborators["invalidate_dive_caches"].assert_awaited_once_with(USER_ID)

    def test_a_vanished_tag_on_a_dive_is_named(self) -> None:
        error = IntegrityError("INSERT", {}, Exception('violates foreign key constraint "dive_tag_tag_id_fkey"'))

        assert dives_module._fk_error_detail(error) == "Tag not found."

    def test_the_list_cache_key_carries_every_new_filter_and_the_order(self) -> None:
        """`@cache` keys on its prefix's placeholders alone, so a filter missing from it
        serves one filtered page for another for sixty seconds."""
        text = Path(dives_module.__file__).read_text()

        for segment in ("tag_{tag_id}", "type_{dive_type}", "sort_{sort}"):
            assert segment in text


# ------------------------------------------------------------------ against Postgres


def _as(user: User) -> dict[str, Any]:
    return {"id": user.id, "uuid": user.uuid, "username": user.username, "is_superuser": False}


def _no_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("invalidate_dive_caches", "invalidate_gear_caches"):
        monkeypatch.setattr(dives_module, name, AsyncMock())


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheDatabase:
    def test_a_name_is_unique_per_diver_once_case_folded(self, db: Session, diver: User) -> None:
        """Unicode case folding, which `lower()` is not: `lower('Großes Riff')` keeps the ß,
        so an index on it would take both spellings."""
        suffix = uuid7().hex[-8:]
        create_tag(db, diver, name=f"Großes Riff {suffix}")
        db.add(Tag(user_id=diver.id, name=f"GROSSES RIFF {suffix.upper()}"))
        with pytest.raises(IntegrityError, match="ux_tag_user_id_name_folded"):
            db.commit()
        db.rollback()

    @pytest.mark.asyncio
    async def test_the_name_check_folds_as_the_index_does(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        mine = create_tag(db, diver, name=f"straße {uuid7().hex[-8:]}")
        name, tag_id, user_id, other_id = mine.name, mine.id, diver.id, other_diver.id

        assert await tag_name_exists(async_db, user_id, name.upper())
        assert not await tag_name_exists(async_db, other_id, name)
        assert not await tag_name_exists(async_db, user_id, name, exclude_id=tag_id)

    @pytest.mark.asyncio
    async def test_a_name_resolves_to_the_row_its_fold_matches_or_makes_one(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        suffix = uuid7().hex[-8:]
        night = create_tag(db, diver, name=f"night {suffix}")
        night_id, user_id = night.id, diver.id

        by_name = await tag_ids_by_name(
            async_db,
            user_id=user_id,
            names=[f"NIGHT {suffix}", f"Großes Riff {suffix}", f"GROSSES RIFF {suffix}"],
        )
        await async_db.commit()

        assert by_name[f"NIGHT {suffix}"] == night_id
        assert by_name[f"Großes Riff {suffix}"] == by_name[f"GROSSES RIFF {suffix}"]
        created = db.get(Tag, by_name[f"Großes Riff {suffix}"])
        assert created is not None and created.name == f"Großes Riff {suffix}"
        # And once in a write's order, at the first place.
        assert await resolve_tag_ids(async_db, user_id=user_id, names=[f"night {suffix}", f"NIGHT {suffix}"]) == [
            night_id
        ]

    @pytest.mark.asyncio
    async def test_the_list_counts_live_dives_and_keeps_a_tag_on_none(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """What `GET /dives?tag_uuid=` matches, so the list and the Tags card agree. A tag no
        dive carries any more stays, counting zero, until it is deleted."""
        carried, unused = create_tag(db, diver, name="wreck"), create_tag(db, diver, name="drift")
        live, hidden = create_dive(db, diver), create_dive(db, diver, is_deleted=True)
        carried_id, user_id = carried.id, diver.id
        await replace_tags_for_dive(async_db, live.id, [carried_id])
        await replace_tags_for_dive(async_db, hidden.id, [carried_id])

        page = await get_tags_page(async_db, user_id=user_id, offset=0, limit=10, search=None)

        assert [(row.name, row.dive_count) for row in page["data"]] == [(unused.name, 0), ("wreck", 1)]

    @pytest.mark.asyncio
    async def test_the_list_orders_under_the_default_collation(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """Not the Unicode one the uniqueness folds under, which orders by code point and
        would put `Wreck` before `drift`."""
        create_tag(db, diver, name="Wreck")
        create_tag(db, diver, name="drift")
        user_id = diver.id

        page = await get_tags_page(async_db, user_id=user_id, offset=0, limit=10, search="r")

        assert [row.name for row in page["data"]] == ["drift", "Wreck"]

    @pytest.mark.asyncio
    async def test_a_dive_write_makes_the_tags_it_names_and_reads_them_back_in_order(
        self, db: Session, async_db: AsyncSession, diver: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_caches(monkeypatch)
        existing = create_tag(db, diver, name="night")
        existing_id, user_id = existing.id, diver.id

        created = await dives_module.write_dive(
            request=MagicMock(),
            dive=DiveCreateRequest.model_validate(
                _dive_body(tags=["Wreck", "NIGHT", "wreck"], type="closed_circuit", rating=4, boat_name="Legend")
            ),
            current_user=_as(diver),
            db=async_db,
        )

        assert created.tags == ["Wreck", "night"]
        assert (created.type, created.rating, created.boat_name) == ("closed_circuit", 4, "Legend")
        ids = await get_tags_page(async_db, user_id=user_id, offset=0, limit=10, search=None)
        assert {row.name for row in ids["data"]} == {"night", "Wreck"}
        dive_id = (await async_db.execute(select(Dive.id).where(Dive.uuid == created.uuid))).scalar_one()
        tag_ids = (
            await async_db.execute(select(DiveTag.tag_id).where(DiveTag.dive_id == dive_id).order_by(DiveTag.position))
        ).scalars()
        assert list(tag_ids)[1] == existing_id

    @pytest.mark.asyncio
    async def test_a_patch_replaces_the_tags_it_sends_and_leaves_them_otherwise(
        self, db: Session, async_db: AsyncSession, diver: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_caches(monkeypatch)
        dive = create_dive(db, diver)
        dive_id, dive_uuid = dive.id, dive.uuid

        async def patch(body: dict[str, Any]) -> list[str]:
            await dives_module.patch_dive(
                request=MagicMock(),
                uuid=dive_uuid,
                values=DiveUpdateRequest.model_validate(body),
                current_user=_as(diver),
                db=async_db,
            )
            return (await get_tags_for_dives(async_db, [dive_id]))[dive_id]

        assert await patch({"tags": ["night", "drift"]}) == ["night", "drift"]
        assert await patch({"rating": 3}) == ["night", "drift"]
        assert await patch({"tags": ["drift"]}) == ["drift"]
        assert await patch({"tags": []}) == []

    @pytest.mark.asyncio
    async def test_deleting_a_tag_takes_it_off_every_dive(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        tag, dive = create_tag(db, diver), create_dive(db, diver)
        tag_id, tag_uuid, dive_id = tag.id, tag.uuid, dive.id
        await replace_tags_for_dive(async_db, dive_id, [tag_id])

        await tags_module.crud_tags.delete(db=async_db, uuid=tag_uuid)

        db.expunge_all()
        assert db.get(Tag, tag_id) is None
        assert (await get_tags_for_dives(async_db, [dive_id]))[dive_id] == []

    @pytest.mark.asyncio
    async def test_the_list_filters_on_a_tag_and_a_type(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        tag = create_tag(db, diver)
        tagged, rebreather, plain = create_dive(db, diver), create_dive(db, diver), create_dive(db, diver)
        rebreather.type = "closed_circuit"
        db.commit()
        await replace_tags_for_dive(async_db, tagged.id, [tag.id])
        tagged_uuid, rebreather_uuid, tag_id, user_id, user_uuid = (
            tagged.uuid,
            rebreather.uuid,
            tag.id,
            diver.id,
            diver.uuid,
        )
        assert plain.uuid not in (tagged_uuid, rebreather_uuid)

        async def page(**filters: Any) -> list[Any]:
            arguments: dict[str, Any] = {
                "trip_id": None,
                "course_id": None,
                "dive_site_id": None,
                "gear_item_id": None,
                "species_id": None,
                "person_id": None,
                "tag_id": None,
                "dive_type": None,
                "sort": DiveListSort.DATE,
            }
            arguments.update(filters)
            result = await dives_module._cached_read_dives.__wrapped__(  # type: ignore[attr-defined]
                request=None, user_id=user_id, user_uuid=user_uuid, db=async_db, page=1, items_per_page=10, **arguments
            )
            return [row["uuid"] for row in result["data"]]

        assert await page(tag_id=tag_id) == [tagged_uuid]
        assert await page(tag_id=-1) == []
        assert await page(dive_type=DiveType.CLOSED_CIRCUIT) == [rebreather_uuid]

    @pytest.mark.asyncio
    async def test_the_rating_order_puts_every_unrated_dive_last(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """`NULLS LAST` spelled out, which only Postgres can pin: a bare `DESC` there puts
        the unrated first, and SQLite would sort them last either way. Ties go newest first."""
        start = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
        rows = [
            Dive(
                user_id=diver.id, dive_number=n, start_time=start + timedelta(days=n), duration=1800, notes="", rating=r
            )
            for n, r in ((1, None), (2, 3), (3, 5), (4, None), (5, 3))
        ]
        db.add_all(rows)
        db.commit()
        by_number = {row.uuid: row.dive_number for row in rows}
        user_id, user_uuid = diver.id, diver.uuid

        result = await dives_module._cached_read_dives.__wrapped__(  # type: ignore[attr-defined]
            request=None,
            user_id=user_id,
            user_uuid=user_uuid,
            db=async_db,
            page=1,
            items_per_page=10,
            trip_id=None,
            course_id=None,
            dive_site_id=None,
            gear_item_id=None,
            species_id=None,
            person_id=None,
            tag_id=None,
            dive_type=None,
            sort=DiveListSort.RATING,
        )

        assert [by_number[row["uuid"]] for row in result["data"]] == [3, 5, 2, 4, 1]
