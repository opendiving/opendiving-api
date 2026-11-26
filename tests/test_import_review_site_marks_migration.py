"""Revision `b8e2d5a41c07` run for real, against a database of its own
(`tests/helpers/migrations.py`): every site in a review stored at shape 1 gains the `owner` and
`proposal_status` a dive read now gives it, and the row is recorded at shape 2.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.models.logbook_import import LogbookImport
from tests.conftest import db_available, unique_email, unique_username
from tests.helpers.generators import create_commons_site
from tests.helpers.migrations import migrate, scratch_database, template_database

pytestmark = [
    pytest.mark.skipif(not db_available(), reason="No database connection available"),
    pytest.mark.xdist_group(__name__),
]

_REVISION = "b8e2d5a41c07"
_BELOW = "4d88602e2ab9"


@pytest.fixture(scope="module")
def below() -> Iterator[str]:
    with template_database(_BELOW) as template:
        yield template


@pytest.fixture
def scratch(below: str) -> Iterator[tuple[str, Engine]]:
    with scratch_database("site_marks", below) as database:
        yield database


def _user(db: Session) -> int:
    """A user row in this revision's schema, written in SQL: the model names the columns later
    revisions add, so it can neither insert nor load the row here."""
    user_id = db.execute(
        text(
            'INSERT INTO "user" (name, username, email, is_superuser, is_deleted, uuid, created_at) '
            "VALUES ('Seed', :username, :email, false, false, :uuid, now()) RETURNING id"
        ),
        {"username": unique_username(), "email": unique_email(), "uuid": uuid7()},
    ).scalar_one()
    db.commit()
    return int(user_id)


def _propose(db: Session, user_id: int, site_id: int, status: str) -> None:
    # In SQL: the model carries columns later revisions add, which this database is below.
    db.execute(
        text(
            "INSERT INTO dive_site_proposal (uuid, created_at, user_id, dive_site_id, kind, members, attestation, "
            "attested_at, status, message, reason) VALUES (:uuid, :now, :user_id, :site_id, 'new_site', '{}', "
            "'I made it', :now, :status, '', :reason)"
        ),
        {
            "uuid": uuid7(),
            "now": datetime.now(UTC),
            "user_id": user_id,
            "site_id": site_id,
            "status": status,
            "reason": "No" if status == "declined" else None,
        },
    )
    db.commit()


def _own_site(db: Session, user_id: int) -> Any:
    """A private site, with the `id` and `uuid` the tests read. In SQL, for `_propose`'s reason: the
    saved row `create_dive_site` writes beside it carries columns later revisions add."""
    site = db.execute(
        text(
            "INSERT INTO dive_site (uuid, created_at, user_id, name) "
            "VALUES (:uuid, :now, :user_id, :name) RETURNING id, uuid"
        ),
        {"uuid": uuid7(), "now": datetime.now(UTC), "user_id": user_id, "name": f"Pescador {uuid7().hex[-8:]}"},
    ).one()
    db.commit()
    return site


def _review(*site_uuids: Any) -> dict[str, Any]:
    return {
        "dives": [
            {"outcome": "new", "dive": {"dive_sites": [{"uuid": str(uuid), "name": "A site"} for uuid in site_uuids]}},
            {"outcome": "skipped", "dive": None},
        ]
    }


def _marks(review: dict[str, Any]) -> list[tuple[str, str | None]]:
    return [(site["owner"], site["proposal_status"]) for site in review["dives"][0]["dive"]["dive_sites"]]


class TestTheUpgrade:
    def test_each_site_reads_as_a_dive_read_now_gives_it(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        with Session(engine) as db:
            diver, other = _user(db), _user(db)
            own, waiting, declined, decided = (_own_site(db, diver) for _ in range(4))
            record = create_commons_site(db)
            _propose(db, diver, waiting.id, "waiting")
            _propose(db, diver, declined.id, "declined")
            _propose(db, diver, decided.id, "declined")
            _propose(db, diver, decided.id, "withdrawn")
            _propose(db, other, record.id, "waiting")
            stored = LogbookImport(
                user_id=diver,
                review_shape=1,
                status="applied",
                review=_review(own.uuid, waiting.uuid, declined.uuid, decided.uuid, record.uuid, uuid7()),
            )
            db.add(stored)
            db.commit()
            stored_id = stored.id

        migrate(name, "upgrade", _REVISION)

        with Session(engine) as db:
            row = db.scalars(select(LogbookImport).where(LogbookImport.id == stored_id)).one()
            assert row.review_shape == 2
            assert _marks(row.review) == [
                ("account", None),
                ("account", "waiting"),
                ("account", "declined"),
                ("account", None),
                ("opendivingcommons", None),
                ("account", None),
            ]
            assert row.review["dives"][1] == {"outcome": "skipped", "dive": None}

    def test_a_review_at_the_new_shape_is_untouched(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        with Session(engine) as db:
            diver = _user(db)
            review = _review(_own_site(db, diver).uuid)
            stored = LogbookImport(user_id=diver, review_shape=2, status="applied", review=review)
            db.add(stored)
            db.commit()
            stored_id = stored.id

        migrate(name, "upgrade", _REVISION)

        with Session(engine) as db:
            assert db.scalars(select(LogbookImport.review).where(LogbookImport.id == stored_id)).one() == review
