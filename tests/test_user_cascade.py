"""`DELETE FROM "user"` reaches every row the account owns.

Ten FKs into `user.id` carried no `ondelete` rule, so that statement raised
`ForeignKeyViolation` for any account that had ever been used. Revision `48781087b2b3`
declares them `CASCADE`; this pins the result. Nothing in the app issues the delete yet -
the purge job is a later change - which is exactly why the guarantee needs a test of its own
rather than arriving with its first caller.

Two halves, and they fail on different things:

- `TestEveryForeignKeyIntoUserCascades` reads `Base.metadata` and needs no database. It is
  the guard against a *new* model reaching `user.id` with a plain `ForeignKey("user.id")` -
  the copy-paste that eight of the ten originals were.
- `TestDeletingAUserTakesEverythingWithIt` needs Postgres, because the models' opinion and
  the database's are separate facts and only the second one governs a real delete. Note
  that `conftest._ensure_tables` uses `create_all`, which never alters an existing table:
  on a dev database that has not run `alembic upgrade head` since this change, the delete
  below fails - correctly, since that database really would refuse it.

Skipped when no database is reachable. On a developer's machine that means
`POSTGRES_SERVER=localhost` (`src/.env` points at the compose hostname, which does not
resolve on the host); CI sets it and fails the job if anything skips. See CONTRIBUTING.md.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from src.app.core.db.database import Base
from src.app.models.auth_audit_event import AuthAuditEvent
from src.app.models.certification import Certification
from src.app.models.certification_file import CertificationFile
from src.app.models.course import Course
from src.app.models.dive import Dive
from src.app.models.dive_dive_site import DiveDiveSite
from src.app.models.dive_file import DiveFile
from src.app.models.dive_site import DiveSite
from src.app.models.gear_item import GearItem
from src.app.models.gear_service_record import GearServiceRecord
from src.app.models.gear_service_schedule import GearServiceSchedule
from src.app.models.gear_set import GearSet
from src.app.models.gear_set_item import GearSetItem
from src.app.models.trip import Trip
from src.app.models.trip_location import TripLocation
from src.app.models.user import User
from src.app.models.user_dive_stats import UserDiveStats
from src.app.models.user_session import UserSession
from src.app.schemas.auth_audit_event import AuthEventType
from tests.conftest import db_available
from tests.helpers.generators import (
    create_course,
    create_dive,
    create_dive_site,
    create_gear_item,
    create_gear_service_record,
    create_gear_service_schedule,
    create_gear_set,
    create_trip,
    create_user,
)


class TestEveryForeignKeyIntoUserCascades:
    """No database: this is the models' own account of the rule.

    A model added later with `ForeignKey("user.id")` and no `ondelete` reinstates exactly
    the bug this change fixes, and would fail nothing else - the purge would raise on the
    first account that owned one of its rows, in a cron job, in production.
    """

    def test_no_foreign_key_into_user_is_left_without_a_delete_rule(self):
        without_cascade = sorted(
            f"{column.table.name}.{column.name}"
            for table in Base.metadata.tables.values()
            for column in table.columns
            for fk in column.foreign_keys
            if fk.column.table.name == "user" and fk.ondelete != "CASCADE"
        )

        assert without_cascade == []


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestDeletingAUserTakesEverythingWithIt:
    """One user, one row in each of the tables the account owns, plus the second-order rows
    that hang off those - then a single `DELETE`.

    Most of them are the ten `48781087b2b3` had to redeclare. The rest are tables added
    since - `course`, then `user_session` and `auth_audit_event` - each of which declared
    `ON DELETE CASCADE` from the outset, which is exactly the case the metadata sweep above
    cannot distinguish from a table that got it right by accident, so they are seeded here
    too. (No count in this sentence on purpose: the previous one said "eleven" and was one
    model away from being wrong, which `DECISIONS.md` §"The counts in the prose go stale
    too" is about.)

    Second-order coverage is not decoration. `certification_file`, `dive_file`,
    `dive_dive_site`, `gear_set_item` and `trip_location` are the tables that would be left
    pointing at nothing if a cascade stopped one level short, and `dive_file` is on both
    lists: it holds `user_id` *and* `dive_id`, so it is reached twice and has to survive
    being deleted by whichever fires first.
    """

    @staticmethod
    def _remaining(db: Session, model: Any, user_id: int) -> int:
        return int(db.execute(select(func.count()).select_from(model).where(model.user_id == user_id)).scalar_one())

    @pytest.fixture
    def populated_diver(self, db: Session) -> User:
        """A logbook with something in every table this change touches."""
        diver = create_user(db)

        dive = create_dive(db, diver)
        site = create_dive_site(db, diver)
        trip = create_trip(db, diver)
        create_course(db, diver)
        item = create_gear_item(db, diver)
        schedule = create_gear_service_schedule(db, diver, item)
        create_gear_service_record(db, diver, item, schedule=schedule)
        gear_set = create_gear_set(db, diver)
        certification = Certification(user_id=diver.id, agency="padi", name="Rescue Diver", notes="")
        db.add_all(
            [
                certification,
                UserDiveStats(user_id=diver.id, total_dives=1, max_depth=18.0, total_time=1800, species_seen=0),
                DiveDiveSite(dive_id=dive.id, dive_site_id=site.id),
                GearSetItem(gear_set_id=gear_set.id, gear_item_id=item.id),
                TripLocation(trip_id=trip.id, name="Moalboal"),
                UserSession(
                    user_id=diver.id,
                    expires_at=datetime.now(UTC) + timedelta(days=7),
                    ip="203.0.113.7",
                    user_agent="Mozilla/5.0",
                ),
                # The *account-tied* audit row, which is the one the cascade is responsible
                # for. Its user-less sibling carries no FK to follow and is erased by the
                # purge's by-email arm instead - pinned in
                # `test_auth_audit.py::TestErasureReachesBothArms`, which also asserts the
                # half this file cannot: that the cascade genuinely does *not* reach it.
                AuthAuditEvent(
                    event_type=AuthEventType.SIGN_IN_SUCCEEDED,
                    ip="203.0.113.7",
                    user_agent="Mozilla/5.0",
                    user_id=diver.id,
                    provider="email",
                ),
            ]
        )
        db.flush()
        db.add_all(
            [
                DiveFile(
                    user_id=diver.id,
                    dive_id=dive.id,
                    sha256="a" * 64,
                    content_type="application/octet-stream",
                    byte_size=1,
                    original_filename="dive.uddf",
                    parser_key="uddf",
                    storage_key=f"dive-files/aa/{diver.uuid}_{'a' * 64}",
                ),
                CertificationFile(
                    certification_id=certification.id,
                    side="front",
                    content_type="image/jpeg",
                    byte_size=1,
                    original_filename="card.jpg",
                    sha256="b" * 64,
                    storage_key=f"certification-files/bb/{diver.uuid}_{'b' * 64}",
                ),
            ]
        )
        db.commit()
        return diver

    def test_the_delete_succeeds_and_leaves_no_owned_row_behind(self, db: Session, populated_diver: User) -> None:
        """The `DELETE` itself is half the assertion: before this change it raised
        `ForeignKeyViolation` on the first of the ten it declared, which is why nothing
        could purge an account that had ever logged a dive."""
        user_id = populated_diver.id

        db.execute(text('DELETE FROM "user" WHERE id = :id'), {"id": user_id})
        db.commit()

        assert db.get(User, user_id) is None
        for model in (
            AuthAuditEvent,
            Certification,
            Course,
            Dive,
            DiveFile,
            DiveSite,
            GearItem,
            GearServiceRecord,
            GearServiceSchedule,
            GearSet,
            Trip,
            UserDiveStats,
            UserSession,
        ):
            assert self._remaining(db, model, user_id) == 0, f"{model.__tablename__} survived the cascade"

    def test_the_rows_that_hang_off_those_go_too(self, db: Session, populated_diver: User) -> None:
        """A cascade that stopped one level short would leave these orphaned rather than
        raising, so counting only the eleven above would pass while they stayed."""
        second_order = {
            model: int(db.execute(select(func.count()).select_from(model)).scalar_one())
            for model in (CertificationFile, DiveDiveSite, GearSetItem, TripLocation)
        }

        db.execute(text('DELETE FROM "user" WHERE id = :id'), {"id": populated_diver.id})
        db.commit()

        for model, before in second_order.items():
            after = int(db.execute(select(func.count()).select_from(model)).scalar_one())
            assert after == before - 1, f"{model.__tablename__} did not follow its parent down"
