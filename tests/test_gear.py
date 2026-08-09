"""Unit tests for the gear feature (`models/gear_*.py`, `schemas/gear_*.py`,
`services/gear_stats.py`, `api/v1/gear_items.py`, `api/v1/gear_sets.py`).

These cover the pieces that are pure logic or pure SQL construction and so need no
database: the public/internal shape conversions, the derived `archived_at` timestamp,
the join-table replace helpers' de-duplication, and the shape of the `dive_count`
recalculation statement. The endpoint behaviour on top of a live Postgres/Redis is
exercised end to end by hand (see DECISIONS.md), not here.
"""

from datetime import UTC, datetime
from fnmatch import fnmatch
from unittest.mock import AsyncMock, MagicMock

import pytest
from uuid6 import uuid7

from src.app.api.v1.gear_items import _to_public_gear_item
from src.app.api.v1.gear_sets import _to_public_gear_set
from src.app.crud.crud_dive_gear_items import replace_gear_items_for_dive
from src.app.crud.crud_gear_set_items import replace_gear_items_for_set
from src.app.models.dive_gear_item import DiveGearItem
from src.app.models.gear_set_item import GearSetItem
from src.app.schemas.gear_item import GearItemInfo, GearItemReadInternal, GearItemUpdate, GearType
from src.app.schemas.gear_set import GearSetCreateRequest, GearSetReadInternal, GearSetUpdateRequest
from src.app.services.cache_invalidation import invalidate_dive_caches, invalidate_gear_caches
from src.app.services.gear_stats import recalculate_gear_dive_counts


def _internal_gear_item(**overrides) -> GearItemReadInternal:
    defaults = {
        "id": 7,
        "user_id": 1,
        "uuid": uuid7(),
        "name": "MK25 EVO",
        "brand": "Scubapro",
        "type": GearType.REGULATOR,
        "notes": "serviced 2025",
        "rented": False,
        "is_archived": False,
        "archived_at": None,
        "dive_count": 12,
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
    }
    return GearItemReadInternal(**{**defaults, **overrides})


class TestPublicShapeConversion:
    def test_gear_item_drops_internal_ids_and_resolves_the_owner_uuid(self) -> None:
        user_uuid = uuid7()
        internal = _internal_gear_item()

        public = _to_public_gear_item(internal, user_uuid=user_uuid)

        assert public.user_uuid == user_uuid
        assert public.uuid == internal.uuid
        assert public.dive_count == 12
        # The sequential internal id/user_id must never reach the public shape.
        assert not hasattr(public, "id")
        assert not hasattr(public, "user_id")

    def test_gear_item_accepts_a_dict_row(self) -> None:
        """`crud.get_multi` yields dicts rather than models, so both must work."""
        user_uuid = uuid7()
        internal = _internal_gear_item(dive_count=0)

        public = _to_public_gear_item(internal.model_dump(), user_uuid=user_uuid)

        assert public.name == "MK25 EVO"
        assert public.dive_count == 0

    def test_gear_set_embeds_its_items_in_order(self) -> None:
        user_uuid = uuid7()
        internal = GearSetReadInternal(
            id=3, user_id=1, uuid=uuid7(), name="Sidemount", created_at=datetime(2025, 1, 1, tzinfo=UTC)
        )
        items = [
            GearItemInfo(uuid=uuid7(), name="Left reg", brand="Apeks"),
            GearItemInfo(uuid=uuid7(), name="Right reg", brand="Apeks", is_archived=True),
        ]

        public = _to_public_gear_set(internal, user_uuid=user_uuid, gear_items=items)

        assert [i.name for i in public.gear_items] == ["Left reg", "Right reg"]
        assert public.gear_items[1].is_archived is True
        assert public.user_uuid == user_uuid
        assert not hasattr(public, "user_id")


