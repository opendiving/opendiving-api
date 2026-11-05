"""A Commons record is one row every account shares, and what is theirs alone sits beside it.

The revision runs for real against a database of its own (`tests/helpers/migrations.py`). The
sync runs against the suite's database with records of its own, since the app's lifespan is told
not to here (`tests/conftest.py`). Everything else goes through the routes as two accounts, because
that is how a leak between them shows: `Sign` switches who the app reads as signed in.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import asyncio
import json
import logging
import uuid as uuid_pkg
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.setup import create_application
from src.app.core.utils import cache as cache_module
from src.app.crud.crud_users import read_account
from src.app.models.dive import Dive
from src.app.models.dive_dive_site import DiveDiveSite
from src.app.models.dive_site import DiveSite
from src.app.models.dive_site_tag import DiveSiteTag
from src.app.models.saved_site import SavedSite
from src.app.models.user import User
from src.app.services import commons_records
from src.app.services.commons_records import CommonsBundle, columns_of, sync_commons
from src.app.services.export.envelope import _export_dive_site
from src.app.services.export.loader import load_export_bundle
from src.app.services.export.tabular import write_dive_sites_csv
from src.app.services.logbook_import.planner import plan_import
from src.app.services.logbook_import.writer import write_import
from tests.conftest import db_available
from tests.helpers.fake_redis import FakeRedis
from tests.helpers.generators import (
    create_commons_site,
    create_dive,
    create_dive_site,
    create_species,
    create_user,
    save_dive_site,
)
from tests.helpers.import_parts import load_one
from tests.helpers.migrations import migrate, scratch_database, template_database

pytestmark = [pytest.mark.skipif(not db_available(), reason="No database connection available")]

_REVISION = "8c1e4f2a9d37"
_BELOW = "63b752e6b762"

OSM_CREDIT = "[Data © OpenStreetMap contributors, ODbL 1.0.](https://osm.org/copyright)"


def _marked(label: str = "Term") -> str:
    """A name no other test's row can match, these rows outliving the test."""
    return f"{label}{uuid_pkg.uuid4().hex[:10]}"


# -------------- the revision --------------

SEED = """
INSERT INTO "user" (id, name, username, email, is_superuser, is_deleted, uuid, created_at)
VALUES (1, 'Seed', 'seed', 'seed@example.com', false, false, gen_random_uuid(), now());
INSERT INTO dive_site (id, user_id, name, notes, uuid, created_at)
VALUES (1, 1, 'Cleared on the serving build', 'What it said once', gen_random_uuid(), '2024-03-01 09:30+00'),
       (2, 1, 'Edited on the build before it', 'Edited since', gen_random_uuid(), '2024-03-02 09:30+00'),
       (3, 1, 'Created on the serving build', '', gen_random_uuid(), '2024-03-03 09:30+00'),
       (4, 1, 'Inserted on the build before it', 'Never had a row', gen_random_uuid(), '2024-03-04 09:30+00'),
       (5, 1, 'Untouched by either', 'The same words', gen_random_uuid(), '2024-03-05 09:30+00');
INSERT INTO saved_site (user_id, dive_site_id, notes, created_at, updated_at)
VALUES (1, 1, '', '2024-03-01 09:30+00', '2026-11-05 10:00+00'),
       (1, 2, 'What it said once', '2024-03-02 09:30+00', NULL),
       (1, 3, 'Written on the serving build', '2024-03-03 09:30+00', NULL),
       (1, 5, 'The same words', '2024-03-05 09:30+00', NULL)
"""


@pytest.fixture(scope="module")
def below() -> Iterator[str]:
    with template_database(_BELOW) as template:
        yield template


@pytest.fixture
def scratch(below: str) -> Iterator[tuple[str, Engine]]:
    with scratch_database("commons", below) as database:
        name, engine = database
        with engine.begin() as connection:
            for statement in SEED.split(";\n"):
                connection.execute(text(statement))
        yield name, engine


@pytest.mark.xdist_group(__name__)
class TestTheRevision:
    def _notes(self, engine: Engine) -> dict[int, str]:
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT dive_site_id, notes FROM saved_site ORDER BY dive_site_id"))
            return {row[0]: row[1] for row in rows}

    def test_a_note_cleared_on_the_serving_build_stays_cleared(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch

        migrate(name, "upgrade", _REVISION)

        assert self._notes(engine)[1] == ""

    def test_a_note_the_build_before_it_edited_reaches_the_row(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch

        migrate(name, "upgrade", _REVISION)

        assert self._notes(engine)[2] == "Edited since"

    def test_a_site_created_on_the_serving_build_keeps_what_its_row_holds(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch

        migrate(name, "upgrade", _REVISION)

        assert self._notes(engine)[3] == "Written on the serving build"

    def test_a_site_with_no_saved_row_gains_one_with_its_notes_and_its_date(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch

        migrate(name, "upgrade", _REVISION)

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT user_id, notes, to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') "
                    "FROM saved_site WHERE dive_site_id = 4"
                )
            ).one()
        assert tuple(row) == (1, "Never had a row", "2024-03-04")

    def test_afterwards_every_private_site_has_exactly_one_row_and_the_column_is_gone(
        self, scratch: tuple[str, Engine]
    ) -> None:
        name, engine = scratch

        migrate(name, "upgrade", _REVISION)

        with engine.connect() as connection:
            per_site = connection.execute(
                text(
                    "SELECT count(saved_site.id) FROM dive_site LEFT JOIN saved_site "
                    "ON saved_site.dive_site_id = dive_site.id AND saved_site.user_id = dive_site.user_id "
                    "GROUP BY dive_site.id"
                )
            ).scalars()
            assert set(per_site) == {1}
            columns = connection.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'dive_site'")
            ).scalars()
            assert "notes" not in set(columns)

    def test_the_downgrade_takes_the_commons_rows_and_copies_the_notes_back(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        migrate(name, "upgrade", _REVISION)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO dive_site (id, user_id, name, uuid, created_at, commons_source, commons_license) "
                    "VALUES (100, NULL, 'Commons', gen_random_uuid(), now(), 'contribution', 'CC0-1.0')"
                )
            )

        migrate(name, "downgrade", _BELOW)

        with engine.connect() as connection:
            rows = connection.execute(text("SELECT id, notes FROM dive_site ORDER BY id")).all()
        assert [tuple(row) for row in rows] == [
            (1, ""),
            (2, "Edited since"),
            (3, "Written on the serving build"),
            (4, "Never had a row"),
            (5, "The same words"),
        ]


