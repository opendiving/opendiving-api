"""Tests for `move_dives_to` on `DELETE /trip/{uuid}` and `DELETE /dive-site/{uuid}` -
the one-request form of "put these dives somewhere else, then delete this".

Split the way `test_trips.py` splits, and for the same reason. The route half is what the
caller is told and in what order (`422` for a replacement that isn't theirs, the reassign
landing *before* the delete that commits it, dive caches dropped only when a dive actually
moved) - questions about the call, answerable against stubs.

The crud half is what the database ends up holding, which no stubbed session can answer.
`replace_dive_site_on_dives` in particular is three set-based statements standing in for a
per-dive read-modify-write, and every rule it has to preserve is invisible to a mock: the
replacement inheriting the doomed site's *slot* (position 0 is the primary site the dive
list shows), a dive already logged at both sites ending up with one row rather than
violating `ux_dive_dive_site_dive_id_dive_site_id`, and the positions left contiguous
afterwards. Those skip themselves when no database is reachable - see CONTRIBUTING.md for
why a run on the host needs `POSTGRES_SERVER=localhost` to make them execute.
"""

import uuid as uuid_pkg
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1 import dive_sites as dive_sites_module
from src.app.api.v1 import trips as trips_module
from src.app.core.exceptions.http_exceptions import NotFoundException, UnprocessableEntityException
from src.app.core.utils import cache as cache_module
from src.app.crud.crud_dive_dive_sites import replace_dive_site_on_dives, replace_dive_sites_for_dive
from src.app.crud.crud_dives import reassign_dives_to_trip
from src.app.models.dive import Dive
from src.app.models.dive_dive_site import DiveDiveSite
from src.app.models.user import User
from src.app.schemas.dive_site import DiveSiteReadInternal
from src.app.schemas.trip import TripReadInternal
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_dive_site, create_trip

USER_ID = 7
USER_UUID = uuid7()


class _FakeRedis:
    """A cache that never hits, recording what the decorator deletes. Both routes carry a
    `@cache` decorator that needs a client in place or the call raises before the body."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def get(self, key: str) -> None:
        return None

    async def set(self, key: str, value: str) -> None:
        return None

    async def expire(self, key: str, seconds: int) -> None:
        return None

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


def _current_user() -> dict[str, Any]:
    return {"id": USER_ID, "uuid": USER_UUID}


def _records(calls: list[str], name: str, result: Any = None) -> Any:
    """An `AsyncMock` side effect that notes it ran, so a test can assert the order two
    stubs were called in rather than only that both were."""

    def side_effect(**_: Any) -> Any:
        calls.append(name)
        return result

    return side_effect


def _internal_trip(uuid: uuid_pkg.UUID) -> TripReadInternal:
    return TripReadInternal(
        id=11,
        uuid=uuid,
        user_id=USER_ID,
        name="Cebu 2026",
        start_date=date(2026, 3, 1),
        end_date=None,
        notes="",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _internal_dive_site(uuid: uuid_pkg.UUID) -> DiveSiteReadInternal:
    return DiveSiteReadInternal(
        id=21,
        uuid=uuid,
        user_id=USER_ID,
        name="Pescador Island",
        location="Moalboal",
        latitude=None,
        longitude=None,
        notes="",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def trip_route(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stubs everything `erase_trip` touches, recording the calls and their order.

    `calls` is the point of the shared list: the reassignment is only atomic because it
    writes through the same session as the delete that commits it, so it has to run
    *first*. Nothing else about the two calls says so.
    """
    calls: list[str] = []
    uuid = uuid7()
    stubs: dict[str, Any] = {
        "uuid": uuid,
        "calls": calls,
        "owned": AsyncMock(return_value=_internal_trip(uuid)),
        "resolve": AsyncMock(return_value=99),
        "reassign": AsyncMock(side_effect=_records(calls, "reassign", 3)),
        "delete": AsyncMock(side_effect=_records(calls, "delete")),
        "invalidate_list": AsyncMock(),
        "invalidate_dives": AsyncMock(),
        "redis": _FakeRedis(),
    }

    monkeypatch.setattr(cache_module, "client", stubs["redis"])
    monkeypatch.setattr(trips_module, "_get_owned_trip", stubs["owned"])
    monkeypatch.setattr(trips_module, "resolve_trip_id_for_user", stubs["resolve"])
    monkeypatch.setattr(trips_module, "reassign_dives_to_trip", stubs["reassign"])
    monkeypatch.setattr(trips_module.crud_trips, "delete", stubs["delete"])
    monkeypatch.setattr(trips_module._trip_cache, "invalidate_list", stubs["invalidate_list"])
    monkeypatch.setattr(trips_module, "invalidate_dive_caches", stubs["invalidate_dives"])

    return stubs


