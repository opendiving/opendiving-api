"""Revision `cc2976dc0dc2` run for real, against a database of its own
(`tests/helpers/migrations.py`): every record a diver owns starts with no custom properties,
and every dive in a review stored at shape 2 gains the empty list a dive read now gives it, the
row recorded at shape 3.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import importlib.util
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.core.db.migrations import MIGRATIONS_PATH
from src.app.models.logbook_import import LogbookImport
from tests.conftest import db_available
from tests.helpers.generators import create_user
from tests.helpers.migrations import migrate, scratch_database, template_database

pytestmark = [
    pytest.mark.skipif(not db_available(), reason="No database connection available"),
    pytest.mark.xdist_group(__name__),
]

_REVISION = "cc2976dc0dc2"
_BELOW = "90c6f077a7d4"


@pytest.fixture(scope="module")
def below() -> Iterator[str]:
    with template_database(_BELOW) as template:
        yield template


@pytest.fixture
def scratch(below: str) -> Iterator[tuple[str, Engine]]:
    with scratch_database("custom_properties", below) as database:
        yield database


def _import(db: Session, *, shape: int, review: dict[str, Any]) -> int:
    stored = LogbookImport(user_id=create_user(db).id, review_shape=shape, status="applied", review=review)
    db.add(stored)
    db.commit()
    return stored.id


def _stored(db: Session, import_id: int) -> tuple[int, dict[str, Any]]:
    row = db.scalars(select(LogbookImport).where(LogbookImport.id == import_id)).one()
    return row.review_shape, row.review


_DIVE = {"uuid": "0193a0b4-0000-7000-8000-000000000001", "notes": ""}
_REVIEW: dict[str, Any] = {
    "dives": [
        {"outcome": "new", "dive": _DIVE},
        {"outcome": "skipped", "dive": None},
    ]
}


class TestTheUpgrade:
    def test_a_review_at_shape_2_gains_the_empty_list_on_each_dive(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        with Session(engine) as db:
            import_id = _import(db, shape=2, review=_REVIEW)

        migrate(name, "upgrade", _REVISION)

        with Session(engine) as db:
            shape, review = _stored(db, import_id)
        assert shape == 3
        assert review["dives"][0]["dive"] == {**_DIVE, "custom_properties": []}
        assert review["dives"][1] == {"outcome": "skipped", "dive": None}

    def test_a_review_at_another_shape_is_untouched(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        with Session(engine) as db:
            import_id = _import(db, shape=1, review=_REVIEW)

        migrate(name, "upgrade", _REVISION)

        with Session(engine) as db:
            assert _stored(db, import_id) == (1, _REVIEW)

    def test_every_existing_record_starts_with_none(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        with Session(engine) as db:
            # In SQL: the model carries the column this revision adds, which the database is below.
            db.execute(
                text(
                    "INSERT INTO trip (uuid, created_at, user_id, name, notes) "
                    "VALUES (:uuid, :now, :user_id, :name, '')"
                ),
                {
                    "uuid": uuid7(),
                    "now": datetime.now(UTC),
                    "user_id": create_user(db).id,
                    "name": f"Dahab {uuid7().hex}",
                },
            )
            db.commit()

        migrate(name, "upgrade", _REVISION)

        with Session(engine) as db:
            assert db.execute(text("SELECT DISTINCT custom_properties::text FROM trip")).scalars().all() == ["[]"]

    def test_the_downgrade_drops_the_columns(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        migrate(name, "upgrade", _REVISION)
        migrate(name, "downgrade", _BELOW)

        with Session(engine) as db:
            left = db.execute(
                text("SELECT table_name FROM information_schema.columns WHERE column_name = 'custom_properties'")
            ).all()
        assert left == []


def _revision() -> Any:
    path = next((MIGRATIONS_PATH / "versions").glob(f"{_REVISION}_*.py"))
    spec = importlib.util.spec_from_file_location(f"revision_{_REVISION}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheHiddenSets:
    def test_a_set_hiding_sightings_hides_them_too_in_their_place(self) -> None:
        revision = _revision()

        assert revision.with_custom_properties(["sightings", "mixture.helium", "file_import"]) == [
            "sightings",
            "custom_properties",
            "mixture.helium",
            "file_import",
        ]
        assert revision.with_custom_properties(["trip_uuid", "sightings"]) == [
            "trip_uuid",
            "sightings",
            "custom_properties",
        ]
        assert revision.with_custom_properties(["water_type", "mixture.role"]) == ["water_type", "mixture.role"]

    def test_the_upgrade_rewrites_a_stored_set_and_the_downgrade_takes_it_back(
        self, scratch: tuple[str, Engine]
    ) -> None:
        name, engine = scratch
        with Session(engine) as db:
            user = create_user(db)
            db.execute(
                text('UPDATE "user" SET dive_form_hidden_fields = CAST(:hidden AS json) WHERE id = :id'),
                {"hidden": '["sightings", "mixture.helium"]', "id": user.id},
            )
            db.commit()
            user_id = user.id

        def stored() -> Any:
            with Session(engine) as db:
                return db.execute(
                    text('SELECT dive_form_hidden_fields FROM "user" WHERE id = :id'), {"id": user_id}
                ).scalar_one()

        migrate(name, "upgrade", _REVISION)
        assert stored() == ["sightings", "custom_properties", "mixture.helium"]

        migrate(name, "downgrade", _BELOW)
        assert stored() == ["sightings", "mixture.helium"]
