"""Request bodies are bounded in length: every list a write takes, and a dive draft's text.

A list reaches Postgres as a bind parameter per entry, and past asyncpg's ceiling that is a 500
rather than a 422; a draft's text is what one row stores. The schema and wire tests need no
database. The cap on drafts runs against Postgres and skips silently without one - on a
developer's machine that means `POSTGRES_SERVER=localhost`. See CONTRIBUTING.md.
"""

import uuid as uuid_pkg
from collections.abc import AsyncGenerator, Callable, Generator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.api.v1.dive_drafts import write_dive_draft
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.schemas import MAX_LIST_ITEMS, NOTES_MAX_LENGTH
from src.app.core.setup import create_application
from src.app.core.utils import cache as cache_module
from src.app.crud.crud_users import read_account
from src.app.models.dive_draft import DiveDraft
from src.app.models.user import User
from src.app.schemas.dive import BOAT_NAME_MAX, DiveCreateRequest, DiveUpdateRequest, EntryType
from src.app.schemas.dive_draft import DiveDraftValues
from src.app.schemas.dive_site import DiveSiteCreate, DiveSiteUpdateRequest
from src.app.schemas.gear_set import GearSetCreateRequest, GearSetUpdateRequest
from src.app.schemas.tag import TAG_NAME_MAX
from src.app.services.dive_drafts import MAX_DIVE_DRAFTS_PER_USER
from tests.conftest import db_available
from tests.helpers.fake_redis import FakeRedis

START = "2026-10-01T10:00:00+02:00"
DIVE = {"dive_number": 1, "start_time": START, "submerged_time": 2400}
SITE = {"name": "Blue Hole", "latitude": 28.57, "longitude": 34.54}


def _uuid(_: int) -> str:
    return str(uuid_pkg.uuid4())


# Each list's entries, distinct so a rule against repeats has nothing to say - but for the
# entry types, which have four words to repeat and are kept one of each.
ITEMS: dict[str, Callable[[int], Any]] = {
    "mixtures": lambda _: {},
    "dive_site_uuids": _uuid,
    "gear_item_uuids": _uuid,
    "sightings": lambda _: {"species_uuid": _uuid(_)},
    "people": lambda _: {"person_uuid": _uuid(_)},
    "tags": lambda index: f"tag {index}",
    "other_names": lambda index: f"Name {index}",
    "external_ids": lambda index: {"registry": "openstreetmap", "identifier": f"node/{index + 1}"},
    "entry_types": lambda index: list(EntryType)[index % len(EntryType)].value,
}
# A draft's lists are a dive's.
DIVE_LISTS = ("mixtures", "dive_site_uuids", "gear_item_uuids", "sightings", "people", "tags")
SITE_LISTS = ("other_names", "external_ids", "entry_types", "tags")

BOUNDED: list[tuple[type[BaseModel], dict[str, Any], str]] = [
    *((DiveCreateRequest, DIVE, field) for field in DIVE_LISTS),
    *((DiveUpdateRequest, {}, field) for field in DIVE_LISTS),
    *((DiveDraftValues, {}, field) for field in DIVE_LISTS),
    (GearSetCreateRequest, {"name": "Sidemount"}, "gear_item_uuids"),
    (GearSetUpdateRequest, {}, "gear_item_uuids"),
    *((DiveSiteCreate, SITE, field) for field in SITE_LISTS),
    *((DiveSiteUpdateRequest, {}, field) for field in SITE_LISTS),
]
BOUNDED_IDS = [f"{schema.__name__}.{field}" for schema, _, field in BOUNDED]


def _entries(field: str, count: int) -> list[Any]:
    return [ITEMS[field](index) for index in range(count)]


