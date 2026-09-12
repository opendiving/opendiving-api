"""Tests for `GET /dive/{uuid}/neighbors` - the service that answers it
(`services/dive_neighbors.py`) and the route that guards it
(`api/v1/dives.py::read_dive_neighbors`).

The service half runs against a live Postgres, like `test_dive_numbering.py` and for the
same reason: everything it does is a comparison the database evaluates - a row comparison
over `(start_time, id)`, a `LIMIT 1` off a partial index - and mocking the session would
only assert that we built the query we built. In particular the tie-break, which is the
one behaviour here that can silently produce a loop in a client walking the chain, is a
statement about what Postgres returns when two dives share a start time.

Automatically skipped when no database is reachable (e.g. running `pytest` outside the
project's docker compose setup).

The route half needs no database: what it has to get right is the *order* of the
ownership check and the cache, which is a question about the call, not about the data.
"""

import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1 import dives as dives_module
from src.app.core.exceptions.http_exceptions import NotFoundException
from src.app.core.utils import cache as cache_module
from src.app.core.utils.cache import across_builds, namespaced
from src.app.models.dive import Dive
from src.app.models.user import User
from src.app.schemas.dive import DiveNeighbors
from src.app.services.dive_neighbors import find_dive_neighbors
from tests.conftest import db_available
from tests.helpers.generators import create_dive_log, log_day


