"""Integration tests for `services/export/loader.py`, against a live Postgres.

Everything else about the export is pure and tested without a database. This is the one
part that is a query, and it is the part where getting it wrong is worst: `load_export_bundle`
decides *whose* data ends up in a file the caller downloads. A missing `user_id` filter
here would be a cross-account data leak that no writer test could catch, because every
writer faithfully renders whatever bundle it is handed.

So the two assertions that matter are the boring ones - only the caller's rows, and
nothing soft-deleted - and they are asserted per table rather than once for dives, since
each table applies them itself. The second only has three tables left to be wrong about:
`Dive`, `GearServiceRecord` and `Certification` still soft-delete, and the other six are
hard-deleted, so `_owned` skips a filter it cannot express rather than one it forgot.

Like `test_dive_check_constraints.py`, these are skipped when no database is reachable.
On a developer's machine that means `POSTGRES_SERVER=localhost` (`src/.env` points at the
compose hostname, which does not resolve on the host); CI sets it and fails the job if
anything skips. See CONTRIBUTING.md.
"""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from src.app.core.db.database import async_engine, local_session
from src.app.models.certification import Certification
from src.app.models.course import Course
from src.app.models.dive import Dive
from src.app.models.dive_dive_site import DiveDiveSite
from src.app.models.dive_gear_item import DiveGearItem
from src.app.models.dive_mixture import DiveMixture
from src.app.models.dive_site import DiveSite
from src.app.models.gear_item import GearItem
from src.app.models.gear_service_record import GearServiceRecord
from src.app.models.gear_service_schedule import GearServiceSchedule
from src.app.models.gear_set import GearSet
from src.app.models.gear_set_item import GearSetItem
from src.app.models.trip import Trip
from src.app.models.trip_location import TripLocation
from src.app.models.user import User
from src.app.services.export.loader import ExportBundle, load_export_bundle
from tests.conftest import db_available
from tests.helpers.generators import create_user

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")


@pytest.fixture
def owner(db: Session) -> User:
    return create_user(db)


@pytest.fixture
def stranger(db: Session) -> User:
    return create_user(db)


def _dive(db: Session, user: User, *, number: int, deleted: bool = False) -> Dive:
    dive = Dive(
        user_id=user.id,
        dive_number=number,
        start_time=datetime(2026, 6, number, 6, 15, tzinfo=UTC),
        utc_offset_minutes=120,
        duration=1800,
        notes="",
        is_deleted=deleted,
    )
    db.add(dive)
    db.commit()
    db.refresh(dive)
    return dive


async def _load(user_id: int) -> ExportBundle:
    """Run `load_export_bundle` against its own async session.

    The fixtures above write through the suite's *sync* session (`conftest.py`'s `db`),
    which is what every database-backed test here uses; the loader is async. The rows are
    committed by the time this runs, so the two see the same data.

    The engine is disposed on **both** sides of the call, because `pytest-asyncio` gives
    each test its own event loop and a pooled asyncpg connection is bound to the loop that
    opened it - reusing one across loops fails with "another operation is in progress" or
    "attached to a different loop", either of which reads like a bug in the code under
    test. Disposing afterwards protects the next test; disposing first protects this one,
    and it is needed because this test is not the only thing that puts connections in that
    pool. The session-scoped `client` fixture enters the app's real lifespan on
    `TestClient`'s own portal loop, and the lifespan opens the shared engine for its
    startup work - so by the time any test here runs, the pool may already hold a
    connection belonging to a loop that is not this one.
    """
    await async_engine.dispose()
    try:
        async with local_session() as session:
            return await load_export_bundle(session, user_id=user_id)
    finally:
        await async_engine.dispose()


