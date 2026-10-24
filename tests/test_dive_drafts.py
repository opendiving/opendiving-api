"""Dive drafts: the values' shape, the routes, and the references a read keeps.

The shape tests need no database. The route tests run against Postgres and skip silently
without one - on a developer's machine that means `POSTGRES_SERVER=localhost`; CI sets it and
fails the job if anything skips. See CONTRIBUTING.md.

What is not here: the ownership 404s (`test_ownership.py`), the hard delete
(`test_hard_delete.py`), the `Idempotency-Key` on both creates (`test_idempotency_key.py`),
the cascade with the account (`test_user_cascade.py`, `test_account_deletion.py`) and the
archive member (`test_export_archive.py`), which this resource joined rather than restates.
"""

import typing
import uuid as uuid_pkg
from collections.abc import AsyncGenerator
from enum import StrEnum
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import BaseModel, ValidationError
from sqlalchemy import delete, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.setup import create_application
from src.app.core.utils import cache as cache_module
from src.app.crud.crud_users import read_account
from src.app.models.dive import Dive
from src.app.models.dive_draft import DiveDraft
from src.app.models.user import User
from src.app.schemas.dive import DiveCreateRequest
from src.app.schemas.dive_draft import (
    DiveDraftCreate,
    DiveDraftCreateInternal,
    DiveDraftUpdate,
    DiveDraftValues,
    DiveDraftValuesRead,
)
from src.app.services.export.loader import load_export_bundle
from tests.conftest import db_available
from tests.helpers.fake_redis import FakeRedis
from tests.helpers.generators import (
    create_contact,
    create_course,
    create_dive_draft,
    create_dive_site,
    create_gear_item,
    create_gear_set,
    create_person,
    create_species,
    create_trip,
    create_user,
)

START = "2026-10-01T10:00:00+02:00"


def _models_in(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [model for arg in typing.get_args(annotation) for model in _models_in(arg)]


def _enums_in(annotation: Any) -> set[type[StrEnum]]:
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return {annotation}
    return {enum for arg in typing.get_args(annotation) for enum in _enums_in(arg)}


def _shape(model: type[BaseModel]) -> dict[str, Any]:
    """A model's member names, each mapped to its own members' shape where it holds a model."""
    shape: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        nested = _models_in(field.annotation)
        shape[name] = _shape(nested[0]) if nested else None
    return shape


def _vocabularies(model: type[BaseModel], path: str = "") -> dict[str, set[type[StrEnum]]]:
    found: dict[str, set[type[StrEnum]]] = {}
    for name, field in model.model_fields.items():
        if enums := _enums_in(field.annotation):
            found[path + name] = enums
        for nested in _models_in(field.annotation):
            found |= _vocabularies(nested, f"{path}{name}.")
    return found


class TestTheValuesAreTheDiveForm:
    """`values` is stored in `DiveCreateRequest`'s names, so the two must not drift: a member
    the dive gains that the draft lacks would turn every draft write sending it into a 422."""

    @pytest.mark.parametrize("draft", [DiveDraftValues, DiveDraftValuesRead], ids=lambda model: model.__name__)
    def test_the_members_are_the_create_requests_all_the_way_down(self, draft: type[BaseModel]) -> None:
        assert _shape(draft) == _shape(DiveCreateRequest)

    def test_every_vocabulary_is_the_create_requests(self) -> None:
        assert _vocabularies(DiveDraftValues) == _vocabularies(DiveCreateRequest)

    def test_the_read_takes_every_vocabulary_as_a_string(self) -> None:
        assert _vocabularies(DiveDraftValuesRead) == {}

    def test_every_member_is_optional(self) -> None:
        assert DiveDraftValues.model_validate({}).stored() == {}


class TestWhatADraftRefuses:
    @pytest.mark.parametrize(
        "values",
        [
            {"start_time": START},
            {"mixtures": [{"start_pressure": 50, "end_pressure": 200}]},
            {"mixtures": [{"start_pressure": 400, "po2_limit": 0.1, "gas_number": -1}]},
            {"max_depth": 10, "avg_depth": 30, "rating": 9, "submerged_time": -5},
            {"sightings": [{"species_uuid": str(uuid_pkg.uuid4()), "count": 0, "abundance": "many"}]},
            {"tags": ["", "Night", "night"], "boat_name": "", "notes": "x" * 20_000},
        ],
        ids=[
            "a start time alone",
            "pressures out of order",
            "past every bound",
            "across members",
            "a sighting",
            "text",
        ],
    )
    def test_nothing_a_dive_bounds(self, values: dict[str, Any]) -> None:
        DiveDraftValues.model_validate(values)

    @pytest.mark.parametrize(
        ("values", "loc"),
        [
            ({"max_depth": "deep"}, ("max_depth",)),
            ({"water_type": "soda"}, ("water_type",)),
            ({"mixtures": [{"role": "spare"}]}, ("mixtures", 0, "role")),
            ({"start_time": "2026-10-01T10:00:00"}, ("start_time",)),
            ({"buddy": "Alex"}, ("buddy",)),
            ({"max_depth": float("nan")}, ("max_depth",)),
        ],
        ids=["a word for a depth", "a word outside the vocabulary", "a cylinder's", "no offset", "unknown", "nan"],
    )
    def test_a_wrong_type_or_word(self, values: dict[str, Any], loc: tuple[Any, ...]) -> None:
        with pytest.raises(ValidationError) as excinfo:
            DiveDraftValues.model_validate(values)

        assert [error["loc"] for error in excinfo.value.errors()] == [loc]

    def test_values_null_on_an_update(self) -> None:
        with pytest.raises(ValidationError, match="values cannot be null"):
            DiveDraftUpdate.model_validate({"values": None})

    def test_the_admin_panel_stores_values_as_the_routes_do(self) -> None:
        stored = DiveDraftCreateInternal(user_id=1, values={"start_time": START, "max_depth": None})

        assert stored.values == {"start_time": START}
        with pytest.raises(ValidationError):
            DiveDraftCreateInternal(user_id=1, values={"water_type": "soda"})

    def test_a_create_needs_nothing(self) -> None:
        assert DiveDraftCreate.model_validate({}).values.stored() == {}


# -------------- against Postgres --------------


@pytest.fixture(scope="module")
def drafts_app() -> Any:
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


@pytest_asyncio.fixture
async def http(
    drafts_app: Any, async_sessions: async_sessionmaker[AsyncSession], diver: User, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[httpx.AsyncClient]:
    """A session per request, as the app runs, signed in as `diver`."""
    monkeypatch.setattr(cache_module, "client", FakeRedis())

    async def per_request() -> AsyncGenerator[AsyncSession]:
        async with async_sessions() as session:
            yield session

    async with async_sessions() as db:
        account = await read_account(db, uuid=diver.uuid)
    drafts_app.dependency_overrides[async_get_db] = per_request
    drafts_app.dependency_overrides[get_current_user] = lambda: account
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=drafts_app), base_url="http://test/api/v1"
    ) as client:
        yield client
    drafts_app.dependency_overrides = {}


