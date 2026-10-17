"""A per-user unique name is a 422 when it collides, whatever whitespace it arrives with.

Over HTTP against Postgres, because the difference between a 422 and a 500 lives at the
wire and the agreement between `unique_key_taken` and the index lives in the database. The
write schemas trim (`Trimmed`), the probe asks the index's own expressions, and the index
has the last word - so a body that differs from a stored name only in surrounding
whitespace, or in a NULL against a blank where the index coalesces, is answered by the
probe rather than by an `IntegrityError`.
"""

from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api import router as api_router
from src.app.api.dependencies import get_current_user
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.setup import create_application
from src.app.core.utils import cache
from src.app.crud.crud_users import read_account
from src.app.models.trip import Trip
from src.app.models.user import User
from tests.conftest import db_available
from tests.helpers.generators import create_gear_item

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")


@pytest.fixture(scope="module")
def unique_app() -> Any:
    return create_application(router=api_router, settings=settings, apply_migrations_on_start=False)


@pytest_asyncio.fixture
async def http(
    unique_app: Any, async_db: AsyncSession, diver: User, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[httpx.AsyncClient]:
    """A cache that never hits, so every read reaches the row."""

    async def the_tests_session() -> AsyncGenerator[AsyncSession]:
        yield async_db

    redis = AsyncMock()
    redis.get.return_value = None
    redis.scan.return_value = (0, [])
    monkeypatch.setattr(cache, "client", redis)

    account = await read_account(async_db, uuid=diver.uuid)
    unique_app.dependency_overrides[async_get_db] = the_tests_session
    unique_app.dependency_overrides[get_current_user] = lambda: account
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=unique_app), base_url="http://test/api/v1"
    ) as client:
        yield client
    unique_app.dependency_overrides = {}


@dataclass(frozen=True)
class Named:
    """One resource with a per-user unique name: where it is created and patched, and the
    body around a name. `body` takes the diver's session and account for the one resource
    that hangs off another row."""

    create: str
    patch: str
    body: Callable[[Session, User, str], dict[str, Any]]


def _gear_item_uuid(db: Session, diver: User) -> str:
    return str(create_gear_item(db, diver).uuid)


NAMED = {
    "trip": Named("/trip", "/trip/{}", lambda db, diver, name: {"name": name}),
    "contact": Named("/contact", "/contact/{}", lambda db, diver, name: {"name": name}),
    "person": Named("/person", "/person/{}", lambda db, diver, name: {"name": name}),
    "gear-set": Named("/gear-set", "/gear-set/{}", lambda db, diver, name: {"name": name}),
    "dive-form-preset": Named("/dive-form-preset", "/dive-form-preset/{}", lambda db, diver, name: {"name": name}),
    "dive-site": Named("/dive-site", "/dive-site/{}", lambda db, diver, name: {"name": name}),
    "gear-item": Named("/gear-item", "/gear-item/{}", lambda db, diver, name: {"name": name}),
}
each_named = pytest.mark.parametrize("named", [pytest.param(value, id=key) for key, value in NAMED.items()])


def _name() -> str:
    return f"Egypt {uuid7().hex[-8:]}"


class TestTheName:
    @each_named
    @pytest.mark.asyncio
    async def test_it_is_stored_trimmed(self, http: httpx.AsyncClient, db: Session, diver: User, named: Named) -> None:
        name = _name()

        response = await http.post(named.create, json=named.body(db, diver, f"  {name} \t"))

        assert response.status_code == 201, response.text
        assert response.json()["name"] == name

    @each_named
    @pytest.mark.parametrize(("first", "second"), [("{} ", "{} "), ("{}", " {} "), (" {}", "{}")])
    @pytest.mark.asyncio
    async def test_surrounding_whitespace_does_not_make_it_another_name(
        self, http: httpx.AsyncClient, db: Session, diver: User, named: Named, first: str, second: str
    ) -> None:
        name = _name()
        assert (await http.post(named.create, json=named.body(db, diver, first.format(name)))).status_code == 201

        response = await http.post(named.create, json=named.body(db, diver, second.format(name.upper())))

        assert response.status_code == 422, response.text

    @each_named
    @pytest.mark.asyncio
    async def test_nor_does_it_on_a_rename(
        self, http: httpx.AsyncClient, db: Session, diver: User, named: Named
    ) -> None:
        taken = _name()
        assert (await http.post(named.create, json=named.body(db, diver, f"{taken} "))).status_code == 201
        other = await http.post(named.create, json=named.body(db, diver, _name()))

        response = await http.patch(named.patch.format(other.json()["uuid"]), json={"name": f" {taken}  "})

        assert response.status_code == 422, response.text

    @each_named
    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    @pytest.mark.asyncio
    async def test_a_blank_one_is_refused(
        self, http: httpx.AsyncClient, db: Session, diver: User, named: Named, blank: str
    ) -> None:
        created = await http.post(named.create, json=named.body(db, diver, _name()))

        assert (await http.post(named.create, json=named.body(db, diver, blank))).status_code == 422
        assert (await http.patch(named.patch.format(created.json()["uuid"]), json={"name": blank})).status_code == 422


