"""A request never names its owner; the session does.

Three claims, each asserted at the wire rather than a layer below it: no route in the
application declares a `user_uuid` query parameter; a create body that carries one is
accepted and the row is still the caller's; and a list route handed one in the query
answers the caller's own page, never the named account's and never a 403.

The create half still accepts the field on purpose. All eight create schemas are
`extra="forbid"`, and the API and the web build deploy separately, so a schema that
stopped declaring `user_uuid` would 422 every create for whichever web build the
instance is still serving in between. The follow-up that removes the field is what
turns `TestACreateBodyMayStillNameAnOwner` into a 422 naming `extra_forbidden`.

The routes are exercised over HTTP with a stubbed session and a stubbed data source,
the shape `tests/test_dive_mixture_pressures.py` uses: the wire is what the previously
deployed web build speaks, and a test that built the schema by hand would not see a
query parameter at all.
"""

import uuid as uuid_pkg
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.dependencies import get_current_user
from src.app.api.v1 import certifications as certifications_module
from src.app.api.v1 import courses as courses_module
from src.app.api.v1 import dive_form_presets as dive_form_presets_module
from src.app.api.v1 import dive_sites as dive_sites_module
from src.app.api.v1 import dives as dives_module
from src.app.api.v1 import gear_items as gear_items_module
from src.app.api.v1 import gear_service as gear_service_module
from src.app.api.v1 import gear_sets as gear_sets_module
from src.app.api.v1 import trips as trips_module
from src.app.core.db.database import async_get_db
from src.app.main import app as real_app
from src.app.schemas.certification import CertificationReadInternal
from src.app.schemas.course import CourseReadInternal
from src.app.schemas.dive import DiveReadInternal
from src.app.schemas.dive_form_preset import DiveFormPresetReadInternal
from src.app.schemas.dive_site import DiveSiteReadInternal
from src.app.schemas.gear_item import GearItemReadInternal
from src.app.schemas.gear_set import GearSetReadInternal
from src.app.schemas.trip import TripReadInternal
from tests.helpers.routes import iter_api_routes

CALLER_ID = 7
CALLER_UUID = uuid_pkg.UUID("00000000-0000-0000-0000-0000000000aa")
SOMEONE_ELSE = uuid_pkg.UUID("00000000-0000-0000-0000-0000000000bb")

ROW_UUID = uuid_pkg.UUID("00000000-0000-0000-0000-0000000000cc")
CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)
START_TIME = "2026-06-01T09:00:00+02:00"

