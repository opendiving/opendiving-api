"""Revision `d3f8a61c27e4` run for real, against a database of its own
(`tests/helpers/migrations.py`).

It swaps one constraint on a table holding real rows, and a revision has no way back on the hosted
instance, so what is pinned is that every kind of row the old constraint allowed passes the new one
untouched, what each direction then admits, and that the revision and the model say the same thing.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import importlib.util
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy import CheckConstraint, Engine, Table, insert, text
from sqlalchemy.exc import IntegrityError

from src.app.core.db.migrations import MIGRATIONS_PATH
from src.app.models.dive_site import DiveSite
from src.app.services.commons_records import load_bundle
from tests.conftest import db_available
from tests.helpers.migrations import migrate, scratch_database, template_database

pytestmark = [
    pytest.mark.skipif(not db_available(), reason="No database connection available"),
    pytest.mark.xdist_group(__name__),
]

_REVISION = "d3f8a61c27e4"
_BELOW = "b4d27e6f1c03"
_CONSTRAINT = "ck_dive_site_owner_provenance"
_TABLE = cast(Table, DiveSite.__table__)

_INSERT = (
    "INSERT INTO dive_site (id, user_id, name, uuid, created_at, commons_source, commons_license, "
    "commons_seed_registry, commons_seed_identifier, commons_retrieved) VALUES "
    "(:id, :user_id, :name, gen_random_uuid(), now(), :source, :license, :registry, :identifier, :retrieved)"
)


def _row(
    identifier: int, source: str | None, license: str | None, entry: str | None, user_id: int | None = None
) -> dict[str, Any]:
    return {
        "id": identifier,
        "user_id": user_id,
        "name": f"Site {identifier}",
        "source": source,
        "license": license,
        "registry": source if entry else None,
        "identifier": entry,
        "retrieved": "2026-11-07" if entry else None,
    }


# Every kind of row the hosted instance holds.
STORED = [
    _row(1, None, None, None, user_id=1),
    _row(2, "openstreetmap", "ODbL-1.0", "node/255316037"),
    _row(3, "wikidata", "CC0-1.0", "Q1"),
    _row(4, "contribution", "CC0-1.0", None),
]
CURATED = _row(5, "fwc-artificial-reefs", "LicenseRef-FWC-PD", "MO0061")


def _revision() -> Any:
    path = next((MIGRATIONS_PATH / "versions").glob(f"{_REVISION}_*.py"))
    spec = importlib.util.spec_from_file_location(f"revision_{_REVISION}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert(engine: Engine, row: dict[str, Any]) -> None:
    with engine.begin() as connection:
        connection.execute(text(_INSERT), row)


def _stored(engine: Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, user_id, name, uuid, commons_source, commons_license, commons_seed_registry, "
                "commons_seed_identifier, commons_retrieved FROM dive_site ORDER BY id"
            )
        )
        return [tuple(row) for row in rows]


@pytest.fixture(scope="module")
def below() -> Iterator[str]:
    with template_database(_BELOW) as template:
        yield template


@pytest.fixture
def scratch(below: str) -> Iterator[tuple[str, Engine]]:
    with scratch_database("provenance", below) as (name, engine):
        with engine.begin() as connection:
            connection.execute(
                text(
                    'INSERT INTO "user" (id, name, username, email, is_superuser, is_deleted, uuid, created_at) '
                    "VALUES (1, 'Seed', 'seed1', 'seed@example.com', false, false, gen_random_uuid(), now())"
                )
            )
            for row in STORED:
                connection.execute(text(_INSERT), row)
        yield name, engine


class TestTheUpgrade:
    def test_every_stored_row_stands_and_nothing_moves(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        before = _stored(engine)

        migrate(name, "upgrade", _REVISION)

        assert _stored(engine) == before

    def test_below_it_a_curated_record_is_refused_and_above_it_it_is_not(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        with pytest.raises(IntegrityError, match=_CONSTRAINT):
            _insert(engine, CURATED)

        migrate(name, "upgrade", _REVISION)
        _insert(engine, CURATED)

        assert len(_stored(engine)) == len(STORED) + 1

    @pytest.mark.parametrize(
        "row",
        [
            _row(6, "fwc-artificial-reefs", "LicenseRef-FWC-PD", None),
            _row(6, "contribution", "CC0-1.0", "x"),
            _row(6, None, "CC0-1.0", None),
            _row(6, "fwc-artificial-reefs", None, "MO0061"),
            _row(6, "fwc-artificial-reefs", "LicenseRef-FWC-PD", "MO0061", user_id=1),
        ],
        ids=["seed-with-no-entry", "contribution-with-an-entry", "no-source", "no-licence", "private-with-provenance"],
    )
    def test_above_it_a_half_written_row_is_still_refused(
        self, scratch: tuple[str, Engine], row: dict[str, Any]
    ) -> None:
        name, engine = scratch
        migrate(name, "upgrade", _REVISION)

        with pytest.raises(IntegrityError, match=_CONSTRAINT):
            _insert(engine, row)


class TestTheDowngrade:
    def test_a_curated_record_goes_with_its_links_and_the_rest_stand(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        before = _stored(engine)
        migrate(name, "upgrade", _REVISION)
        _insert(engine, CURATED)
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO saved_site (user_id, dive_site_id, notes, created_at) VALUES (1, 5, 'Mine', now())")
            )

        migrate(name, "downgrade", _BELOW)

        assert _stored(engine) == before
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM saved_site WHERE dive_site_id = 5")).scalar() == 0

    def test_afterwards_a_curated_record_is_refused_again(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        migrate(name, "upgrade", _REVISION)
        migrate(name, "downgrade", _BELOW)

        with pytest.raises(IntegrityError, match=_CONSTRAINT):
            _insert(engine, CURATED)


class TestTheBundle:
    def test_every_shipped_record_stands_under_it(self, scratch: tuple[str, Engine]) -> None:
        """The sync is one transaction, so one record the table refuses would roll it back on every
        start."""
        name, engine = scratch
        migrate(name, "upgrade", "head")
        bundle = load_bundle()
        assert bundle is not None
        now = datetime.now(UTC)

        with engine.begin() as connection:
            connection.execute(text("SELECT setval(pg_get_serial_sequence('dive_site', 'id'), 100)"))
            connection.execute(insert(_TABLE), [{**record, "created_at": now} for record in bundle.records])
            count = connection.execute(text("SELECT count(*) FROM dive_site WHERE user_id IS NULL")).scalar()

        assert count == len(bundle.records) + len(STORED) - 1


class TestTheModel:
    def test_it_says_what_the_revision_says(self) -> None:
        """`alembic check` compares no constraint's text, so a difference would otherwise stand."""
        declared = next(
            constraint
            for constraint in _TABLE.constraints
            if isinstance(constraint, CheckConstraint) and constraint.name == _CONSTRAINT
        )

        assert str(declared.sqltext) == _revision().OWNER_PROVENANCE
