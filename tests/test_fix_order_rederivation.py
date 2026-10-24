"""Integration tests for revision `1f96cf18d60e`, which re-derives every dive's fixes in attach
order from its files' stored fixes.

Runs the revision's own SQL - loaded from the module, as `test_vocabulary_repair.py` does - against
a live Postgres, since what is worth pinning is what Postgres does with `DISTINCT ON` and the row
comparison rather than the text.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import importlib.util
from typing import cast

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from src.app.core.db.migrations import MIGRATIONS_PATH
from src.app.models.dive import Dive
from src.app.models.dive_file import DiveFile
from src.app.models.user import User
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_dive_recording, create_user

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

_REVISION = "1f96cf18d60e_a_dives_fixes_follow_attach_order"


def _apply(db: Session) -> None:
    spec = importlib.util.spec_from_file_location(_REVISION, MIGRATIONS_PATH / "versions" / f"{_REVISION}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for statement in cast(tuple[str, ...], module.REDERIVATIONS):
        db.execute(text(statement))
    db.commit()


def _file(
    db: Session,
    user: User,
    dive: Dive,
    recording_id: int,
    n: int,
    *,
    entry: tuple[float, float] | None = None,
    exit_: tuple[float, float] | None = None,
    read: bool = True,
) -> None:
    digest = f"{n:064x}"
    db.add(
        DiveFile(
            user_id=user.id,
            recording_id=recording_id,
            dive_id=dive.id,
            sha256=digest,
            content_type="application/json",
            byte_size=1,
            stored_byte_size=1,
            original_filename=f"{n}.json",
            parser_key="suunto_json",
            storage_key=f"dive-files/{digest}",
            entry_latitude=None if entry is None else entry[0],
            entry_longitude=None if entry is None else entry[1],
            exit_latitude=None if exit_ is None else exit_[0],
            exit_longitude=None if exit_ is None else exit_[1],
            fixes_read=read,
        )
    )
    db.commit()


def _fixes(db: Session, dive: Dive) -> tuple[tuple[float | None, float | None], ...]:
    db.expire_all()
    row = db.execute(
        select(Dive.entry_latitude, Dive.entry_longitude, Dive.exit_latitude, Dive.exit_longitude).where(
            Dive.id == dive.id
        )
    ).one()
    return (row[0], row[1]), (row[2], row[3])


@pytest.fixture
def owner(db: Session) -> User:
    return create_user(db)


class TestRederivingInAttachOrder:
    def test_the_first_attached_file_wins_whatever_its_recordings_ordinal(self, db: Session, owner: User) -> None:
        """The case the worker's first pass got the other way round: the earlier file sits on
        a recording promoted out of ordinal 0."""
        dive = create_dive(db, owner)
        secondary = create_dive_recording(db, owner, dive, ordinal=1)
        primary = create_dive_recording(db, owner, dive, ordinal=0)
        _file(db, owner, dive, secondary.id, 1, entry=(1.0, 1.0))
        _file(db, owner, dive, primary.id, 2, entry=(2.0, 2.0), exit_=(3.0, 3.0))
        dive.entry_latitude, dive.entry_longitude = 2.0, 2.0
        dive.exit_latitude, dive.exit_longitude = 3.0, 3.0
        db.commit()

        _apply(db)

        assert _fixes(db, dive) == ((1.0, 1.0), (3.0, 3.0))

    def test_a_fix_no_read_file_gives_is_kept(self, db: Session, owner: User) -> None:
        """A document's, or one whose file the worker has not read yet."""
        dive = create_dive(db, owner)
        recording = create_dive_recording(db, owner, dive, ordinal=0)
        _file(db, owner, dive, recording.id, 3, entry=(5.0, 5.0), read=False)
        _file(db, owner, dive, recording.id, 4)
        dive.entry_latitude, dive.entry_longitude = 4.0, 4.0
        db.commit()

        _apply(db)

        assert _fixes(db, dive) == ((4.0, 4.0), (None, None))
