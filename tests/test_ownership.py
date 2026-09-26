"""Tests for `api.dependencies.fetch_owned_or_raise` and the contract it publishes.

This is the single implementation behind every "fetch one resource the caller owns"
route in `api/v1` - dives, dive sites, trips, gear items, gear sets and certifications
all reach it through a thin per-entity wrapper. It used to be seven hand-rolled copies,
so these tests exist to keep the one that replaced them honest.

Mostly unit tests of the function, plus the classes at the bottom that drive every real
`{uuid}` route over HTTP. Those are not redundant: "someone else's row is
indistinguishable from a missing one" is a security contract, and a contract asserted only
one layer below the wire is one a route can quietly stop honouring.

`TestEveryUuidRouteIsAccountedFor` is the structural guard over the lot - it enumerates the
app's real route table and fails on a route that names a resource by uuid without being
listed here. See its docstring for what that catches that the rest of this file does not.
"""

import importlib
import logging
import uuid as uuid_pkg
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.app.api import router
from src.app.api.dependencies import current_session_uuid, fetch_owned_or_raise, get_current_user
from src.app.core.config import settings
from src.app.core.exceptions.http_exceptions import NotFoundException
from src.app.core.setup import create_application
from src.app.main import app
from tests.helpers.routes import iter_api_routes


class _Row(BaseModel):
    id: int
    user_id: int


class _SoftDeletingModel:
    """Stands in for `Certification`, the last model routed through `fetch_owned_or_raise`
    that still carries the column."""

    is_deleted = False


class _HardDeletingModel:
    """Stands in for `Trip`/`DiveSite`/`GearItem`/`GearSet`."""


def _crud(row: _Row | None, *, model: type = _SoftDeletingModel) -> Any:
    """A stubbed FastCRUD.

    `model` is set explicitly rather than left to `AsyncMock`'s auto-attribute, which
    answers `hasattr` for anything and would make the liveness-filter tests below pass
    whichever branch ran.
    """
    crud = AsyncMock()
    crud.model = model
    crud.get = AsyncMock(return_value=row)
    return crud


CALLER = {"id": 7, "uuid": uuid_pkg.uuid4()}