@pytest.fixture
def dive_site_route(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The same stubbing for `erase_dive_site`."""
    calls: list[str] = []
    uuid = uuid7()
    replacement_uuid = uuid7()
    stubs: dict[str, Any] = {
        "uuid": uuid,
        "replacement_uuid": replacement_uuid,
        "calls": calls,
        "owned": AsyncMock(return_value=_internal_dive_site(uuid)),
        "resolve": AsyncMock(return_value={replacement_uuid: 99}),
        "replace": AsyncMock(side_effect=_records(calls, "replace", 3)),
        "delete": AsyncMock(side_effect=_records(calls, "delete")),
        "invalidate_list": AsyncMock(),
        "invalidate_dives": AsyncMock(),
        "redis": _FakeRedis(),
    }

    monkeypatch.setattr(cache_module, "client", stubs["redis"])
    monkeypatch.setattr(dive_sites_module, "_get_owned_dive_site", stubs["owned"])
    monkeypatch.setattr(dive_sites_module, "resolve_dive_site_ids_for_user", stubs["resolve"])
    monkeypatch.setattr(dive_sites_module, "replace_dive_site_on_dives", stubs["replace"])
    monkeypatch.setattr(dive_sites_module.crud_dive_sites, "delete", stubs["delete"])
    monkeypatch.setattr(dive_sites_module._dive_site_cache, "invalidate_list", stubs["invalidate_list"])
    monkeypatch.setattr(dive_sites_module, "invalidate_dive_caches", stubs["invalidate_dives"])

    return stubs


def _request() -> MagicMock:
    request = MagicMock()
    request.method = "DELETE"
    return request


async def _erase_trip(stubs: dict[str, Any], move_dives_to: uuid_pkg.UUID | None = None) -> Any:
    return await trips_module.erase_trip(
        request=_request(),
        uuid=stubs["uuid"],
        current_user=_current_user(),
        db=MagicMock(),
        move_dives_to=move_dives_to,
    )


async def _erase_dive_site(stubs: dict[str, Any], move_dives_to: uuid_pkg.UUID | None = None) -> Any:
    return await dive_sites_module.erase_dive_site(
        request=_request(),
        uuid=stubs["uuid"],
        current_user=_current_user(),
        db=MagicMock(),
        move_dives_to=move_dives_to,
    )


class TestEraseTripWithoutTheParameter:
    """The web app keeps calling the bare form, so it has to keep behaving as it did."""

    @pytest.mark.asyncio
    async def test_nothing_is_reassigned(self, trip_route: dict[str, Any]) -> None:
        await _erase_trip(trip_route)

        trip_route["reassign"].assert_not_awaited()
        trip_route["delete"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_response_is_the_bare_message(self, trip_route: dict[str, Any]) -> None:
        """The same `{"message": ...}` every other delete on the API answers with. This
        route used to publish a `moved_dives` count as well, for a toast that no longer
        names a number."""
        assert await _erase_trip(trip_route) == {"message": "Trip deleted"}

    @pytest.mark.asyncio
    async def test_the_dive_caches_are_dropped_anyway(self, trip_route: dict[str, Any]) -> None:
        """A plain delete nulls `dive.trip_id` on every dive that was on the trip - the FK is
        `ON DELETE SET NULL` and the delete is real - so each of those dives starts reading
        back `trip_uuid: null`. Skipping this would leave the cached reads naming a trip a
        fresh read no longer does, for the rest of the hour. The route did skip it once, back
        when a soft-deleted trip left both the column and the lookup answering as before."""
        await _erase_trip(trip_route)

        trip_route["invalidate_dives"].assert_awaited_once_with(USER_ID)


class TestEraseTripWithAReplacement:
    @pytest.mark.asyncio
    async def test_the_dives_move_before_the_delete_commits_them(self, trip_route: dict[str, Any]) -> None:
        """The whole reason for the parameter: one transaction, so a failure cannot leave
        half the log moved and the trip still standing."""
        await _erase_trip(trip_route, move_dives_to=uuid7())

        assert trip_route["calls"] == ["reassign", "delete"]

    @pytest.mark.asyncio
    async def test_the_move_is_scoped_to_the_owner_and_the_two_trips(self, trip_route: dict[str, Any]) -> None:
        await _erase_trip(trip_route, move_dives_to=uuid7())

        assert trip_route["reassign"].await_args.kwargs == {
            "db": trip_route["reassign"].await_args.kwargs["db"],
            "user_id": USER_ID,
            "from_trip_id": 11,
            "to_trip_id": 99,
        }

    @pytest.mark.asyncio
    async def test_the_response_says_nothing_about_what_moved(self, trip_route: dict[str, Any]) -> None:
        """Identical to the bare delete's, though the stub moved three dives: the count is
        not in the contract any more, so a move is not distinguishable from a plain delete
        by the response body."""
        assert await _erase_trip(trip_route, move_dives_to=uuid7()) == {"message": "Trip deleted"}

    @pytest.mark.asyncio
    async def test_the_moved_dives_reads_are_dropped(self, trip_route: dict[str, Any]) -> None:
        await _erase_trip(trip_route, move_dives_to=uuid7())

        trip_route["invalidate_dives"].assert_awaited_once_with(USER_ID)

    @pytest.mark.asyncio
    async def test_a_replacement_that_moved_nothing_still_drops_the_caches(self, trip_route: dict[str, Any]) -> None:
        """Nothing gates the invalidation: the trip is gone from every dive read either
        way, so an empty trip's delete has to drop the caches like any other. This once
        keyed off the number the reassignment returned, which is why the zero case is
        pinned separately."""
        trip_route["reassign"].side_effect = _records(trip_route["calls"], "reassign", 0)

        await _erase_trip(trip_route, move_dives_to=uuid7())

        trip_route["invalidate_dives"].assert_awaited_once_with(USER_ID)

    @pytest.mark.asyncio
    async def test_a_replacement_that_is_not_the_callers_is_a_422(self, trip_route: dict[str, Any]) -> None:
        """422 rather than 404, matching `PATCH /dive`'s answer for a `trip_uuid` it
        cannot resolve - the per-dive call this parameter replaces. The 404s on this route
        belong to the trip being addressed."""
        trip_route["resolve"].return_value = None

        with pytest.raises(UnprocessableEntityException):
            await _erase_trip(trip_route, move_dives_to=uuid7())

    @pytest.mark.asyncio
    async def test_a_trip_cannot_be_moved_onto_itself(self, trip_route: dict[str, Any]) -> None:
        """It would resolve fine and move every dive onto a trip that is about to be
        deleted, which is the one outcome the diver cannot have asked for."""
        with pytest.raises(UnprocessableEntityException):
            await _erase_trip(trip_route, move_dives_to=trip_route["uuid"])

    @pytest.mark.asyncio
    async def test_a_bad_replacement_deletes_nothing(self, trip_route: dict[str, Any]) -> None:
        trip_route["resolve"].return_value = None

        with pytest.raises(UnprocessableEntityException):
            await _erase_trip(trip_route, move_dives_to=uuid7())

        trip_route["delete"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_second_delete_moves_nothing(self, trip_route: dict[str, Any]) -> None:
        """This route used to be idempotent, and is not any more. The insurance was against
        a half-failed multi-statement delete; one `DELETE FROM trip` in one transaction
        cannot half-fail, so a repeat call finds nothing and must not run the reassignment
        against a trip id that no longer names a row."""
        trip_route["owned"].side_effect = NotFoundException("Trip not found")

        with pytest.raises(NotFoundException):
            await _erase_trip(trip_route, move_dives_to=uuid7())

        trip_route["reassign"].assert_not_awaited()
        trip_route["delete"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_trip_that_is_not_the_callers_is_still_a_404(self, trip_route: dict[str, Any]) -> None:
        """Ownership of the addressed trip is settled before the replacement is even
        looked at, so a bad replacement cannot turn someone else's 404 into a 422 that
        confirms the uuid exists."""
        trip_route["owned"].side_effect = NotFoundException("Trip not found")

        with pytest.raises(NotFoundException):
            await _erase_trip(trip_route, move_dives_to=uuid7())

        trip_route["resolve"].assert_not_awaited()


class TestEraseDiveSiteWithoutTheParameter:
    @pytest.mark.asyncio
    async def test_nothing_is_reassigned(self, dive_site_route: dict[str, Any]) -> None:
        await _erase_dive_site(dive_site_route)

        dive_site_route["replace"].assert_not_awaited()
        dive_site_route["delete"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_response_is_the_bare_message(self, dive_site_route: dict[str, Any]) -> None:
        assert await _erase_dive_site(dive_site_route) == {"message": "Dive site deleted"}

    @pytest.mark.asyncio
    async def test_the_dive_caches_are_dropped_anyway(self, dive_site_route: dict[str, Any]) -> None:
        """The cascade removes this site from every dive logged at it, so a bare delete
        changes what all of those cached reads should say. Making this conditional on
        whether anything moved would leave them holding a site that no longer exists. The
        trip route once did exactly that, back when a soft-deleted trip changed nothing
        about a dive read; the two match now."""
        await _erase_dive_site(dive_site_route)

        dive_site_route["invalidate_dives"].assert_awaited_once_with(USER_ID)


class TestEraseDiveSiteWithAReplacement:
    @pytest.mark.asyncio
    async def test_the_dives_move_before_the_delete_commits_them(self, dive_site_route: dict[str, Any]) -> None:
        await _erase_dive_site(dive_site_route, move_dives_to=dive_site_route["replacement_uuid"])

        assert dive_site_route["calls"] == ["replace", "delete"]

    @pytest.mark.asyncio
    async def test_the_move_is_scoped_to_the_owner_and_the_two_sites(self, dive_site_route: dict[str, Any]) -> None:
        await _erase_dive_site(dive_site_route, move_dives_to=dive_site_route["replacement_uuid"])

        kwargs = dive_site_route["replace"].await_args.kwargs
        assert (kwargs["user_id"], kwargs["from_dive_site_id"], kwargs["to_dive_site_id"]) == (USER_ID, 21, 99)

    @pytest.mark.asyncio
    async def test_the_response_says_nothing_about_what_moved(self, dive_site_route: dict[str, Any]) -> None:
        assert await _erase_dive_site(dive_site_route, move_dives_to=dive_site_route["replacement_uuid"]) == {
            "message": "Dive site deleted"
        }

    @pytest.mark.asyncio
    async def test_a_replacement_that_is_not_the_callers_is_a_422(self, dive_site_route: dict[str, Any]) -> None:
        dive_site_route["resolve"].return_value = None

        with pytest.raises(UnprocessableEntityException):
            await _erase_dive_site(dive_site_route, move_dives_to=uuid7())

        dive_site_route["delete"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_site_cannot_be_moved_onto_itself(self, dive_site_route: dict[str, Any]) -> None:
        with pytest.raises(UnprocessableEntityException):
            await _erase_dive_site(dive_site_route, move_dives_to=dive_site_route["uuid"])

    @pytest.mark.asyncio
    async def test_a_second_delete_moves_nothing(self, dive_site_route: dict[str, Any]) -> None:
        """The mirror of the trip case: idempotency is gone, so a repeat call 404s before
        it can re-point anything at a site id that no longer names a row."""
        dive_site_route["owned"].side_effect = NotFoundException("Dive site not found")

        with pytest.raises(NotFoundException):
            await _erase_dive_site(dive_site_route, move_dives_to=dive_site_route["replacement_uuid"])

        dive_site_route["replace"].assert_not_awaited()
        dive_site_route["delete"].assert_not_awaited()


def _trip_ids(db: Session, *dives: Dive) -> list[int | None]:
    """Each dive's stored `trip_id`, in the order given.

    A column select rather than `db.query(Dive)`: the seeded `Dive` instances are already
    in this session's identity map, and an entity query hands the cached objects back
    without re-reading the columns - so a dive the async session moved reads back as
    wherever it was when this session last saw it.
    """
    rows = db.execute(select(Dive.id, Dive.trip_id).where(Dive.id.in_([dive.id for dive in dives]))).all()
    by_id = {row.id: row.trip_id for row in rows}
    return [by_id[dive.id] for dive in dives]


async def _sites_of(async_db: AsyncSession, dive: Dive) -> list[tuple[int, int]]:
    """A dive's `(dive_site_id, position)` rows, in stored order."""
    rows = await async_db.execute(
        select(DiveDiveSite.dive_site_id, DiveDiveSite.position)
        .where(DiveDiveSite.dive_id == dive.id)
        .order_by(DiveDiveSite.position)
    )
    return [(site_id, position) for site_id, position in rows]


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestReassignDivesToTrip:
    """`reassign_dives_to_trip` against a live Postgres - what the `dive` table holds
    afterwards, and, as much to the point, what it does *not*."""

    @pytest.mark.asyncio
    async def test_every_dive_on_the_trip_moves(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        doomed, replacement = create_trip(db, diver), create_trip(db, diver)
        first, second = create_dive(db, diver, trip=doomed), create_dive(db, diver, trip=doomed)

        moved = await reassign_dives_to_trip(
            async_db, user_id=diver.id, from_trip_id=doomed.id, to_trip_id=replacement.id
        )
        await async_db.commit()

        assert moved == 2
        assert _trip_ids(db, first, second) == [replacement.id, replacement.id]

    @pytest.mark.asyncio
    async def test_dives_on_other_trips_are_left_alone(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        doomed, replacement, elsewhere = create_trip(db, diver), create_trip(db, diver), create_trip(db, diver)
        moving, staying = create_dive(db, diver, trip=doomed), create_dive(db, diver, trip=elsewhere)
        unattached = create_dive(db, diver)

        moved = await reassign_dives_to_trip(
            async_db, user_id=diver.id, from_trip_id=doomed.id, to_trip_id=replacement.id
        )
        await async_db.commit()

        assert moved == 1
        assert _trip_ids(db, moving, staying, unattached) == [replacement.id, elsewhere.id, None]

    @pytest.mark.asyncio
    async def test_another_divers_dives_are_never_touched(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        """The trip id alone would be enough here, since a trip belongs to one diver. The
        `user_id` condition is what keeps that true after a mis-resolved id, and this is
        the only test that would notice it being dropped."""
        doomed, replacement = create_trip(db, diver), create_trip(db, diver)
        theirs = create_trip(db, other_diver)
        mine, not_mine = create_dive(db, diver, trip=doomed), create_dive(db, other_diver, trip=theirs)

        await reassign_dives_to_trip(async_db, user_id=diver.id, from_trip_id=theirs.id, to_trip_id=replacement.id)
        await async_db.commit()

        assert _trip_ids(db, mine, not_mine) == [doomed.id, theirs.id]

    @pytest.mark.asyncio
    async def test_soft_deleted_dives_stay_behind(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """They are outside everything the diver can see, and leaving them preserves the
        pairing they were logged with - which is what a plain delete does to every dive.
        The count has to match too, or the toast contradicts the number the confirmation
        dialog got from `GET /dives?trip_uuid=...`."""
        doomed, replacement = create_trip(db, diver), create_trip(db, diver)
        live = create_dive(db, diver, trip=doomed)
        erased = create_dive(db, diver, trip=doomed, is_deleted=True)

        moved = await reassign_dives_to_trip(
            async_db, user_id=diver.id, from_trip_id=doomed.id, to_trip_id=replacement.id
        )
        await async_db.commit()

        assert moved == 1
        assert _trip_ids(db, live, erased) == [replacement.id, doomed.id]

    @pytest.mark.asyncio
    async def test_a_moved_dive_is_stamped_as_updated(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """`TimestampMixin` gives `updated_at` no `onupdate`, so a bulk `UPDATE` that
        forgets it leaves the row claiming it has not changed since it was written. The
        same edit through `PATCH /dive` does stamp it, and these have to agree."""
        doomed, replacement = create_trip(db, diver), create_trip(db, diver)
        dive = create_dive(db, diver, trip=doomed)
        assert db.execute(select(Dive.updated_at).where(Dive.id == dive.id)).scalar_one() is None

        await reassign_dives_to_trip(async_db, user_id=diver.id, from_trip_id=doomed.id, to_trip_id=replacement.id)
        await async_db.commit()

        assert db.execute(select(Dive.updated_at).where(Dive.id == dive.id)).scalar_one() is not None

    @pytest.mark.asyncio
    async def test_an_empty_trip_moves_nothing(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        doomed, replacement = create_trip(db, diver), create_trip(db, diver)

        moved = await reassign_dives_to_trip(
            async_db, user_id=diver.id, from_trip_id=doomed.id, to_trip_id=replacement.id
        )

        assert moved == 0


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestReplaceDiveSiteOnDives:
    """`replace_dive_site_on_dives` against a live Postgres.

    Three set-based statements standing in for a read-modify-write per dive, and every
    rule they have to preserve lives in the rows rather than in the calls: the slot the
    replacement lands in, the unique constraint a dive logged at both sites would
    otherwise violate, and the contiguous numbering every other writer leaves behind.
    """

    @pytest.mark.asyncio
    async def test_the_replacement_takes_the_doomed_sites_slot(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """Position 0 is the primary site - the one a dive list shows as "Site Name +2" -
        so replacing the primary has to leave the replacement primary, not append it."""
        doomed, replacement, other = (
            create_dive_site(db, diver),
            create_dive_site(db, diver),
            create_dive_site(db, diver),
        )
        dive = create_dive(db, diver)
        await replace_dive_sites_for_dive(async_db, dive_id=dive.id, dive_site_ids=[doomed.id, other.id])

        moved = await replace_dive_site_on_dives(
            async_db, user_id=diver.id, from_dive_site_id=doomed.id, to_dive_site_id=replacement.id
        )
        await async_db.commit()

        assert moved == 1
        assert await _sites_of(async_db, dive) == [(replacement.id, 0), (other.id, 1)]

    @pytest.mark.asyncio
    async def test_a_dive_at_both_sites_ends_up_holding_one(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """`ux_dive_dive_site_dive_id_dive_site_id` would turn a blind `UPDATE` into a 500
        here. The survivor keeps the earlier of the two slots, which is the doomed site's
        - the same "first occurrence wins" rule `replace_dive_sites_for_dive` applies to a
        hand-edited list."""
        doomed, replacement = create_dive_site(db, diver), create_dive_site(db, diver)
        dive = create_dive(db, diver)
        await replace_dive_sites_for_dive(async_db, dive_id=dive.id, dive_site_ids=[doomed.id, replacement.id])

        moved = await replace_dive_site_on_dives(
            async_db, user_id=diver.id, from_dive_site_id=doomed.id, to_dive_site_id=replacement.id
        )
        await async_db.commit()

        assert moved == 1
        assert await _sites_of(async_db, dive) == [(replacement.id, 0)]

    @pytest.mark.asyncio
    async def test_a_dive_already_primarily_at_the_replacement_keeps_it_primary(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The mirror of the case above: the replacement was already the earlier of the
        two, so it is the doomed site's row that goes. It still counts as a dive that
        moved - it referenced the site being deleted, and now does not."""
        doomed, replacement = create_dive_site(db, diver), create_dive_site(db, diver)
        dive = create_dive(db, diver)
        await replace_dive_sites_for_dive(async_db, dive_id=dive.id, dive_site_ids=[replacement.id, doomed.id])

        moved = await replace_dive_site_on_dives(
            async_db, user_id=diver.id, from_dive_site_id=doomed.id, to_dive_site_id=replacement.id
        )
        await async_db.commit()

        assert moved == 1
        assert await _sites_of(async_db, dive) == [(replacement.id, 0)]

    @pytest.mark.asyncio
    async def test_the_positions_left_behind_are_contiguous(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """Dropping the duplicate out of the middle of a three-site drift dive leaves a
        hole at position 1 unless something renumbers. Nothing reads `position` as more
        than a sort key today, so this is about not having one writer that disagrees with
        every other about what the column means."""
        doomed, replacement, last = (
            create_dive_site(db, diver),
            create_dive_site(db, diver),
            create_dive_site(db, diver),
        )
        dive = create_dive(db, diver)
        await replace_dive_sites_for_dive(async_db, dive_id=dive.id, dive_site_ids=[doomed.id, replacement.id, last.id])

        await replace_dive_site_on_dives(
            async_db, user_id=diver.id, from_dive_site_id=doomed.id, to_dive_site_id=replacement.id
        )
        await async_db.commit()

        assert await _sites_of(async_db, dive) == [(replacement.id, 0), (last.id, 1)]

    @pytest.mark.asyncio
    async def test_every_dive_at_the_site_moves(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        doomed, replacement = create_dive_site(db, diver), create_dive_site(db, diver)
        first, second = create_dive(db, diver), create_dive(db, diver)
        for dive in (first, second):
            await replace_dive_sites_for_dive(async_db, dive_id=dive.id, dive_site_ids=[doomed.id])

        moved = await replace_dive_site_on_dives(
            async_db, user_id=diver.id, from_dive_site_id=doomed.id, to_dive_site_id=replacement.id
        )
        await async_db.commit()

        assert moved == 2
        assert await _sites_of(async_db, first) == [(replacement.id, 0)]
        assert await _sites_of(async_db, second) == [(replacement.id, 0)]

    @pytest.mark.asyncio
    async def test_dives_logged_elsewhere_are_left_alone(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        doomed, replacement, elsewhere = (
            create_dive_site(db, diver),
            create_dive_site(db, diver),
            create_dive_site(db, diver),
        )
        moving, staying = create_dive(db, diver), create_dive(db, diver)
        await replace_dive_sites_for_dive(async_db, dive_id=moving.id, dive_site_ids=[doomed.id])
        await replace_dive_sites_for_dive(async_db, dive_id=staying.id, dive_site_ids=[elsewhere.id])

        moved = await replace_dive_site_on_dives(
            async_db, user_id=diver.id, from_dive_site_id=doomed.id, to_dive_site_id=replacement.id
        )
        await async_db.commit()

        assert moved == 1
        assert await _sites_of(async_db, staying) == [(elsewhere.id, 0)]

    @pytest.mark.asyncio
    async def test_another_divers_dives_are_never_touched(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        """Unlike the trip case, a site id is not enough on its own to notice: the join
        table has no `user_id`, so without the subquery this would rewrite whatever rows
        happened to reference the id."""
        replacement = create_dive_site(db, diver)
        theirs = create_dive_site(db, other_diver)
        not_mine = create_dive(db, other_diver)
        await replace_dive_sites_for_dive(async_db, dive_id=not_mine.id, dive_site_ids=[theirs.id])

        moved = await replace_dive_site_on_dives(
            async_db, user_id=diver.id, from_dive_site_id=theirs.id, to_dive_site_id=replacement.id
        )
        await async_db.commit()

        assert moved == 0
        assert await _sites_of(async_db, not_mine) == [(theirs.id, 0)]

    @pytest.mark.asyncio
    async def test_soft_deleted_dives_stay_behind(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        doomed, replacement = create_dive_site(db, diver), create_dive_site(db, diver)
        live, erased = create_dive(db, diver), create_dive(db, diver, is_deleted=True)
        for dive in (live, erased):
            await replace_dive_sites_for_dive(async_db, dive_id=dive.id, dive_site_ids=[doomed.id])

        moved = await replace_dive_site_on_dives(
            async_db, user_id=diver.id, from_dive_site_id=doomed.id, to_dive_site_id=replacement.id
        )
        await async_db.commit()

        assert moved == 1
        assert await _sites_of(async_db, erased) == [(doomed.id, 0)]

    @pytest.mark.asyncio
    async def test_a_site_moved_onto_itself_destroys_nothing(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """`erase_dive_site` refuses this with a 422, so it is unreachable through the API
        - but the refusal is in another module, and without the `doomed.id !=
        already_there.id` join condition every row would match itself, the `DELETE` would
        strip the site off every dive in the log and the `UPDATE` would match nothing. A
        silent wipe reported as a plausible count is the worst failure this function has,
        so it is pinned here rather than left to the caller."""
        site, other = create_dive_site(db, diver), create_dive_site(db, diver)
        dive = create_dive(db, diver)
        await replace_dive_sites_for_dive(async_db, dive_id=dive.id, dive_site_ids=[site.id, other.id])

        await replace_dive_site_on_dives(async_db, user_id=diver.id, from_dive_site_id=site.id, to_dive_site_id=site.id)
        await async_db.commit()

        assert await _sites_of(async_db, dive) == [(site.id, 0), (other.id, 1)]

    @pytest.mark.asyncio
    async def test_a_site_nothing_was_logged_at_moves_nothing(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        doomed, replacement = create_dive_site(db, diver), create_dive_site(db, diver)

        moved = await replace_dive_site_on_dives(
            async_db, user_id=diver.id, from_dive_site_id=doomed.id, to_dive_site_id=replacement.id
        )

        assert moved == 0
