"""Tests the premise the "no view for a deleted item's history" decision rests on:
archiving a gear item keeps its service history reachable, deleting it does not.

`_owned_gear_item` is the gate on both service listings, and it filters `is_deleted` and
nothing else. That is what makes archiving - rather than a new endpoint, a flag on
`GET /gear-service-records`, or a relaxed 422 - the answer to a diver who wants to retire
a piece of kit and still read what was done to it. Adding `is_archived=False` here to
match the gear *listing*'s filter looks like a tidy-up and would quietly take the history
off archived items too, which is why the archived half is asserted rather than assumed.

See "A deleted gear item's service history has no view, and archiving is the surface that
does" in DECISIONS.md.

These run against a live Postgres and skip themselves otherwise - see CONTRIBUTING.md for
why a run on the host needs `POSTGRES_SERVER=localhost` to make them execute.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.api.v1.gear_service import _owned_gear_item
from src.app.core.exceptions.http_exceptions import UnprocessableEntityException
from src.app.models.user import User
from tests.conftest import db_available
from tests.helpers.generators import create_gear_item


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestServiceHistoryReachability:
    @pytest.mark.asyncio
    async def test_an_archived_item_still_resolves(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """The whole decision in one assertion: an archived item passes the gate, so
        `?gear_item_uuid=` goes on listing its schedules and records while the digest
        leaves it alone."""
        item = create_gear_item(db, diver, is_archived=True)

        assert (await _owned_gear_item(async_db, item.uuid, diver.id)).id == item.id

    @pytest.mark.asyncio
    async def test_a_deleted_item_is_refused(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """The other half, deliberately: its records survive for export and for an undelete
        that does not exist yet, not for a view."""
        item = create_gear_item(db, diver, is_deleted=True)

        with pytest.raises(UnprocessableEntityException):
            await _owned_gear_item(async_db, item.uuid, diver.id)

    @pytest.mark.asyncio
    async def test_another_divers_item_is_refused_the_same_way(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        """Archiving is not a way to probe someone else's uuids: "not yours" and
        "doesn't exist" answer identically here, as `_owned_gear_item`'s docstring says."""
        item = create_gear_item(db, other_diver, is_archived=True)

        with pytest.raises(UnprocessableEntityException):
            await _owned_gear_item(async_db, item.uuid, diver.id)