class TestGearType:
    def test_is_a_closed_vocabulary(self) -> None:
        """Unknown categories are rejected rather than stored as free text, which is
        what keeps the same kit named the same way across a diver's whole list."""
        with pytest.raises(ValueError):
            GearItemUpdate(type="spaceship")  # type: ignore[arg-type]

    def test_accepts_a_member_by_its_string_value(self) -> None:
        """Clients send the wire value ("fins"), not the Python member."""
        assert GearItemUpdate(type="fins").type is GearType.FINS  # type: ignore[arg-type]

    def test_serializes_as_its_plain_string_value(self) -> None:
        """`StrEnum` keeps the JSON shape a plain string, so clients never see
        "GearType.FINS"."""
        assert GearItemInfo(uuid=uuid7(), name="Jetfins", type=GearType.FINS).model_dump(mode="json")["type"] == "fins"

    def test_is_optional_everywhere(self) -> None:
        """Gear logged before types existed has none, and categorizing a one-off piece
        of kit shouldn't be required to save it."""
        assert GearItemUpdate().type is None
        assert GearItemInfo(uuid=uuid7(), name="Odd kit").type is None

    def test_members_are_declared_in_kit_order_not_alphabetically(self) -> None:
        """Declaration order is part of the contract - callers sort by it to list kit
        the way a diver lays it out."""
        values = [t.value for t in GearType]
        assert values[:3] == ["mask", "snorkel", "fins"]
        assert values[-1] == "other"
        assert values != sorted(values)


class TestGearSchemas:
    def test_gear_item_update_distinguishes_unset_from_explicit_null_brand(self) -> None:
        """The PATCH handler keys off `model_fields_set` to tell "leave the brand alone"
        apart from "clear the brand", so an omitted field must not look like an explicit
        `None`."""
        assert "brand" not in GearItemUpdate().model_fields_set
        assert "brand" in GearItemUpdate(brand=None).model_fields_set

    def test_gear_set_update_leaves_items_untouched_when_omitted(self) -> None:
        assert GearSetUpdateRequest().gear_item_uuids is None
        # An explicit empty list is a real instruction: empty the set.
        assert GearSetUpdateRequest(gear_item_uuids=[]).gear_item_uuids == []

    def test_gear_set_create_defaults_to_an_empty_item_list(self) -> None:
        assert GearSetCreateRequest(user_uuid=uuid7(), name="Rec").gear_item_uuids == []

    def test_gear_item_update_rejects_unknown_fields(self) -> None:
        """`extra="forbid"` keeps derived columns (`dive_count`, `archived_at`) from
        being set straight from a request body."""
        with pytest.raises(ValueError):
            GearItemUpdate(dive_count=99)  # type: ignore[call-arg]


def _replace_db_mock() -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db


class TestReplaceJoinRows:
    @pytest.mark.asyncio
    async def test_dive_gear_is_written_in_order(self) -> None:
        db = _replace_db_mock()

        await replace_gear_items_for_dive(db, dive_id=5, gear_item_ids=[9, 4, 7])

        added = [call.args[0] for call in db.add.call_args_list]
        assert all(isinstance(row, DiveGearItem) for row in added)
        assert [(row.gear_item_id, row.position) for row in added] == [(9, 0), (4, 1), (7, 2)]
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_duplicate_ids_are_collapsed_keeping_first_position(self) -> None:
        db = _replace_db_mock()

        await replace_gear_items_for_dive(db, dive_id=5, gear_item_ids=[9, 4, 9])

        added = [call.args[0] for call in db.add.call_args_list]
        assert [(row.gear_item_id, row.position) for row in added] == [(9, 0), (4, 1)]

    @pytest.mark.asyncio
    async def test_empty_list_clears_the_dive_gear(self) -> None:
        db = _replace_db_mock()

        await replace_gear_items_for_dive(db, dive_id=5, gear_item_ids=[])

        db.add.assert_not_called()
        # The DELETE still runs, so passing [] genuinely empties the list.
        db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_gear_set_items_are_written_in_order(self) -> None:
        db = _replace_db_mock()

        await replace_gear_items_for_set(db, gear_set_id=2, gear_item_ids=[3, 1])

        added = [call.args[0] for call in db.add.call_args_list]
        assert all(isinstance(row, GearSetItem) for row in added)
        assert [(row.gear_item_id, row.position) for row in added] == [(3, 0), (1, 1)]

    @pytest.mark.asyncio
    async def test_skips_commit_when_commit_is_false(self) -> None:
        db = _replace_db_mock()

        await replace_gear_items_for_dive(db, dive_id=5, gear_item_ids=[1], commit=False)

        db.commit.assert_not_awaited()


