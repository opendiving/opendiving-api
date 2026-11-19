"""Revision `c3f81a6d9e20` run for real, against a database of its own
(`tests/helpers/migrations.py`): every recording and file in a review stored at shape 2 gains the
`created_at` a dive read now gives it, and the row is recorded at shape 3.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app import models
from src.app.models.logbook_import import LogbookImport
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_dive_recording, create_user
from tests.helpers.migrations import migrate, scratch_database, template_database

pytestmark = [
    pytest.mark.skipif(not db_available(), reason="No database connection available"),
    pytest.mark.xdist_group(__name__),
]

_REVISION = "c3f81a6d9e20"
_BELOW = "90c6f077a7d4"

_RECORDED = datetime(2026, 9, 8, 18, 0, tzinfo=UTC)
_UPLOADED = datetime(2026, 9, 9, 7, 30, tzinfo=UTC)
_IMPORTED = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def below() -> Iterator[str]:
    with template_database(_BELOW) as template:
        yield template


@pytest.fixture
def scratch(below: str) -> Iterator[tuple[str, Engine]]:
    with scratch_database("recording_times", below) as database:
        yield database


def _stored_recording(db: Session, diver: models.User) -> tuple[str, str]:
    """One recording holding one file, at `_RECORDED` and `_UPLOADED`: their uuids."""
    dive = create_dive(db, diver)
    recording = create_dive_recording(db, diver, dive)
    recording.created_at = _RECORDED
    file = models.DiveFile(
        user_id=diver.id,
        recording_id=recording.id,
        dive_id=dive.id,
        sha256="c" * 64,
        content_type="application/octet-stream",
        byte_size=1,
        stored_byte_size=1,
        original_filename="dive.uddf",
        parser_key="uddf",
        storage_key=f"dive-files/{uuid7()}",
        created_at=_UPLOADED,
    )
    db.add(file)
    db.commit()
    return str(recording.uuid), str(file.uuid)


def _review(*recordings: tuple[Any, list[Any]]) -> dict[str, Any]:
    return {
        "dives": [
            {
                "outcome": "modified",
                "dive": {
                    "recordings": [
                        {"uuid": str(uuid), "files": [{"uuid": str(file)} for file in files]}
                        for uuid, files in recordings
                    ]
                },
            },
            {"outcome": "skipped", "dive": None},
        ]
    }


def _times(review: dict[str, Any]) -> list[tuple[str, list[str]]]:
    return [
        (recording["created_at"], [file["created_at"] for file in recording["files"]])
        for recording in review["dives"][0]["dive"]["recordings"]
    ]


class TestTheUpgrade:
    def test_each_recording_and_file_reads_as_a_dive_read_now_gives_it(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        with Session(engine) as db:
            diver, other = create_user(db), create_user(db)
            recording, file = _stored_recording(db, diver)
            someone_elses, their_file = _stored_recording(db, other)
            stored = LogbookImport(
                user_id=diver.id,
                review_shape=2,
                status="pending",
                created_at=_IMPORTED,
                review=_review((recording, [file]), (uuid7(), [uuid7()]), (someone_elses, [their_file])),
            )
            db.add(stored)
            db.commit()
            stored_id = stored.id

        migrate(name, "upgrade", _REVISION)

        with Session(engine) as db:
            row = db.scalars(select(LogbookImport).where(LogbookImport.id == stored_id)).one()
            assert row.review_shape == 3
            # The review's own and another account's both name nothing of the import's account.
            assert _times(row.review) == [
                ("2026-09-08T18:00:00Z", ["2026-09-09T07:30:00Z"]),
                ("2026-11-18T12:00:00Z", ["2026-11-18T12:00:00Z"]),
                ("2026-11-18T12:00:00Z", ["2026-11-18T12:00:00Z"]),
            ]
            assert row.review["dives"][1] == {"outcome": "skipped", "dive": None}

    def test_a_review_at_the_new_shape_is_untouched(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        with Session(engine) as db:
            diver = create_user(db)
            review = _review((uuid7(), []))
            stored = LogbookImport(user_id=diver.id, review_shape=3, status="applied", review=review)
            db.add(stored)
            db.commit()
            stored_id = stored.id

        migrate(name, "upgrade", _REVISION)

        with Session(engine) as db:
            assert db.scalars(select(LogbookImport.review).where(LogbookImport.id == stored_id)).one() == review