class TestFetchOwnedOrRaise:
    @pytest.mark.asyncio
    async def test_returns_the_row_when_the_caller_owns_it(self):
        row = _Row(id=1, user_id=7)

        result = await fetch_owned_or_raise(
            db=AsyncMock(),
            crud=_crud(row),
            uuid=uuid_pkg.uuid4(),
            current_user=CALLER,
            schema=_Row,
            not_found_message="Thing not found",
        )

        assert result is row

    @pytest.mark.asyncio
    async def test_missing_row_is_a_404(self):
        """Same exception and same message as someone else's row below - deliberately."""
        with pytest.raises(NotFoundException, match="Thing not found"):
            await fetch_owned_or_raise(
                db=AsyncMock(),
                crud=_crud(None),
                uuid=uuid_pkg.uuid4(),
                current_user=CALLER,
                schema=_Row,
                not_found_message="Thing not found",
            )

    @pytest.mark.asyncio
    async def test_someone_elses_row_is_a_404_too(self):
        """Indistinguishable from a missing row, down to the message: a 403 here would
        confirm that an opaque uuid names a real row belonging to someone.
        """
        with pytest.raises(NotFoundException, match="Thing not found"):
            await fetch_owned_or_raise(
                db=AsyncMock(),
                crud=_crud(_Row(id=1, user_id=8)),
                uuid=uuid_pkg.uuid4(),
                current_user=CALLER,
                schema=_Row,
                not_found_message="Thing not found",
            )

    @pytest.mark.asyncio
    async def test_the_two_cases_are_still_distinguishable_in_the_log(self, caplog):
        """The client is told nothing, but the server log keeps the distinction - it is
        what makes "the client says 404" debuggable.

        Captures at `DEBUG` so both lines are visible, then asserts each one's level
        rather than filtering by it. Capturing at the level under test would also catch a
        downgrade - the record would vanish - but it fails as "expected 2, got 1", which
        names neither the line nor the level. This says what is required and, when it
        breaks, says what the level actually is. The wrong-owner threshold is the load-
        bearing half; see the note in `fetch_owned_or_raise` for why it must clear
        `WARNING` specifically.
        """
        uuid = uuid_pkg.uuid4()

        with caplog.at_level(logging.DEBUG, logger="src.app.api.dependencies"):
            with pytest.raises(NotFoundException):
                await fetch_owned_or_raise(
                    db=AsyncMock(),
                    crud=_crud(None),
                    uuid=uuid,
                    current_user=CALLER,
                    schema=_Row,
                    not_found_message="Thing not found",
                )
            with pytest.raises(NotFoundException):
                await fetch_owned_or_raise(
                    db=AsyncMock(),
                    crud=_crud(_Row(id=1, user_id=8)),
                    uuid=uuid,
                    current_user=CALLER,
                    schema=_Row,
                    not_found_message="Thing not found",
                )

        # `caplog`'s handler sits on the root logger and catches anything that
        # propagates, so narrow to ours rather than assuming these are the only two.
        records = [record for record in caplog.records if record.name.endswith("api.dependencies")]

        assert len(records) == 2
        absent, wrong_owner = records

        # Deliberately lopsided: only the wrong-owner line has to survive the default
        # level, and only it is not caller-paced.
        assert wrong_owner.levelno >= logging.WARNING
        assert absent.levelno < logging.WARNING

        assert "no _Row with uuid" in absent.getMessage()
        assert "belongs to user_id 8, caller is user_id 7" in wrong_owner.getMessage()

    @pytest.mark.asyncio
    async def test_soft_deleted_rows_are_excluded_by_default(self):
        crud = _crud(_Row(id=1, user_id=7))

        await fetch_owned_or_raise(
            db=AsyncMock(),
            crud=crud,
            uuid=uuid_pkg.uuid4(),
            current_user=CALLER,
            schema=_Row,
            not_found_message="Thing not found",
        )

        assert crud.get.await_args.kwargs["is_deleted"] is False

    @pytest.mark.asyncio
    async def test_include_deleted_drops_the_filter(self):
        """For the routes that legitimately act on a soft-deleted row - restoring a
        certification, say.
        """
        crud = _crud(_Row(id=1, user_id=7))

        await fetch_owned_or_raise(
            db=AsyncMock(),
            crud=crud,
            uuid=uuid_pkg.uuid4(),
            current_user=CALLER,
            schema=_Row,
            not_found_message="Thing not found",
            include_deleted=True,
        )

        assert "is_deleted" not in crud.get.await_args.kwargs

    @pytest.mark.asyncio
    async def test_a_hard_deleting_model_is_never_filtered(self):
        """The filter has to be conditional rather than unconditional, and this is what
        that costs if it is not: FastCRUD's `get_model_column` raises `ValueError` for a
        column the model lacks instead of ignoring it, so an unconditional
        `is_deleted=False` would turn every `GET`/`PATCH`/`DELETE` on a trip, dive site,
        gear item or gear set into a 500.
        """
        crud = _crud(_Row(id=1, user_id=7), model=_HardDeletingModel)

        await fetch_owned_or_raise(
            db=AsyncMock(),
            crud=crud,
            uuid=uuid_pkg.uuid4(),
            current_user=CALLER,
            schema=_Row,
            not_found_message="Thing not found",
        )

        assert "is_deleted" not in crud.get.await_args.kwargs

    @pytest.mark.asyncio
    async def test_the_row_is_looked_up_by_public_uuid(self):
        """Never by the internal integer id - that's the whole reason the public API
        exposes uuids.
        """
        crud = _crud(_Row(id=1, user_id=7))
        uuid = uuid_pkg.uuid4()

        await fetch_owned_or_raise(
            db=AsyncMock(),
            crud=crud,
            uuid=uuid,
            current_user=CALLER,
            schema=_Row,
            not_found_message="Thing not found",
        )

        assert crud.get.await_args.kwargs["uuid"] == uuid


class TestEveryOwnedRouteUsesIt:
    def test_no_route_file_hand_rolls_the_check(self):
        """The regression this guards: the same fetch-and-check-the-owner block was
        copy-pasted into seven route files, so a fix to one (notably the "authorize
        before `@cache`" ordering) silently missed the other six.
        """
        from pathlib import Path

        routes_dir = Path(__file__).resolve().parents[1] / "src" / "app" / "api" / "v1"
        offenders = []

        for path in sorted(routes_dir.glob("*.py")):
            source = path.read_text()
            # The signature of the old inline block: a cast straight into an ownership
            # comparison against the caller's internal id.
            if 'user_id != current_user["id"]' in source:
                offenders.append(path.name)

        assert offenders == [], f"hand-rolled ownership check(s) still in: {offenders}"


