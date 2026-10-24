"""`Idempotency-Key` on the create routes (`api/idempotency.py`).

What is pinned: every `201` create under `/api/v1` either declares the dependency or is named
below as excluded, with its reason; a repeat under one key answers the first record with the
first status and makes nothing; another body under the key is a 422; another account under it
creates its own; a repeat whose record was deleted is a 410; two at once make one record and
one 409; a create that fails holds nothing; and the hourly sweep takes a hold past the
extended window and leaves a younger one.
"""

import asyncio
import uuid as uuid_pkg
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.api.idempotency import _PENDING, DELETED, DIFFERENT_REQUEST, IN_FLIGHT, IdempotentCreate, _Pending
from src.app.api.v1 import dive_sites as dive_sites_module
from src.app.core.config import settings
from src.app.core.db.database import async_engine, async_get_db
from src.app.core.setup import create_application
from src.app.core.utils import cache as cache_module
from src.app.core.worker.functions import purge_expired_idempotency_holds
from src.app.core.worker.settings import WorkerSettings
from src.app.crud.crud_users import read_account
from src.app.main import app
from src.app.models.certification import Certification
from src.app.models.contact import Contact
from src.app.models.course import Course
from src.app.models.dive import Dive
from src.app.models.dive_form_preset import DiveFormPreset
from src.app.models.dive_site import DiveSite
from src.app.models.gear_item import GearItem
from src.app.models.gear_service_record import GearServiceRecord
from src.app.models.gear_service_schedule import GearServiceSchedule
from src.app.models.gear_set import GearSet
from src.app.models.idempotency_hold import MAX_IDEMPOTENCY_KEY_LENGTH, IdempotencyHold
from src.app.models.person import Person
from src.app.models.trip import Trip
from src.app.models.user import User
from tests.conftest import db_available
from tests.helpers.fake_redis import FakeRedis
from tests.helpers.generators import create_gear_item, create_user
from tests.helpers.routes import API_PREFIX, dependency_calls, iter_api_routes

# The `201` creates that take no key, each for its reason.
EXCLUDED: dict[str, str] = {
    "/api/v1/user/checkin-link": "Mints a token rather than a record, and minting again revokes the last one.",
    "/api/v1/user/invite-link": "Mints a token rather than a record, and minting again replaces the last one.",
    "/api/v1/user/invitations": "Sends mail, bounded by its own limit, and a second send is a 409 already.",
    "/api/v1/user/passkey/verify": "Completes a single-use ceremony, whose challenge a repeat cannot reuse.",
    "/api/v1/dive/{uuid}/recordings": "Idempotent by its bytes: a repeat upload finds the file it stored.",
}


@dataclass(frozen=True)
class Create:
    path: str
    model: type[Any]
    # The body, given a gear item of the diver's for the two service routes, and a variant.
    body: Callable[[uuid_pkg.UUID, str], dict[str, Any]]

    def __repr__(self) -> str:
        return self.path


def _named(**fields: Any) -> Callable[[uuid_pkg.UUID, str], dict[str, Any]]:
    return lambda _gear, name: {"name": name, **fields}


COVERED: tuple[Create, ...] = (
    Create("/certification", Certification, _named(agency="padi")),
    Create("/contact", Contact, _named()),
    Create("/course", Course, _named()),
    Create("/dive-form-preset", DiveFormPreset, _named()),
    Create("/dive-site", DiveSite, _named()),
    Create(
        "/dive",
        Dive,
        lambda _gear, name: {
            "dive_number": 1,
            "start_time": "2026-10-01T10:00:00+02:00",
            "submerged_time": 2400,
            "notes": name,
        },
    ),
    Create("/gear-item", GearItem, _named()),
    Create(
        "/gear-service-schedule",
        GearServiceSchedule,
        lambda gear, name: {
            "gear_item_uuid": str(gear),
            "kind": "other",
            "label": name,
            "starts_on": "2026-01-01",
            "interval_months": 12,
        },
    ),
    Create(
        "/gear-service-record",
        GearServiceRecord,
        lambda gear, name: {"gear_item_uuid": str(gear), "kind": "other", "label": name, "serviced_on": "2026-01-01"},
    ),
    Create("/gear-set", GearSet, _named()),
    Create("/person", Person, _named()),
    Create("/trip", Trip, _named()),
)


DIVE = next(create for create in COVERED if create.path == "/dive")


def _creates() -> dict[str, APIRoute]:
    return {
        context.path: context.original_route
        for context in iter_route_contexts(app.routes)
        if isinstance(context.original_route, APIRoute)
        and context.path is not None
        and context.path.startswith(API_PREFIX)
        and "POST" in (context.methods or set())
        and context.original_route.status_code == 201
    }


