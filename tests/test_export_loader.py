"""Integration tests for `services/export/loader.py`, against a live Postgres.

Everything else about the export is pure and tested without a database. This is the one
part that is a query, and it is the part where getting it wrong is worst: `load_export_bundle`
decides *whose* data ends up in a file the caller downloads. A missing `user_id` filter
here would be a cross-account data leak that no writer test could catch, because every
writer faithfully renders whatever bundle it is handed.

So the two assertions that matter are the boring ones - only the caller's rows, and
nothing soft-deleted - and they are asserted per table rather than once for dives, since
each table carries its own copy of both filters.

Like `test_dive_check_constraints.py`, these are skipped when no database is reachable.
On a developer's machine that means `POSTGRES_SERVER=localhost` (`src/.env` points at the
compose hostname, which does not resolve on the host); CI sets it and fails the job if
anything skips. See CONTRIBUTING.md.
"""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from src.app.core.db.database import Base, async_engine, local_session
from src.app.models.certification import Certification
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
from src.app.models.user import User
from src.app.services.export.loader import ExportBundle, load_export_bundle
from tests.conftest import sync_engine
from tests.helpers.generators import create_user


def _db_available() -> bool:
    try:
        with sync_engine.connect():
            return True
    except OperationalError:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="No database connection available")


