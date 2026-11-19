"""Custom properties: the diver's own key-value pairs on the records they own.

The schema half runs without a database. The route half creates, reads and patches each of the
nine records through the API against real Postgres, since what it guards is that every route
writes the list it was sent and reads the stored one back - which a mocked session cannot tell -
and is automatically skipped if no database is reachable, as `test_dive_check_constraints.py`.
"""

import json
import uuid as uuid_pkg
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.setup import create_application
from src.app.core.utils import cache as cache_module
from src.app.crud.crud_users import read_account
from src.app.models.user import User
from src.app.schemas.custom_property import (
    CUSTOM_PROPERTIES_MAX,
    CustomPropertiesUpdate,
    CustomPropertiesWrite,
    merge_custom_properties,
)
from src.app.services.export import load_export_bundle, write_divejson
from src.app.services.logbook_import import plan_import, write_import
from tests.conftest import db_available
from tests.helpers.fake_redis import FakeRedis
from tests.helpers.import_parts import load_one

PROPERTIES = [{"key": "Firmware", "value": "2.4.1"}, {"key": "Serial", "value": "SN-1182"}]


class TestTheSchema:
    def test_members_are_trimmed_and_the_order_kept(self) -> None:
        written = CustomPropertiesWrite.model_validate(
            {"custom_properties": [{"key": " Serial ", "value": " SN-1182 "}, {"key": "Firmware", "value": "2.4.1"}]}
        )

        assert [prop.model_dump() for prop in written.custom_properties] == [
            {"key": "Serial", "value": "SN-1182"},
            {"key": "Firmware", "value": "2.4.1"},
        ]

    def test_omitted_is_none(self) -> None:
        assert CustomPropertiesWrite.model_validate({}).custom_properties == []

    @pytest.mark.parametrize(
        "prop",
        [
            {"key": "", "value": "x"},
            {"key": "   ", "value": "x"},
            {"key": "Serial", "value": ""},
            {"key": "Serial"},
            {"key": "k" * 101, "value": "x"},
            {"key": "Serial", "value": "v" * 1001},
            {"key": "Serial", "value": "x", "unit": "bar"},
        ],
    )
    def test_a_property_a_record_cannot_hold_is_refused(self, prop: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            CustomPropertiesWrite.model_validate({"custom_properties": [prop]})

    def test_a_key_given_twice_ignoring_case_is_refused_on_both_writes(self) -> None:
        twice = {"custom_properties": [{"key": "Serial", "value": "1"}, {"key": " SERIAL", "value": "2"}]}

        for schema in (CustomPropertiesWrite, CustomPropertiesUpdate):
            with pytest.raises(ValidationError, match="twice"):
                schema.model_validate(twice)

    def test_more_than_a_record_holds_is_refused(self) -> None:
        many = [{"key": f"Key {index}", "value": "x"} for index in range(CUSTOM_PROPERTIES_MAX + 1)]

        with pytest.raises(ValidationError):
            CustomPropertiesWrite.model_validate({"custom_properties": many})


class TestMerging:
    def test_a_key_both_hold_keeps_its_place_and_both_values(self) -> None:
        held = [{"key": "Serial", "value": "A1"}, {"key": "Firmware", "value": "2.4"}]
        joining = [{"key": "firmware", "value": "2.4"}, {"key": "serial", "value": "B2"}, {"key": "Shop", "value": "X"}]

        assert merge_custom_properties(held, joining) == [
            {"key": "Serial", "value": "A1; B2"},
            {"key": "Firmware", "value": "2.4"},
            {"key": "Shop", "value": "X"},
        ]

    def test_neither_list_is_changed(self) -> None:
        held = [{"key": "Serial", "value": "A1"}]
        joining = [{"key": "Serial", "value": "B2"}]

        merge_custom_properties(held, joining)

        assert (held, joining) == ([{"key": "Serial", "value": "A1"}], [{"key": "Serial", "value": "B2"}])


# ---------------------------------------------------------------- through the routes


def _marked(label: str) -> str:
    return f"{label} {uuid_pkg.uuid4().hex[:10]}"


# Each record: where it is created and read, where it is listed, and the least body a create takes.
RECORDS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "dive": (
        "dive",
        "dives",
        {"dive_number": 1, "start_time": "2026-03-02T09:15:00+02:00", "submerged_time": 2400, "max_depth": 18.2},
    ),
    "dive site": ("dive-site", "dive-sites", {"name": "Site"}),
    "trip": ("trip", "trips", {"name": "Trip"}),
    "course": ("course", "courses", {"name": "Course", "status": "completed"}),
    "gear item": ("gear-item", "gear-items", {"name": "Regulator", "type": "regulator"}),
    "gear set": ("gear-set", "gear-sets", {"name": "Set"}),
    "person": ("person", "people", {"name": "Person"}),
    "contact": ("contact", "contacts", {"name": "Contact"}),
    "certification": ("certification", "certifications", {"agency": "padi", "name": "Open Water Diver"}),
}


@pytest.fixture(scope="module")
def properties_app() -> Any:
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