class TestTheOwnerConstraint:
    @pytest.mark.parametrize(
        "columns",
        [
            {},
            {"commons_source": "openstreetmap"},
            {"commons_source": "contribution", "commons_license": None},
            {
                "commons_source": "openstreetmap",
                "commons_license": "ODbL-1.0",
                "commons_seed_registry": "openstreetmap",
                "commons_seed_identifier": "node/1",
            },
            {
                "commons_source": "contribution",
                "commons_license": "CC0-1.0",
                "commons_retrieved": date(2026, 10, 17),
            },
            {"commons_source": "a-scraped-map", "commons_license": "ODbL-1.0"},
        ],
        ids=["nothing", "no-licence", "no-licence-contribution", "no-date", "contribution-with-date", "unknown"],
    )
    def test_a_commons_row_cannot_be_half_written(self, db: Session, columns: dict[str, Any]) -> None:
        db.add(DiveSite(user_id=None, name=_marked(), **columns))
        with pytest.raises(IntegrityError, match="ck_dive_site_owner_provenance"):
            db.commit()
        db.rollback()

    def test_a_private_row_carries_no_provenance(self, db: Session, diver: User) -> None:
        db.add(DiveSite(user_id=diver.id, name=_marked(), commons_source="contribution", commons_license="CC0-1.0"))
        with pytest.raises(IntegrityError, match="ck_dive_site_owner_provenance"):
            db.commit()
        db.rollback()

    def test_a_change_of_owner_is_one_update_on_the_same_row(self, db: Session, diver: User) -> None:
        site = create_dive_site(db, diver)
        uuid = site.uuid

        site.user_id, site.commons_source, site.commons_license = None, "contribution", "CC0-1.0"
        db.commit()

        assert db.get(DiveSite, site.id).uuid == uuid  # type: ignore[union-attr]

    def test_two_commons_records_may_share_a_name_and_a_locality(self, db: Session) -> None:
        name = _marked("Shark Point")

        create_commons_site(db, name=name, location_name="Egypt")
        create_commons_site(db, name=name, location_name="Egypt")

        assert len(db.scalars(select(DiveSite).where(DiveSite.name == name)).all()) == 2


# -------------- the sync --------------


def _record(**columns: Any) -> dict[str, Any]:
    identifier = f"node/{uuid_pkg.uuid4().int % 10**12 + 1}"
    return {
        "uuid": uuid_pkg.uuid4(),
        "name": _marked("Seed"),
        "other_names": [],
        "latitude": 27.814092,
        "longitude": 33.920048,
        "location_name": "South Sinai, Egypt",
        "external_ids": [{"registry": "openstreetmap", "identifier": identifier}],
        "depth_from": None,
        "depth_to": None,
        "water_type": None,
        "altitude": None,
        "entry_types": [],
        "commons_source": "openstreetmap",
        "commons_license": "ODbL-1.0",
        "commons_seed_registry": "openstreetmap",
        "commons_seed_identifier": identifier,
        "commons_retrieved": date(2026, 10, 17),
        **columns,
    }


def _bundle(*records: dict[str, Any]) -> CommonsBundle:
    return CommonsBundle(
        commit="f" * 40,
        licence="ODbL 1.0",
        licence_url="https://opendatacommons.org/licenses/odbl/1-0/",
        notice="The notice",
        attributions={"openstreetmap": OSM_CREDIT},
        records=records,
    )


