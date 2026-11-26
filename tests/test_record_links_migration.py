"""Revision `28d1ff486573` run for real, against a database of its own
(`tests/helpers/migrations.py`).

A contact holding a website before it has that address, as stored, as its only link after; one
without has none, and so has every person, course, gear item and trip.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text

from tests.conftest import db_available
from tests.helpers.migrations import migrate, scratch_database, template_database

pytestmark = [
    pytest.mark.skipif(not db_available(), reason="No database connection available"),
    pytest.mark.xdist_group(__name__),
]

_REVISION = "28d1ff486573"
_BELOW = "a8c9743e21b0"
# What the check the column arrived with admitted, and the format's schema refuses: the fill
# copies it as it is.
_SPACED = "https://blueocean.example/a page"


@pytest.fixture(scope="module")
def below() -> Iterator[str]:
    with template_database(_BELOW) as template:
        yield template


@pytest.fixture
def upgraded(below: str) -> Iterator[tuple[str, Engine]]:
    with scratch_database("recordlinks", below) as (name, engine):
        with engine.begin() as connection:
            connection.execute(
                text(
                    'INSERT INTO "user" (id, name, username, email, is_superuser, is_deleted, uuid, created_at) '
                    "VALUES (1, 'Seed', 'seed1', 'seed@example.com', false, false, gen_random_uuid(), now())"
                )
            )
            for contact_name, website in (
                ("Listed", "https://blueocean.example"),
                ("Spaced", _SPACED),
                ("Unlisted", None),
            ):
                connection.execute(
                    text(
                        "INSERT INTO contact (user_id, name, roles, website, notes, uuid, created_at) "
                        "VALUES (1, :name, '[]', :website, '', gen_random_uuid(), now())"
                    ),
                    {"name": contact_name, "website": website},
                )
            connection.execute(
                text(
                    "INSERT INTO person (user_id, name, notes, uuid, created_at) "
                    "VALUES (1, 'Alex', '', gen_random_uuid(), now())"
                )
            )
        migrate(name, "upgrade", _REVISION)
        yield name, engine


def test_a_stored_website_is_the_contact_s_only_link_as_stored(upgraded: tuple[str, Engine]) -> None:
    _, engine = upgraded
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT name, links, website FROM contact ORDER BY name")).all()

    assert [tuple(row) for row in rows] == [
        ("Listed", [{"url": "https://blueocean.example", "label": None}], "https://blueocean.example"),
        ("Spaced", [{"url": _SPACED, "label": None}], _SPACED),
        ("Unlisted", [], None),
    ]


@pytest.mark.parametrize("table", ["person", "course", "gear_item", "trip"])
def test_the_other_records_start_with_none(upgraded: tuple[str, Engine], table: str) -> None:
    _, engine = upgraded
    with engine.connect() as connection:
        default = connection.execute(
            text(
                "SELECT column_default, is_nullable FROM information_schema.columns "
                "WHERE table_name = :table AND column_name = 'links'"
            ),
            {"table": table},
        ).one()
        links = connection.execute(text(f"SELECT links FROM {table}")).scalars().all()  # noqa: S608 - a fixed name

    assert (default.column_default, default.is_nullable) == ("'[]'::json", "NO")
    assert all(value == [] for value in links)


def test_the_downgrade_drops_the_columns_and_the_triggers(upgraded: tuple[str, Engine]) -> None:
    name, engine = upgraded
    migrate(name, "downgrade", _BELOW)

    with engine.connect() as connection:
        columns = connection.execute(
            text("SELECT table_name FROM information_schema.columns WHERE column_name = 'links'")
        ).all()
        triggers = connection.execute(
            text("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'contact_website_mirror%'")
        ).all()
        website = connection.execute(text("SELECT website FROM contact WHERE name = 'Listed'")).scalar_one()

    assert (columns, triggers, website) == ([], [], "https://blueocean.example")