def _declares_the_key(route: APIRoute) -> bool:
    return any(isinstance(call, IdempotentCreate) for call in dependency_calls(route.dependant))


class TestEveryCreateIsCoveredOrExcluded:
    """A create route added later is classified here before anything passes."""

    def test_the_covered_and_the_excluded_are_every_create(self) -> None:
        covered = {path for path, route in _creates().items() if _declares_the_key(route)}

        assert covered == {API_PREFIX + create.path for create in COVERED}
        assert set(_creates()) - covered == set(EXCLUDED)

    @pytest.mark.parametrize("create", COVERED, ids=repr)
    def test_each_names_its_record_and_replays_through_its_own_get(self, create: Create) -> None:
        route = _creates()[API_PREFIX + create.path]
        (dependency,) = [call for call in dependency_calls(route.dependant) if isinstance(call, IdempotentCreate)]
        gets = {
            read.endpoint
            for read in iter_api_routes(app)
            if read.method == "GET" and read.path == f"{API_PREFIX}{create.path}/{{uuid}}"
        }

        assert dependency.record is create.model
        assert gets == {dependency.read_endpoint(app.routes)}

    def test_the_header_is_documented_on_each(self) -> None:
        schema = app.openapi()
        for create in COVERED:
            parameters = schema["paths"][API_PREFIX + create.path]["post"]["parameters"]
            (header,) = [p for p in parameters if p["name"] == "Idempotency-Key"]
            assert header["in"] == "header" and header["required"] is False


class TestTheSweepIsOnTheHour:
    def test_it_runs_hourly_and_at_startup_like_the_other_sweeps(self) -> None:
        (job,) = [job for job in WorkerSettings.cron_jobs if job.coroutine is purge_expired_idempotency_holds]

        assert (job.minute, job.hour, job.run_at_startup) == (0, None, True)


# -------------- against Postgres --------------