@dataclass(frozen=True)
class OwnedRoute:
    """One `{uuid}` route, and how to make its ownership lookup come back empty.

    `crud` names the module-level FastCRUD singleton the route's lookup goes through;
    stubbing its `get` is what stands in for both "no such row" and "somebody else's row"
    without a database. `extra` is whatever it takes to get *past body validation* and
    reach the handler at all - FastAPI validates the body before calling it, so a `PUT`
    with no multipart part answers 422 and never exercises the check.
    """

    method: str
    path: str
    crud: str
    detail: str
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return self.method, self.path

    def __str__(self) -> str:
        return f"{self.method} {self.path}"

    def url(self, uuid: uuid_pkg.UUID) -> str:
        """The path with every placeholder filled in.

        `{rid}` and `{fid}` get a uuid of their own rather than the dive's - a real client
        would send an unrelated one - and it need not name anything, because every route
        carrying one resolves the dive first and answers 404 before looking at it. That
        ordering is the property this whole class exists to pin, one identifier deeper.
        """
        return (
            self.path.replace("{uuid}", str(uuid))
            .replace("{side}", "front")
            .replace("{rid}", str(uuid_pkg.uuid4()))
            .replace("{fid}", str(uuid_pkg.uuid4()))
        )

    def crud_singleton(self) -> Any:
        module, _, name = self.crud.partition(":")
        return getattr(importlib.import_module(module), name)


# The three methods every owned resource exposes, and what each needs to get past body
# validation. An empty `PATCH` is enough: every update schema is all-optional, and the
# ownership check runs before any field is looked at.
_CRUD_METHODS: tuple[tuple[str, dict[str, Any]], ...] = (("GET", {}), ("PATCH", {"json": {}}), ("DELETE", {}))

# A file part small enough to be free and well-formed enough to validate. Its *content*
# never matters: every route below raises on ownership before looking at it.
_A_FILE = {"file": ("export.xml", b"<dive/>", "text/xml")}