ROUTER_MODULES = (
    trips_module,
    courses_module,
    dives_module,
    dive_sites_module,
    dive_form_presets_module,
    certifications_module,
    gear_items_module,
    gear_sets_module,
    gear_service_module,
)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Every router this node touches on one app, with auth and the session stubbed.

    Its paths carry no `/api/v1` prefix - the routers are included directly rather than
    through `create_application`, which would start a lifespan that wants Postgres. The
    structural sweep below is the one thing that needs the real app, and it only reads
    the route table.
    """
    app = FastAPI()
    for module in ROUTER_MODULES:
        app.include_router(module.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": CALLER_ID,
        "uuid": CALLER_UUID,
        "is_superuser": False,
    }
    app.dependency_overrides[async_get_db] = lambda: AsyncMock(spec=AsyncSession)
    with TestClient(app) as test_client:
        yield test_client


class TestNoRouteDeclaresAUserUuidQueryParameter:
    """The structural half, over the app's real route table.

    A behavioural test can only assert the routes it names, and the failure this guards
    against is a *new* list route written against the old shape. Enumerated from the
    route table for the same reason `test_ownership.py`'s sweep is.
    """

    def test_no_route_declares_one(self) -> None:
        offenders = sorted(
            str(route)
            for route in iter_api_routes(real_app)
            if "user_uuid" in {query_param.name for query_param in route.dependant.query_params}
        )

        assert not offenders, (
            "these routes take the caller's own uuid as a query parameter:\n"
            + "\n".join(f"  {offender}" for offender in offenders)
            + "\n\nThe session already names the caller - scope by `current_user` instead."
        )

    def test_the_sweep_can_see_query_parameters_at_all(self) -> None:
        """Without this the sweep above passes for a walk that found no routes, or found
        them with empty dependants - the way a structural guard goes quiet."""
        dives = next(route for route in iter_api_routes(real_app) if route.key == ("GET", "/api/v1/dives"))

        assert "items_per_page" in {query_param.name for query_param in dives.dependant.query_params}


def _internal_trip() -> TripReadInternal:
    return TripReadInternal(id=11, user_id=CALLER_ID, uuid=ROW_UUID, name="Cebu 2026", created_at=CREATED_AT)


def _install_trip(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    created = AsyncMock(return_value=_internal_trip())
    monkeypatch.setattr(trips_module, "trip_name_exists", AsyncMock(return_value=False))
    monkeypatch.setattr(trips_module, "replace_locations_for_trip", AsyncMock())
    monkeypatch.setattr(trips_module, "get_locations_for_trip", AsyncMock(return_value=[]))
    monkeypatch.setattr(trips_module._trip_cache, "invalidate_list", AsyncMock())
    monkeypatch.setattr(trips_module.crud_trips, "create", created)
    monkeypatch.setattr(trips_module.crud_trips, "get", AsyncMock(return_value=_internal_trip()))
    return created


def _install_course(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    created = AsyncMock(
        return_value=CourseReadInternal(
            id=11, user_id=CALLER_ID, uuid=ROW_UUID, name="Advanced Nitrox", status="completed", created_at=CREATED_AT
        )
    )
    monkeypatch.setattr(courses_module, "invalidate_course_caches", AsyncMock())
    monkeypatch.setattr(courses_module.crud_courses, "create", created)
    return created


def _install_dive(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    created = AsyncMock(return_value=MagicMock(id=11))
    stored = DiveReadInternal(
        id=11,
        user_id=CALLER_ID,
        uuid=ROW_UUID,
        dive_number=1,
        start_time=datetime(2026, 6, 1, 7, tzinfo=UTC),
        utc_offset_minutes=120,
        duration=1800,
        created_at=CREATED_AT,
    ).model_dump()
    for name in ("replace_mixtures_for_dive", "replace_dive_sites_for_dive", "replace_gear_items_for_dive"):
        monkeypatch.setattr(dives_module, name, AsyncMock())
    for name in ("replace_species_for_dive", "recalculate_dive_stats", "recalculate_gear_dive_counts"):
        monkeypatch.setattr(dives_module, name, AsyncMock())
    for name in ("invalidate_dive_caches", "invalidate_gear_caches"):
        monkeypatch.setattr(dives_module, name, AsyncMock())
    for name in ("get_mixtures_for_dive", "get_dive_sites_for_dive", "get_gear_items_for_dive", "get_species_for_dive"):
        monkeypatch.setattr(dives_module, name, AsyncMock(return_value=[]))
    monkeypatch.setattr(dives_module.crud_dives, "create", created)
    monkeypatch.setattr(dives_module.crud_dives, "get", AsyncMock(return_value=stored))
    return created


def _install_dive_site(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    stored = DiveSiteReadInternal(id=11, user_id=CALLER_ID, uuid=ROW_UUID, name="Blue Hole", created_at=CREATED_AT)
    created = AsyncMock(return_value=stored)
    monkeypatch.setattr(dive_sites_module, "dive_site_name_exists", AsyncMock(return_value=False))
    monkeypatch.setattr(dive_sites_module._dive_site_cache, "invalidate_list", AsyncMock())
    monkeypatch.setattr(dive_sites_module.crud_dive_sites, "create", created)
    monkeypatch.setattr(dive_sites_module.crud_dive_sites, "get", AsyncMock(return_value=stored))
    return created


def _install_dive_form_preset(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    created = AsyncMock(
        return_value=DiveFormPresetReadInternal(
            id=11, user_id=CALLER_ID, uuid=ROW_UUID, name="Warm water", hidden_fields=[], created_at=CREATED_AT
        )
    )
    monkeypatch.setattr(dive_form_presets_module, "dive_form_preset_name_exists", AsyncMock(return_value=False))
    monkeypatch.setattr(dive_form_presets_module.crud_dive_form_presets, "create", created)
    return created


def _install_certification(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    created = AsyncMock(
        return_value=CertificationReadInternal(
            id=11, user_id=CALLER_ID, uuid=ROW_UUID, agency="tdi", name="Advanced Nitrox", created_at=CREATED_AT
        )
    )
    monkeypatch.setattr(certifications_module, "invalidate_certification_caches", AsyncMock())
    monkeypatch.setattr(certifications_module.crud_certifications, "create", created)
    return created


def _install_gear_item(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    stored = GearItemReadInternal(id=11, user_id=CALLER_ID, uuid=ROW_UUID, name="MK25 EVO", created_at=CREATED_AT)
    created = AsyncMock(return_value=stored)
    monkeypatch.setattr(gear_items_module, "gear_item_name_exists", AsyncMock(return_value=False))
    monkeypatch.setattr(gear_items_module, "invalidate_gear_caches", AsyncMock())
    monkeypatch.setattr(gear_items_module.crud_gear_items, "create", created)
    monkeypatch.setattr(gear_items_module.crud_gear_items, "get", AsyncMock(return_value=stored))
    return created


def _install_gear_set(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    stored = GearSetReadInternal(id=11, user_id=CALLER_ID, uuid=ROW_UUID, name="Rec", created_at=CREATED_AT)
    created = AsyncMock(return_value=stored)
    monkeypatch.setattr(gear_sets_module, "gear_set_name_exists", AsyncMock(return_value=False))
    monkeypatch.setattr(gear_sets_module, "replace_gear_items_for_set", AsyncMock())
    monkeypatch.setattr(gear_sets_module, "get_gear_items_for_set", AsyncMock(return_value=[]))
    monkeypatch.setattr(gear_sets_module, "invalidate_gear_caches", AsyncMock())
    monkeypatch.setattr(gear_sets_module.crud_gear_sets, "create", created)
    monkeypatch.setattr(gear_sets_module.crud_gear_sets, "get", AsyncMock(return_value=stored))
    return created


@dataclass(frozen=True)
class CreateRoute:
    """One `POST` route, its minimal body, and how to stub what it writes through.

    `install` returns the `crud.create` stub, because the `*CreateInternal` handed to it
    is where the owner is settled - `user_id`, taken from the session, is the only thing
    that reaches the row.
    """

    path: str
    body: dict[str, Any]
    install: Callable[[pytest.MonkeyPatch], AsyncMock]

    def __str__(self) -> str:
        return f"POST {self.path}"


CREATE_ROUTES = (
    CreateRoute("/trip", {"name": "Cebu 2026", "start_date": "2026-03-01"}, _install_trip),
    CreateRoute("/course", {"name": "Advanced Nitrox", "agency": "tdi"}, _install_course),
    CreateRoute(
        "/dive",
        {"dive_number": 1, "start_time": START_TIME, "duration": 1800, "notes": ""},
        _install_dive,
    ),
    CreateRoute("/dive-site", {"name": "Blue Hole"}, _install_dive_site),
    CreateRoute("/dive-form-preset", {"name": "Warm water", "hidden_fields": []}, _install_dive_form_preset),
    CreateRoute("/certification", {"agency": "tdi", "name": "Advanced Nitrox"}, _install_certification),
    CreateRoute("/gear-item", {"name": "MK25 EVO"}, _install_gear_item),
    CreateRoute("/gear-set", {"name": "Rec", "gear_item_uuids": []}, _install_gear_set),
)


class TestACreateBodyMayStillNameAnOwner:
    """The field is accepted and has no authority: whoever it names, the row is the
    caller's. Three bodies per route - absent, the caller's own, and somebody else's -
    because only the third distinguishes "ignored" from "happened to match".
    """

    @pytest.mark.parametrize("route", CREATE_ROUTES, ids=str)
    @pytest.mark.parametrize("owner", [None, CALLER_UUID, SOMEONE_ELSE], ids=["absent", "the caller's", "another's"])
    def test_the_row_belongs_to_the_caller(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        route: CreateRoute,
        owner: uuid_pkg.UUID | None,
    ) -> None:
        create = route.install(monkeypatch)
        body = dict(route.body) if owner is None else dict(route.body) | {"user_uuid": str(owner)}

        response = client.post(route.path, json=body)

        assert response.status_code == 201, response.text
        assert create.await_args is not None
        assert create.await_args.kwargs["object"].user_id == CALLER_ID
        assert response.json()["user_uuid"] == str(CALLER_UUID)


def _paginated(row: dict[str, Any] | None, owner: uuid_pkg.UUID) -> dict[str, Any]:
    rows = [] if row is None else [row | {"user_uuid": str(owner)}]
    return {"data": rows, "total_count": len(rows), "has_more": False, "page": 1, "items_per_page": 10}


@dataclass(frozen=True)
class ListRoute:
    """One `GET` route and the collaborator that receives the owner it decided on.

    `row` is the public shape that collaborator answers with, minus `user_uuid`: the
    stub fills that in from the keyword it was handed, so the response body reports
    which uuid the route passed on. The keyword matters as much as the value - `@cache`
    formats its key from keyword arguments, so a helper called positionally would raise
    rather than quietly key two callers onto one entry.
    """

    path: str
    owner: Any
    attr: str
    row: dict[str, Any] | None = None
    overview: dict[str, Any] | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        if self.overview is not None:
            monkeypatch.setattr(self.owner, self.attr, AsyncMock(return_value=self.overview))
            return

        async def _answer(*_: Any, user_uuid: uuid_pkg.UUID, **__: Any) -> dict[str, Any]:
            return _paginated(self.row, user_uuid)

        monkeypatch.setattr(self.owner, self.attr, _answer)

    def __str__(self) -> str:
        return f"GET {self.path}"


_PUBLIC_ROW = {"uuid": str(ROW_UUID), "created_at": CREATED_AT.isoformat()}

LIST_ROUTES = (
    ListRoute("/trips", trips_module, "_cached_read_trips", _PUBLIC_ROW | {"name": "Cebu 2026"}),
    ListRoute(
        "/courses",
        courses_module,
        "_cached_read_courses",
        _PUBLIC_ROW | {"name": "Advanced Nitrox", "status": "completed"},
    ),
    ListRoute(
        "/dives",
        dives_module,
        "_cached_read_dives",
        _PUBLIC_ROW | {"dive_number": 1, "start_time": START_TIME, "duration": 1800},
    ),
    ListRoute("/dive-sites", dive_sites_module._dive_site_cache, "read_list", _PUBLIC_ROW | {"name": "Blue Hole"}),
    ListRoute(
        "/certifications",
        certifications_module,
        "_cached_read_certifications",
        _PUBLIC_ROW | {"agency": "tdi", "name": "Advanced Nitrox"},
    ),
    ListRoute("/gear-items", gear_items_module, "_cached_read_gear_items", _PUBLIC_ROW | {"name": "MK25 EVO"}),
    ListRoute("/gear-sets", gear_sets_module, "_cached_read_gear_sets", _PUBLIC_ROW | {"name": "Rec"}),
    ListRoute(
        "/gear-service-schedules",
        gear_service_module,
        "_cached_read_schedules",
        _PUBLIC_ROW
        | {"kind": "service", "starts_on": "2026-01-01", "gear_item_uuid": str(ROW_UUID), "interval_months": 12},
    ),
    ListRoute(
        "/gear-service-records",
        gear_service_module,
        "_cached_read_records",
        _PUBLIC_ROW | {"kind": "service", "serviced_on": "2026-01-01", "gear_item_uuid": str(ROW_UUID)},
    ),
    # The two that used the value for the refusal alone answer an overview rather than a
    # page, and now hand it nowhere - so there is no owner in the response to read.
    ListRoute(
        "/certifications-expiring",
        certifications_module,
        "_cached_read_expiring",
        overview={"data": [], "truncated": False},
    ),
    ListRoute("/gear-service-due", gear_service_module, "_cached_read_due", overview={"data": [], "truncated": False}),
)


class TestAListRouteIgnoresAUserUuidInTheQuery:
    """`?user_uuid=` names nothing the route reads. It answers the caller's own page
    either way - never the named account's, and never a 403.

    `/dive-form-presets` is absent here and has its own case below: it is the one list
    route with no cached read helper to stub.
    """

    @pytest.mark.parametrize("route", LIST_ROUTES, ids=str)
    def test_the_page_is_the_same_with_and_without_it(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, route: ListRoute
    ) -> None:
        route.install(monkeypatch)

        without = client.get(route.path)
        with_another = client.get(route.path, params={"user_uuid": str(SOMEONE_ELSE)})

        assert (without.status_code, with_another.status_code) == (200, 200), without.text
        assert without.json() == with_another.json()

    @pytest.mark.parametrize("route", [route for route in LIST_ROUTES if route.row is not None], ids=str)
    def test_the_rows_are_stamped_with_the_session_not_the_query(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, route: ListRoute
    ) -> None:
        route.install(monkeypatch)

        page = client.get(route.path, params={"user_uuid": str(SOMEONE_ELSE)}).json()

        assert [row["user_uuid"] for row in page["data"]] == [str(CALLER_UUID)]


class TestThePresetListIgnoresItToo:
    """The one list route that stamps the owner itself rather than through a cached
    helper - nothing embeds a preset, so it is deliberately uncached."""

    def test_the_rows_are_stamped_with_the_session_not_the_query(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        async def _stored(**_: Any) -> dict[str, Any]:
            # A fresh page per call: the route rewrites `data["data"]` into public shapes
            # in place, so a shared return value would hand the second request the first
            # one's already-stamped rows.
            row = {
                "id": 11,
                "user_id": CALLER_ID,
                "uuid": ROW_UUID,
                "name": "Warm water",
                "hidden_fields": [],
                "created_at": CREATED_AT,
            }
            return {"data": [row], "total_count": 1}

        monkeypatch.setattr(dive_form_presets_module.crud_dive_form_presets, "get_multi", _stored)

        without = client.get("/dive-form-presets")
        with_another = client.get("/dive-form-presets", params={"user_uuid": str(SOMEONE_ELSE)})

        assert (without.status_code, with_another.status_code) == (200, 200), without.text
        assert without.json() == with_another.json()
        assert [row["user_uuid"] for row in without.json()["data"]] == [str(CALLER_UUID)]