class _HoldTheFirstCommit:
    """`async_get_db` with a session per request, whose first commit waits until another
    request is blocked on a lock - so two creates under one key overlap for certain."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.gate = False
        self.taken = False

    async def _until_a_request_waits(self) -> None:
        async with self.sessions() as probe:
            for _ in range(200):
                waiting = await probe.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
                await probe.rollback()
                if waiting:
                    return
                await asyncio.sleep(0.025)
        raise AssertionError("No second request ever waited on the first's hold.")

    async def __call__(self) -> AsyncGenerator[AsyncSession]:
        async with self.sessions() as session:
            commit = session.commit

            async def gated_commit() -> None:
                if self.gate and not self.taken:
                    self.taken = True
                    await self._until_a_request_waits()
                await commit()

            session.commit = gated_commit  # type: ignore[method-assign]
            yield session


@pytest.fixture(scope="module")
def keyed_app() -> Any:
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


@pytest_asyncio.fixture
async def sessions(
    keyed_app: Any, async_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[_HoldTheFirstCommit]:
    monkeypatch.setattr(cache_module, "client", FakeRedis())
    per_request = _HoldTheFirstCommit(async_sessions)
    keyed_app.dependency_overrides[async_get_db] = per_request
    yield per_request
    keyed_app.dependency_overrides = {}


@pytest_asyncio.fixture
async def http(keyed_app: Any, sessions: _HoldTheFirstCommit) -> AsyncGenerator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=keyed_app), base_url="http://test") as client:
        yield client


async def _sign_in(keyed_app: Any, sessions: _HoldTheFirstCommit, user: User) -> None:
    async with sessions.sessions() as db:
        account = await read_account(db, uuid=user.uuid)
    keyed_app.dependency_overrides[get_current_user] = lambda: account


def _count(db: Session, model: type[Any], user: User) -> int:
    db.expire_all()
    return int(db.execute(select(func.count()).select_from(model).where(model.user_id == user.id)).scalar_one())


def _holds(db: Session, user: User) -> list[IdempotencyHold]:
    db.expire_all()
    return list(db.execute(select(IdempotencyHold).where(IdempotencyHold.user_id == user.id)).scalars())


async def _post(http: httpx.AsyncClient, path: str, body: dict[str, Any], key: str | None) -> httpx.Response:
    headers = {} if key is None else {"Idempotency-Key": key}
    return await http.post(API_PREFIX + path, json=body, headers=headers)


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestAgainstPostgres:
    @pytest_asyncio.fixture
    async def diver(self, db: Session, keyed_app: Any, sessions: _HoldTheFirstCommit) -> User:
        user = create_user(db)
        await _sign_in(keyed_app, sessions, user)
        return user

    @pytest.fixture
    def gear(self, db: Session, diver: User) -> uuid_pkg.UUID:
        return create_gear_item(db, diver).uuid

    @pytest.mark.asyncio
    @pytest.mark.parametrize("create", COVERED, ids=repr)
    async def test_a_repeat_answers_the_first_record_and_makes_nothing(
        self, http: httpx.AsyncClient, db: Session, diver: User, gear: uuid_pkg.UUID, create: Create
    ) -> None:
        before = _count(db, create.model, diver)
        body = create.body(gear, "First")

        first = await _post(http, create.path, body, "key-1")
        second = await _post(http, create.path, body, "key-1")

        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        assert second.json() == first.json()
        assert _count(db, create.model, diver) == before + 1
        (hold,) = _holds(db, diver)
        assert (hold.route, hold.key, hold.status_code) == (API_PREFIX + create.path, "key-1", 201)
        assert str(hold.record_uuid) == first.json()["uuid"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("create", COVERED, ids=repr)
    async def test_another_body_under_the_key_is_a_422(
        self, http: httpx.AsyncClient, db: Session, diver: User, gear: uuid_pkg.UUID, create: Create
    ) -> None:
        before = _count(db, create.model, diver)
        assert (await _post(http, create.path, create.body(gear, "First"), "key-1")).status_code == 201

        other = await _post(http, create.path, create.body(gear, "Second"), "key-1")

        assert other.status_code == 422
        assert other.json() == {"detail": DIFFERENT_REQUEST}
        assert _count(db, create.model, diver) == before + 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("create", COVERED, ids=repr)
    async def test_another_account_under_the_key_creates_its_own(
        self,
        http: httpx.AsyncClient,
        db: Session,
        keyed_app: Any,
        sessions: _HoldTheFirstCommit,
        diver: User,
        gear: uuid_pkg.UUID,
        create: Create,
    ) -> None:
        first = await _post(http, create.path, create.body(gear, "First"), "key-1")
        other_diver = create_user(db)
        other_gear = create_gear_item(db, other_diver).uuid
        await _sign_in(keyed_app, sessions, other_diver)

        theirs = await _post(http, create.path, create.body(other_gear, "First"), "key-1")

        assert (first.status_code, theirs.status_code) == (201, 201), theirs.text
        assert theirs.json()["uuid"] != first.json()["uuid"]
        assert len(_holds(db, diver)) == len(_holds(db, other_diver)) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("create", COVERED, ids=repr)
    async def test_a_repeat_after_the_record_was_deleted_is_a_410(
        self, http: httpx.AsyncClient, db: Session, diver: User, gear: uuid_pkg.UUID, create: Create
    ) -> None:
        body = create.body(gear, "First")
        first = await _post(http, create.path, body, "key-1")
        deleted = await http.delete(f"{API_PREFIX}{create.path}/{first.json()['uuid']}")
        assert deleted.status_code == 200, deleted.text
        before = _count(db, create.model, diver)

        again = await _post(http, create.path, body, "key-1")

        assert again.status_code == 410
        assert again.json() == {"detail": DELETED}
        assert _count(db, create.model, diver) == before

    @pytest.mark.asyncio
    @pytest.mark.parametrize("create", COVERED, ids=repr)
    async def test_two_at_once_make_one_record_and_one_409(
        self,
        http: httpx.AsyncClient,
        db: Session,
        sessions: _HoldTheFirstCommit,
        diver: User,
        gear: uuid_pkg.UUID,
        create: Create,
    ) -> None:
        before = _count(db, create.model, diver)
        body = create.body(gear, "First")
        sessions.gate = True

        answers = await asyncio.gather(*(_post(http, create.path, body, "key-1") for _ in range(2)))

        assert sorted(answer.status_code for answer in answers) == [201, 409], [a.text for a in answers]
        (in_flight,) = [answer for answer in answers if answer.status_code == 409]
        assert in_flight.json() == {"detail": IN_FLIGHT}
        assert _count(db, create.model, diver) == before + 1
        assert len(_holds(db, diver)) == 1

    @pytest.mark.asyncio
    async def test_without_the_header_a_repeat_creates_again_and_holds_nothing(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        body = DIVE.body(uuid_pkg.uuid4(), "Twice")

        answers = [await _post(http, "/dive", body, None) for _ in range(2)]

        assert [answer.status_code for answer in answers] == [201, 201]
        assert answers[0].json()["uuid"] != answers[1].json()["uuid"]
        assert _count(db, Dive, diver) == 2
        assert _holds(db, diver) == []

    @pytest.mark.asyncio
    async def test_a_create_the_route_refuses_holds_nothing_and_the_corrected_one_creates(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        dive = DIVE.body(uuid_pkg.uuid4(), "Logged")

        refused = await _post(http, "/dive", {**dive, "trip_uuid": str(uuid_pkg.uuid4())}, "key-1")
        assert refused.status_code == 422
        assert _holds(db, diver) == []

        created = await _post(http, "/dive", dive, "key-1")

        assert created.status_code == 201, created.text
        assert _count(db, Dive, diver) == 1
        assert len(_holds(db, diver)) == 1

    @pytest.mark.asyncio
    async def test_a_create_its_own_write_rolls_back_holds_nothing(
        self, http: httpx.AsyncClient, db: Session, diver: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A name taken between the route's probe and its write is `write_or_duplicate`'s
        rollback, and the claim goes with it."""
        assert (await _post(http, "/dive-site", {"name": "Taken"}, None)).status_code == 201
        monkeypatch.setattr(dive_sites_module, "dive_site_name_exists", AsyncMock(return_value=False))

        refused = await _post(http, "/dive-site", {"name": "Taken"}, "key-1")
        assert refused.status_code == 422, refused.text
        assert _holds(db, diver) == []

        created = await _post(http, "/dive-site", {"name": "Free"}, "key-1")

        assert created.status_code == 201, created.text
        assert _count(db, DiveSite, diver) == 2
        assert len(_holds(db, diver)) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["", "k" * (MAX_IDEMPOTENCY_KEY_LENGTH + 1)], ids=["empty", "too long"])
    async def test_a_key_outside_its_bounds_is_refused_before_anything_is_written(
        self, http: httpx.AsyncClient, db: Session, diver: User, key: str
    ) -> None:
        refused = await _post(http, "/trip", {"name": "Bounded"}, key)

        assert refused.status_code == 422
        assert refused.json()["detail"][0]["loc"] == ["header", "Idempotency-Key"]
        assert _count(db, Trip, diver) == 0

    @pytest.mark.asyncio
    async def test_the_longest_key_is_taken(self, http: httpx.AsyncClient, db: Session, diver: User) -> None:
        created = await _post(http, "/trip", {"name": "Bounded"}, "k" * MAX_IDEMPOTENCY_KEY_LENGTH)

        assert created.status_code == 201, created.text

    @pytest.mark.asyncio
    async def test_the_sweep_takes_a_hold_past_the_extended_window_and_leaves_a_younger_one(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        for key in ("old", "young"):
            assert (await _post(http, "/trip", {"name": key}, key)).status_code == 201
        window = timedelta(days=settings.EXTENDED_REFRESH_TOKEN_EXPIRE_DAYS)
        now = datetime.now(UTC)
        for key, age in (("old", window + timedelta(minutes=1)), ("young", window - timedelta(minutes=1))):
            db.execute(
                update(IdempotencyHold)
                .where(IdempotencyHold.user_id == diver.id, IdempotencyHold.key == key)
                .values(created_at=now - age)
            )
        db.commit()

        # The sweep opens its session from the app's module-level engine, and a pooled asyncpg
        # connection belongs to the loop that opened it.
        await async_engine.dispose()
        try:
            await purge_expired_idempotency_holds({})
        finally:
            await async_engine.dispose()

        assert [hold.key for hold in _holds(db, diver)] == ["young"]

    @pytest.mark.asyncio
    async def test_a_commit_that_made_no_record_is_refused_and_keeps_no_hold(
        self, db: Session, async_sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        owner = create_user(db)
        async with async_sessions() as session:
            hold = IdempotencyHold(
                user_id=owner.id, route="/api/v1/trip", key="k", request_digest="0" * 64, status_code=201
            )
            session.add(hold)
            await session.flush()
            session.sync_session.info[_PENDING] = _Pending(hold=hold, record=Trip)

            with pytest.raises(RuntimeError, match="committed without its Trip"):
                await session.commit()

        assert _holds(db, owner) == []

    def test_a_hold_goes_with_its_account(self, db: Session) -> None:
        owner = create_user(db)
        db.add(
            IdempotencyHold(user_id=owner.id, route="/api/v1/trip", key="k", request_digest="0" * 64, status_code=201)
        )
        db.commit()

        owner_id = owner.id
        db.delete(owner)
        db.commit()

        assert db.execute(select(IdempotencyHold).where(IdempotencyHold.user_id == owner_id)).first() is None
