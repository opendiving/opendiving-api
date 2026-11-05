"""A site reaches a diver through their `saved_site` row, which holds their notes.

The revision runs for real against a database of its own (`tests/helpers/migrations.py`); the
rest read and write through the routes and helpers against the suite's. A site with no saved row
is what the previous build inserts during the deploy that ships this table, so the classes below
use one to show that every read starts at the saved row rather than at `dive_site.user_id`.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import uuid as uuid_pkg
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.api.v1 import dive_sites as dive_sites_module
from src.app.core.exceptions.http_exceptions import NotFoundException
from src.app.crud.crud_dive_sites import (
    get_dive_sites_page,
    get_saved_dive_site,
    resolve_dive_site_ids_for_user,
    sites_by_external_id,
)
from src.app.models.dive_site import DiveSite
from src.app.models.saved_site import SavedSite
from src.app.models.user import User
from src.app.schemas.dive_site import DiveSiteCreate, DiveSiteListSort, DiveSiteRead, DiveSiteUpdateRequest
from src.app.services import dive_drafts
from src.app.services.export.loader import load_export_bundle
from tests.conftest import db_available
from tests.helpers.generators import create_dive_site, create_user
from tests.helpers.migrations import migrate, scratch_database, template_database

pytestmark = [pytest.mark.skipif(not db_available(), reason="No database connection available")]

_REVISION = "63b752e6b762"
_BELOW = "3b5134d444b3"

CREATED = datetime(2024, 3, 1, 9, 30, tzinfo=UTC)

SEED = """
INSERT INTO "user" (id, name, username, email, is_superuser, is_deleted, uuid, created_at)
VALUES (1, 'Seed', 'seed', 'seed@example.com', false, false, gen_random_uuid(), now()),
       (2, 'Other', 'other', 'other@example.com', false, false, gen_random_uuid(), now());
INSERT INTO dive_site (id, user_id, name, notes, uuid, created_at)
VALUES (1, 1, 'Blue Hole', 'Gate code 1234', gen_random_uuid(), '2024-03-01 09:30+00'),
       (2, 1, 'Canyon', '', gen_random_uuid(), '2024-03-02 09:30+00'),
       (3, 2, 'Blue Hole', 'Their own words', gen_random_uuid(), '2024-03-03 09:30+00')
