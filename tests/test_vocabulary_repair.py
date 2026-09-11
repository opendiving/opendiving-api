"""Integration tests for revision `f9d04a823776`, which repairs two stored vocabulary
values that no write path could have produced.

These run the revision's own SQL - imported from the module, not retyped - against a live
Postgres through the sync session, because what is worth pinning is not the text but what
Postgres does with it: the `gear_service_schedule` arm skips a row whose rename would
collide with `ux_gear_service_schedule_item_kind_label`, and getting that wrong aborts the
API's startup `alembic upgrade head` rather than failing a request. A container that dies
in its own migration is the failure mode `DECISIONS.md` describes under *"Migrations run on
startup, and every schema change ships one"*, and it is invisible until the next restart.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import importlib.util
from datetime import UTC, date, datetime
from typing import cast

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.app.core.db.migrations import MIGRATIONS_PATH
from src.app.models.dive import Dive
from src.app.models.gear_service_record import GearServiceRecord
from src.app.models.gear_service_schedule import GearServiceSchedule
from src.app.models.user import User
from tests.conftest import db_available
from tests.helpers.generators import create_gear_item, create_user

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

_REVISION = "f9d04a823776_repair_two_vocabulary_values_no_write"


def _repairs() -> tuple[str, ...]:
    """The revision's statements, loaded by path.

    `migrations/versions/` is not an importable package - Alembic loads each file
    directly - so the test does the same rather than keeping a second copy of the SQL that
    could quietly stop matching the one that ships.
    """
    spec = importlib.util.spec_from_file_location(_REVISION, MIGRATIONS_PATH / "versions" / f"{_REVISION}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(tuple[str, ...], module.REPAIRS)


def _apply(db: Session) -> None:
    for statement in _repairs():
        db.execute(text(statement))
    db.commit()


@pytest.fixture
def owner(db: Session) -> User:
    return create_user(db)


def _schedule(db: Session, user: User, gear_item_id: int, kind: str, label: str | None = None) -> GearServiceSchedule:
    schedule = GearServiceSchedule(
        user_id=user.id,
        gear_item_id=gear_item_id,
        kind=kind,
        starts_on=date(2026, 1, 1),
        interval_months=12,
        label=label,
    )
    db.add(schedule)
    db.commit()
    return schedule


class TestTheInspectionRepair:
    def test_a_lone_inspection_schedule_becomes_visual_inspection(self, db: Session, owner: User) -> None:
        item = create_gear_item(db, owner)
        schedule = _schedule(db, owner, item.id, kind="inspection")

        _apply(db)

        db.refresh(schedule)
        assert schedule.kind == "visual_inspection"

    def test_a_service_record_is_repaired_too(self, db: Session, owner: User) -> None:
        """The reported traceback named only the schedule, but `gear_service_record.kind`
        came from the same fixture and reads through the same enum."""
        item = create_gear_item(db, owner)
        record = GearServiceRecord(
            user_id=owner.id,
            gear_item_id=item.id,
            gear_service_schedule_id=None,
            kind="inspection",
            serviced_on=date(2026, 6, 1),
            dive_count_at_service=0,
            notes="",
        )
        db.add(record)
        db.commit()

        _apply(db)

        db.refresh(record)
        assert record.kind == "visual_inspection"

    def test_a_rename_that_would_collide_is_skipped_rather_than_aborting(self, db: Session, owner: User) -> None:
        """The row already has the name it would be renamed to, on the same item and label.
        Renaming it violates the unique index; the repair leaves it alone instead, which is
        only safe because an unrecognized `kind` is no longer a 500 on read.
        """
        item = create_gear_item(db, owner)
        existing = _schedule(db, owner, item.id, kind="visual_inspection", label="First stage")
        colliding = _schedule(db, owner, item.id, kind="inspection", label="First stage")

        _apply(db)

        db.refresh(existing)
        db.refresh(colliding)
        assert existing.kind == "visual_inspection"
        assert colliding.kind == "inspection"

    def test_a_differently_labelled_sibling_is_still_repaired(self, db: Session, owner: User) -> None:
        """The skip is keyed on (item, label), not on the item - so it must not swallow the
        rows it was never about."""
        item = create_gear_item(db, owner)
        blocked = _schedule(db, owner, item.id, kind="visual_inspection", label="First stage")
        free = _schedule(db, owner, item.id, kind="inspection", label="Second stage")

        _apply(db)

        db.refresh(blocked)
        db.refresh(free)
        assert free.kind == "visual_inspection"

    def test_running_it_twice_changes_nothing_further(self, db: Session, owner: User) -> None:
        """`alembic upgrade head` runs on every container start; a repair that is not
        idempotent would be a different database after every restart."""
        item = create_gear_item(db, owner)
        schedule = _schedule(db, owner, item.id, kind="inspection")

        _apply(db)
        _apply(db)

        db.refresh(schedule)
        assert schedule.kind == "visual_inspection"


class TestTheSodaRepair:
    def test_soda_is_cleared_to_not_recorded(self, db: Session, owner: User) -> None:
        dive = Dive(
            user_id=owner.id,
            dive_number=1,
            start_time=datetime.now(UTC),
            duration=1800,
            notes="",
            water_type="soda",
        )
        db.add(dive)
        db.commit()

        _apply(db)

        db.refresh(dive)
        assert dive.water_type is None

    def test_a_real_water_type_is_left_alone(self, db: Session, owner: User) -> None:
        """The repair names one literal. It is not a sweep of everything outside
        `WaterType` - see the revision's docstring for why that distinction is the point.
        """
        dive = Dive(
            user_id=owner.id,
            dive_number=2,
            start_time=datetime.now(UTC),
            duration=1800,
            notes="",
            water_type="brackish",
        )
        db.add(dive)
        db.commit()

        _apply(db)

        db.refresh(dive)
        assert dive.water_type == "brackish"
