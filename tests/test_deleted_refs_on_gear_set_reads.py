"""Tests that a gear set stops naming a gear item the diver has deleted.

The last surface in the family `test_deleted_refs_on_dive_reads.py` covers, and the one
where the answer was chosen rather than inherited: a gear set is a template, so clearing
the `gear_set_item` rows in `erase_gear_item` was on the table too. It reads instead, and
the rows stay - see DECISIONS.md, and `get_gear_items_for_set` for the short version.

Same shape as the dive-read module: the links survive the delete, so nothing but the
query's own `WHERE` decides whether the app renders an orphan, and a stubbed session
would only answer with whatever rows the stub was handed. These run against a live
Postgres and skip themselves otherwise - see CONTRIBUTING.md for why a run on the host
needs `POSTGRES_SERVER=localhost` to make them execute.

The route-level half lives with its siblings rather than here, since it is one route and
one stub: `TestErasingGearItemDropsTheCachedReads` in `test_deleted_refs_on_dive_reads.py`
pins that `erase_gear_item` drops the gear caches (the set reads' staleness) as well as
the dive ones. That the gear pattern reaches the set keys at all is pinned separately, by
`TestCacheInvalidationPatterns` in `test_gear.py`.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.crud.crud_gear_set_items import (
    get_gear_items_for_set,
    get_gear_items_for_sets,
    replace_gear_items_for_set,
)
from src.app.models.gear_set_item import GearSetItem
from src.app.models.user import User
from tests.conftest import db_available
from tests.helpers.generators import create_gear_item, create_gear_set


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestDeletedGearItemsAreNotRenderedInSets:
    @pytest.mark.asyncio
    async def test_a_deleted_item_drops_out_of_the_set(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """The bug in one assertion: `GET /gear-item/{uuid}` 404s for this item and
        `PATCH /gear-set` refuses it back, so the set must not go on listing it."""
        live, deleted = create_gear_item(db, diver), create_gear_item(db, diver, is_deleted=True)
        gear_set = create_gear_set(db, diver)
        await replace_gear_items_for_set(async_db, gear_set_id=gear_set.id, gear_item_ids=[live.id, deleted.id])

        items = await get_gear_items_for_set(async_db, gear_set_id=gear_set.id)

        assert [item.uuid for item in items] == [live.uuid]

    @pytest.mark.asyncio
    async def test_a_set_whose_only_item_is_deleted_reads_back_empty(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        deleted = create_gear_item(db, diver, is_deleted=True)
        gear_set = create_gear_set(db, diver)
        await replace_gear_items_for_set(async_db, gear_set_id=gear_set.id, gear_item_ids=[deleted.id])

        assert await get_gear_items_for_set(async_db, gear_set_id=gear_set.id) == []

    @pytest.mark.asyncio
    async def test_the_rest_keep_their_order(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """`position` is a sort key, not an identity, so removing the item that held 0
        leaves the survivors in the order the diver put them in rather than renumbering
        anything. Nothing writes a compacted `position` back - the gap stays until the
        next `replace_gear_items_for_set`, and no constraint minds."""
        deleted = create_gear_item(db, diver, is_deleted=True)
        second, third = create_gear_item(db, diver), create_gear_item(db, diver)
        gear_set = create_gear_set(db, diver)
        await replace_gear_items_for_set(
            async_db, gear_set_id=gear_set.id, gear_item_ids=[deleted.id, second.id, third.id]
        )

        items = await get_gear_items_for_set(async_db, gear_set_id=gear_set.id)

        assert [item.uuid for item in items] == [second.uuid, third.uuid]

    @pytest.mark.asyncio
    async def test_an_archived_item_still_comes_through(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """The distinction the filter has to keep, and it matters more on a set than on a
        dive: archiving retires kit from the dive form's picker, and a set that quietly
        shed its archived members would stop loading the configuration it names."""
        archived = create_gear_item(db, diver, is_archived=True)
        gear_set = create_gear_set(db, diver)
        await replace_gear_items_for_set(async_db, gear_set_id=gear_set.id, gear_item_ids=[archived.id])

        items = await get_gear_items_for_set(async_db, gear_set_id=gear_set.id)

        assert [(item.uuid, item.is_archived) for item in items] == [(archived.uuid, True)]

    @pytest.mark.asyncio
    async def test_the_batched_loader_hides_them_too(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """`GET /gear-sets` builds its page through the batched loader, so a filter on the
        single-set one alone would leave the list still showing the deleted item."""
        live, deleted = create_gear_item(db, diver), create_gear_item(db, diver, is_deleted=True)
        with_live, with_deleted = create_gear_set(db, diver), create_gear_set(db, diver)
        await replace_gear_items_for_set(async_db, gear_set_id=with_live.id, gear_item_ids=[live.id])
        await replace_gear_items_for_set(async_db, gear_set_id=with_deleted.id, gear_item_ids=[deleted.id])

        by_set = await get_gear_items_for_sets(async_db, gear_set_ids=[with_live.id, with_deleted.id])

        assert [item.uuid for item in by_set[with_live.id]] == [live.uuid]
        # Present and empty rather than absent - the pre-seeded lists are what keep a set
        # whose every item is gone from dropping out of the mapping its caller indexes.
        assert by_set[with_deleted.id] == []

    @pytest.mark.asyncio
    async def test_the_link_rows_survive_the_filter(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """The half that is easy to lose to a later "tidy up the orphans" change: hiding
        was chosen *over* clearing the rows at delete time, so the membership has to still
        be in the database for `_owned` to resurrect the item into an export."""
        deleted = create_gear_item(db, diver, is_deleted=True)
        gear_set = create_gear_set(db, diver)
        await replace_gear_items_for_set(async_db, gear_set_id=gear_set.id, gear_item_ids=[deleted.id])

        rows = await async_db.execute(select(GearSetItem.gear_item_id).where(GearSetItem.gear_set_id == gear_set.id))

        assert list(rows.scalars().all()) == [deleted.id]