"""


@pytest.fixture(scope="module")
def below() -> Iterator[str]:
    with template_database(_BELOW) as template:
        yield template


@pytest.fixture
def scratch(below: str) -> Iterator[tuple[str, Engine]]:
    with scratch_database("saved_site", below) as database:
        name, engine = database
        with engine.begin() as connection:
            for statement in SEED.split(";\n"):
                connection.execute(text(statement))
        yield name, engine


@pytest.mark.xdist_group(__name__)
class TestTheRevision:
    def test_every_site_gains_one_saved_row_for_its_owner_with_its_notes_and_its_date(
        self, scratch: tuple[str, Engine]
    ) -> None:
        name, engine = scratch

        migrate(name, "upgrade", _REVISION)

        with engine.connect() as connection:
            saved = connection.execute(
                text(
                    "SELECT dive_site_id, user_id, notes, to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') "
                    "FROM saved_site ORDER BY dive_site_id"
                )
            ).all()
        assert [tuple(row) for row in saved] == [
            (1, 1, "Gate code 1234", "2024-03-01"),
            (2, 1, "", "2024-03-02"),
            (3, 2, "Their own words", "2024-03-03"),
        ]

    def test_the_column_stays_and_a_site_inserted_without_it_takes_the_default(
        self, scratch: tuple[str, Engine]
    ) -> None:
        name, engine = scratch

        migrate(name, "upgrade", _REVISION)

        with engine.begin() as connection:
            kept = connection.scalar(text("SELECT notes FROM dive_site WHERE id = 1"))
            connection.execute(
                text(
                    "INSERT INTO dive_site (id, user_id, name, uuid, created_at) "
                    "VALUES (4, 1, 'New', gen_random_uuid(), now())"
                )
            )
            inserted = connection.scalar(text("SELECT notes FROM dive_site WHERE id = 4"))
        assert (kept, inserted) == ("Gate code 1234", "")

    def test_the_downgrade_copies_the_notes_back_and_takes_the_table(self, scratch: tuple[str, Engine]) -> None:
        name, engine = scratch
        migrate(name, "upgrade", _REVISION)
        with engine.begin() as connection:
            connection.execute(text("UPDATE saved_site SET notes = 'Rewritten' WHERE dive_site_id = 2"))

        migrate(name, "downgrade", _BELOW)

        with engine.connect() as connection:
            notes = connection.execute(text("SELECT notes FROM dive_site ORDER BY id")).scalars().all()
            table = connection.scalar(text("SELECT to_regclass('saved_site')"))
        assert (notes, table) == (["Gate code 1234", "Rewritten", "Their own words"], None)


class TestTheMapper:
    def test_the_notes_are_on_the_table_and_off_the_mapper(self) -> None:
        assert "notes" in DiveSite.__table__.columns
        assert "notes" not in inspect(DiveSite).columns


def _as(user: User) -> dict[str, Any]:
    return {"id": user.id, "uuid": user.uuid, "username": user.username, "is_superuser": False}


async def _read(async_db: AsyncSession, user: User, uuid: Any) -> DiveSiteRead:
    read = await dive_sites_module._cached_read_dive_site.__wrapped__(  # type: ignore[attr-defined]
        None, user_id=user.id, uuid=uuid, owner_uuid=user.uuid, db=async_db
    )
    return DiveSiteRead.model_validate(read)


async def _patch(async_db: AsyncSession, user: User, uuid: Any, body: dict[str, Any]) -> None:
    await dive_sites_module.patch_dive_site(
        request=MagicMock(),
        uuid=uuid,
        values=DiveSiteUpdateRequest.model_validate(body),
        current_user=_as(user),
        db=async_db,
    )


def _on_nobody_s_list(db: Session, owner: User) -> DiveSite:
    site = DiveSite(
        user_id=owner.id,
        name=f"Unlisted {uuid_pkg.uuid4().hex[:8]}",
        external_ids=[{"registry": "wikidata", "identifier": "Q1047347"}],
    )
    db.add(site)
    db.commit()
    return site


@pytest.fixture
def no_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("invalidate_dive_site_caches", "invalidate_dive_caches", "invalidate_trip_caches"):
        monkeypatch.setattr(dive_sites_module, name, AsyncMock())


@pytest.mark.usefixtures("no_caches")
class TestTheRoutes:
    @pytest.mark.asyncio
    async def test_a_create_writes_the_saved_row_and_leaves_the_site_s_column_empty(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        created = await dive_sites_module.write_dive_site(
            request=MagicMock(),
            dive_site=DiveSiteCreate.model_validate(
                {"name": f"Canyon {uuid_pkg.uuid4().hex[:8]}", "notes": "Gate code 1234"}
            ),
            current_user=_as(diver),
            db=async_db,
        )

        saved = db.execute(select(SavedSite).join(DiveSite).where(DiveSite.uuid == created.uuid)).scalars().one()
        column = db.scalar(text("SELECT notes FROM dive_site WHERE id = :id"), {"id": saved.dive_site_id})
        assert (saved.user_id, saved.notes, saved.created_at) == (diver.id, "Gate code 1234", created.created_at)
        assert column == ""
        assert created.notes == "Gate code 1234"

    @pytest.mark.asyncio
    async def test_a_read_answers_the_saved_row_s_notes_and_date(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        site = create_dive_site(db, diver)
        saved = db.execute(select(SavedSite).where(SavedSite.dive_site_id == site.id)).scalars().one()
        saved.notes, saved.created_at = "Mine", CREATED
        db.commit()

        read = await _read(async_db, diver, site.uuid)
        page = await get_dive_sites_page(
            async_db, user_id=diver.id, offset=0, limit=50, search=None, tag_id=None, sort=DiveSiteListSort.NAME
        )

        assert (read.notes, read.created_at) == ("Mine", CREATED)
        assert [(row["notes"], row["created_at"]) for row in page["data"]] == [("Mine", CREATED)]
        assert page["total_count"] == 1

    @pytest.mark.asyncio
    async def test_a_patch_of_the_notes_writes_the_saved_row_and_no_other(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        site = create_dive_site(db, diver)
        name = site.name

        await _patch(async_db, diver, site.uuid, {"notes": "Mine"})

        db.expire_all()
        saved = db.execute(select(SavedSite).where(SavedSite.dive_site_id == site.id)).scalars().one()
        assert (saved.notes, saved.updated_at is not None) == ("Mine", True)
        assert db.scalar(text("SELECT notes FROM dive_site WHERE id = :id"), {"id": site.id}) == ""
        assert db.get(DiveSite, site.id).name == name  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_a_patch_of_the_other_members_leaves_the_notes(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        site = create_dive_site(db, diver)
        await _patch(async_db, diver, site.uuid, {"notes": "Mine"})

        await _patch(async_db, diver, site.uuid, {"altitude": 372})

        read = await _read(async_db, diver, site.uuid)
        assert (read.altitude, read.notes) == (372, "Mine")

    @pytest.mark.asyncio
    async def test_a_delete_takes_the_saved_row(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        site = create_dive_site(db, diver)

        await dive_sites_module.erase_dive_site(
            request=MagicMock(), uuid=site.uuid, current_user=_as(diver), db=async_db, move_dives_to=None
        )

        assert db.execute(select(SavedSite).where(SavedSite.dive_site_id == site.id)).first() is None


@pytest.mark.usefixtures("no_caches")
class TestASiteNoSavedRowNames:
    """Its owner's id is on it and nothing reaches it, because nothing starts there."""

    @pytest.mark.asyncio
    async def test_every_read_passes_it_by(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        site = _on_nobody_s_list(db, diver)

        page = await get_dive_sites_page(
            async_db, user_id=diver.id, offset=0, limit=50, search=None, tag_id=None, sort=DiveSiteListSort.NAME
        )

        assert (page["data"], page["total_count"]) == ([], 0)
        assert await get_saved_dive_site(async_db, uuid=site.uuid, user_id=diver.id) is None
        assert await resolve_dive_site_ids_for_user(async_db, [site.uuid], diver.id) is None
        assert await sites_by_external_id(async_db, user_id=diver.id) == {}
        assert (await load_export_bundle(async_db, user_id=diver.id)).dive_sites == []
        assert await dive_drafts._held(async_db, {DiveSite: {site.uuid}}, user_id=diver.id) == {DiveSite: set()}
        with pytest.raises(NotFoundException):
            await _read(async_db, diver, site.uuid)

    @pytest.mark.asyncio
    async def test_a_patch_of_its_notes_is_a_404_and_not_a_write_that_went_nowhere(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        site = _on_nobody_s_list(db, diver)

        with pytest.raises(NotFoundException):
            await _patch(async_db, diver, site.uuid, {"notes": "Mine"})


class TestAnotherDiver:
    @pytest.mark.asyncio
    async def test_their_list_does_not_reach_mine(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        other = create_user(db)
        site = create_dive_site(db, diver)

        assert await get_saved_dive_site(async_db, uuid=site.uuid, user_id=other.id) is None
        assert await resolve_dive_site_ids_for_user(async_db, [site.uuid], other.id) is None


class TestTheExport:
    @pytest.mark.asyncio
    async def test_it_reads_the_saved_row_s_notes_and_date(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        site = create_dive_site(db, diver)
        saved = db.execute(select(SavedSite).where(SavedSite.dive_site_id == site.id)).scalars().one()
        saved.notes, saved.created_at = "Mine", CREATED
        db.commit()

        bundle = await load_export_bundle(async_db, user_id=diver.id)

        assert [row.id for row in bundle.dive_sites] == [site.id]
        held = bundle.saved_by_site[site.id]
        assert (held.notes, held.created_at) == ("Mine", CREATED)