class TestRecalculateGearDiveCounts:
    @pytest.mark.asyncio
    async def test_updates_only_the_users_items_and_only_live_dives_count(self) -> None:
        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()

        await recalculate_gear_dive_counts(db, user_id=42)

        statement = str(db.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
        assert statement.startswith("UPDATE gear_item SET dive_count=")
        assert "gear_item.user_id = 42" in statement
        # Soft-deleted dives must not contribute to any item's count.
        assert "dive.is_deleted IS false" in statement
        # Rows already holding the right count are skipped rather than rewritten.
        assert "IS DISTINCT FROM" in statement
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_commit_when_commit_is_false(self) -> None:
        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()

        await recalculate_gear_dive_counts(db, user_id=1, commit=False)

        db.commit.assert_not_awaited()


class TestCacheInvalidationPatterns:
    """`invalidate_dive_caches`/`invalidate_gear_caches` are pattern deletes, so the
    patterns are the contract: too narrow and renames go stale, too broad and they
    wipe unrelated resources' caches. See `services/cache_invalidation.py`.
    """

    @pytest.mark.asyncio
    async def test_dive_invalidation_covers_both_list_and_item_keys(self, monkeypatch) -> None:
        patterns: list[str] = []
        monkeypatch.setattr(
            "src.app.services.cache_invalidation.delete_keys_by_pattern",
            AsyncMock(side_effect=lambda pattern: patterns.append(pattern)),
        )

        await invalidate_dive_caches(7)

        assert patterns == ["user_7_dives:*", "user_7_dive:*"]

    @pytest.mark.asyncio
    async def test_dive_invalidation_does_not_sweep_the_dive_site_list(self, monkeypatch) -> None:
        """A single `user_7_dive*` would also match `user_7_dive_sites:page_...`, making
        every dive edit needlessly rebuild the dive *site* list cache."""
        patterns: list[str] = []
        monkeypatch.setattr(
            "src.app.services.cache_invalidation.delete_keys_by_pattern",
            AsyncMock(side_effect=lambda pattern: patterns.append(pattern)),
        )

        await invalidate_dive_caches(7)

        assert not any(fnmatch("user_7_dive_sites:page_1:items_per_page:10", p) for p in patterns)
        # ...while still matching the keys it is meant to drop.
        assert any(fnmatch("user_7_dive:019f-abc", p) for p in patterns)
        assert any(fnmatch("user_7_dives:page_1:items_per_page:10:trip_None:site_None:gear_None", p) for p in patterns)

    @pytest.mark.asyncio
    async def test_gear_invalidation_covers_every_gear_key_and_nothing_else(self, monkeypatch) -> None:
        patterns: list[str] = []
        monkeypatch.setattr(
            "src.app.services.cache_invalidation.delete_keys_by_pattern",
            AsyncMock(side_effect=lambda pattern: patterns.append(pattern)),
        )

        await invalidate_gear_caches(7)

        matches = lambda key: any(fnmatch(key, p) for p in patterns)  # noqa: E731
        assert matches("user_7_gear_items:page_1:items_per_page:10:archived_False")
        assert matches("user_7_gear_item:019f-abc")
        assert matches("user_7_gear_sets:page_1:items_per_page:10")
        assert matches("user_7_gear_set:019f-abc")
        # Another user's gear, and this user's non-gear caches, must survive.
        assert not matches("user_8_gear_items:page_1:items_per_page:10:archived_False")
        assert not matches("user_7_dives:page_1:items_per_page:10")
