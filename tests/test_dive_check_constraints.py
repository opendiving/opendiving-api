"""Integration tests for the `CheckConstraint`s on `Dive`/`DiveMixture`.

Unlike the rest of this suite, these tests insert real rows through a sync SQLAlchemy
session against a live Postgres database (see the `db` fixture in `conftest.py`), so
they verify the actual constraints Postgres enforces - not just the Python-level
`CheckConstraint` declarations in `src/app/models/dive.py`/`dive_mixture.py`. See
`test_dive_constraint_messages.py` for DB-free tests of the corresponding API error
messages.

Automatically skipped if no database is reachable (e.g. running `pytest` outside the
project's docker compose setup), since no other test in this suite requires a live DB.
"""

from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from src.app.core.db.database import Base
from src.app.models.dive import Dive
from src.app.models.dive_mixture import DiveMixture
from src.app.models.user import User
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
    """Create any missing tables (idempotent) so these tests don't depend on the
    `web` service having already run its startup `create_tables()` lifespan hook.
    """
    Base.metadata.create_all(sync_engine)


@pytest.fixture
def dive_owner(db: Session) -> User:
    return create_user(db)


def _make_dive(user_id: int, **overrides: Any) -> Dive:
    defaults: dict[str, Any] = {
        "user_id": user_id,
        "dive_number": 1,
        "start_time": datetime.now(UTC),
        "duration": 1800,
        "notes": "",
    }
    defaults.update(overrides)
    return Dive(**defaults)


def _make_mixture(dive_id: int, **overrides: Any) -> DiveMixture:
    defaults: dict[str, Any] = {"dive_id": dive_id, "volume": 12.0, "oxygen": 21.0, "helium": 0.0}
    defaults.update(overrides)
    return DiveMixture(**defaults)


def _assert_violates(db: Session, obj: Any, constraint_name: str) -> None:
    db.add(obj)
    with pytest.raises(IntegrityError, match=constraint_name):
        db.commit()
    db.rollback()


def _assert_rejected(db: Session, obj: Any) -> None:
    """Like `_assert_violates`, but without asserting which constraint fired.

    Used where two constraints legitimately overlap - e.g. `oxygen > 100` with a
    valid (>= 0) `helium` also always pushes `oxygen + helium` over 100, so either
    `ck_dive_mixture_oxygen_range` or `ck_dive_mixture_oxygen_helium_sum` is a
    correct rejection reason.
    """
    db.add(obj)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


class TestDiveCheckConstraints:
    def test_zero_duration_is_rejected(self, db: Session, dive_owner: User) -> None:
        _assert_violates(db, _make_dive(dive_owner.id, duration=0), "ck_dive_duration_positive")

    def test_negative_duration_is_rejected(self, db: Session, dive_owner: User) -> None:
        _assert_violates(db, _make_dive(dive_owner.id, duration=-5), "ck_dive_duration_positive")

    def test_positive_duration_is_allowed(self, db: Session, dive_owner: User) -> None:
        db.add(_make_dive(dive_owner.id, duration=1))
        db.commit()

    def test_negative_visibility_is_rejected(self, db: Session, dive_owner: User) -> None:
        _assert_violates(db, _make_dive(dive_owner.id, visibility=-1), "ck_dive_visibility_non_negative")

    def test_zero_visibility_is_allowed(self, db: Session, dive_owner: User) -> None:
        db.add(_make_dive(dive_owner.id, visibility=0))
        db.commit()

    def test_null_visibility_is_allowed(self, db: Session, dive_owner: User) -> None:
        db.add(_make_dive(dive_owner.id, visibility=None))
        db.commit()

    def test_zero_max_depth_is_rejected(self, db: Session, dive_owner: User) -> None:
        _assert_violates(db, _make_dive(dive_owner.id, max_depth=0), "ck_dive_max_depth_positive")

    def test_negative_max_depth_is_rejected(self, db: Session, dive_owner: User) -> None:
        _assert_violates(db, _make_dive(dive_owner.id, max_depth=-10), "ck_dive_max_depth_positive")

    def test_positive_max_depth_is_allowed(self, db: Session, dive_owner: User) -> None:
        db.add(_make_dive(dive_owner.id, max_depth=18.5))
        db.commit()

    def test_null_max_depth_is_allowed(self, db: Session, dive_owner: User) -> None:
        db.add(_make_dive(dive_owner.id, max_depth=None))
        db.commit()

    def test_zero_avg_depth_is_rejected(self, db: Session, dive_owner: User) -> None:
        _assert_violates(db, _make_dive(dive_owner.id, avg_depth=0), "ck_dive_avg_depth_positive")

    def test_negative_avg_depth_is_rejected(self, db: Session, dive_owner: User) -> None:
        _assert_violates(db, _make_dive(dive_owner.id, avg_depth=-3), "ck_dive_avg_depth_positive")

    def test_positive_avg_depth_is_allowed(self, db: Session, dive_owner: User) -> None:
        db.add(_make_dive(dive_owner.id, avg_depth=12.0))
        db.commit()

    def test_null_avg_depth_is_allowed(self, db: Session, dive_owner: User) -> None:
        db.add(_make_dive(dive_owner.id, avg_depth=None))
        db.commit()