@pytest_asyncio.fixture
async def http(
    properties_app: Any,
    async_sessions: async_sessionmaker[AsyncSession],
    diver: User,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[httpx.AsyncClient]:
    monkeypatch.setattr(cache_module, "client", FakeRedis())
    async with async_sessions() as session:
        account = await read_account(session, uuid=diver.uuid)
    properties_app.dependency_overrides[get_current_user] = lambda: account

    async def per_request() -> AsyncGenerator[AsyncSession]:
        async with async_sessions() as session:
            yield session

    properties_app.dependency_overrides[async_get_db] = per_request
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=properties_app), base_url="http://test/api/v1"
    ) as client:
        yield client
    properties_app.dependency_overrides = {}


async def _create(http: httpx.AsyncClient, record: str, **extra: Any) -> tuple[str, dict[str, Any]]:
    path, _, body = RECORDS[record]
    if "name" in body:
        body = {**body, "name": _marked(body["name"])}
    response = await http.post(f"/{path}", json={**body, **extra})
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return f"/{path}/{created['uuid']}", created


async def _read(http: httpx.AsyncClient, url: str) -> list[dict[str, str]]:
    response = await http.get(url)
    assert response.status_code == 200, response.text
    properties: list[dict[str, str]] = response.json()["custom_properties"]
    return properties


@pytest.mark.skipif(not db_available(), reason="No database connection available")
@pytest.mark.parametrize("record", RECORDS)
class TestEveryRecord:
    @pytest.mark.asyncio
    async def test_a_create_stores_them_in_order_and_a_read_gives_them_back(
        self, http: httpx.AsyncClient, record: str
    ) -> None:
        url, created = await _create(http, record, custom_properties=PROPERTIES)

        assert created["custom_properties"] == PROPERTIES
        assert await _read(http, url) == PROPERTIES

    @pytest.mark.asyncio
    async def test_the_list_carries_them(self, http: httpx.AsyncClient, record: str) -> None:
        _, created = await _create(http, record, custom_properties=PROPERTIES)

        response = await http.get(f"/{RECORDS[record][1]}", params={"items_per_page": 100})

        assert response.status_code == 200, response.text
        (listed,) = [row for row in response.json()["data"] if row["uuid"] == created["uuid"]]
        assert listed["custom_properties"] == PROPERTIES

    @pytest.mark.asyncio
    async def test_a_record_created_without_them_has_none(self, http: httpx.AsyncClient, record: str) -> None:
        url, _ = await _create(http, record)

        assert await _read(http, url) == []

    @pytest.mark.asyncio
    async def test_a_patch_replaces_them_whole_and_leaves_them_when_omitted(
        self, http: httpx.AsyncClient, record: str
    ) -> None:
        url, _ = await _create(http, record, custom_properties=PROPERTIES)
        replaced = [{"key": "Bought at", "value": "Blue Ocean"}]

        assert (await http.patch(url, json={"custom_properties": replaced})).status_code == 200
        assert await _read(http, url) == replaced

        other = {"notes": "Edited"} if record == "dive" else {"name": _marked("Renamed")}
        assert (await http.patch(url, json=other)).status_code == 200
        assert await _read(http, url) == replaced

        assert (await http.patch(url, json={"custom_properties": []})).status_code == 200
        assert await _read(http, url) == []

    @pytest.mark.asyncio
    async def test_a_patch_with_null_is_refused_and_writes_nothing(self, http: httpx.AsyncClient, record: str) -> None:
        url, _ = await _create(http, record, custom_properties=PROPERTIES)

        response = await http.patch(url, json={"custom_properties": None})

        assert response.status_code == 422
        assert await _read(http, url) == PROPERTIES


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestASite:
    @pytest.mark.asyncio
    async def test_they_are_the_diver_s_on_a_commons_record_they_saved(
        self, http: httpx.AsyncClient, db: Session
    ) -> None:
        from tests.helpers.generators import create_commons_site

        record = create_commons_site(db)
        url = f"/dive-site/{record.uuid}"

        saved = await http.put(f"{url}/saved", json={"custom_properties": PROPERTIES})
        assert saved.status_code == 200, saved.text
        assert await _read(http, url) == PROPERTIES

        assert (await http.patch(url, json={"custom_properties": []})).status_code == 200
        assert await _read(http, url) == []

    @pytest.mark.asyncio
    async def test_another_diver_reading_the_record_sees_none_of_them(
        self,
        http: httpx.AsyncClient,
        db: Session,
        properties_app: Any,
        async_sessions: async_sessionmaker[AsyncSession],
        other_diver: User,
    ) -> None:
        from tests.helpers.generators import create_commons_site

        record = create_commons_site(db)
        url = f"/dive-site/{record.uuid}"
        await http.put(f"{url}/saved", json={"custom_properties": PROPERTIES})

        async with async_sessions() as session:
            other = await read_account(session, uuid=other_diver.uuid)
        properties_app.dependency_overrides[get_current_user] = lambda: other

        assert await _read(http, url) == []

    @pytest.mark.asyncio
    async def test_replacing_the_diver_s_site_with_a_record_joins_them_to_what_they_had_there(
        self, http: httpx.AsyncClient, db: Session
    ) -> None:
        from tests.helpers.generators import create_commons_site

        record = create_commons_site(db)
        held = [{"key": "Serial", "value": "A1"}, {"key": "Mooring", "value": "Buoy 4"}]
        await http.put(f"/dive-site/{record.uuid}/saved", json={"custom_properties": held})
        site_url, _ = await _create(
            http, "dive site", custom_properties=[{"key": "serial", "value": "B2"}, {"key": "Firmware", "value": "2"}]
        )

        response = await http.post(f"{site_url}/replace", json={"commons_site_uuid": str(record.uuid)})

        assert response.status_code == 200, response.text
        assert response.json()["custom_properties"] == [
            {"key": "Serial", "value": "A1; B2"},
            {"key": "Mooring", "value": "Buoy 4"},
            {"key": "Firmware", "value": "2"},
        ]