@pytest_asyncio.fixture
async def sync_engine(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[Any]:
    """The sync opens connections of its own, so it gets an engine of this test's event loop."""
    engine = create_async_engine(settings.POSTGRES_ASYNC_PREFIX + settings.POSTGRES_URI.get_secret_value())
    monkeypatch.setattr(commons_records, "engine", engine)
    yield engine
    await engine.dispose()


async def _sync(monkeypatch: pytest.MonkeyPatch, *records: dict[str, Any], primary: bool = False) -> None:
    monkeypatch.setattr(commons_records, "_loaded", _bundle(*records))
    monkeypatch.setattr(settings, "COMMONS_PRIMARY", primary)
    await sync_commons()


def _row(db: Session, uuid: uuid_pkg.UUID) -> DiveSite:
    db.expire_all()
    return db.scalars(select(DiveSite).where(DiveSite.uuid == uuid)).one()


def _version(db: Session, uuid: uuid_pkg.UUID) -> Any:
    """Postgres's own stamp of the writing transaction: unchanged means nothing wrote the row."""
    db.rollback()
    return db.scalar(text("SELECT xmin::text FROM dive_site WHERE uuid = :uuid"), {"uuid": uuid})


@pytest.mark.usefixtures("sync_engine")
class TestTheSync:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("primary", [False, True], ids=["copy", "primary"])
    async def test_it_inserts_the_records_it_lacks(
        self, db: Session, monkeypatch: pytest.MonkeyPatch, primary: bool
    ) -> None:
        record = _record(other_names=["Thistlegorm"])

        await _sync(monkeypatch, record, primary=primary)

        row = _row(db, record["uuid"])
        assert (row.user_id, row.name, row.other_names) == (None, record["name"], ["Thistlegorm"])
        assert row.commons_seed_identifier == record["commons_seed_identifier"]
        assert row.created_at is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("primary", [False, True], ids=["copy", "primary"])
    async def test_a_restart_with_an_unchanged_bundle_writes_no_row(
        self, db: Session, monkeypatch: pytest.MonkeyPatch, primary: bool
    ) -> None:
        record = _record()
        await _sync(monkeypatch, record, primary=primary)
        written = _version(db, record["uuid"])

        await _sync(monkeypatch, record, primary=primary)

        assert _version(db, record["uuid"]) == written
        assert _row(db, record["uuid"]).updated_at is None

    @pytest.mark.asyncio
    async def test_a_copy_updates_a_row_whose_bundled_facts_differ(
        self, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = _record()
        await _sync(monkeypatch, record)

        await _sync(monkeypatch, record | {"name": "Renamed in the repository", "depth_to": 30.0})

        row = _row(db, record["uuid"])
        assert (row.name, row.depth_to) == ("Renamed in the repository", 30.0)
        assert row.updated_at is not None

    @pytest.mark.asyncio
    async def test_the_primary_changes_no_existing_row(self, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        record = _record()
        await _sync(monkeypatch, record, primary=True)
        written = _version(db, record["uuid"])

        await _sync(monkeypatch, record | {"name": "Renamed in the repository"}, primary=True)

        assert _version(db, record["uuid"]) == written

    @pytest.mark.asyncio
    @pytest.mark.parametrize("primary", [False, True], ids=["copy", "primary"])
    async def test_it_deletes_nothing(self, db: Session, monkeypatch: pytest.MonkeyPatch, primary: bool) -> None:
        record = _record()
        await _sync(monkeypatch, record, primary=primary)

        await _sync(monkeypatch, _record(), primary=primary)

        assert _row(db, record["uuid"]) is not None

    @pytest.mark.asyncio
    async def test_a_row_a_diver_saved_survives_an_update_with_their_layer_on_it(
        self, db: Session, diver: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = _record()
        await _sync(monkeypatch, record)
        row = _row(db, record["uuid"])
        save_dive_site(db, row, notes="Gate code 1234", user=diver)

        await _sync(monkeypatch, record | {"name": "Renamed in the repository"})

        db.expire_all()
        saved = db.scalars(select(SavedSite).where(SavedSite.dive_site_id == row.id)).one()
        assert saved.notes == "Gate code 1234"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("primary", [False, True], ids=["copy", "primary"])
    async def test_a_bundled_uuid_naming_a_private_row_is_skipped_and_logged(
        self,
        db: Session,
        diver: User,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        primary: bool,
    ) -> None:
        site = create_dive_site(db, diver)
        name, uuid = site.name, site.uuid
        written = _version(db, uuid)

        with caplog.at_level(logging.WARNING, logger=commons_records.__name__):
            await _sync(monkeypatch, _record(uuid=uuid), primary=primary)

        assert _version(db, uuid) == written
        row = _row(db, uuid)
        assert (row.user_id, row.name) == (diver.id, name)
        assert str(uuid) in caplog.text

    @pytest.mark.asyncio
    async def test_a_private_site_with_the_same_name_is_never_touched(
        self, db: Session, diver: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        site = create_dive_site(db, diver)
        uuid = site.uuid
        written = _version(db, uuid)

        await _sync(monkeypatch, _record(name=site.name, location_name=site.location_name))

        assert _version(db, uuid) == written

    @pytest.mark.asyncio
    async def test_two_processes_starting_together_leave_one_row_per_record(
        self, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        records = [_record() for _ in range(25)]
        monkeypatch.setattr(commons_records, "_loaded", _bundle(*records))

        await asyncio.gather(sync_commons(), sync_commons())

        uuids = [record["uuid"] for record in records]
        assert sorted(db.scalars(select(DiveSite.uuid).where(DiveSite.uuid.in_(uuids))).all()) == sorted(uuids)

    @pytest.mark.asyncio
    async def test_the_insert_holds_that_without_the_lock(self, db: Session, sync_engine: Any) -> None:
        """Both have read the table before either writes, which the lock is there to avoid and
        the unique uuid is there to survive."""
        records = [_record() for _ in range(25)]

        async def apply() -> None:
            async with sync_engine.begin() as conn:
                await commons_records._apply(conn, records, primary=False)

        await asyncio.gather(apply(), apply())

        uuids = [record["uuid"] for record in records]
        assert sorted(db.scalars(select(DiveSite.uuid).where(DiveSite.uuid.in_(uuids))).all()) == sorted(uuids)

    @pytest.mark.asyncio
    async def test_an_unreadable_bundle_logs_and_leaves_the_start_alone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.setattr(commons_records, "_loaded", None)
        monkeypatch.setattr(commons_records, "_DATA_PATH", tmp_path / "not-here.geojson")

        await sync_commons()

    @pytest.mark.asyncio
    async def test_a_schema_behind_the_build_logs_and_leaves_the_start_alone(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(commons_records, "_apply", AsyncMock(side_effect=RuntimeError("no such column")))

        with caplog.at_level(logging.WARNING, logger=commons_records.__name__):
            await _sync(monkeypatch, _record())

        assert "Could not sync the Commons" in caplog.text


class TestTheLifespan:
    def test_it_syncs_whether_or_not_this_process_ran_the_migrations(self) -> None:
        app = create_application(router=router, settings=settings, apply_migrations_on_start=False)
        from src.app.core import setup

        sync = setup.sync_commons
        assert isinstance(sync, AsyncMock)
        sync.reset_mock()

        with TestClient(app):
            pass

        sync.assert_awaited_once()


# -------------- the routes, as two accounts --------------


@dataclass
class Sign:
    """Who the app reads as signed in, switched between requests."""

    account: dict[str, Any]
    accounts: dict[int, dict[str, Any]]

    def as_(self, user: User) -> None:
        self.account = self.accounts[user.id]


@pytest.fixture(scope="module")
def sites_app() -> Any:
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


@pytest_asyncio.fixture
async def signed(
    sites_app: Any, async_sessions: async_sessionmaker[AsyncSession], diver: User, other_diver: User
) -> AsyncGenerator[Sign]:
    async with async_sessions() as db:
        accounts = {
            user.id: cast(dict[str, Any], await read_account(db, uuid=user.uuid)) for user in (diver, other_diver)
        }
    sign = Sign(account=accounts[diver.id], accounts=accounts)
    sites_app.dependency_overrides[get_current_user] = lambda: sign.account
    yield sign
    sites_app.dependency_overrides = {}


@pytest_asyncio.fixture
async def http(
    sites_app: Any,
    async_sessions: async_sessionmaker[AsyncSession],
    signed: Sign,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[httpx.AsyncClient]:
    monkeypatch.setattr(cache_module, "client", FakeRedis())

    async def per_request() -> AsyncGenerator[AsyncSession]:
        async with async_sessions() as session:
            yield session

    sites_app.dependency_overrides[async_get_db] = per_request
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=sites_app), base_url="http://test/api/v1") as client:
        yield client


@pytest.fixture
def commons(db: Session) -> DiveSite:
    return create_commons_site(db, location_name="South Sinai, Egypt", depth_to=30.0, other_names=["Thistlegorm"])


async def _read(http: httpx.AsyncClient, uuid: Any) -> dict[str, Any]:
    response = await http.get(f"/dive-site/{uuid}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _save(http: httpx.AsyncClient, uuid: Any, **body: Any) -> dict[str, Any]:
    response = await http.put(f"/dive-site/{uuid}/saved", json=body or None)
    assert response.status_code == 200, response.text
    saved: dict[str, Any] = response.json()
    return saved


async def _list(http: httpx.AsyncClient, **params: Any) -> list[dict[str, Any]]:
    response = await http.get("/dive-sites", params={"items_per_page": 50, **params})
    assert response.status_code == 200, response.text
    rows: list[dict[str, Any]] = response.json()["data"]
    return rows


async def _lookup(http: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
    response = await http.get("/dive-sites/lookup", params={"items_per_page": 50, **params})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


class TestTheRead:
    @pytest.mark.asyncio
    async def test_a_commons_record_reads_for_anyone_with_nothing_of_theirs_on_it(
        self, http: httpx.AsyncClient, commons: DiveSite
    ) -> None:
        read = await _read(http, commons.uuid)

        assert (read["owner"], read["saved"], read["attribution"]) == ("opendivingcommons", False, OSM_CREDIT)
        assert (read["notes"], read["tags"], read["dive_count"]) == ("", [], 0)
        assert datetime.fromisoformat(read["created_at"]) == commons.created_at
        assert read["location"]["name"] == "South Sinai, Egypt"
        assert "user_uuid" not in read

    @pytest.mark.asyncio
    async def test_a_private_site_reads_as_the_account_s(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        read = await _read(http, create_dive_site(db, diver).uuid)

        assert (read["owner"], read["saved"], read["attribution"]) == ("account", True, None)
        assert "user_uuid" not in read

    @pytest.mark.asyncio
    async def test_another_account_s_private_site_is_a_404(
        self, http: httpx.AsyncClient, db: Session, other_diver: User
    ) -> None:
        response = await http.get(f"/dive-site/{create_dive_site(db, other_diver).uuid}")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_the_list_holds_the_saved_and_not_the_rest(
        self, http: httpx.AsyncClient, db: Session, diver: User, commons: DiveSite
    ) -> None:
        own = create_dive_site(db, diver)
        unsaved = create_commons_site(db)
        await _save(http, commons.uuid)

        rows = {row["uuid"]: row for row in await _list(http)}

        assert str(unsaved.uuid) not in rows
        assert rows[str(own.uuid)]["owner"] == "account"
        assert rows[str(commons.uuid)]["owner"] == "opendivingcommons"
        assert rows[str(commons.uuid)]["attribution"] == OSM_CREDIT


class TestSaving:
    @pytest.mark.asyncio
    async def test_it_needs_no_body(self, http: httpx.AsyncClient, commons: DiveSite) -> None:
        read = await _save(http, commons.uuid)

        assert (read["saved"], read["notes"], read["tags"]) == (True, "", [])

    @pytest.mark.asyncio
    async def test_it_can_carry_notes_and_tags(self, http: httpx.AsyncClient, commons: DiveSite) -> None:
        read = await _save(http, commons.uuid, notes="Gate code 1234", tags=["Wreck"])

        assert (read["saved"], read["notes"], read["tags"]) == (True, "Gate code 1234", ["Wreck"])
        assert (await _read(http, commons.uuid))["notes"] == "Gate code 1234"

    @pytest.mark.asyncio
    async def test_a_retry_cannot_overwrite(self, http: httpx.AsyncClient, commons: DiveSite) -> None:
        await _save(http, commons.uuid, notes="First", tags=["Wreck"])
        await http.patch(f"/dive-site/{commons.uuid}", json={"notes": "Edited since"})

        read = await _save(http, commons.uuid, notes="Retried", tags=["Other"])

        assert (read["notes"], read["tags"]) == ("Edited since", ["Wreck"])

    @pytest.mark.asyncio
    async def test_a_retry_makes_no_tag(
        self, http: httpx.AsyncClient, commons: DiveSite, async_sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await _save(http, commons.uuid)

        await _save(http, commons.uuid, tags=["Never made"])

        assert (await http.get("/tags", params={"search": "Never made"})).json()["data"] == []

    @pytest.mark.asyncio
    async def test_on_the_caller_s_own_site_it_changes_nothing(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        site = create_dive_site(db, diver)

        read = await _save(http, site.uuid, notes="Ignored")

        assert (read["owner"], read["notes"]) == ("account", "")

    @pytest.mark.asyncio
    async def test_another_account_s_private_site_is_a_404(
        self, http: httpx.AsyncClient, db: Session, other_diver: User
    ) -> None:
        site = create_dive_site(db, other_diver)

        response = await http.put(f"/dive-site/{site.uuid}/saved")

        assert response.status_code == 404
        assert db.scalars(select(SavedSite).where(SavedSite.dive_site_id == site.id)).all() != []
        assert len(db.scalars(select(SavedSite).where(SavedSite.dive_site_id == site.id)).all()) == 1

    @pytest.mark.asyncio
    async def test_a_body_with_a_shared_member_is_a_422(self, http: httpx.AsyncClient, commons: DiveSite) -> None:
        response = await http.put(f"/dive-site/{commons.uuid}/saved", json={"name": "Mine now"})

        assert response.status_code == 422


class TestThePatch:
    @pytest.mark.asyncio
    async def test_a_saved_record_takes_notes_and_tags(self, http: httpx.AsyncClient, commons: DiveSite) -> None:
        await _save(http, commons.uuid)

        response = await http.patch(f"/dive-site/{commons.uuid}", json={"notes": "Mine", "tags": ["Wreck"]})

        assert response.status_code == 200, response.text
        read = await _read(http, commons.uuid)
        assert (read["notes"], read["tags"]) == ("Mine", ["Wreck"])

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [{"name": "Mine now"}, {"notes": "Mine", "depth_to": 12.0}, {"latitude": 1.0, "longitude": 2.0}],
        ids=["name", "alongside-notes", "pin"],
    )
    async def test_a_body_naming_a_shared_member_is_a_403_and_writes_nothing(
        self, http: httpx.AsyncClient, db: Session, commons: DiveSite, body: dict[str, Any]
    ) -> None:
        name = commons.name
        await _save(http, commons.uuid)

        response = await http.patch(f"/dive-site/{commons.uuid}", json=body)

        assert response.status_code == 403
        read = await _read(http, commons.uuid)
        assert (read["name"], read["notes"], read["depth_to"]) == (name, "", 30.0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [{"notes": "Mine"}, {"tags": ["Wreck"]}, {"name": "Mine now"}, {}],
        ids=["notes", "tags", "name", "empty"],
    )
    async def test_an_unsaved_record_is_a_409_whatever_the_body_names_and_writes_nothing(
        self, http: httpx.AsyncClient, db: Session, commons: DiveSite, body: dict[str, Any]
    ) -> None:
        response = await http.patch(f"/dive-site/{commons.uuid}", json=body)

        assert response.status_code == 409
        assert db.scalars(select(DiveSiteTag).where(DiveSiteTag.dive_site_id == commons.id)).all() == []
        assert db.scalars(select(SavedSite).where(SavedSite.dive_site_id == commons.id)).all() == []

    @pytest.mark.asyncio
    async def test_a_refused_tag_is_not_made(self, http: httpx.AsyncClient, commons: DiveSite) -> None:
        name = _marked("Refused")

        await http.patch(f"/dive-site/{commons.uuid}", json={"tags": [name]})

        assert (await http.get("/tags", params={"search": name})).json()["data"] == []

    @pytest.mark.asyncio
    async def test_the_caller_s_own_site_takes_every_member(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        site = create_dive_site(db, diver)
        name = _marked("Renamed")

        response = await http.patch(f"/dive-site/{site.uuid}", json={"name": name, "notes": "Mine"})

        assert response.status_code == 200, response.text
        read = await _read(http, site.uuid)
        assert (read["name"], read["notes"]) == (name, "Mine")


class TestRemoving:
    @pytest.mark.asyncio
    async def test_it_takes_the_caller_s_layer_and_leaves_the_record(
        self, http: httpx.AsyncClient, db: Session, diver: User, commons: DiveSite
    ) -> None:
        dive = create_dive(db, diver)
        await _save(http, commons.uuid, notes="Mine", tags=["Wreck"])
        db.add(DiveDiveSite(dive_id=dive.id, dive_site_id=commons.id, position=0))
        db.commit()

        response = await http.delete(f"/dive-site/{commons.uuid}")

        assert response.status_code == 200, response.text
        read = await _read(http, commons.uuid)
        assert (read["saved"], read["notes"], read["tags"], read["dive_count"]) == (False, "", [], 0)
        db.expire_all()
        assert db.scalars(select(DiveDiveSite).where(DiveDiveSite.dive_id == dive.id)).all() == []
        assert db.get(Dive, dive.id) is not None

    @pytest.mark.asyncio
    async def test_it_leaves_another_saver_s_layer_alone(
        self, http: httpx.AsyncClient, signed: Sign, db: Session, diver: User, other_diver: User, commons: DiveSite
    ) -> None:
        theirs = create_dive(db, other_diver)
        await _save(http, commons.uuid, notes="Mine", tags=["Wreck"])
        signed.as_(other_diver)
        await _save(http, commons.uuid, notes="Theirs", tags=["Night"])
        db.add(DiveDiveSite(dive_id=theirs.id, dive_site_id=commons.id, position=0))
        db.commit()
        signed.as_(diver)

        await http.delete(f"/dive-site/{commons.uuid}")

        signed.as_(other_diver)
        cache_module.client.store.clear()  # type: ignore[union-attr]
        read = await _read(http, commons.uuid)
        assert (read["saved"], read["notes"], read["tags"], read["dive_count"]) == (True, "Theirs", ["Night"], 1)

    @pytest.mark.asyncio
    async def test_a_record_the_caller_has_not_saved_is_a_404(self, http: httpx.AsyncClient, commons: DiveSite) -> None:
        assert (await http.delete(f"/dive-site/{commons.uuid}")).status_code == 404

    @pytest.mark.asyncio
    async def test_a_second_remove_is_a_404(self, http: httpx.AsyncClient, commons: DiveSite) -> None:
        await _save(http, commons.uuid)
        await http.delete(f"/dive-site/{commons.uuid}")

        assert (await http.delete(f"/dive-site/{commons.uuid}")).status_code == 404

    @pytest.mark.asyncio
    async def test_the_dives_can_move_to_another_record_which_is_saved_for_the_caller(
        self, http: httpx.AsyncClient, db: Session, diver: User, commons: DiveSite
    ) -> None:
        elsewhere = create_commons_site(db)
        dive = create_dive(db, diver)
        await _save(http, commons.uuid)
        db.add(DiveDiveSite(dive_id=dive.id, dive_site_id=commons.id, position=0))
        db.commit()

        response = await http.delete(f"/dive-site/{commons.uuid}", params={"move_dives_to": str(elsewhere.uuid)})

        assert response.status_code == 200, response.text
        read = await _read(http, elsewhere.uuid)
        assert (read["saved"], read["dive_count"]) == (True, 1)
        assert (await _read(http, commons.uuid))["saved"] is False

    @pytest.mark.asyncio
    async def test_an_own_site_s_dives_can_move_to_a_commons_record(
        self, http: httpx.AsyncClient, db: Session, diver: User, commons: DiveSite
    ) -> None:
        site = create_dive_site(db, diver)
        dive = create_dive(db, diver)
        db.add(DiveDiveSite(dive_id=dive.id, dive_site_id=site.id, position=0))
        db.commit()

        response = await http.delete(f"/dive-site/{site.uuid}", params={"move_dives_to": str(commons.uuid)})

        assert response.status_code == 200, response.text
        read = await _read(http, commons.uuid)
        assert (read["saved"], read["dive_count"]) == (True, 1)

    @pytest.mark.asyncio
    async def test_a_move_onto_another_account_s_site_is_a_422(
        self, http: httpx.AsyncClient, db: Session, diver: User, other_diver: User
    ) -> None:
        site = create_dive_site(db, diver)
        theirs = create_dive_site(db, other_diver)

        response = await http.delete(f"/dive-site/{site.uuid}", params={"move_dives_to": str(theirs.uuid)})

        assert response.status_code == 422


class TestTheLookup:
    @pytest.mark.asyncio
    async def test_without_a_search_it_never_lists_an_unsaved_record(
        self, http: httpx.AsyncClient, db: Session, diver: User, commons: DiveSite
    ) -> None:
        own = create_dive_site(db, diver)

        page = await _lookup(http)

        assert [row["uuid"] for row in page["data"]] == [str(own.uuid)]
        assert page["total_count"] == 1

    @pytest.mark.asyncio
    async def test_a_search_lists_saved_sites_then_unsaved_records(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        term = _marked()
        unsaved = create_commons_site(db, name=f"{term} Commons")
        saved = create_commons_site(db, name=f"{term} Saved Commons")
        own = create_dive_site(db, diver)
        own.name = f"{term} Own"
        db.commit()
        await _save(http, saved.uuid)

        page = await _lookup(http, search=term)

        listed = [(row["uuid"], row["saved"], row["owner"]) for row in page["data"]]
        assert listed == [
            (str(saved.uuid), True, "opendivingcommons"),
            (str(own.uuid), True, "account"),
            (str(unsaved.uuid), False, "opendivingcommons"),
        ]
        assert page["total_count"] == 3

    @pytest.mark.asyncio
    async def test_exact_names_come_before_prefixes_before_substrings(
        self, http: httpx.AsyncClient, db: Session
    ) -> None:
        term = _marked()
        inside = create_commons_site(db, name=f"AAA {term}")
        prefix = create_commons_site(db, name=f"{term} Point")
        exact = create_commons_site(db, name="ZZZ", other_names=[term.upper()])

        page = await _lookup(http, search=term.lower())

        assert [row["uuid"] for row in page["data"]] == [str(exact.uuid), str(prefix.uuid), str(inside.uuid)]

    @pytest.mark.asyncio
    async def test_within_a_match_the_nearer_comes_first_when_a_position_is_sent(
        self, http: httpx.AsyncClient, db: Session
    ) -> None:
        term = _marked()
        far = create_commons_site(db, name=term, latitude=4.2, longitude=73.5, location_name="Maldives")
        near = create_commons_site(db, name=term, latitude=5.9, longitude=102.7, location_name="Malaysia")

        from_malaysia = await _lookup(http, search=term, latitude=5.9, longitude=102.7)
        from_maldives = await _lookup(http, search=term, latitude=4.2, longitude=73.5)

        assert [row["uuid"] for row in from_malaysia["data"]] == [str(near.uuid), str(far.uuid)]
        assert [row["uuid"] for row in from_maldives["data"]] == [str(far.uuid), str(near.uuid)]

    @pytest.mark.asyncio
    async def test_half_a_position_is_a_422(self, http: httpx.AsyncClient) -> None:
        response = await http.get("/dive-sites/lookup", params={"search": "shark", "latitude": 4.1})

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_a_commons_record_is_not_found_by_its_locality_but_a_saved_site_is(
        self, http: httpx.AsyncClient, db: Session, commons: DiveSite
    ) -> None:
        town = _marked("Town")
        unsaved = create_commons_site(db, location_name=town)
        saved = create_commons_site(db, location_name=town)
        await _save(http, saved.uuid)

        rows = (await _lookup(http, search=town))["data"]

        assert [row["uuid"] for row in rows] == [str(saved.uuid)]
        assert str(unsaved.uuid) not in {row["uuid"] for row in rows}

    @pytest.mark.asyncio
    async def test_the_uuid_narrowing_reads_any_commons_record_and_no_other_account_s_site(
        self, http: httpx.AsyncClient, db: Session, other_diver: User, commons: DiveSite
    ) -> None:
        theirs = create_dive_site(db, other_diver)

        rows = (await _lookup(http, uuid=[str(commons.uuid), str(theirs.uuid)]))["data"]

        assert [(row["uuid"], row["saved"]) for row in rows] == [(str(commons.uuid), False)]

    @pytest.mark.asyncio
    async def test_a_row_carries_what_a_picker_and_a_place_search_read(
        self, http: httpx.AsyncClient, commons: DiveSite
    ) -> None:
        (row,) = (await _lookup(http, uuid=[str(commons.uuid)]))["data"]

        assert (row["latitude"], row["longitude"]) == (commons.latitude, commons.longitude)
        assert row["external_ids"] == commons.external_ids
        assert (row["owner"], row["attribution"]) == ("opendivingcommons", OSM_CREDIT)

    @pytest.mark.asyncio
    async def test_a_page_can_straddle_the_two_groups(self, http: httpx.AsyncClient, db: Session) -> None:
        term = _marked()
        saved = [create_commons_site(db, name=f"{term} A{index}") for index in range(2)]
        unsaved = [create_commons_site(db, name=f"{term} B{index}") for index in range(3)]
        for site in saved:
            await _save(http, site.uuid)

        pages = [await _lookup(http, search=term, page=page, items_per_page=2) for page in (1, 2, 3)]

        seen = [row["uuid"] for page in pages for row in page["data"]]
        assert seen[:2] != [] and set(seen[:2]) == {str(site.uuid) for site in saved}
        assert seen[2:] == [str(site.uuid) for site in unsaved]
        assert [page["has_more"] for page in pages] == [True, True, False]

    @pytest.mark.asyncio
    async def test_a_saved_record_is_ordered_by_when_it_was_saved_not_by_the_row(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        older_row = create_commons_site(db)
        own = create_dive_site(db, diver)
        await _save(http, older_row.uuid)

        rows = (await _lookup(http))["data"]

        assert [row["uuid"] for row in rows] == [str(older_row.uuid), str(own.uuid)]

    @pytest.mark.asyncio
    async def test_the_position_is_part_of_the_cached_key(self, http: httpx.AsyncClient, db: Session) -> None:
        term = _marked()
        far = create_commons_site(db, name=term, latitude=4.2, longitude=73.5)
        near = create_commons_site(db, name=term, latitude=5.9, longitude=102.7)

        first = await _lookup(http, search=term, latitude=5.9, longitude=102.7)
        second = await _lookup(http, search=term, latitude=4.2, longitude=73.5)

        assert first["data"][0]["uuid"] == str(near.uuid)
        assert second["data"][0]["uuid"] == str(far.uuid)


class TestNamingARecordOnADive:
    @pytest.mark.asyncio
    async def test_a_new_dive_saves_it_and_a_second_changes_nothing(
        self, http: httpx.AsyncClient, commons: DiveSite
    ) -> None:
        body = {
            "dive_number": 1,
            "start_time": "2026-01-02T09:00:00+00:00",
            "submerged_time": 2400,
            "dive_site_uuids": [str(commons.uuid)],
        }

        created = await http.post("/dive", json=body)
        assert created.status_code == 201, created.text
        await http.patch(f"/dive-site/{commons.uuid}", json={"notes": "Mine"})
        again = await http.post("/dive", json=body | {"dive_number": 2, "start_time": "2026-01-03T09:00:00+00:00"})

        assert again.status_code == 201, again.text
        read = await _read(http, commons.uuid)
        assert (read["saved"], read["notes"], read["dive_count"]) == (True, "Mine", 2)
        dive = (await http.get(f"/dive/{created.json()['uuid']}")).json()
        assert dive["dive_sites"][0]["uuid"] == str(commons.uuid)

    @pytest.mark.asyncio
    async def test_the_dive_s_summary_of_the_site_keeps_its_shape(
        self, http: httpx.AsyncClient, commons: DiveSite
    ) -> None:
        created = await http.post(
            "/dive",
            json={
                "dive_number": 1,
                "start_time": "2026-01-02T09:00:00+00:00",
                "submerged_time": 2400,
                "dive_site_uuids": [str(commons.uuid)],
            },
        )

        dive = (await http.get(f"/dive/{created.json()['uuid']}")).json()
        assert set(dive["dive_sites"][0]) == {"uuid", "name", "location", "latitude", "longitude"}

    @pytest.mark.asyncio
    async def test_a_patch_saves_it_too_and_the_site_lists_at_once(
        self, http: httpx.AsyncClient, db: Session, diver: User, commons: DiveSite
    ) -> None:
        await _list(http)
        dive = create_dive(db, diver)

        response = await http.patch(f"/dive/{dive.uuid}", json={"dive_site_uuids": [str(commons.uuid)]})

        assert response.status_code == 200, response.text
        assert str(commons.uuid) in {row["uuid"] for row in await _list(http)}

    @pytest.mark.asyncio
    async def test_another_account_s_private_site_stays_refused(
        self, http: httpx.AsyncClient, db: Session, other_diver: User
    ) -> None:
        theirs = create_dive_site(db, other_diver)

        response = await http.post(
            "/dive",
            json={
                "dive_number": 1,
                "start_time": "2026-01-02T09:00:00+00:00",
                "submerged_time": 2400,
                "dive_site_uuids": [str(theirs.uuid)],
            },
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_a_draft_keeps_naming_it_and_saves_nothing(self, http: httpx.AsyncClient, commons: DiveSite) -> None:
        drafted = await http.post("/dive-draft", json={"values": {"dive_site_uuids": [str(commons.uuid)]}})
        assert drafted.status_code == 201, drafted.text

        read = await http.get(f"/dive-draft/{drafted.json()['uuid']}")

        assert read.json()["values"]["dive_site_uuids"] == [str(commons.uuid)]
        assert (await _read(http, commons.uuid))["saved"] is False


class TestAFormHoldingAnUnsavedRecord:
    @pytest.mark.asyncio
    async def test_the_species_menu_takes_it(
        self, http: httpx.AsyncClient, db: Session, diver: User, commons: DiveSite
    ) -> None:
        response = await http.get("/user/species/suggest", params={"dive_site_uuid": str(commons.uuid)})

        assert response.status_code == 200, response.text

    @pytest.mark.asyncio
    async def test_the_conditions_lookup_takes_it(
        self, http: httpx.AsyncClient, commons: DiveSite, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "DIVE_CONDITIONS_PROVIDER", "open-meteo", raising=False)
        from src.app.api.v1 import dive_conditions

        preview = AsyncMock(return_value=None)
        monkeypatch.setattr(dive_conditions, "preview_conditions", preview)

        response = await http.get(
            "/dive-conditions/lookup",
            params={"dive_site_uuid": str(commons.uuid), "start_time": "2024-06-01T10:00:00+00:00"},
        )

        assert response.status_code == 503
        assert preview.await_args is not None
        assert preview.await_args.kwargs["latitude"] == commons.latitude


class TestPrivacy:
    """P1: nothing of one account's reaches another through a route that reads a Commons record."""

    @pytest_asyncio.fixture
    async def shared(
        self, http: httpx.AsyncClient, db: Session, diver: User, commons: DiveSite
    ) -> AsyncGenerator[DiveSite]:
        """The first account's whole layer on the record: a note, a tag, a rated dive."""
        species = create_species(db)
        created = await http.post(
            "/dive",
            json={
                "dive_number": 1,
                "start_time": "2026-01-02T09:00:00+00:00",
                "submerged_time": 2400,
                "dive_site_uuids": [str(commons.uuid)],
                "max_depth": 28.5,
                "rating": 5,
                "sightings": [{"species_uuid": str(species.uuid)}],
            },
        )
        assert created.status_code == 201, created.text
        await http.patch(f"/dive-site/{commons.uuid}", json={"notes": "Gate code 1234", "tags": ["Secret spot"]})
        yield commons

    @pytest.mark.asyncio
    async def test_the_second_account_reads_none_of_it(
        self, http: httpx.AsyncClient, signed: Sign, other_diver: User, shared: DiveSite
    ) -> None:
        signed.as_(other_diver)

        read = await _read(http, shared.uuid)

        assert (read["notes"], read["tags"], read["saved"]) == ("", [], False)
        assert (read["dive_count"], read["species_count"], read["last_dived_on"]) == (0, 0, None)
        assert (read["max_dive_depth"], read["average_rating"]) == (None, None)
        assert await _list(http) == []

    @pytest.mark.asyncio
    async def test_saving_it_brings_none_of_it_either(
        self, http: httpx.AsyncClient, signed: Sign, other_diver: User, shared: DiveSite
    ) -> None:
        signed.as_(other_diver)

        read = await _save(http, shared.uuid)

        assert (read["notes"], read["tags"], read["dive_count"], read["average_rating"]) == ("", [], 0, None)

    @pytest.mark.asyncio
    async def test_the_lookup_row_says_nothing_of_the_first_account(
        self, http: httpx.AsyncClient, signed: Sign, other_diver: User, shared: DiveSite
    ) -> None:
        signed.as_(other_diver)

        (row,) = (await _lookup(http, search=shared.name))["data"]

        assert row["saved"] is False
        assert "Gate code" not in json.dumps(row) and "Secret spot" not in json.dumps(row)

    @pytest.mark.asyncio
    async def test_re_tagging_from_one_side_leaves_the_other_s_tags(
        self, http: httpx.AsyncClient, signed: Sign, diver: User, other_diver: User, commons: DiveSite
    ) -> None:
        await _save(http, commons.uuid, tags=["Mine"])
        signed.as_(other_diver)
        await _save(http, commons.uuid, tags=["Theirs"])

        signed.as_(diver)
        await http.patch(f"/dive-site/{commons.uuid}", json={"tags": ["Mine again"]})
        mine = await _read(http, commons.uuid)
        signed.as_(other_diver)
        theirs = await _read(http, commons.uuid)

        assert (mine["tags"], theirs["tags"]) == (["Mine again"], ["Theirs"])

    @pytest.mark.asyncio
    async def test_the_tag_filter_reads_the_caller_s_vocabulary_alone(
        self, http: httpx.AsyncClient, signed: Sign, diver: User, other_diver: User, commons: DiveSite
    ) -> None:
        await _save(http, commons.uuid, tags=["Wreck"])
        (tag,) = (await http.get("/tags")).json()["data"]
        signed.as_(other_diver)
        await _save(http, commons.uuid)

        assert await _list(http, tag_uuid=tag["uuid"]) == []

    @pytest.mark.asyncio
    async def test_a_tag_s_count_is_its_own(
        self, http: httpx.AsyncClient, signed: Sign, other_diver: User, commons: DiveSite
    ) -> None:
        await _save(http, commons.uuid, tags=["Wreck"])
        signed.as_(other_diver)
        await _save(http, commons.uuid, tags=["Wreck"])

        (tag,) = (await http.get("/tags")).json()["data"]

        assert tag["site_count"] == 1

    @pytest.mark.asyncio
    async def test_the_export_carries_the_caller_s_layer_and_not_the_other_s(
        self,
        http: httpx.AsyncClient,
        signed: Sign,
        other_diver: User,
        shared: DiveSite,
        async_db: AsyncSession,
    ) -> None:
        signed.as_(other_diver)
        await _save(http, shared.uuid, notes="Theirs", tags=["Theirs too"])

        bundle = await load_export_bundle(async_db, user_id=other_diver.id)

        (site,) = bundle.dive_sites
        exported = _export_dive_site(bundle, site)
        assert (exported.notes, exported.tags) == ("Theirs", ["Theirs too"])

    @pytest.mark.asyncio
    async def test_the_private_site_of_another_account_stays_unreadable_on_every_route(
        self, http: httpx.AsyncClient, db: Session, other_diver: User
    ) -> None:
        """P2: nothing here reaches a private site, and the queue that does comes later."""
        theirs = create_dive_site(db, other_diver)
        uuid = str(theirs.uuid)

        answers = [
            (await http.get(f"/dive-site/{uuid}")).status_code,
            (await http.patch(f"/dive-site/{uuid}", json={"notes": "Mine"})).status_code,
            (await http.delete(f"/dive-site/{uuid}")).status_code,
            (await http.put(f"/dive-site/{uuid}/saved")).status_code,
        ]

        assert answers == [404, 404, 404, 404]
        assert (await _lookup(http, uuid=[uuid]))["data"] == []
        assert (await _lookup(http, search=theirs.name))["data"] == []


class TestThePublicExport:
    @pytest.fixture
    def primary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "COMMONS_PRIMARY", True)

    @pytest.fixture
    def anonymous(self, signed: Sign, sites_app: Any) -> None:
        del sites_app.dependency_overrides[get_current_user]

    def _feature(self, body: dict[str, Any], uuid: Any) -> dict[str, Any]:
        return next(feature for feature in body["features"] if feature["properties"]["uuid"] == str(uuid))

    @pytest.mark.asyncio
    async def test_it_is_a_404_on_a_copy(self, http: httpx.AsyncClient, anonymous: None) -> None:
        assert (await http.get("/commons/dive-sites")).status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("primary")
    async def test_it_answers_anyone_with_the_notice(self, http: httpx.AsyncClient, anonymous: None) -> None:
        response = await http.get("/commons/dive-sites")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["type"] == "FeatureCollection"
        assert body["licence"] == "ODbL 1.0"
        assert "© OpenStreetMap contributors" in body["notice"]

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("primary", "sync_engine")
    async def test_a_record_through_the_bundle_the_sync_and_the_route_is_the_record_that_went_in(
        self, http: httpx.AsyncClient, anonymous: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entry = {"registry": "openstreetmap", "identifier": f"node/{uuid_pkg.uuid4().int % 10**12 + 1}"}
        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [33.920048, 27.814092]},
            "properties": {
                "uuid": str(uuid_pkg.uuid4()),
                "name": _marked("Round trip"),
                "other_names": ["Thistlegorm"],
                "location": {"name": "South Sinai, Egypt"},
                "external_ids": [entry],
                "depth_from": 16.5,
                "depth_to": 30,
                "water_type": "salt",
                "altitude": 0,
                "entry_types": ["boat"],
                "extensions": {
                    "opendivingcommons": {
                        "source": "openstreetmap",
                        "license": "ODbL-1.0",
                        "retrieved": "2026-10-17",
                        "seeded_from": entry,
                    }
                },
            },
        }

        await _sync(monkeypatch, columns_of(feature), primary=True)

        body = (await http.get("/commons/dive-sites")).json()
        assert self._feature(body, feature["properties"]["uuid"]) == feature  # type: ignore[index]

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("primary")
    async def test_nothing_of_a_diver_s_reaches_it(
        self, http: httpx.AsyncClient, signed: Sign, sites_app: Any, db: Session, diver: User, commons: DiveSite
    ) -> None:
        """P3. And no private site either: it reads what the Commons owns."""
        private = create_dive_site(db, diver)
        await _save(http, commons.uuid, notes="Gate code 1234", tags=["Secret spot"])

        response = await http.get("/commons/dive-sites")

        assert "Gate code" not in response.text and "Secret spot" not in response.text
        assert private.name not in response.text
        assert set(self._feature(response.json(), commons.uuid)["properties"]) <= {
            "uuid",
            "name",
            "other_names",
            "location",
            "external_ids",
            "depth_from",
            "depth_to",
            "water_type",
            "altitude",
            "entry_types",
            "extensions",
        }

    def test_the_row_mapping_reads_only_the_shared_members(self) -> None:
        assert not set(commons_records._FACT_COLUMNS) & {"notes", "tags", "user_id", "created_at", "updated_at"}

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("primary")
    async def test_past_the_limit_it_is_a_429(
        self, http: httpx.AsyncClient, anonymous: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "COMMONS_EXPORT_RATE_LIMIT_PER_IP", 1)

        answers = [(await http.get("/commons/dive-sites")).status_code for _ in range(2)]

        assert answers == [200, 429]


class TestTheConfig:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("primary", [False, True])
    async def test_it_says_whether_this_is_the_primary(
        self, http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, primary: bool
    ) -> None:
        monkeypatch.setattr(settings, "COMMONS_PRIMARY", primary)

        assert (await http.get("/config")).json()["commons_primary"] is primary


# -------------- export and import --------------


async def _import(async_db: AsyncSession, user: User, document: dict[str, Any]) -> Any:
    with await load_one(json.dumps(document).encode(), "logbook.divejson") as loaded:
        plan = await plan_import(async_db, user_id=user.id, loaded=loaded, resolution_ran=True)
        await write_import(async_db, user_id=user.id, loaded=loaded, plan=plan)
        await async_db.commit()
        return plan


def _document(*sites: dict[str, Any], dives: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"format": "divejson", "version": "1.0", "sites": list(sites), "dives": dives or []}


class TestTheExport:
    @pytest.mark.asyncio
    async def test_a_saved_record_is_the_diver_s_own_record_naming_the_commons(
        self, db: Session, diver: User, async_db: AsyncSession, commons: DiveSite
    ) -> None:
        saved = save_dive_site(db, commons, notes="Gate code 1234", user=diver)

        bundle = await load_export_bundle(async_db, user_id=diver.id)
        exported = _export_dive_site(bundle, bundle.dive_sites[0])

        assert (exported.uuid, exported.notes, exported.created_at) == (
            commons.uuid,
            "Gate code 1234",
            saved.created_at,
        )
        entries = [(entry.registry, entry.identifier) for entry in exported.external_ids or []]
        assert entries == [
            ("openstreetmap", commons.commons_seed_identifier),
            ("opendivingcommons", str(commons.uuid)),
        ]

    @pytest.mark.asyncio
    async def test_a_private_site_names_no_commons(self, db: Session, diver: User, async_db: AsyncSession) -> None:
        create_dive_site(db, diver)

        bundle = await load_export_bundle(async_db, user_id=diver.id)

        assert bundle.external_ids_for(bundle.dive_sites[0]) == []

    @pytest.mark.asyncio
    async def test_the_csv_follows_the_same_rule(
        self, db: Session, diver: User, async_db: AsyncSession, commons: DiveSite
    ) -> None:
        save_dive_site(db, commons, user=diver)

        bundle = await load_export_bundle(async_db, user_id=diver.id)

        assert f"opendivingcommons:{commons.uuid}" in "".join(write_dive_sites_csv(bundle))


def _saved_row(db: Session, user: User, site: DiveSite) -> SavedSite | None:
    db.expire_all()
    return db.scalars(
        select(SavedSite).where(SavedSite.user_id == user.id, SavedSite.dive_site_id == site.id)
    ).one_or_none()


class TestTheImport:
    @pytest.mark.asyncio
    async def test_a_site_naming_a_record_by_uuid_saves_it_with_the_notes_and_tags(
        self, db: Session, diver: User, async_db: AsyncSession, commons: DiveSite
    ) -> None:
        site = {"uuid": str(commons.uuid), "name": "Called something else", "notes": "Gate code", "tags": ["Wreck"]}

        await _import(async_db, diver, _document(site))

        saved = _saved_row(db, diver, commons)
        assert saved is not None and saved.notes == "Gate code"
        assert db.scalars(select(DiveSite).where(DiveSite.user_id == diver.id)).all() == []
        assert db.get(DiveSite, commons.id).name == commons.name  # type: ignore[union-attr]
        tags = db.scalars(select(DiveSiteTag).where(DiveSiteTag.dive_site_id == commons.id)).all()
        assert len(tags) == 1

    @pytest.mark.asyncio
    async def test_a_site_naming_a_record_by_the_registry_entry_saves_it(
        self, db: Session, diver: User, async_db: AsyncSession, commons: DiveSite
    ) -> None:
        site_uuid = str(uuid_pkg.uuid4())
        dive_uuid = str(uuid_pkg.uuid4())
        site = {
            "uuid": site_uuid,
            "name": "Mine",
            "external_ids": [{"registry": "opendivingcommons", "identifier": str(commons.uuid)}],
        }
        dive = {
            "uuid": dive_uuid,
            "date_time": "2026-01-02T09:00:00+00:00",
            "sites": [site_uuid],
            "site_uuids": [site_uuid],
        }

        await _import(async_db, diver, _document(site, dives=[dive]))

        assert _saved_row(db, diver, commons) is not None
        assert db.scalars(select(DiveSite).where(DiveSite.user_id == diver.id)).all() == []

    @pytest.mark.asyncio
    async def test_a_record_already_saved_links_and_writes_nothing(
        self, db: Session, diver: User, async_db: AsyncSession, commons: DiveSite
    ) -> None:
        save_dive_site(db, commons, notes="Mine", user=diver)
        site = {"uuid": str(commons.uuid), "name": commons.name, "notes": "From the file", "tags": ["Wreck"]}

        plan = await _import(async_db, diver, _document(site))

        saved = _saved_row(db, diver, commons)
        assert saved is not None and saved.notes == "Mine"
        assert db.scalars(select(DiveSiteTag).where(DiveSiteTag.dive_site_id == commons.id)).all() == []
        assert plan.commons_to_save() == []

    @pytest.mark.asyncio
    async def test_two_sites_of_a_document_naming_one_record_save_it_once(
        self, db: Session, diver: User, async_db: AsyncSession, commons: DiveSite
    ) -> None:
        entry = {"registry": "opendivingcommons", "identifier": str(commons.uuid)}
        document = _document(
            {"uuid": str(commons.uuid), "name": "One", "notes": "First"},
            {"uuid": str(uuid_pkg.uuid4()), "name": "Two", "notes": "Second", "external_ids": [entry]},
        )

        await _import(async_db, diver, document)

        saved = _saved_row(db, diver, commons)
        assert saved is not None and saved.notes == "First"

    @pytest.mark.asyncio
    async def test_an_export_imported_into_a_fresh_account_saves_the_record(
        self, db: Session, diver: User, async_db: AsyncSession, commons: DiveSite
    ) -> None:
        save_dive_site(db, commons, notes="Gate code 1234", user=diver)
        bundle = await load_export_bundle(async_db, user_id=diver.id)
        exported = _export_dive_site(bundle, bundle.dive_sites[0]).model_dump(mode="json", exclude_none=True)
        fresh = create_user(db)

        await _import(async_db, fresh, _document(exported))

        saved = _saved_row(db, fresh, commons)
        assert saved is not None and saved.notes == "Gate code 1234"
        assert db.scalars(select(DiveSite).where(DiveSite.user_id == fresh.id)).all() == []

    @pytest.mark.asyncio
    async def test_an_openstreetmap_entry_never_links_to_a_record_the_caller_has_not_saved(
        self, db: Session, diver: User, async_db: AsyncSession, commons: DiveSite
    ) -> None:
        site = {"uuid": str(uuid_pkg.uuid4()), "name": _marked("Mine"), "external_ids": commons.external_ids}

        await _import(async_db, diver, _document(site))

        assert _saved_row(db, diver, commons) is None
        assert len(db.scalars(select(DiveSite).where(DiveSite.user_id == diver.id)).all()) == 1

    @pytest.mark.asyncio
    async def test_a_saved_record_matches_by_name_and_locality_and_an_unsaved_one_does_not(
        self, db: Session, diver: User, async_db: AsyncSession, commons: DiveSite
    ) -> None:
        unsaved = create_commons_site(db, location_name="Elsewhere")
        save_dive_site(db, commons, user=diver)
        document = _document(
            {"uuid": str(uuid_pkg.uuid4()), "name": commons.name, "location": {"name": commons.location_name}},
            {"uuid": str(uuid_pkg.uuid4()), "name": unsaved.name, "location": {"name": "Elsewhere"}},
        )

        await _import(async_db, diver, document)

        created = db.scalars(select(DiveSite.name).where(DiveSite.user_id == diver.id)).all()
        assert created == [unsaved.name]

    @pytest.mark.asyncio
    async def test_a_document_naming_nothing_imports_as_it_always_did(
        self, db: Session, diver: User, async_db: AsyncSession
    ) -> None:
        name = _marked("Mine")

        await _import(async_db, diver, _document({"uuid": str(uuid_pkg.uuid4()), "name": name, "notes": "Mine"}))

        site = db.scalars(select(DiveSite).where(DiveSite.user_id == diver.id)).one()
        assert site.name == name
        assert _saved_row(db, diver, site) is not None
