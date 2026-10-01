"""Revision `53ea55b922e5`'s backfill, run against a live Postgres: a dive with no bottom
temperature takes its primary recording's coldest sample, as an import of it now would.

The revision's own SQL, loaded by path as `test_vocabulary_repair.py` loads its revision's.
Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import importlib.util
from typing import cast

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.app.core.db.migrations import MIGRATIONS_PATH
from src.app.models.dive import Dive
from src.app.models.dive_profile import DiveProfile
from src.app.models.user import User
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_dive_recording, create_user

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

_REVISION = "53ea55b922e5_an_imported_dive_takes_its_bottom_"


def _backfill(db: Session) -> None:
    spec = importlib.util.spec_from_file_location(_REVISION, MIGRATIONS_PATH / "versions" / f"{_REVISION}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db.execute(text(cast(str, module.BACKFILL)))
    db.commit()


def _dive(db: Session, user: User, *, coldest: int | None, ordinal: int = 0, stated: float | None = None) -> Dive:
    """A dive whose recording at `ordinal` has a profile whose coldest sample is `coldest` tenths."""
    dive = create_dive(db, user)
    dive.bottom_temperature = stated
    if ordinal:
        create_dive_recording(db, user, dive)
    db.add(
        DiveProfile(
            recording_id=create_dive_recording(db, user, dive, ordinal=ordinal).id,
            dive_id=dive.id,
            source_sha256="b" * 64,
            parser_key="suunto_json",
            extractor_version=3,
            duration=1800,
            depth_sample_count=2,
            data={},
            min_temperature_c10=coldest,
        )
    )
    db.commit()
    return dive


def _stored(db: Session, dive: Dive) -> float | None:
    db.refresh(dive)
    return dive.bottom_temperature


class TestTheBackfill:
    def test_a_dive_with_none_takes_its_primary_recordings_coldest_sample(self, db: Session) -> None:
        user = create_user(db)
        dive = _dive(db, user, coldest=282)

        _backfill(db)

        assert _stored(db, dive) == 282 / 10

    def test_a_stated_temperature_is_left_alone(self, db: Session) -> None:
        user = create_user(db)
        dive = _dive(db, user, coldest=282, stated=25.0)

        _backfill(db)

        assert _stored(db, dive) == 25.0

    @pytest.mark.parametrize(("coldest", "ordinal"), [(None, 0), (282, 1)], ids=["no channel", "a second recording"])
    def test_nothing_else_fills_it(self, db: Session, coldest: int | None, ordinal: int) -> None:
        user = create_user(db)
        dive = _dive(db, user, coldest=coldest, ordinal=ordinal)

        _backfill(db)

        assert _stored(db, dive) is None