class TestACoalescedMember:
    """Where the index reads `coalesce(lower(x), '')`, a blank member and an absent one are
    the same slot, and the API stores both as NULL."""

    @pytest.mark.parametrize("blank", [None, "", "  "])
    @pytest.mark.asyncio
    async def test_a_brandless_item_collides_whichever_way_the_brand_is_left_out(
        self, http: httpx.AsyncClient, blank: str | None
    ) -> None:
        name = _name()
        first = await http.post("/gear-item", json={"name": name, "brand": None})
        assert first.status_code == 201, first.text

        response = await http.post("/gear-item", json={"name": name, "brand": blank})

        assert response.status_code == 422, response.text

    @pytest.mark.asyncio
    async def test_a_brand_is_stored_trimmed_and_a_blank_one_as_null(self, http: httpx.AsyncClient) -> None:
        branded = await http.post("/gear-item", json={"name": _name(), "brand": " Scubapro "})
        blank = await http.post("/gear-item", json={"name": _name(), "brand": "  "})

        assert branded.json()["brand"] == "Scubapro"
        assert blank.json()["brand"] is None

    @pytest.mark.asyncio
    async def test_clearing_a_brand_onto_a_brandless_twin_is_a_duplicate(self, http: httpx.AsyncClient) -> None:
        name = _name()
        await http.post("/gear-item", json={"name": name})
        branded = await http.post("/gear-item", json={"name": name, "brand": "Scubapro"})

        response = await http.patch(f"/gear-item/{branded.json()['uuid']}", json={"brand": " "})

        assert response.status_code == 422, response.text

    @pytest.mark.parametrize("label", [None, "", "  "])
    @pytest.mark.asyncio
    async def test_an_unlabelled_schedule_collides_whichever_way_the_label_is_left_out(
        self, http: httpx.AsyncClient, db: Session, diver: User, label: str | None
    ) -> None:
        body = {"gear_item_uuid": _gear_item_uuid(db, diver), "kind": "service", "starts_on": "2026-01-01"}
        first = await http.post("/gear-service-schedule", json={**body, "interval_months": 12})
        assert first.status_code == 201, first.text

        response = await http.post("/gear-service-schedule", json={**body, "interval_months": 6, "label": label})

        assert response.status_code == 422, response.text

    @pytest.mark.asyncio
    async def test_a_labelled_schedule_collides_through_surrounding_whitespace(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        body = {"gear_item_uuid": _gear_item_uuid(db, diver), "kind": "other", "starts_on": "2026-01-01"}
        first = await http.post("/gear-service-schedule", json={**body, "interval_months": 12, "label": "First stage"})
        assert first.status_code == 201, first.text

        response = await http.post(
            "/gear-service-schedule", json={**body, "interval_months": 6, "label": " first stage "}
        )

        assert response.status_code == 422, response.text

    @pytest.mark.asyncio
    async def test_a_record_finds_its_schedule_through_surrounding_whitespace(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        item = _gear_item_uuid(db, diver)
        schedule = await http.post(
            "/gear-service-schedule",
            json={
                "gear_item_uuid": item,
                "kind": "other",
                "starts_on": "2026-01-01",
                "interval_months": 12,
                "label": "First stage",
            },
        )

        record = await http.post(
            "/gear-service-record",
            json={"gear_item_uuid": item, "kind": "other", "serviced_on": "2026-03-01", "label": "First stage  "},
        )

        assert record.status_code == 201, record.text
        assert record.json()["label"] == "First stage"
        assert record.json()["gear_service_schedule_uuid"] == schedule.json()["uuid"]

    @pytest.mark.asyncio
    async def test_a_site_collides_through_whitespace_around_its_locality(self, http: httpx.AsyncClient) -> None:
        name = _name()
        first = await http.post("/dive-site", json={"name": name, "location": {"name": "Dahab, Egypt"}})
        assert first.status_code == 201, first.text

        response = await http.post("/dive-site", json={"name": name, "location": {"name": " dahab, egypt "}})

        assert response.status_code == 422, response.text


class TestARowStoredUntrimmed:
    """Rows already holding surrounding whitespace keep it - nothing rewrites them - and the
    probe takes them as the index does, so neither the read nor the next write is a 500."""

    @pytest.mark.asyncio
    async def test_it_reads_back_as_stored_even_when_it_is_all_whitespace(
        self, http: httpx.AsyncClient, db: Session
    ) -> None:
        created = await http.post("/trip", json={"name": _name()})
        db.execute(update(Trip).where(Trip.uuid == created.json()["uuid"]).values(name="   "))
        db.commit()

        response = await http.get(f"/trip/{created.json()['uuid']}")

        assert response.status_code == 200, response.text
        assert response.json()["name"] == "   "

    @pytest.mark.asyncio
    async def test_its_trimmed_twin_is_another_key_to_the_index_and_so_to_the_probe(
        self, http: httpx.AsyncClient, db: Session
    ) -> None:
        name = _name()
        created = await http.post("/trip", json={"name": name})
        db.execute(update(Trip).where(Trip.uuid == created.json()["uuid"]).values(name=f"{name} "))
        db.commit()

        response = await http.post("/trip", json={"name": name})

        assert response.status_code == 201, response.text

    @pytest.mark.asyncio
    async def test_saving_it_through_the_api_trims_it(self, http: httpx.AsyncClient, db: Session) -> None:
        name = _name()
        created = await http.post("/trip", json={"name": name})
        db.execute(update(Trip).where(Trip.uuid == created.json()["uuid"]).values(name=f"{name} "))
        db.commit()

        response = await http.patch(f"/trip/{created.json()['uuid']}", json={"name": f"{name} "})

        assert response.status_code == 200, response.text
        assert (await http.get(f"/trip/{created.json()['uuid']}")).json()["name"] == name