class TestScoping:
    @pytest.mark.asyncio
    async def test_it_loads_only_the_callers_dives(self, db: Session, owner: User, stranger: User):
        mine = _dive(db, owner, number=1)
        _dive(db, stranger, number=1)

        bundle = await _load(owner.id)
        assert [dive.id for dive in bundle.dives] == [mine.id]

    @pytest.mark.asyncio
    async def test_soft_deleted_dives_are_excluded(self, db: Session, owner: User):
        live = _dive(db, owner, number=1)
        _dive(db, owner, number=2, deleted=True)

        bundle = await _load(owner.id)
        assert [dive.id for dive in bundle.dives] == [live.id]

    @pytest.mark.asyncio
    async def test_every_other_table_is_scoped_to_the_caller_too(self, db: Session, owner: User, stranger: User):
        """Each of these applies the `user_id` scope itself, so each is its own chance to
        leave it off."""
        db.add_all(
            [
                DiveSite(user_id=owner.id, name="Mine", notes=""),
                DiveSite(user_id=stranger.id, name="Theirs", notes=""),
                Trip(user_id=owner.id, name="Mine", start_date=date(2026, 6, 1), notes=""),
                Trip(user_id=stranger.id, name="Theirs", start_date=date(2026, 6, 1), notes=""),
                Course(user_id=owner.id, name="Mine", agency="tdi", status="completed", notes=""),
                Course(user_id=stranger.id, name="Theirs", agency="tdi", status="completed", notes=""),
                GearItem(user_id=owner.id, name="Mine", notes=""),
                GearItem(user_id=stranger.id, name="Theirs", notes=""),
                Certification(user_id=owner.id, agency="padi", name="Mine", notes=""),
                Certification(user_id=stranger.id, agency="padi", name="Theirs", notes=""),
            ]
        )
        db.commit()

        bundle = await _load(owner.id)
        assert [site.name for site in bundle.dive_sites] == ["Mine"]
        assert [trip.name for trip in bundle.trips] == ["Mine"]
        assert [course.name for course in bundle.courses] == ["Mine"]
        assert [item.name for item in bundle.gear_items] == ["Mine"]
        assert [cert.name for cert in bundle.certifications] == ["Mine"]

    @pytest.mark.asyncio
    async def test_the_three_tables_that_still_soft_delete_are_filtered(self, db: Session, owner: User):
        """`_owned` branches on whether the model carries the column, so a table gaining
        one later is filtered by default. These three are what that branch is for; `Dive`
        has its own test above, and this pins the two that would otherwise be checked
        nowhere."""
        item = GearItem(user_id=owner.id, name="Reg", notes="")
        db.add_all([item, Certification(user_id=owner.id, agency="padi", name="Gone", notes="", is_deleted=True)])
        db.commit()
        db.add(
            GearServiceRecord(
                user_id=owner.id,
                gear_item_id=item.id,
                kind="service",
                serviced_on=date(2026, 1, 1),
                dive_count_at_service=0,
                is_deleted=True,
            )
        )
        db.commit()

        bundle = await _load(owner.id)
        assert bundle.certifications == []
        assert bundle.service_records == []

    @pytest.mark.asyncio
    async def test_an_account_with_nothing_in_it_loads_an_empty_bundle(self, owner: User):
        bundle = await _load(owner.id)
        assert bundle.dives == []
        assert bundle.user.id == owner.id

    @pytest.mark.asyncio
    async def test_a_missing_user_fails_loudly(self):
        with pytest.raises(LookupError):
            await _load(-1)


