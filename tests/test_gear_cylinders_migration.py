"""Revision `e41fcd2a6308` run for real, against a database of its own
(`tests/helpers/migrations.py`): every gear item's dive time is filled from its dives, both time
snapshots from the dives created at or before the row, hidden dive-form sets gain the vessel key
where they hide the start pressure, and the downgrade takes it all back out.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import json
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text

from tests.conftest import db_available
from tests.helpers.migrations import migrate, scratch_database, template_database

pytestmark = [
    pytest.mark.skipif(not db_available(), reason="No database connection available"),
    pytest.mark.xdist_group(__name__),
]

_REVISION = "e41fcd2a6308"
_BELOW = "9d5f74172b52"

BASIC = ["trip_uuid", "sightings", "mixture.po2_limit", "mixture.start_pressure", "file_import"]
PLAIN = ["trip_uuid", "mixture.helium"]

# Dives 1 and 2 use the regulator, dive 3 is deleted, dive 4 is another diver's. The schedule
# was written between dives 1 and 2 and the record after dive 2.
SEED = """
INSERT INTO "user" (id, name, username, email, is_superuser, is_deleted, uuid, created_at, dive_form_hidden_fields)
VALUES (1, 'Seed', 'seed', 'seed@example.com', false, false, gen_random_uuid(), now(), CAST(:basic AS json)),
       (2, 'Other', 'other', 'other@example.com', false, false, gen_random_uuid(), now(), '[]');
INSERT INTO dive (id, user_id, dive_number, start_time, submerged_time, notes, uuid, created_at, is_deleted)
VALUES (1, 1, 1, '2025-01-01 10:00+00', 2700, '', gen_random_uuid(), '2025-01-01 12:00+00', false),
       (2, 1, 2, '2025-01-02 10:00+00', 3600, '', gen_random_uuid(), '2025-01-03 12:00+00', false),
       (3, 1, 3, '2025-01-04 10:00+00', 1800, '', gen_random_uuid(), '2025-01-01 13:00+00', true),
       (4, 2, 1, '2025-01-01 10:00+00', 9999, '', gen_random_uuid(), '2025-01-01 12:00+00', false);
INSERT INTO gear_item (id, user_id, name, notes, uuid, created_at)
VALUES (1, 1, 'MK25', '', gen_random_uuid(), '2024-12-01 00:00+00'),
       (2, 1, 'Unused', '', gen_random_uuid(), '2024-12-01 00:00+00'),
       (3, 2, 'Theirs', '', gen_random_uuid(), '2024-12-01 00:00+00');
INSERT INTO dive_gear_item (dive_id, gear_item_id, position)
VALUES (1, 1, 0), (2, 1, 0), (3, 1, 0), (4, 3, 0);
INSERT INTO gear_service_schedule (id, user_id, gear_item_id, kind, starts_on, interval_months, uuid, created_at)
VALUES (1, 1, 1, 'service', '2020-01-01', 12, gen_random_uuid(), '2025-01-02 00:00+00');
INSERT INTO gear_service_record (user_id, gear_item_id, kind, serviced_on, dive_count_at_service, notes, uuid,
                                 created_at, is_deleted)
VALUES (1, 1, 'service', '2025-01-01', 1, '', gen_random_uuid(), '2025-01-04 00:00+00', false);
INSERT INTO dive_form_preset (user_id, name, hidden_fields, uuid, created_at)
VALUES (1, 'Basic', CAST(:basic AS json), gen_random_uuid(), now()),
       (1, 'Plain', CAST(:plain AS json), gen_random_uuid(), now())
"""


@pytest.fixture(scope="module")
def below() -> Iterator[str]:
    with template_database(_BELOW) as template:
        yield template


@pytest.fixture
def scratch(below: str) -> Iterator[tuple[str, Engine]]:
    with scratch_database("gear_cylinders", below) as database:
        name, engine = database
        with engine.begin() as connection:
            for statement in SEED.split(";\n"):
                connection.execute(text(statement), {"basic": json.dumps(BASIC), "plain": json.dumps(PLAIN)})
        yield name, engine


def _scalar(engine: Engine, sql: str) -> object:
    with engine.connect() as connection:
        return connection.scalar(text(sql))


class TestTheUpgrade:
    def test_a_gear_item_s_dive_time_sums_its_owners_live_dives(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch

        migrate(name, "upgrade", _REVISION)

        with engine.connect() as connection:
            times: dict[int, int] = dict(connection.execute(text("SELECT id, dive_time FROM gear_item")).tuples().all())
        assert times == {1: 2700 + 3600, 2: 0, 3: 9999}

    def test_the_snapshots_count_the_dives_created_at_or_before_the_row(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch

        migrate(name, "upgrade", _REVISION)

        # What the live path would have snapshotted: dive 1 before the schedule, both after
        # the record. The deleted dive is no longer seen.
        assert _scalar(engine, "SELECT dive_time_at_start FROM gear_service_schedule") == 2700
        assert _scalar(engine, "SELECT dive_time_at_service FROM gear_service_record") == 2700 + 3600

    def test_a_hidden_set_gains_the_vessel_key_where_it_hides_the_start_pressure(
        self, scratch: tuple[str, Engine]
    ) -> None:
        name, engine = scratch

        migrate(name, "upgrade", _REVISION)

        with engine.connect() as connection:
            presets: dict[str, list[str]] = dict(
                connection.execute(text("SELECT name, hidden_fields FROM dive_form_preset")).tuples().all()
            )
            account = connection.scalar(text('SELECT dive_form_hidden_fields FROM "user" WHERE id = 1'))
        with_vessel = [*BASIC[:2], "mixture.gear_item_uuid", *BASIC[2:]]
        assert presets == {"Basic": with_vessel, "Plain": PLAIN}
        assert account == with_vessel

    def test_the_interval_rule_takes_an_hours_arm_alone(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch

        migrate(name, "upgrade", _REVISION)

        with engine.begin() as connection:
            connection.execute(
                text("UPDATE gear_service_schedule SET interval_months = NULL, interval_hours = 50 WHERE id = 1")
            )
        assert _scalar(engine, "SELECT interval_hours FROM gear_service_schedule") == 50


class TestTheDowngrade:
    def test_it_takes_the_key_and_the_hours_only_rules_back_out(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        migrate(name, "upgrade", _REVISION)
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE gear_service_schedule SET interval_months = NULL, interval_hours = 50 WHERE id = 1")
            )

        migrate(name, "downgrade", _BELOW)

        assert _scalar(engine, "SELECT count(*) FROM gear_service_schedule") == 0
        with engine.connect() as connection:
            presets: dict[str, list[str]] = dict(
                connection.execute(text("SELECT name, hidden_fields FROM dive_form_preset")).tuples().all()
            )
        assert presets == {"Basic": BASIC, "Plain": PLAIN}