async def _neighbors_of(async_db: AsyncSession, dive: Dive) -> Any:
    return await find_dive_neighbors(async_db, user_id=dive.user_id, start_time=dive.start_time, dive_id=dive.id)


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestFindDiveNeighbors:
    @pytest.mark.asyncio
    async def test_a_dive_in_the_middle_has_both(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        first, middle, last = create_dive_log(db, diver, (1, 0), (2, 1), (3, 2))

        neighbors = await _neighbors_of(async_db, middle)

        assert neighbors.previous is not None
        assert neighbors.next is not None
        assert neighbors.previous.uuid == first.uuid
        assert neighbors.next.uuid == last.uuid

    @pytest.mark.asyncio
    async def test_next_means_later_in_time(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """The direction that reverses between this endpoint and `GET /dives`: the list is
        newest first, so a dive's `next` is the row *above* it there."""
        _, middle, last = create_dive_log(db, diver, (1, 0), (2, 1), (3, 2))

        neighbors = await _neighbors_of(async_db, middle)

        assert neighbors.next is not None
        assert neighbors.next.start_time > middle.start_time.replace(tzinfo=UTC)
        assert neighbors.next.uuid == last.uuid

    @pytest.mark.asyncio
    async def test_the_oldest_dive_has_no_previous(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        first, second = create_dive_log(db, diver, (1, 0), (2, 1))

        neighbors = await _neighbors_of(async_db, first)

        assert neighbors.previous is None
        assert neighbors.next is not None
        assert neighbors.next.uuid == second.uuid

    @pytest.mark.asyncio
    async def test_the_newest_dive_has_no_next(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        first, second = create_dive_log(db, diver, (1, 0), (2, 1))

        neighbors = await _neighbors_of(async_db, second)

        assert neighbors.next is None
        assert neighbors.previous is not None
        assert neighbors.previous.uuid == first.uuid

    @pytest.mark.asyncio
    async def test_a_log_of_one_has_neither(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        (only,) = create_dive_log(db, diver, (1, 0))

        neighbors = await _neighbors_of(async_db, only)

        assert neighbors.previous is None
        assert neighbors.next is None

    @pytest.mark.asyncio
    async def test_carries_enough_to_label_the_link(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        first, second = create_dive_log(db, diver, (47, 0), (48, 1))

        neighbors = await _neighbors_of(async_db, second)

        assert neighbors.previous is not None
        assert neighbors.previous.dive_number == 47
        assert neighbors.previous.start_time == first.start_time.replace(tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_the_neighbour_keeps_its_own_utc_offset(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """A prev/next label reads in the timezone *that* dive was logged in, not this
        one's - the same rule `DiveRead` follows."""
        create_dive_log(db, diver, (1, 0), utc_offset_minutes=420)
        (second,) = create_dive_log(db, diver, (2, 1))

        neighbors = await _neighbors_of(async_db, second)

        assert neighbors.previous is not None
        assert neighbors.previous.start_time.utcoffset() == timedelta(hours=7)
        # Same instant either way - only how it is expressed changes.
        assert neighbors.previous.start_time == log_day(0)

    @pytest.mark.asyncio
    async def test_chronology_is_start_time_not_dive_number(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """`dive_number` is a label nothing orders by (see `services/dive_numbering.py`),
        so a log numbered out of date order still walks in date order."""
        _, middle, later = create_dive_log(db, diver, (9, 0), (4, 1), (5, 2))

        neighbors = await _neighbors_of(async_db, middle)

        assert neighbors.next is not None
        assert neighbors.next.uuid == later.uuid
        assert neighbors.next.dive_number == 5

    @pytest.mark.asyncio
    async def test_another_divers_dives_are_never_neighbours(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        create_dive_log(db, other_diver, (400, 0), (401, 2))
        (mine,) = create_dive_log(db, diver, (1, 1))

        neighbors = await _neighbors_of(async_db, mine)

        assert neighbors.previous is None
        assert neighbors.next is None

    @pytest.mark.asyncio
    async def test_deleted_dives_are_skipped(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        first, _ = create_dive_log(db, diver, (1, 0), (3, 3))
        create_dive_log(db, diver, (2, 1), is_deleted=True, deleted_at=datetime.now(UTC))

        neighbors = await _neighbors_of(async_db, first)

        assert neighbors.next is not None
        assert neighbors.next.dive_number == 3


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestSharedStartTimes:
    """Two dives can carry the same `start_time` - a computer that records to the minute,
    or a repetitive dive entered twice by hand - and `id` is what breaks the tie.

    Without it both queries would answer about a partial order, and the two failures below
    are the ones that follow: a dive that is its own neighbour, and a chain that never
    reaches the end of the log.
    """

    @pytest.mark.asyncio
    async def test_a_tied_dive_is_never_its_own_neighbour(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        _, tied, _ = create_dive_log(db, diver, (1, 0), (2, 0), (3, 0))

        neighbors = await _neighbors_of(async_db, tied)

        assert neighbors.previous is not None
        assert neighbors.next is not None
        assert neighbors.previous.uuid != tied.uuid
        assert neighbors.next.uuid != tied.uuid

    @pytest.mark.asyncio
    async def test_tied_dives_are_ordered_by_id(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        first, tied, last = create_dive_log(db, diver, (1, 0), (2, 0), (3, 0))

        neighbors = await _neighbors_of(async_db, tied)

        assert neighbors.previous is not None
        assert neighbors.next is not None
        assert neighbors.previous.uuid == first.uuid
        assert neighbors.next.uuid == last.uuid

    @pytest.mark.asyncio
    async def test_walking_forwards_through_a_tie_terminates(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """What a client actually does with this endpoint. A `>=` comparison would hand
        back the same dive forever; the tie-break is what makes the walk finite."""
        dives = create_dive_log(db, diver, (1, 0), (2, 0), (3, 0), (4, 1))
        by_uuid = {dive.uuid: dive for dive in dives}

        visited: list[uuid_pkg.UUID] = []
        current: Dive | None = dives[0]
        while current is not None and len(visited) <= len(dives):
            visited.append(current.uuid)
            neighbors = await _neighbors_of(async_db, current)
            current = by_uuid[neighbors.next.uuid] if neighbors.next is not None else None

        assert visited == [dive.uuid for dive in dives]

    @pytest.mark.asyncio
    async def test_the_ends_of_a_wholly_tied_log_are_still_ends(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        first, _, last = create_dive_log(db, diver, (1, 0), (2, 0), (3, 0))

        assert (await _neighbors_of(async_db, first)).previous is None
        assert (await _neighbors_of(async_db, last)).next is None


class TestReadDiveNeighborsRoute:
    """The route's own job: establish ownership *before* reaching the cached helper.

    `@cache` serves a hit without running the function it wraps, so an ownership check
    that happened inside it would not run on a hit - an IDOR, not a slow path (see
    `api/dependencies.py::fetch_owned_or_raise`).
    """

    @staticmethod
    def _dive(dive_id: int = 11) -> MagicMock:
        dive = MagicMock()
        dive.id = dive_id
        dive.user_id = 1
        dive.start_time = log_day(2)
        return dive

    @pytest.mark.asyncio
    async def test_passes_the_owned_dives_position(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The uuid keys the cache; the query runs off the `(start_time, id)` the
        ownership lookup already resolved, so it never re-reads the row."""
        dive = self._dive()
        cached: Any = AsyncMock(return_value=MagicMock())
        monkeypatch.setattr(dives_module, "_get_owned_dive", AsyncMock(return_value=dive))
        monkeypatch.setattr(dives_module, "_cached_read_dive_neighbors", cached)
        uuid = uuid7()

        await dives_module.read_dive_neighbors(
            request=MagicMock(), uuid=uuid, current_user={"id": 1, "uuid": uuid7()}, db=MagicMock()
        )

        kwargs = cached.await_args.kwargs
        assert kwargs["user_id"] == 1
        assert kwargs["uuid"] == uuid
        assert kwargs["start_time"] == dive.start_time
        assert kwargs["dive_id"] == 11

    @pytest.mark.asyncio
    async def test_an_unowned_dive_never_reaches_the_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cached = AsyncMock()
        monkeypatch.setattr(dives_module, "_get_owned_dive", AsyncMock(side_effect=NotFoundException("Dive not found")))
        monkeypatch.setattr(dives_module, "_cached_read_dive_neighbors", cached)

        with pytest.raises(NotFoundException):
            await dives_module.read_dive_neighbors(
                request=MagicMock(), uuid=uuid7(), current_user={"id": 1, "uuid": uuid7()}, db=MagicMock()
            )

        cached.assert_not_awaited()


class TestCacheKey:
    """The key sits under the *list* prefix (`user_{id}_dives:`), not the single-dive one.

    That is what makes `invalidate_dive_caches()` cover it without a change: it already
    sweeps `user_{id}_dives:*` after every dive create, update and delete, which is
    exactly the set of events that can move a neighbour. Under `user_{id}_dive:` it would
    be swept too - but by the pattern that means "this dive changed", which is the wrong
    reason and the wrong set.
    """

    @pytest.mark.asyncio
    async def test_the_key_matches_what_dive_writes_sweep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dives_module, "find_dive_neighbors", AsyncMock(return_value=DiveNeighbors()))
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock(return_value=True)
        redis.expire = AsyncMock(return_value=True)
        request = MagicMock()
        request.method = "GET"
        uuid = uuid7()

        with patch.object(cache_module, "client", redis):
            await dives_module._cached_read_dive_neighbors(
                request, user_id=7, uuid=uuid, start_time=log_day(0), dive_id=11, db=MagicMock()
            )

        key = redis.set.call_args[0][0]
        assert key == namespaced(f"user_7_dives:neighbors:{uuid}")
        # The pattern `invalidate_dive_caches(7)` deletes, widened to every build's
        # namespace the way `_delete_keys_by_pattern` widens it. Redis glob, `fnmatch` here.
        assert fnmatch(key, across_builds("user_7_dives:*"))