class TestTheCascadeLeavesNothingDangling:
    """What replaced the resurrection.

    `_owned` used to take a `still_referenced` set and read deleted-but-referenced rows
    back, because `erase_dive_site` left the site on the dives logged at it,
    `erase_gear_item` left the item on its dives and sets, `erase_trip` left the trip on
    its dives, and a service record went on naming a deleted schedule. Without that,
    `sites_for`/`gear_for` came back a `KeyError` - a 500 on all four export endpoints for
    any diver who had ever deleted a site - and `logbook.divejson` carried uuids nothing in
    the file defined, with UDDF's `xs:IDREF` version of the same reference refusing to validate.

    All five of those tables are hard-deleted now, so the join row goes with the row it
    points at and a dangling reference cannot be created to be repaired. These pin that,
    since it is the premise the whole simplification rests on.
    """

    @pytest.mark.asyncio
    async def test_deleting_a_site_takes_its_dive_links_with_it(self, db: Session, owner: User):
        site = DiveSite(user_id=owner.id, name="Gone", notes="")
        db.add(site)
        db.commit()
        dive = _dive(db, owner, number=1)
        db.add(DiveDiveSite(dive_id=dive.id, dive_site_id=site.id, position=0))
        db.commit()

        db.delete(site)
        db.commit()

        bundle = await _load(owner.id)
        assert bundle.dive_sites == []
        assert bundle.site_ids_by_dive[bundle.dives[0].id] == []
        assert bundle.sites_for(bundle.dives[0]) == []

    @pytest.mark.asyncio
    async def test_deleting_a_gear_item_takes_its_dive_and_set_links_with_it(self, db: Session, owner: User):
        item = GearItem(user_id=owner.id, name="Gone", notes="")
        gear_set = GearSet(user_id=owner.id, name="Tech")
        db.add_all([item, gear_set])
        db.commit()
        dive = _dive(db, owner, number=1)
        db.add_all(
            [
                DiveGearItem(dive_id=dive.id, gear_item_id=item.id, position=0),
                GearSetItem(gear_set_id=gear_set.id, gear_item_id=item.id, position=0),
            ]
        )
        db.commit()

        db.delete(item)
        db.commit()

        bundle = await _load(owner.id)
        assert bundle.gear_items == []
        assert bundle.gear_for(bundle.dives[0]) == []
        assert bundle.item_ids_by_set[gear_set.id] == []

    @pytest.mark.asyncio
    async def test_deleting_a_trip_leaves_its_dives_pointing_at_nothing(self, db: Session, owner: User):
        """`SET NULL`, not `CASCADE` - the dive is the irreplaceable record and survives
        losing its trip. `trip_for` then answers `None` off the nulled column rather than
        off a lookup miss."""
        trip = Trip(user_id=owner.id, name="Gone", start_date=date(2026, 6, 1), notes="")
        db.add(trip)
        db.commit()
        dive = _dive(db, owner, number=1)
        dive.trip_id = trip.id
        db.commit()

        db.delete(trip)
        db.commit()

        bundle = await _load(owner.id)
        assert bundle.trips == []
        assert len(bundle.dives) == 1
        assert bundle.dives[0].trip_id is None
        assert bundle.trip_for(bundle.dives[0]) is None

    @pytest.mark.asyncio
    async def test_deleting_a_schedule_leaves_its_records_unlinked(self, db: Session, owner: User):
        """The one `SET NULL` on the gear side, and the reason the schedule half of this
        change is safe: deleting a reminder must never throw away the receipts."""
        item = GearItem(user_id=owner.id, name="Reg", notes="")
        db.add(item)
        db.commit()
        schedule = GearServiceSchedule(
            user_id=owner.id, gear_item_id=item.id, kind="service", starts_on=date(2026, 1, 1), interval_months=12
        )
        db.add(schedule)
        db.commit()
        record = GearServiceRecord(
            user_id=owner.id,
            gear_item_id=item.id,
            kind="service",
            serviced_on=date(2026, 1, 1),
            dive_count_at_service=0,
            gear_service_schedule_id=schedule.id,
        )
        db.add(record)
        db.commit()

        db.delete(schedule)
        db.commit()

        bundle = await _load(owner.id)
        assert bundle.schedules == []
        assert [r.id for r in bundle.service_records] == [record.id]
        assert bundle.service_records[0].gear_service_schedule_id is None

    @pytest.mark.asyncio
    async def test_deleting_a_gear_item_takes_its_schedules_and_records_with_it(self, db: Session, owner: User):
        """The one deletion a diver can notice as a loss, and the delete dialog already
        promises it - archiving is the non-destructive path that keeps a service history."""
        item = GearItem(user_id=owner.id, name="Retired reg", notes="")
        db.add(item)
        db.commit()
        schedule = GearServiceSchedule(
            user_id=owner.id, gear_item_id=item.id, kind="service", starts_on=date(2026, 1, 1), interval_months=12
        )
        db.add(schedule)
        db.commit()
        db.add(
            GearServiceRecord(
                user_id=owner.id,
                gear_item_id=item.id,
                kind="service",
                serviced_on=date(2026, 1, 1),
                dive_count_at_service=0,
                gear_service_schedule_id=schedule.id,
            )
        )
        db.commit()

        db.delete(item)
        db.commit()

        bundle = await _load(owner.id)
        assert bundle.gear_items == []
        assert bundle.schedules == []
        assert bundle.service_records == []

    @pytest.mark.asyncio
    async def test_a_link_to_someone_elses_site_is_skipped_rather_than_exported(
        self, db: Session, owner: User, stranger: User
    ):
        """The `user_id` scope is not negotiable, and a stray join row must not become a
        way in. This one cannot be created through the API - every write validates
        ownership first - so it stands for hand-edited data, and the answer has to be
        "skip", not "export" and not "500 the one endpoint a diver uses to leave"."""
        theirs = DiveSite(user_id=stranger.id, name="Theirs", notes="")
        db.add(theirs)
        db.commit()
        dive = _dive(db, owner, number=1)
        db.add(DiveDiveSite(dive_id=dive.id, dive_site_id=theirs.id, position=0))
        db.commit()

        bundle = await _load(owner.id)
        assert bundle.dive_sites == []
        assert bundle.sites_for(bundle.dives[0]) == []