@pytest.fixture(scope="module", autouse=True)
def _ensure_tables() -> None:
    Base.metadata.create_all(sync_engine)


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

    The engine is disposed afterwards because `pytest-asyncio` gives each test its own
    event loop, and a pooled asyncpg connection is bound to the loop that opened it -
    reusing one across tests fails with a "attached to a different loop" error that reads
    like a bug in the code under test.
    """
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
    async def test_every_other_table_is_scoped_and_filtered_too(self, db: Session, owner: User, stranger: User):
        """Each of these carries its own `user_id`/`is_deleted` pair, so each is its own
        chance to leave one of the two off."""
        db.add_all(
            [
                DiveSite(user_id=owner.id, name="Mine", notes=""),
                DiveSite(user_id=owner.id, name="Gone", notes="", is_deleted=True),
                DiveSite(user_id=stranger.id, name="Theirs", notes=""),
                Trip(user_id=owner.id, name="Mine", start_date=date(2026, 6, 1), notes=""),
                Trip(user_id=owner.id, name="Gone", start_date=date(2026, 6, 1), notes="", is_deleted=True),
                Trip(user_id=stranger.id, name="Theirs", start_date=date(2026, 6, 1), notes=""),
                GearItem(user_id=owner.id, name="Mine", notes=""),
                GearItem(user_id=owner.id, name="Gone", notes="", is_deleted=True),
                GearItem(user_id=stranger.id, name="Theirs", notes=""),
                Certification(user_id=owner.id, agency="padi", name="Mine", notes=""),
                Certification(user_id=owner.id, agency="padi", name="Gone", notes="", is_deleted=True),
                Certification(user_id=stranger.id, agency="padi", name="Theirs", notes=""),
            ]
        )
        db.commit()

        bundle = await _load(owner.id)
        assert [site.name for site in bundle.dive_sites] == ["Mine"]
        assert [trip.name for trip in bundle.trips] == ["Mine"]
        assert [item.name for item in bundle.gear_items] == ["Mine"]
        assert [cert.name for cert in bundle.certifications] == ["Mine"]

    @pytest.mark.asyncio
    async def test_an_account_with_nothing_in_it_loads_an_empty_bundle(self, owner: User):
        bundle = await _load(owner.id)
        assert bundle.dives == []
        assert bundle.user.id == owner.id

    @pytest.mark.asyncio
    async def test_a_missing_user_fails_loudly(self):
        with pytest.raises(LookupError):
            await _load(-1)


class TestStillReferencedButDeleted:
    """The rows that are soft-deleted and still shown, which the app has three of.

    `erase_dive_site` leaves the site on the dives logged at it, `erase_gear_item` leaves
    the item on its dives and sets, `erase_trip` leaves the trip on its dives, and the
    service-record listing resolves a schedule uuid with no `is_deleted` filter. Reading
    only the live rows made `sites_for`/`gear_for` a `KeyError` - a 500 on all three
    export endpoints for any diver who had ever deleted a site - and would have left
    `export.json` with uuids nothing in the file defined and UDDF with dangling
    `xs:IDREF`s.
    """

    @pytest.mark.asyncio
    async def test_a_deleted_site_and_gear_item_still_on_a_dive_come_back(self, db: Session, owner: User):
        site = DiveSite(user_id=owner.id, name="Gone", notes="", is_deleted=True)
        item = GearItem(user_id=owner.id, name="Gone", notes="", is_deleted=True)
        db.add_all([site, item])
        db.commit()
        dive = _dive(db, owner, number=1)
        db.add_all(
            [
                DiveDiveSite(dive_id=dive.id, dive_site_id=site.id, position=0),
                DiveGearItem(dive_id=dive.id, gear_item_id=item.id, position=0),
            ]
        )
        db.commit()

        bundle = await _load(owner.id)
        assert [s.name for s in bundle.sites_for(bundle.dives[0])] == ["Gone"]
        assert [i.name for i in bundle.gear_for(bundle.dives[0])] == ["Gone"]
        assert [s.is_deleted for s in bundle.dive_sites] == [True]

    @pytest.mark.asyncio
    async def test_a_deleted_trip_still_on_a_dive_comes_back(self, db: Session, owner: User):
        trip = Trip(user_id=owner.id, name="Gone", start_date=date(2026, 6, 1), notes="", is_deleted=True)
        db.add(trip)
        db.commit()
        dive = _dive(db, owner, number=1)
        dive.trip_id = trip.id
        db.commit()

        bundle = await _load(owner.id)
        exported = bundle.trip_for(bundle.dives[0])
        assert exported is not None
        assert exported.is_deleted is True

    @pytest.mark.asyncio
    async def test_a_deleted_gear_item_still_in_a_set_comes_back(self, db: Session, owner: User):
        item = GearItem(user_id=owner.id, name="Gone", notes="", is_deleted=True)
        gear_set = GearSet(user_id=owner.id, name="Tech")
        db.add_all([item, gear_set])
        db.commit()
        db.add(GearSetItem(gear_set_id=gear_set.id, gear_item_id=item.id, position=0))
        db.commit()

        bundle = await _load(owner.id)
        assert bundle.item_ids_by_set[gear_set.id] == [item.id]
        assert item.id in bundle.gear_item_by_id

    @pytest.mark.asyncio
    async def test_a_deleted_schedule_still_on_a_record_comes_back(self, db: Session, owner: User):
        item = GearItem(user_id=owner.id, name="Reg", notes="")
        db.add(item)
        db.commit()
        schedule = GearServiceSchedule(
            user_id=owner.id,
            gear_item_id=item.id,
            kind="service",
            starts_on=date(2026, 1, 1),
            interval_months=12,
            is_deleted=True,
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

        bundle = await _load(owner.id)
        assert schedule.id in bundle.schedule_by_id

    @pytest.mark.asyncio
    async def test_a_deleted_gear_item_whose_only_referrer_is_a_service_record_comes_back(
        self, db: Session, owner: User
    ):
        """The longest referrer chain there is, and the one the first fix missed.

        `erase_gear_item` soft-deletes the item *and* its schedules while deliberately
        keeping the records. So a live record drags back a dead schedule, and the schedule
        is then the only thing still naming a dead item - an item that was never dived and
        never in a set is reachable by no other path.
        """
        item = GearItem(user_id=owner.id, name="Retired reg", notes="", is_deleted=True)
        db.add(item)
        db.commit()
        schedule = GearServiceSchedule(
            user_id=owner.id,
            gear_item_id=item.id,
            kind="service",
            starts_on=date(2026, 1, 1),
            interval_months=12,
            is_deleted=True,
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

        bundle = await _load(owner.id)
        assert item.id in bundle.gear_item_by_id
        assert schedule.id in bundle.schedule_by_id

    @pytest.mark.asyncio
    async def test_a_deleted_row_nothing_references_stays_out(self, db: Session, owner: User):
        """The exception is only for what the app still shows. An orphaned deleted site
        is genuinely gone, and resurrecting it would be the surprise."""
        db.add(DiveSite(user_id=owner.id, name="Orphan", notes="", is_deleted=True))
        db.commit()

        bundle = await _load(owner.id)
        assert bundle.dive_sites == []

    @pytest.mark.asyncio
    async def test_a_deleted_row_belonging_to_someone_else_is_never_resurrected(
        self, db: Session, owner: User, stranger: User
    ):
        """`still_referenced` widens the soft-delete filter and nothing else - the
        `user_id` scope is not negotiable, and a stray id must not become a way in."""
        theirs = DiveSite(user_id=stranger.id, name="Theirs", notes="", is_deleted=True)
        db.add(theirs)
        db.commit()
        dive = _dive(db, owner, number=1)
        db.add(DiveDiveSite(dive_id=dive.id, dive_site_id=theirs.id, position=0))
        db.commit()

        bundle = await _load(owner.id)
        assert bundle.dive_sites == []
        # And the export still builds: `sites_for` skips what it cannot resolve rather
        # than raising, so a row that could only exist through hand-edited data does not
        # take down the one endpoint a diver uses to leave with everything else.
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