# Every `{uuid}` route whose ownership goes through `fetch_owned_or_raise`.
FETCH_OWNED_ROUTES = [
    *(
        OwnedRoute(method, "/api/v1/dive/{uuid}", "src.app.api.v1.dives:crud_dives", "Dive not found", extra)
        for method, extra in _CRUD_METHODS
    ),
    OwnedRoute("GET", "/api/v1/dive/{uuid}/neighbors", "src.app.api.v1.dives:crud_dives", "Dive not found"),
    # The recordings and their files. Every one of them resolves the *dive* first and the
    # recording or file second, so someone else's dive uuid is a 404 before the `{rid}`/
    # `{fid}` in the path is looked at - which is what makes a second identifier here no
    # wider a surface than the dive uuid already was.
    OwnedRoute(
        "GET", "/api/v1/dive/{uuid}/recording/{rid}/profile", "src.app.api.v1.dives:crud_dives", "Dive not found"
    ),
    OwnedRoute("GET", "/api/v1/dive/{uuid}/file/{fid}", "src.app.api.v1.dives:crud_dives", "Dive not found"),
    OwnedRoute(
        "POST",
        "/api/v1/dive/{uuid}/recordings",
        "src.app.api.v1.dives:crud_dives",
        "Dive not found",
        {"files": _A_FILE, "data": {"file_token": "not-looked-at"}},
    ),
    OwnedRoute("DELETE", "/api/v1/dive/{uuid}/file/{fid}", "src.app.api.v1.dives:crud_dives", "Dive not found"),
    OwnedRoute("DELETE", "/api/v1/dive/{uuid}/recording/{rid}", "src.app.api.v1.dives:crud_dives", "Dive not found"),
    OwnedRoute(
        "PATCH",
        "/api/v1/dive/{uuid}/recording/{rid}",
        "src.app.api.v1.dives:crud_dives",
        "Dive not found",
        {"json": {"primary": True}},
    ),
    *(
        OwnedRoute(
            method,
            "/api/v1/dive-site/{uuid}",
            "src.app.api.v1.dive_sites:crud_dive_sites",
            "Dive site not found",
            extra,
        )
        for method, extra in _CRUD_METHODS
    ),
    *(
        OwnedRoute(method, "/api/v1/trip/{uuid}", "src.app.api.v1.trips:crud_trips", "Trip not found", extra)
        for method, extra in _CRUD_METHODS
    ),
    *(
        OwnedRoute(method, "/api/v1/course/{uuid}", "src.app.api.v1.courses:crud_courses", "Course not found", extra)
        for method, extra in _CRUD_METHODS
    ),
    *(
        OwnedRoute(
            method, "/api/v1/contact/{uuid}", "src.app.api.v1.contacts:crud_contacts", "Contact not found", extra
        )
        for method, extra in _CRUD_METHODS
    ),
    *(
        OwnedRoute(
            method,
            "/api/v1/gear-item/{uuid}",
            "src.app.api.v1.gear_items:crud_gear_items",
            "Gear item not found",
            extra,
        )
        for method, extra in _CRUD_METHODS
    ),
    *(
        OwnedRoute(
            method, "/api/v1/gear-set/{uuid}", "src.app.api.v1.gear_sets:crud_gear_sets", "Gear set not found", extra
        )
        for method, extra in _CRUD_METHODS
    ),
    *(
        OwnedRoute(
            method,
            "/api/v1/dive-form-preset/{uuid}",
            "src.app.api.v1.dive_form_presets:crud_dive_form_presets",
            "Dive form preset not found",
            extra,
        )
        for method, extra in _CRUD_METHODS
    ),
    *(
        OwnedRoute(
            method,
            "/api/v1/certification/{uuid}",
            "src.app.api.v1.certifications:crud_certifications",
            "Certification not found",
            extra,
        )
        for method, extra in _CRUD_METHODS
    ),
    OwnedRoute(
        "GET",
        "/api/v1/certification/{uuid}/file/{side}",
        "src.app.api.v1.certifications:crud_certifications",
        "Certification not found",
    ),
    OwnedRoute(
        "PUT",
        "/api/v1/certification/{uuid}/file/{side}",
        "src.app.api.v1.certifications:crud_certifications",
        "Certification not found",
        {"files": _A_FILE},
    ),
    OwnedRoute(
        "DELETE",
        "/api/v1/certification/{uuid}/file/{side}",
        "src.app.api.v1.certifications:crud_certifications",
        "Certification not found",
    ),
    # Only PATCH and DELETE: passkeys have no single-item GET, since `GET /user/passkeys`
    # returns the caller's whole (capped) list and there is nothing per-row to fetch.
    *(
        OwnedRoute(
            method,
            "/api/v1/user/passkey/{uuid}",
            "src.app.api.v1.passkeys:crud_webauthn_credentials",
            "Passkey not found",
            extra,
        )
        for method, extra in _CRUD_METHODS
        if method != "GET"
    ),
    # DELETE alone: a session has no single-item GET or PATCH - `GET /user/sessions` returns
    # the caller's whole live list and there is nothing about a row a diver may edit.
    OwnedRoute(
        "DELETE",
        "/api/v1/user/session/{uuid}",
        "src.app.api.v1.sessions:crud_user_sessions",
        "Session not found",
    ),
    # DELETE alone, for the same reason as the session above: `GET /user/invitations`
    # returns the caller's whole list and an invitation has nothing a diver may edit.
    # Reachable at all only on an `invite`-mode instance, which is the default the suite
    # runs under - in `open` mode the route answers 404 before the ownership check, with a
    # different message, and that is the self-hiding the web's settings card depends on.
    OwnedRoute(
        "DELETE",
        "/api/v1/user/invitation/{uuid}",
        "src.app.api.v1.invitations:crud_invitations",
        "Invitation not found",
    ),
]

# The other family. These six resolve ownership in SQL rather than through
# `fetch_owned_or_raise` - `resolve_schedule_for_user`/`resolve_record_for_user` filter on
# `user_id` in the `WHERE` clause and answer `None` for both "absent" and "not yours" (see
# their docstrings). That collapse is the whole design, and it is also why they need their
# own assertions: at this seam the two cases are already one, so the thing worth checking
# is that the route hands the resolver the *caller's own* id.
RESOLVER_OWNED_ROUTES = [
    *(
        OwnedRoute(
            method,
            "/api/v1/gear-service-schedule/{uuid}",
            "src.app.api.v1.gear_service:resolve_schedule_for_user",
            "Service schedule not found",
            extra,
        )
        for method, extra in _CRUD_METHODS
    ),
    *(
        OwnedRoute(
            method,
            "/api/v1/gear-service-record/{uuid}",
            "src.app.api.v1.gear_service:resolve_record_for_user",
            "Service record not found",
            extra,
        )
        for method, extra in _CRUD_METHODS
    ),
]