class TestOrdering:
    @pytest.mark.asyncio
    async def test_dives_come_back_oldest_first(self, db: Session, owner: User):
        """Chronological is the order a logbook is read in, and it is what makes the
        golden files and the archive's member names stable."""
        later = _dive(db, owner, number=9)
        earlier = _dive(db, owner, number=2)

        bundle = await _load(owner.id)
        assert [dive.id for dive in bundle.dives] == [earlier.id, later.id]

    @pytest.mark.asyncio
    async def test_a_dives_sites_keep_their_visit_order(self, db: Session, owner: User):
        """Index 0 is the primary site, and it is the one fact UDDF's single-site
        convention would otherwise lose."""
        first = DiveSite(user_id=owner.id, name="Zulu", notes="")
        second = DiveSite(user_id=owner.id, name="Alpha", notes="")
        db.add_all([first, second])
        db.commit()
        dive = _dive(db, owner, number=1)
        # Deliberately added in the order that is *not* alphabetical and *not* id order,
        # so only `position` can produce the expected answer.
        db.add_all(
            [
                DiveDiveSite(dive_id=dive.id, dive_site_id=second.id, position=0),
                DiveDiveSite(dive_id=dive.id, dive_site_id=first.id, position=1),
            ]
        )
        db.commit()

        bundle = await _load(owner.id)
        assert [site.name for site in bundle.sites_for(bundle.dives[0])] == ["Alpha", "Zulu"]

    @pytest.mark.asyncio
    async def test_cylinders_keep_the_order_they_were_saved_in(self, db: Session, owner: User):
        dive = _dive(db, owner, number=1)
        db.add_all(
            [
                DiveMixture(dive_id=dive.id, volume=12.0, oxygen=32.0),
                DiveMixture(dive_id=dive.id, volume=11.1, oxygen=50.0),
            ]
        )
        db.commit()

        bundle = await _load(owner.id)
        assert [m.oxygen for m in bundle.mixtures_by_dive[dive.id]] == [32.0, 50.0]

    @pytest.mark.asyncio
    async def test_a_trips_places_keep_the_order_they_were_listed_in(self, db: Session, owner: User):
        """The only thing that orders them - duplicate names are legal here, so `position`
        is the whole answer - and it is what both flat formats join on."""
        trip = Trip(user_id=owner.id, name="Visayas 2026", start_date=date(2026, 6, 1), notes="")
        empty = Trip(user_id=owner.id, name="Somewhere", start_date=date(2026, 7, 1), notes="")
        db.add_all([trip, empty])
        db.commit()
        # Added last-first, so only `position` can produce the expected answer.
        db.add_all(
            [
                TripLocation(trip_id=trip.id, name="Bohol", position=1),
                TripLocation(trip_id=trip.id, name="Moalboal", position=0),
            ]
        )
        db.commit()

        bundle = await _load(owner.id)
        assert [location.name for location in bundle.locations_by_trip[trip.id]] == ["Moalboal", "Bohol"]
        # Keyed for every trip, so a writer can index it without asking first.
        assert bundle.locations_by_trip[empty.id] == []
