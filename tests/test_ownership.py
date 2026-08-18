"""Tests for `api.dependencies.fetch_owned_or_raise` and the contract it publishes.

This is the single implementation behind every "fetch one resource the caller owns"
route in `api/v1` - dives, dive sites, trips, gear items, gear sets and certifications
all reach it through a thin per-entity wrapper. It used to be seven hand-rolled copies,
so these tests exist to keep the one that replaced them honest.

Mostly unit tests of the function, plus one class that drives the six real routes over
HTTP. That last one is not redundant: "someone else's row is indistinguishable from a
missing one" is a security contract, and a contract asserted only one layer below the
wire is one a route can quietly stop honouring.
"""

import logging
import uuid as uuid_pkg
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.app.api import router
from src.app.api.dependencies import fetch_owned_or_raise, get_current_user
from src.app.core.config import settings
from src.app.core.exceptions.http_exceptions import NotFoundException
from src.app.core.setup import create_application


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


# (route path, the module whose `crud_*` singleton backs it, its not-found wording).
OWNED_ROUTES = [
    ("/api/v1/dive/{uuid}", "src.app.api.v1.dives", "crud_dives", "Dive not found"),
    ("/api/v1/dive-site/{uuid}", "src.app.api.v1.dive_sites", "crud_dive_sites", "Dive site not found"),
    ("/api/v1/trip/{uuid}", "src.app.api.v1.trips", "crud_trips", "Trip not found"),
    ("/api/v1/gear-item/{uuid}", "src.app.api.v1.gear_items", "crud_gear_items", "Gear item not found"),
    ("/api/v1/gear-set/{uuid}", "src.app.api.v1.gear_sets", "crud_gear_sets", "Gear set not found"),
    (
        "/api/v1/certification/{uuid}",
        "src.app.api.v1.certifications",
        "crud_certifications",
        "Certification not found",
    ),
]

OWNER_ID = 7
SOMEONE_ELSE_ID = 8
OWNER = {"id": OWNER_ID, "uuid": uuid_pkg.uuid4(), "username": "ada", "is_superuser": False}


@pytest.fixture(scope="module")
def owned_app() -> Any:
    """Its own app with `create_tables_on_start=False`, like `test_export_endpoints.py`.

    The shared `client` fixture opens `src.app.main`'s app, whose startup hook connects to
    Postgres - which would put these in the database-backed subset for no reason. Nothing
    below the route runs here anyway: every one of these raises before it reaches its
    cached read helper, which is the ordering `TestEveryOwnedRouteUsesIt` exists to
    protect.
    """
    return create_application(router=router, settings=settings, create_tables_on_start=False)


@pytest.fixture
def signed_in_client(owned_app: Any) -> Generator[TestClient]:
    owned_app.dependency_overrides[get_current_user] = lambda: OWNER
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
    """

    @pytest.mark.parametrize("method", ("GET", "PATCH", "DELETE"))
    @pytest.mark.parametrize(("path", "module", "crud_name", "detail"), OWNED_ROUTES)
    def test_a_row_owned_by_someone_else_reads_as_a_missing_one(
        self,
        signed_in_client: TestClient,
        monkeypatch: Any,
        method: str,
        path: str,
        module: str,
        crud_name: str,
        detail: str,
    ):
        """All three methods, not just the read.

        The mutating routes are the ones most likely to grow a bespoke pre-check later,
        and an empty `PATCH` body is enough: every update schema is all-optional, and the
        ownership check runs before any field is looked at.
        """
        import importlib

        crud = getattr(importlib.import_module(module), crud_name)
        url = path.format(uuid=uuid_pkg.uuid4())
        body: dict[str, Any] | None = {} if method == "PATCH" else None

        monkeypatch.setattr(crud, "get", AsyncMock(return_value=None))
        absent = signed_in_client.request(method, url, json=body)

        monkeypatch.setattr(crud, "get", AsyncMock(return_value=_Row(id=1, user_id=SOMEONE_ELSE_ID)))
        someone_elses = signed_in_client.request(method, url, json=body)

        assert absent.status_code == 404
        assert someone_elses.status_code == absent.status_code
        assert someone_elses.json() == absent.json() == {"detail": detail}
