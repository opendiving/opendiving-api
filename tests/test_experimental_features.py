"""Experimental features: the predicate, the route gate, and the document an instance serves.

Every test here sets `EXPERIMENTAL_FEATURES` itself: a developer's `src/.env` may say `all`, and
CI has no such file.

The routes are a small application of the test's own, built with `create_application` and no
`TestClient` context manager, so nothing here needs Postgres or Redis.
"""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from src.app.api.dependencies import get_current_user
from src.app.core.config import settings
from src.app.core.experimental import experimental_feature_on, gating_features, require_experimental_feature
from src.app.core.setup import OPENAPI_URL, create_application
from tests.helpers.openapi import document_with_every_feature_on, served_document_with_every_feature_on
from tests.helpers.routes import iter_api_routes

SIGNED_IN = {"id": 1, "uuid": "00000000-0000-0000-0000-000000000001", "is_superuser": False}


@pytest.fixture(autouse=True)
def nothing_on() -> Iterator[None]:
    with patch.object(settings, "EXPERIMENTAL_FEATURES", None):
        yield


class Shared(BaseModel):
    label: str = Field(description="Shared by a gated route and an ungated one")


class TrialDepth(BaseModel):
    metres: float = Field(description="Reached only through the gated routes, one model down")


class TrialEntry(BaseModel):
    depth: TrialDepth = Field(description="The nested model")
    shared: Shared = Field(description="The shared model")


class TwinDepth(BaseModel):
    metres: float = Field(description="`TrialDepth`, for the ungated twin")


class TwinEntry(BaseModel):
    """`TrialEntry` field for field, so the twin answers as the gated route does without reaching
    its models."""

    depth: TwinDepth = Field(description="The nested model")
    shared: Shared = Field(description="The shared model")


def _router(*, gated: bool) -> APIRouter:
    """`/plain` and `/twin` ungated, and with `gated`: a router gated on `trial`, one of whose
    routes is also gated on `second`."""
    router = APIRouter(prefix="/api/v1")

    @router.post("/plain", response_model=Shared)
    async def plain(body: Shared) -> Shared:
        """An ungated route sharing a model with the gated ones."""
        return body

    @router.post("/twin", response_model=TwinEntry)
    async def twin(body: TwinEntry, user: dict = Depends(get_current_user)) -> TwinEntry:
        """The gated route's body, ungated."""
        return body

    if gated:
        trial = APIRouter(dependencies=[Depends(require_experimental_feature("trial"))])

        @trial.post("/trial", response_model=TrialEntry)
        async def create_trial(body: TrialEntry, user: dict = Depends(get_current_user)) -> TrialEntry:
            """A gated route behind a sign-in."""
            return body

        @trial.get("/trial/open")
        async def read_trial() -> dict[str, bool]:
            """A gated route anyone may call."""
            return {"open": True}

        @trial.get("/both", dependencies=[Depends(require_experimental_feature("second"))])
        async def read_both(entry: TrialDepth = Depends()) -> TrialDepth:
            """A route gated twice, by its router and by its own gate."""
            return entry

        router.include_router(trial)

    return router


def _app(*, gated: bool = True) -> FastAPI:
    return create_application(router=_router(gated=gated), settings=settings, apply_migrations_on_start=False)


def _signed_in(app: FastAPI) -> TestClient:
    app.dependency_overrides[get_current_user] = lambda: SIGNED_IN
    return TestClient(app)


ENTRY = {"depth": {"metres": 18.5}, "shared": {"label": "x"}}


class TestThePredicate:
    @pytest.mark.parametrize(
        ("setting", "expected"),
        [(None, False), ("", False), ("other", False), ("trial", True), ("other,trial", True), ("all", True)],
    )
    def test_a_feature_is_on_where_the_setting_names_it_or_says_all(self, setting: str | None, expected: bool) -> None:
        with patch.object(settings, "EXPERIMENTAL_FEATURES", setting):
            assert experimental_feature_on("trial") is expected

    @pytest.mark.parametrize("setting", [None, "trial", "all"])
    @pytest.mark.parametrize("name", ["all", "Trial", "trial_v2", ""])
    def test_a_gate_cannot_name_what_no_feature_is_called(self, name: str, setting: str | None) -> None:
        """Whatever the setting says, so a mistyped name fails on the stack that says `all` as it
        would where none is on."""
        with patch.object(settings, "EXPERIMENTAL_FEATURES", setting):
            with pytest.raises(ValueError, match=re.escape(repr(name))):
                require_experimental_feature(name)
            with pytest.raises(ValueError, match=re.escape(repr(name))):
                experimental_feature_on(name)

    def test_the_gate_is_found_again_in_the_routes_dependencies(self) -> None:
        features = {route.key: gating_features(route.dependant) for route in iter_api_routes(_app())}

        assert features[("POST", "/api/v1/trial")] == {"trial"}
        assert features[("GET", "/api/v1/both")] == {"trial", "second"}
        assert features[("POST", "/api/v1/plain")] == set()