class TestEveryListIsBounded:
    @pytest.mark.parametrize(("schema", "base", "field"), BOUNDED, ids=BOUNDED_IDS)
    def test_one_past_the_bound_is_too_long(self, schema: type[BaseModel], base: dict[str, Any], field: str) -> None:
        with pytest.raises(ValidationError) as excinfo:
            schema.model_validate({**base, field: _entries(field, MAX_LIST_ITEMS + 1)})

        assert [(error["loc"], error["type"]) for error in excinfo.value.errors()] == [((field,), "too_long")]

    @pytest.mark.parametrize(("schema", "base", "field"), BOUNDED, ids=BOUNDED_IDS)
    def test_the_bound_itself_is_taken(self, schema: type[BaseModel], base: dict[str, Any], field: str) -> None:
        validated = getattr(schema.model_validate({**base, field: _entries(field, MAX_LIST_ITEMS)}), field)

        assert len(validated) == (len(EntryType) if field == "entry_types" else MAX_LIST_ITEMS)

    @pytest.mark.parametrize(
        "schema", [DiveUpdateRequest, DiveDraftValues, GearSetUpdateRequest, DiveSiteUpdateRequest]
    )
    def test_an_update_still_takes_null_for_every_nullable_list(self, schema: type[BaseModel]) -> None:
        """The bound sits inside the `| None`: beside a list's own validator it would be
        applied to the null as well, and raise a `TypeError` rather than a 422."""
        refused = getattr(schema, "NON_NULLABLE_FIELDS", ())
        fields = [field for s, _, field in BOUNDED if s is schema and field not in refused]

        validated = schema.model_validate(dict.fromkeys(fields))

        assert all(getattr(validated, field) is None for field in fields)


class TestADraftsTextIsBoundedByLengthAlone:
    @pytest.mark.parametrize(
        ("values", "loc"),
        [
            ({"notes": "x" * (NOTES_MAX_LENGTH + 1)}, ("notes",)),
            ({"boat_name": "x" * (BOAT_NAME_MAX + 1)}, ("boat_name",)),
            ({"tags": ["x" * (TAG_NAME_MAX + 1)]}, ("tags", 0)),
            (
                {"sightings": [{"species_uuid": _uuid(0), "notes": "x" * (NOTES_MAX_LENGTH + 1)}]},
                ("sightings", 0, "notes"),
            ),
        ],
        ids=["notes", "boat name", "a tag", "a sighting's notes"],
    )
    def test_past_a_dives_length_is_refused(self, values: dict[str, Any], loc: tuple[Any, ...]) -> None:
        with pytest.raises(ValidationError) as excinfo:
            DiveDraftValues.model_validate(values)

        assert [(error["loc"], error["type"]) for error in excinfo.value.errors()] == [(loc, "string_too_long")]

    def test_at_a_dives_length_and_blank_are_kept_as_typed(self) -> None:
        values = {
            "notes": "x" * NOTES_MAX_LENGTH,
            "boat_name": "",
            "tags": ["", " night ", "x" * TAG_NAME_MAX],
            "sightings": [{"species_uuid": _uuid(0), "notes": ""}],
        }

        assert DiveDraftValues.model_validate(values).stored() == values


# -------------- on the wire --------------

OWNER = {"id": 7, "uuid": uuid_pkg.uuid4(), "username": "ada", "is_superuser": False}


@pytest.fixture(scope="module")
def bounded_app() -> Any:
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


@pytest.fixture
def signed_in_client(bounded_app: Any) -> Generator[TestClient]:
    bounded_app.dependency_overrides[get_current_user] = lambda: OWNER
    try:
        with TestClient(bounded_app) as test_client:
            yield test_client
    finally:
        bounded_app.dependency_overrides = {}


def _refused(response: httpx.Response) -> list[tuple[list[Any], str]]:
    assert response.status_code == 422, response.text
    return [(error["loc"], error["type"]) for error in response.json()["detail"]]


class TestOnTheWire:
    @pytest.mark.parametrize("field", DIVE_LISTS)
    def test_a_dive_with_a_list_past_the_bound_is_a_422(self, signed_in_client: TestClient, field: str) -> None:
        response = signed_in_client.post("/api/v1/dive", json={**DIVE, field: _entries(field, MAX_LIST_ITEMS + 1)})

        assert _refused(response) == [(["body", field], "too_long")]

    @pytest.mark.parametrize("field", DIVE_LISTS)
    def test_a_draft_with_a_list_past_the_bound_is_a_422(self, signed_in_client: TestClient, field: str) -> None:
        response = signed_in_client.post(
            "/api/v1/dive-draft", json={"values": {field: _entries(field, MAX_LIST_ITEMS + 1)}}
        )

        assert _refused(response) == [(["body", "values", field], "too_long")]

    @pytest.mark.parametrize("field", SITE_LISTS)
    def test_a_dive_site_with_a_list_past_the_bound_is_a_422(self, signed_in_client: TestClient, field: str) -> None:
        response = signed_in_client.post("/api/v1/dive-site", json={**SITE, field: _entries(field, MAX_LIST_ITEMS + 1)})

        assert _refused(response) == [(["body", field], "too_long")]

    def test_a_draft_with_notes_past_a_dives_is_a_422(self, signed_in_client: TestClient) -> None:
        response = signed_in_client.post("/api/v1/dive-draft", json={"values": {"notes": "x" * (NOTES_MAX_LENGTH + 1)}})

        assert _refused(response) == [(["body", "values", "notes"], "string_too_long")]