class TestDiveMixtureCheckConstraints:
    @pytest.fixture
    def dive(self, db: Session, dive_owner: User) -> Generator[Dive, Any]:
        dive = _make_dive(dive_owner.id)
        db.add(dive)
        db.commit()
        db.refresh(dive)
        yield dive

    def test_zero_volume_is_rejected(self, db: Session, dive: Dive) -> None:
        _assert_violates(db, _make_mixture(dive.id, volume=0), "ck_dive_mixture_volume_positive")

    def test_negative_volume_is_rejected(self, db: Session, dive: Dive) -> None:
        _assert_violates(db, _make_mixture(dive.id, volume=-1), "ck_dive_mixture_volume_positive")

    def test_negative_oxygen_is_rejected(self, db: Session, dive: Dive) -> None:
        _assert_violates(db, _make_mixture(dive.id, oxygen=-1, helium=0), "ck_dive_mixture_oxygen_range")

    def test_oxygen_over_100_is_rejected(self, db: Session, dive: Dive) -> None:
        # oxygen=101 with a valid helium also always violates the oxygen+helium<=100
        # constraint, so we can't pin down which specific constraint fires here.
        _assert_rejected(db, _make_mixture(dive.id, oxygen=101, helium=0))

    def test_negative_helium_is_rejected(self, db: Session, dive: Dive) -> None:
        _assert_violates(db, _make_mixture(dive.id, oxygen=0, helium=-1), "ck_dive_mixture_helium_range")

    def test_helium_over_100_is_rejected(self, db: Session, dive: Dive) -> None:
        # Same overlap as test_oxygen_over_100_is_rejected, mirrored for helium.
        _assert_rejected(db, _make_mixture(dive.id, oxygen=0, helium=101))

    def test_oxygen_plus_helium_over_100_is_rejected(self, db: Session, dive: Dive) -> None:
        _assert_violates(db, _make_mixture(dive.id, oxygen=60, helium=50), "ck_dive_mixture_oxygen_helium_sum")

    def test_oxygen_plus_helium_equal_to_100_is_allowed(self, db: Session, dive: Dive) -> None:
        db.add(_make_mixture(dive.id, oxygen=60, helium=40))
        db.commit()

    def test_oxygen_and_helium_at_boundaries_are_allowed(self, db: Session, dive: Dive) -> None:
        db.add(_make_mixture(dive.id, oxygen=0, helium=0))
        db.commit()

    def test_valid_mixture_is_allowed(self, db: Session, dive: Dive) -> None:
        db.add(_make_mixture(dive.id, volume=12, oxygen=21, helium=0))
        db.commit()