# The two kinds of entry that belong here. A resource with no owner at all: the species
# catalog is global (`models/species.py`), every row a fact about the ocean that every account
# may reference, so there is no owner to compare a caller against and a species uuid is an
# existence oracle for nothing private - `crud/crud_species.py` documents why the `user_id`
# filter its siblings carry is an omission on purpose. And a resource whose owner is not the
# caller but someone a credential in the path names: a check-in link's diver, whose card the
# route resolves against the link rather than against a signed-in caller.
UNOWNED_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/species/{uuid}"): "The species catalog is global - there is no owner to compare against.",
    ("GET", "/api/v1/species/{uuid}/photo"): (
        "Same global catalog, and this one does not even authenticate - see its entry in "
        "`test_route_authentication.ANONYMOUS_BY_DESIGN`. The bytes are a freely licensed "
        "Commons file shared by every account, so there is no owner to compare against and "
        "nothing an ownership check could protect."
    ),
    ("GET", "/api/v1/checkin/{token}/certification/{uuid}/front"): (
        "Anonymous, so there is no caller to own it: the owner is the diver the check-in link names, "
        "and `find_card_front` scopes the card to that diver's `user_id` - someone else's card is the "
        "same 404 as a dead token. See its entry in `test_route_authentication.ANONYMOUS_BY_DESIGN`."
    ),
}

OWNER_ID = 7
SOMEONE_ELSE_ID = 8
OWNER = {"id": OWNER_ID, "uuid": uuid_pkg.uuid4(), "username": "ada", "is_superuser": False}
CALLERS_SESSION = uuid_pkg.uuid4()


@pytest.fixture(scope="module")
def owned_app() -> Any:
    """Its own app with `apply_migrations_on_start=False`, like `test_export_endpoints.py`.

    The shared `client` fixture opens `src.app.main`'s app, whose startup hook connects to
    Postgres - which would put these in the database-backed subset for no reason. Nothing
    below the route runs here anyway: every one of these raises before it reaches its
    cached read helper, which is the ordering `TestEveryOwnedRouteUsesIt` exists to
    protect.

    Worth saying plainly, since the guard below is one whose silent absence is the failure
    mode: this is *not* one of the `db_available()`-gated modules. It needs no database, so
    it runs on a cold checkout and in CI alike, and it cannot be skipped into passing the
    way `POSTGRES_SERVER` unset skips the Postgres-backed suites (see CONTRIBUTING.md).
    """
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


@pytest.fixture
def signed_in_client(owned_app: Any) -> Generator[TestClient]:
    """A caller who is signed in, which now means two things rather than one.

    `current_session_uuid` is overridden alongside `get_current_user` because
    `DELETE /user/session/{uuid}` depends on both, and that one reaches `oauth2_scheme`
    directly - so without this the request 401s on a missing `Authorization` header before
    the ownership check it is here to exercise ever runs, and the route reads as broken
    when the fixture is what is incomplete.

    A uuid nothing else uses, so the route's current-session 409 can never fire here: these
    cases are about somebody *else's* row, and a collision would swap the 404 they assert
    for a conflict.
    """
    owned_app.dependency_overrides[get_current_user] = lambda: OWNER
    owned_app.dependency_overrides[current_session_uuid] = lambda: CALLERS_SESSION
    try:
        with TestClient(owned_app) as test_client:
            yield test_client
    finally:
        # The app is module-scoped, so a failing test would otherwise leak the override
        # into the next one.
        owned_app.dependency_overrides = {}


class TestSomeoneElsesRowIsIndistinguishableOverHTTP:
    """The security contract, asserted at the wire rather than one layer below it.

    A 403 here would confirm that an opaque uuid names a real row belonging to *someone*.
    Both the status **and** the body have to match a genuinely missing row - a distinct
    `detail` would be the same oracle wearing a 404.

    Every method of every route, not just the reads. The mutating ones are the likeliest
    to grow a bespoke pre-check later, and the sub-resources (`/file`, `/profile`,
    `/neighbors`) are the likeliest to be added without one at all - a new verb on an
    existing resource looks like it inherits the parent's guard and does not.
    """

    @pytest.mark.parametrize("route", FETCH_OWNED_ROUTES, ids=str)
    def test_a_row_owned_by_someone_else_reads_as_a_missing_one(
        self, signed_in_client: TestClient, monkeypatch: Any, route: OwnedRoute
    ):
        crud = route.crud_singleton()
        url = route.url(uuid_pkg.uuid4())

        monkeypatch.setattr(crud, "get", AsyncMock(return_value=None))
        absent = signed_in_client.request(route.method, url, **route.extra)

        monkeypatch.setattr(crud, "get", AsyncMock(return_value=_Row(id=1, user_id=SOMEONE_ELSE_ID)))
        someone_elses = signed_in_client.request(route.method, url, **route.extra)

        assert absent.status_code == 404
        assert someone_elses.status_code == absent.status_code
        assert someone_elses.json() == absent.json() == {"detail": route.detail}