def _count(db: Session, model: type[Any], user: User) -> int:
    db.expire_all()
    return int(db.execute(select(func.count()).select_from(model).where(model.user_id == user.id)).scalar_one())


def _drop(db: Session, row: Any) -> None:
    db.execute(delete(type(row)).where(type(row).id == row.id))
    db.commit()


async def _create(http: httpx.AsyncClient, values: dict[str, Any], gear_set: Any = None) -> dict[str, Any]:
    response = await http.post("/dive-draft", json={"gear_set_uuid": gear_set, "values": values})
    assert response.status_code == 201, response.text
    draft: dict[str, Any] = response.json()
    return draft


async def _read(http: httpx.AsyncClient, uuid: str) -> dict[str, Any]:
    response = await http.get(f"/dive-draft/{uuid}")
    assert response.status_code == 200, response.text
    draft: dict[str, Any] = response.json()
    return draft


def _given(values: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in values.items() if value is not None}


pytestmark_db = pytest.mark.skipif(not db_available(), reason="No database connection available")


@pytestmark_db
class TestWriting:
    @pytest.mark.asyncio
    async def test_a_draft_of_a_start_time_alone_is_a_201(self, http: httpx.AsyncClient) -> None:
        draft = await _create(http, {"start_time": START})

        assert set(draft) == {"uuid", "created_at", "updated_at", "gear_set_uuid", "values"}
        assert _given(draft["values"]) == {"start_time": START}
        assert draft["gear_set_uuid"] is None
        assert draft["created_at"] == draft["updated_at"]

    @pytest.mark.asyncio
    async def test_pressures_a_dive_refuses_are_kept_as_typed(self, http: httpx.AsyncClient) -> None:
        draft = await _create(
            http, {"mixtures": [{"start_pressure": 50, "end_pressure": 200}, {"start_pressure": 400}]}
        )

        assert [_given(mixture) for mixture in draft["values"]["mixtures"]] == [
            {"start_pressure": 50, "end_pressure": 200},
            {"start_pressure": 400},
        ]

    @pytest.mark.asyncio
    async def test_a_depth_given_as_a_word_is_a_422(self, http: httpx.AsyncClient, db: Session, diver: User) -> None:
        response = await http.post("/dive-draft", json={"values": {"max_depth": "deep"}})

        assert response.status_code == 422
        assert _count(db, DiveDraft, diver) == 0

    @pytest.mark.asyncio
    async def test_a_vocabulary_is_read_back_as_its_word(self, http: httpx.AsyncClient) -> None:
        draft = await _create(http, {"water_type": "salt", "mixtures": [{"role": "deco"}]})

        assert draft["values"]["water_type"] == "salt"
        assert draft["values"]["mixtures"][0]["role"] == "deco"

    @pytest.mark.asyncio
    async def test_another_accounts_gear_set_is_stored_as_none_and_the_callers_is_kept(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        theirs = create_gear_set(db, create_user(db))
        mine = create_gear_set(db, diver)

        assert (await _create(http, {}, gear_set=str(theirs.uuid)))["gear_set_uuid"] is None
        assert (await _create(http, {}, gear_set=str(mine.uuid)))["gear_set_uuid"] == str(mine.uuid)

    @pytest.mark.asyncio
    async def test_a_deleted_gear_set_leaves_the_draft_without_one(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        gear_set = create_gear_set(db, diver)
        draft = await _create(http, {"notes": "Checklist"}, gear_set=str(gear_set.uuid))

        _drop(db, gear_set)

        read = await _read(http, draft["uuid"])
        assert read["gear_set_uuid"] is None
        assert read["values"]["notes"] == "Checklist"


@pytestmark_db
class TestReferencesAreCheckedOnTheWayOut:
    @pytest.mark.asyncio
    async def test_another_accounts_site_and_a_deleted_one_read_back_without_them(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        mine, gone, theirs = (
            create_dive_site(db, diver),
            create_dive_site(db, diver),
            create_dive_site(db, create_user(db)),
        )
        draft = await _create(http, {"dive_site_uuids": [str(theirs.uuid), str(mine.uuid), str(gone.uuid)]})
        assert draft["values"]["dive_site_uuids"] == [str(mine.uuid), str(gone.uuid)]

        _drop(db, gone)

        assert (await _read(http, draft["uuid"]))["values"]["dive_site_uuids"] == [str(mine.uuid)]

    @pytest.mark.asyncio
    async def test_a_deleted_trip_drops_off_and_everything_else_stays(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        trip, course, contact = create_trip(db, diver), create_course(db, diver), create_contact(db, diver)
        values = {
            "trip_uuid": str(trip.uuid),
            "course_uuid": str(course.uuid),
            "contact_uuid": str(contact.uuid),
            "notes": "Drift",
            "max_depth": 24.5,
        }
        draft = await _create(http, values)

        _drop(db, trip)

        assert _given((await _read(http, draft["uuid"]))["values"]) == {
            **{name: value for name, value in values.items() if name != "trip_uuid"}
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("member", ["course_uuid", "contact_uuid"])
    async def test_a_deleted_course_or_contact_drops_off(
        self, http: httpx.AsyncClient, db: Session, diver: User, member: str
    ) -> None:
        row = create_course(db, diver) if member == "course_uuid" else create_contact(db, diver)
        draft = await _create(http, {member: str(row.uuid)})

        _drop(db, row)

        assert (await _read(http, draft["uuid"]))["values"][member] is None

    @pytest.mark.asyncio
    async def test_a_deleted_gear_item_leaves_the_list_and_unlinks_its_cylinder(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        kept, gone = create_gear_item(db, diver), create_gear_item(db, diver)
        draft = await _create(
            http,
            {
                "gear_item_uuids": [str(gone.uuid), str(kept.uuid)],
                "mixtures": [{"volume": 12, "gear_item_uuid": str(gone.uuid)}, {"gear_item_uuid": str(kept.uuid)}],
            },
        )

        _drop(db, gone)

        values = (await _read(http, draft["uuid"]))["values"]
        assert values["gear_item_uuids"] == [str(kept.uuid)]
        assert [_given(mixture) for mixture in values["mixtures"]] == [
            {"volume": 12},
            {"gear_item_uuid": str(kept.uuid)},
        ]

    @pytest.mark.asyncio
    async def test_a_deleted_person_and_an_unknown_species_take_their_entries_with_them(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        kept, gone = create_person(db, diver), create_person(db, diver)
        species = create_species(db)
        draft = await _create(
            http,
            {
                "people": [{"person_uuid": str(gone.uuid), "role": "buddy"}, {"person_uuid": str(kept.uuid)}],
                "sightings": [
                    {"species_uuid": str(uuid_pkg.uuid4()), "count": 2},
                    {"species_uuid": str(species.uuid), "notes": "Under the arch"},
                ],
            },
        )

        _drop(db, gone)

        values = (await _read(http, draft["uuid"]))["values"]
        assert values["people"] == [{"person_uuid": str(kept.uuid), "role": None}]
        assert [_given(sighting) for sighting in values["sightings"]] == [
            {"species_uuid": str(species.uuid), "notes": "Under the arch"}
        ]

    @pytest.mark.asyncio
    async def test_the_list_resolves_a_page_in_one_query_per_kind(
        self, http: httpx.AsyncClient, db: Session, diver: User, async_sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        trips = [create_trip(db, diver) for _ in range(4)]
        for trip in trips:
            await _create(
                http, {"trip_uuid": str(trip.uuid), "dive_site_uuids": [str(create_dive_site(db, diver).uuid)]}
            )
        for trip in trips[2:]:
            _drop(db, trip)
        statements: list[str] = []
        engine = async_sessions.kw["bind"].sync_engine

        def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            page = (await http.get("/dive-drafts", params={"items_per_page": 10})).json()
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert sorted(str(row["values"]["trip_uuid"]) for row in page["data"]) == sorted(
            ["None", "None", str(trips[0].uuid), str(trips[1].uuid)]
        )
        for table in ("trip", "dive_site"):
            assert len([s for s in statements if s.startswith(f"SELECT {table}.uuid")]) == 1, statements


@pytestmark_db
class TestTheRoutes:
    @pytest.mark.asyncio
    async def test_the_list_is_the_callers_newest_change_first(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        create_dive_draft(db, create_user(db))
        first = await _create(http, {"notes": "First"})
        second = await _create(http, {"notes": "Second"})
        assert (await http.patch(f"/dive-draft/{first['uuid']}", json={"values": {"notes": "First, again"}})).is_success

        page = (await http.get("/dive-drafts")).json()

        assert [row["uuid"] for row in page["data"]] == [first["uuid"], second["uuid"]]
        assert (page["total_count"], page["has_more"], page["page"], page["items_per_page"]) == (2, False, 1, 10)

    @pytest.mark.asyncio
    async def test_the_list_pages(self, http: httpx.AsyncClient) -> None:
        made = [(await _create(http, {"dive_number": number}))["uuid"] for number in range(3)]

        second = (await http.get("/dive-drafts", params={"page": 2, "items_per_page": 2})).json()

        assert [row["uuid"] for row in second["data"]] == [made[0]]
        assert (second["total_count"], second["has_more"]) == (3, False)

    @pytest.mark.asyncio
    async def test_a_patch_replaces_the_values_whole_and_answers_a_message(self, http: httpx.AsyncClient) -> None:
        draft = await _create(http, {"notes": "Drift", "max_depth": 24.5, "tags": ["night"]})

        patched = await http.patch(f"/dive-draft/{draft['uuid']}", json={"values": {"notes": "Drift dive"}})

        assert patched.status_code == 200
        assert patched.json() == {"message": "Dive draft updated"}
        read = await _read(http, draft["uuid"])
        assert _given(read["values"]) == {"notes": "Drift dive"}
        assert read["updated_at"] > draft["updated_at"]
        assert read["created_at"] == draft["created_at"]

    @pytest.mark.asyncio
    async def test_a_patch_sets_and_clears_the_gear_set_only_when_present(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        gear_set = create_gear_set(db, diver)
        draft = await _create(http, {"notes": "Kit"})
        uuid = draft["uuid"]

        await http.patch(f"/dive-draft/{uuid}", json={"gear_set_uuid": str(gear_set.uuid)})
        assert (await _read(http, uuid))["gear_set_uuid"] == str(gear_set.uuid)

        await http.patch(f"/dive-draft/{uuid}", json={"values": {"notes": "Kit, ticked"}})
        assert (await _read(http, uuid))["gear_set_uuid"] == str(gear_set.uuid)

        await http.patch(f"/dive-draft/{uuid}", json={"gear_set_uuid": None, "values": {}})
        read = await _read(http, uuid)
        assert read["gear_set_uuid"] is None
        assert _given(read["values"]) == {}

    @pytest.mark.asyncio
    async def test_an_empty_patch_changes_nothing(self, http: httpx.AsyncClient) -> None:
        draft = await _create(http, {"notes": "Kept"})

        assert (await http.patch(f"/dive-draft/{draft['uuid']}", json={})).status_code == 200

        assert await _read(http, draft["uuid"]) == draft

    @pytest.mark.asyncio
    async def test_a_patch_of_values_null_is_a_422(self, http: httpx.AsyncClient) -> None:
        draft = await _create(http, {"notes": "Kept"})

        response = await http.patch(f"/dive-draft/{draft['uuid']}", json={"values": None})

        assert response.status_code == 422
        assert (await _read(http, draft["uuid"]))["values"]["notes"] == "Kept"

    @pytest.mark.asyncio
    async def test_a_delete_removes_it_and_a_second_is_a_404(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        draft = await _create(http, {"notes": "Discarded"})

        deleted = await http.delete(f"/dive-draft/{draft['uuid']}")
        again = await http.delete(f"/dive-draft/{draft['uuid']}")

        assert (deleted.status_code, deleted.json()) == (200, {"message": "Dive draft deleted"})
        assert (again.status_code, again.json()) == (404, {"detail": "Dive draft not found"})
        assert (await http.get(f"/dive-draft/{draft['uuid']}")).status_code == 404
        assert _count(db, DiveDraft, diver) == 0


@pytestmark_db
class TestLogging:
    @pytest.mark.asyncio
    async def test_it_creates_the_dive_and_removes_the_draft(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        site = create_dive_site(db, diver)
        draft = await _create(http, {"notes": "Almost"})
        body = {
            "dive_number": 7,
            "start_time": START,
            "submerged_time": 2400,
            "notes": "Logged",
            "dive_site_uuids": [str(site.uuid)],
            "tags": ["drift"],
        }

        logged = await http.post(f"/dive-draft/{draft['uuid']}/dive", json=body)

        assert logged.status_code == 201, logged.text
        dive = logged.json()
        assert (dive["dive_number"], dive["notes"], dive["tags"]) == (7, "Logged", ["drift"])
        assert [row["uuid"] for row in dive["dive_sites"]] == [str(site.uuid)]
        assert _count(db, Dive, diver) == 1
        assert _count(db, DiveDraft, diver) == 0
        assert (await http.get(f"/dive/{dive['uuid']}")).json() == dive

    @pytest.mark.asyncio
    async def test_a_dive_the_api_refuses_leaves_the_draft(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        """A site deleted while a phone held the draft: the log is `POST /dive`'s 422 naming it."""
        site = create_dive_site(db, diver)
        draft = await _create(http, {"dive_site_uuids": [str(site.uuid)]})
        _drop(db, site)

        refused = await http.post(
            f"/dive-draft/{draft['uuid']}/dive",
            json={"dive_number": 1, "start_time": START, "submerged_time": 2400, "dive_site_uuids": [str(site.uuid)]},
        )

        assert (refused.status_code, refused.json()) == (422, {"detail": "Dive site not found."})
        assert _count(db, Dive, diver) == 0
        assert (await _read(http, draft["uuid"]))["values"]["dive_site_uuids"] == []

    @pytest.mark.asyncio
    async def test_a_bound_the_dive_enforces_is_a_422_and_leaves_the_draft(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        draft = await _create(http, {"mixtures": [{"start_pressure": 400}]})

        refused = await http.post(
            f"/dive-draft/{draft['uuid']}/dive",
            json={"dive_number": 1, "start_time": START, "submerged_time": 2400, "mixtures": [{"start_pressure": 400}]},
        )

        assert refused.status_code == 422
        assert _count(db, DiveDraft, diver) == 1
        assert _count(db, Dive, diver) == 0

    @pytest.mark.asyncio
    async def test_a_draft_already_logged_is_a_404_and_logs_nothing(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        draft = await _create(http, {})
        body = {"dive_number": 1, "start_time": START, "submerged_time": 2400}
        assert (await http.post(f"/dive-draft/{draft['uuid']}/dive", json=body)).status_code == 201

        again = await http.post(f"/dive-draft/{draft['uuid']}/dive", json=body)

        assert (again.status_code, again.json()) == (404, {"detail": "Dive draft not found"})
        assert _count(db, Dive, diver) == 1


@pytestmark_db
class TestTheExport:
    @pytest.mark.asyncio
    async def test_the_bundle_carries_the_accounts_drafts_as_they_read(
        self, http: httpx.AsyncClient, db: Session, diver: User, async_db: AsyncSession
    ) -> None:
        trip = create_trip(db, diver)
        create_dive_draft(db, create_user(db))
        draft = await _create(http, {"notes": "Exported", "trip_uuid": str(trip.uuid)})
        _drop(db, trip)

        bundle = await load_export_bundle(async_db, user_id=diver.id)

        assert [read.model_dump(mode="json") for read in bundle.drafts] == [await _read(http, draft["uuid"])]
        assert bundle.drafts[0].values.trip_uuid is None