# -------------- the cap on drafts, against Postgres --------------


@pytest_asyncio.fixture
async def http(
    bounded_app: Any, async_sessions: async_sessionmaker[AsyncSession], diver: User, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[httpx.AsyncClient]:
    """A session per request, as the app runs, signed in as `diver`."""
    monkeypatch.setattr(cache_module, "client", FakeRedis())

    async def per_request() -> AsyncGenerator[AsyncSession]:
        async with async_sessions() as session:
            yield session

    async with async_sessions() as db:
        account = await read_account(db, uuid=diver.uuid)
    bounded_app.dependency_overrides[async_get_db] = per_request
    bounded_app.dependency_overrides[get_current_user] = lambda: account
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=bounded_app), base_url="http://test/api/v1"
    ) as client:
        yield client
    bounded_app.dependency_overrides = {}


def _drafts(db: Session, user: User) -> int:
    db.expire_all()
    return int(db.execute(select(func.count()).select_from(DiveDraft).where(DiveDraft.user_id == user.id)).scalar_one())


def _hold(db: Session, user: User, count: int) -> None:
    db.add_all(DiveDraft(user_id=user.id, values={"notes": f"Draft {index}"}) for index in range(count))
    db.commit()


def test_the_published_description_names_the_cap() -> None:
    assert f"at most {MAX_DIVE_DRAFTS_PER_USER} drafts" in (write_dive_draft.__doc__ or "")


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheCapOnDrafts:
    @pytest.mark.asyncio
    async def test_an_empty_boat_name_is_saved_as_typed(self, http: httpx.AsyncClient) -> None:
        response = await http.post("/dive-draft", json={"values": {"boat_name": ""}})

        assert response.status_code == 201, response.text
        assert response.json()["values"]["boat_name"] == ""

    @pytest.mark.asyncio
    async def test_one_past_the_cap_is_a_409_and_saves_nothing(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        _hold(db, diver, MAX_DIVE_DRAFTS_PER_USER)

        response = await http.post("/dive-draft", json={"values": {"notes": "One more"}})

        assert response.status_code == 409
        assert response.json() == {
            "detail": f"You already have {MAX_DIVE_DRAFTS_PER_USER} dive drafts. Log or delete one to save another."
        }
        assert _drafts(db, diver) == MAX_DIVE_DRAFTS_PER_USER

    @pytest.mark.asyncio
    async def test_deleting_one_makes_room_for_one(self, http: httpx.AsyncClient, db: Session, diver: User) -> None:
        _hold(db, diver, MAX_DIVE_DRAFTS_PER_USER - 1)
        last = await http.post("/dive-draft", json={"values": {}})
        assert last.status_code == 201, last.text
        assert (await http.post("/dive-draft", json={"values": {}})).status_code == 409

        assert (await http.delete(f"/dive-draft/{last.json()['uuid']}")).status_code == 200

        assert (await http.post("/dive-draft", json={"values": {}})).status_code == 201
        assert _drafts(db, diver) == MAX_DIVE_DRAFTS_PER_USER

    @pytest.mark.asyncio
    async def test_a_repeat_under_its_key_at_the_cap_answers_the_first_draft(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        _hold(db, diver, MAX_DIVE_DRAFTS_PER_USER - 1)
        body = {"values": {"notes": "The last one"}}

        first = await http.post("/dive-draft", json=body, headers={"Idempotency-Key": "last"})
        repeat = await http.post("/dive-draft", json=body, headers={"Idempotency-Key": "last"})

        assert first.status_code == 201, first.text
        assert repeat.status_code == 201, repeat.text
        assert repeat.json() == first.json()
        assert _drafts(db, diver) == MAX_DIVE_DRAFTS_PER_USER