class TestAGatedRoute:
    """Off, a gated route answers what the app answers for a path it has no route for, whoever
    asks; on, what its ungated twin answers."""

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [("POST", "/api/v1/trial", ENTRY), ("GET", "/api/v1/trial/open", None), ("GET", "/api/v1/both", None)],
    )
    @pytest.mark.parametrize("signed_in", [False, True])
    def test_off_it_answers_as_an_unrouted_path(
        self, method: str, path: str, body: dict[str, Any] | None, signed_in: bool
    ) -> None:
        app = _app()
        client = _signed_in(app) if signed_in else TestClient(app)

        gated = client.request(method, path, json=body)
        unrouted = client.request(method, "/api/v1/nowhere", json=body)

        assert unrouted.status_code == 404
        assert (gated.status_code, gated.json()) == (unrouted.status_code, unrouted.json())

    def test_off_it_answers_before_the_sign_in_check(self) -> None:
        """The ungated twin refuses the same anonymous request with 401, so the 404 is the gate's."""
        client = TestClient(_app())

        assert client.post("/api/v1/twin", json=ENTRY).status_code == 401
        assert client.post("/api/v1/trial", json=ENTRY).status_code == 404

    @pytest.mark.parametrize("setting", ["trial", "all"])
    @pytest.mark.parametrize("signed_in", [False, True])
    def test_on_it_answers_as_its_ungated_twin(self, setting: str, signed_in: bool) -> None:
        app = _app()
        client = _signed_in(app) if signed_in else TestClient(app)

        with patch.object(settings, "EXPERIMENTAL_FEATURES", setting):
            gated = client.post("/api/v1/trial", json=ENTRY)
            twin = client.post("/api/v1/twin", json=ENTRY)
            invalid = client.post("/api/v1/trial", json={"depth": {}})
            invalid_twin = client.post("/api/v1/twin", json={"depth": {}})

        assert (gated.status_code, gated.json()) == (twin.status_code, twin.json())
        assert (invalid.status_code, invalid.json()) == (invalid_twin.status_code, invalid_twin.json())

    def test_patching_the_setting_switches_it_in_the_same_process(self) -> None:
        client = TestClient(_app())

        assert client.get("/api/v1/trial/open").status_code == 404
        with patch.object(settings, "EXPERIMENTAL_FEATURES", "trial"):
            assert client.get("/api/v1/trial/open").json() == {"open": True}
        assert client.get("/api/v1/trial/open").status_code == 404

    @pytest.mark.parametrize(
        ("setting", "status"), [("trial", 404), ("second", 404), ("trial,second", 200), ("all", 200)]
    )
    def test_a_route_needs_every_feature_that_gates_it(self, setting: str, status: int) -> None:
        with patch.object(settings, "EXPERIMENTAL_FEATURES", setting):
            assert TestClient(_app()).get("/api/v1/both", params={"metres": 3}).status_code == status


class TestTheDocument:
    def test_off_it_is_the_document_of_the_app_without_the_gated_routes(self) -> None:
        """Whole, so a model only the gated routes reach - `TrialDepth`, nested under
        `TrialEntry` - is as absent as their paths."""
        assert _app().openapi() == _app(gated=False).openapi()

    def test_on_it_lists_the_gated_operations_and_their_models(self) -> None:
        with patch.object(settings, "EXPERIMENTAL_FEATURES", "all"):
            document = _app().openapi()

        assert {"/api/v1/trial", "/api/v1/trial/open", "/api/v1/both"} <= set(document["paths"])
        assert {"TrialEntry", "TrialDepth"} <= set(document["components"]["schemas"])

    def test_the_comparison_sees_what_the_gate_hides(self) -> None:
        """Without this the first test passes for gated routes that never reached a document."""
        with patch.object(settings, "EXPERIMENTAL_FEATURES", "all"):
            on = _app().openapi()

        assert on != _app(gated=False).openapi()
        assert "TrialDepth" not in _app(gated=False).openapi()["components"]["schemas"]

    def test_each_feature_hides_only_what_it_gates(self) -> None:
        with patch.object(settings, "EXPERIMENTAL_FEATURES", "trial"):
            paths = set(_app().openapi()["paths"])

        assert "/api/v1/trial" in paths
        assert "/api/v1/both" not in paths

    def test_the_served_document_is_the_applications_own(self) -> None:
        """`/api/v1/openapi.json` serves `app.openapi()`, credentials annotated, so it hides what
        that hides and nothing else."""
        app = _app()
        served = TestClient(app).get(OPENAPI_URL).json()

        assert served == app.openapi()
        assert "/api/v1/trial" not in served["paths"]
        assert "/api/v1/plain" in served["paths"]


# What finds a walk over a document's operations: the derivation the guard below rests on.
WALK = re.compile(r'\["paths"\]\.items\(\)|paths\.items\(\)')
TESTS = Path(__file__).resolve().parent


class TestTheWalkingGuardsSeeEveryGatedOperation:
    """A guard over the document's operations reads a document whose application has every
    feature on: CI runs with every feature off, and a guard reading the served document there
    would never check a gated route."""

    def test_the_helpers_include_a_gated_operation_with_the_setting_unset(self) -> None:
        app = _app()

        assert "/api/v1/trial" not in app.openapi()["paths"]
        assert "/api/v1/trial" in document_with_every_feature_on(app)["paths"]
        assert "/api/v1/trial" in served_document_with_every_feature_on(app)["paths"]
        assert "/api/v1/trial" not in app.openapi()["paths"]

    def test_every_walk_reads_through_them(self) -> None:
        walks = sorted(
            path.relative_to(TESTS).as_posix()
            for path in TESTS.rglob("*.py")
            if WALK.search(path.read_text()) and path != Path(__file__).resolve()
        )
        unguarded = [
            walk
            for walk in walks
            if not re.search(r"\b(served_)?document_with_every_feature_on\(", (TESTS / walk).read_text())
        ]

        assert walks, "the walk pattern found nothing, so this guard checks nothing"
        assert not unguarded, "read these walks' documents through tests/helpers/openapi.py:\n" + "\n".join(unguarded)