# ---------------------------------------------------------------- export and import


async def _export(async_db: AsyncSession, user_id: int) -> dict[str, Any]:
    bundle = await load_export_bundle(async_db, user_id=user_id)
    chunks = [chunk async for chunk in write_divejson(async_db, bundle, exported_at=datetime.now(UTC))]
    await async_db.rollback()
    document: dict[str, Any] = json.loads(b"".join(chunks))
    return document


async def _import(async_db: AsyncSession, user_id: int, document: dict[str, Any]) -> list[Any]:
    with await load_one(json.dumps(document).encode(), "logbook.divejson") as loaded:
        plan = await plan_import(async_db, user_id=user_id, loaded=loaded, resolution_ran=True, claim_links=True)
        await write_import(async_db, user_id=user_id, loaded=loaded, plan=plan)
        await async_db.commit()
    return plan.notes


# Each collection's record, as `RECORDS` names it.
COLLECTIONS = {
    "dives": "dive",
    "sites": "dive site",
    "trips": "trip",
    "courses": "course",
    "gear": "gear item",
    "gear_sets": "gear set",
    "people": "person",
    "contacts": "contact",
    "certifications": "certification",
}


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheDiveJsonExport:
    @pytest.mark.asyncio
    async def test_every_record_carries_them_under_this_producer_s_key_and_an_import_keeps_them(
        self, http: httpx.AsyncClient, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        for record in COLLECTIONS.values():
            await _create(http, record, custom_properties=PROPERTIES)

        document = await _export(async_db, diver.id)

        for collection in COLLECTIONS:
            carried = [
                entry["extensions"]["opendiving"]["custom_properties"]
                for entry in document[collection]
                if "custom_properties" in (entry.get("extensions") or {}).get("opendiving", {})
            ]
            assert carried == [PROPERTIES], collection

        await _import(async_db, other_diver.id, document)
        imported = await _export(async_db, other_diver.id)

        for collection in COLLECTIONS:
            assert [entry["extensions"]["opendiving"]["custom_properties"] for entry in imported[collection]] == [
                PROPERTIES
            ], collection

    @pytest.mark.asyncio
    async def test_an_import_drops_what_a_write_would_refuse_and_says_so(
        self, async_db: AsyncSession, other_diver: User
    ) -> None:
        trip_uuid = str(uuid_pkg.uuid4())
        document = {
            "format": "divejson",
            "version": "1.0",
            "trips": [
                {
                    "uuid": trip_uuid,
                    "name": _marked("Imported"),
                    "extensions": {
                        "opendiving": {
                            "custom_properties": [
                                {"key": " Booking ", "value": " BO-1 "},
                                {"key": "booking", "value": "again"},
                                {"key": "", "value": "no key"},
                                "not an object",
                            ]
                        },
                        "com.example": {"custom_properties": [{"key": "Theirs", "value": "x"}]},
                    },
                }
            ],
        }

        notes = await _import(async_db, other_diver.id, document)

        (trip,) = [entry for entry in (await _export(async_db, other_diver.id))["trips"] if entry["uuid"] == trip_uuid]
        assert trip["extensions"]["opendiving"]["custom_properties"] == [{"key": "Booking", "value": "BO-1"}]
        assert any("3 of its custom properties" in note.message for note in notes)

    @pytest.mark.asyncio
    async def test_another_writer_s_entry_under_the_key_reads_as_none(
        self, async_db: AsyncSession, other_diver: User
    ) -> None:
        trip_uuid = str(uuid_pkg.uuid4())
        document = {
            "format": "divejson",
            "version": "1.0",
            "trips": [
                {
                    "uuid": trip_uuid,
                    "name": _marked("Imported"),
                    "extensions": {"opendiving": {"custom_properties": {"Booking": "BO-1"}}},
                }
            ],
        }

        await _import(async_db, other_diver.id, document)

        (trip,) = [entry for entry in (await _export(async_db, other_diver.id))["trips"] if entry["uuid"] == trip_uuid]
        assert "extensions" not in trip