class TestTheResolverRoutesScopeToTheCaller:
    """The gear-service half, where the owner filter is in the `WHERE` clause.

    Stubbing the resolver can only prove that `None` becomes a 404, which on its own would
    pass just as well for a resolver that never filtered by owner. So the assertion that
    carries the weight is the second one: the route passed *its caller's* `user_id` down.
    A route that resolved the row unscoped and compared afterwards - or not at all - fails
    it.
    """

    @pytest.mark.parametrize("route", RESOLVER_OWNED_ROUTES, ids=str)
    def test_the_resolver_is_called_with_the_callers_id_and_none_is_a_404(
        self, signed_in_client: TestClient, monkeypatch: Any, route: OwnedRoute
    ):
        module_name, _, resolver_name = route.crud.partition(":")
        resolver = AsyncMock(return_value=None)
        monkeypatch.setattr(importlib.import_module(module_name), resolver_name, resolver)

        response = signed_in_client.request(route.method, route.url(uuid_pkg.uuid4()), **route.extra)

        assert response.status_code == 404
        assert response.json() == {"detail": route.detail}
        assert resolver.await_args is not None, "the route answered 404 without consulting the resolver at all"
        assert resolver.await_args.kwargs["user_id"] == OWNER_ID


class TestEveryUuidRouteIsAccountedFor:
    """The drift guard one level up, and the reason the two tables above are exhaustive.

    `TestEveryOwnedRouteUsesIt` catches a route that hand-rolls the check. Neither it nor
    the behavioural tests above notice a route that resolves no ownership *at all* - a new
    `GET /dive/{uuid}/something` that reads by uuid and never asks whose it is is covered
    by nothing, which is precisely how a hand-audited list goes stale. 33 of these routes
    were verified by hand once; this is what keeps the 34th honest.

    Enumerated from the app's real route table rather than from a list, because a list is
    the thing being checked - the same shape as
    `test_every_update_schema_is_accounted_for`.
    """

    def test_every_uuid_route_resolves_ownership(self) -> None:
        covered = {route.key for route in FETCH_OWNED_ROUTES} | {route.key for route in RESOLVER_OWNED_ROUTES}
        covered |= UNOWNED_ROUTES.keys()

        unaccounted = sorted(
            route.key for route in iter_api_routes(app) if "{uuid}" in route.path and route.key not in covered
        )

        assert not unaccounted, (
            "these routes name a resource by uuid and nothing here checks whose it is:\n"
            + "\n".join(f"  {method:6} {path}" for method, path in unaccounted)
            + "\n\nResolve ownership in the handler (`fetch_owned_or_raise`, or a `user_id`-scoped"
            + "\nresolver) and add the route to `FETCH_OWNED_ROUTES` or `RESOLVER_OWNED_ROUTES`."
            + "\nOnly a resource with no owner at all, or one whose owner a credential in the path names"
            + "\nrather than the caller, belongs in `UNOWNED_ROUTES`, with the reason."
        )

    def test_no_entry_names_a_route_that_no_longer_exists(self) -> None:
        """Stale entries in either direction. A covered route that was renamed leaves a
        parametrized case that passes against nothing, and a stale `UNOWNED_ROUTES` entry
        is worse - it pre-approves the path for whatever is registered there next.

        Also what fails if `iter_api_routes` ever stops finding routes, which would
        otherwise let the sweep above pass vacuously.
        """
        registered = {route.key for route in iter_api_routes(app) if "{uuid}" in route.path}
        listed = (
            {route.key for route in FETCH_OWNED_ROUTES}
            | {route.key for route in RESOLVER_OWNED_ROUTES}
            | UNOWNED_ROUTES.keys()
        )

        stale = sorted(listed - registered)

        assert not stale, f"these entries no longer name a registered `{{uuid}}` route: {stale}"
