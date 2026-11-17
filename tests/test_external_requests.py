"""The external request counts: what the outbound helper counts, the Redis buffer, the worker's
move into `daily_external_request`, and `GET /admin/external-requests`.

The move's tests run against Postgres on days in 2019, which nothing else in the suite writes, and
are skipped without a database - see `CONTRIBUTING.md` for why a run on the host needs
`POSTGRES_SERVER=localhost`.
"""

import asyncio
import logging
import textwrap
from collections.abc import AsyncGenerator, Callable, Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import anyio
import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from mypy import api as mypy_api
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.api.v1.admin import MAX_STATS_DAYS, read_external_requests
from src.app.core.config import settings
from src.app.core.db.database import async_engine, async_get_db
from src.app.core.exceptions.http_exceptions import RateLimitException, UnprocessableEntityException
from src.app.core.setup import create_application
from src.app.core.utils import external_requests, outbound
from src.app.core.utils.external_requests import ExternalRequest, Outcome
from src.app.core.utils.outbound import Failed, Fetched, FetchFailure
from src.app.core.worker.functions import move_external_request_counts
from src.app.core.worker.settings import WorkerSettings
from src.app.models.daily_external_request import DailyExternalRequest
from tests.conftest import db_available
from tests.helpers.fake_redis import FakeRedis

needs_a_database = pytest.mark.skipif(not db_available(), reason="No database connection available")

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_URL = "https://provider.example/api"
_SEA: ExternalRequest = ("open-meteo", "sea")
_AVATAR: ExternalRequest = ("google", "avatar")
_PREFIX = "external-requests:"


def _serve(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], Any]) -> None:
    def build(**kwargs: Any) -> httpx.AsyncClient:
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(outbound.httpx, "AsyncClient", build)


async def _fetch(url: str = _URL, counted_as: ExternalRequest = _SEA, **kwargs: Any) -> Fetched | Failed:
    return await outbound.fetch_capped(
        url, counted_as=counted_as, **{"timeout": 1.0, "deadline": 1.0, "max_bytes": 1024, **kwargs}
    )


def _buffered(redis: FakeRedis) -> dict[tuple[str, str, str], tuple[int, int, int]]:
    """Every buffered row as `(provider, kind, outcome) -> (count, total, slowest)`."""
    rows = {}
    for key, value in redis.counters.items():
        _, _, provider, kind, outcome = key.split(":")
        count, total, slowest = (int(figure) for figure in value.split())
        rows[(provider, kind, outcome)] = (count, total, slowest)
    return rows


def _outcomes(redis: FakeRedis) -> dict[tuple[str, str, str], int]:
    return {identity: figures[0] for identity, figures in _buffered(redis).items()}


@pytest.fixture
def redis() -> Generator[FakeRedis]:
    fake = FakeRedis()
    with patch("src.app.core.utils.cache.client", fake):
        yield fake


@contextmanager
def clock(instant: datetime) -> Iterator[None]:
    """The counter's `datetime.now` pinned to `instant`, the only clock that names an event's day."""
    with patch.object(external_requests, "datetime", Mock(now=Mock(return_value=instant))):
        yield


class TestTheOutcomes:
    """Each outcome is produced by its event and by no other."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler", "outcome"),
        [
            (lambda request: httpx.Response(200, content=b"{}"), Outcome.OK),
            (lambda request: httpx.Response(429), Outcome.RATE_LIMITED),
            (lambda request: httpx.Response(503), Outcome.STATUS),
            (lambda request: httpx.Response(404), Outcome.STATUS),
            (lambda request: httpx.Response(200, content=b"x" * 1025), Outcome.TOO_LARGE),
        ],
        ids=["ok", "429", "503", "404", "too-large"],
    )
    async def test_an_answer(
        self, monkeypatch: pytest.MonkeyPatch, redis: FakeRedis, handler: Any, outcome: Outcome
    ) -> None:
        _serve(monkeypatch, handler)

        await _fetch()

        assert _outcomes(redis) == {("open-meteo", "sea", outcome.value): 1}

    @pytest.mark.asyncio
    async def test_the_helpers_own_deadline(self, monkeypatch: pytest.MonkeyPatch, redis: FakeRedis) -> None:
        async def never(request: httpx.Request) -> httpx.Response:
            await anyio.sleep_forever()
            raise AssertionError

        _serve(monkeypatch, never)

        assert await _fetch(deadline=0.05) == Failed(FetchFailure.DEADLINE)
        assert _outcomes(redis) == {("open-meteo", "sea", "deadline"): 1}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("timeout", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout])
    async def test_a_timeout_the_client_raises_is_a_deadline_and_still_returned_as_before(
        self, monkeypatch: pytest.MonkeyPatch, redis: FakeRedis, timeout: type[httpx.TimeoutException]
    ) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            raise timeout("slow", request=request)

        _serve(monkeypatch, slow)

        assert await _fetch() == Failed(FetchFailure.TRANSPORT, error=timeout.__name__)
        assert _outcomes(redis) == {("open-meteo", "sea", "deadline"): 1}

    @pytest.mark.asyncio
    async def test_a_connection_failure_and_a_malformed_url(
        self, monkeypatch: pytest.MonkeyPatch, redis: FakeRedis
    ) -> None:
        def unreachable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unreachable", request=request)

        _serve(monkeypatch, unreachable)

        await _fetch()
        monkeypatch.setattr(outbound.httpx, "AsyncClient", _REAL_ASYNC_CLIENT)
        await _fetch("http://provider.example:8O80/api")

        assert _outcomes(redis) == {("open-meteo", "sea", "transport"): 2}

    @pytest.mark.asyncio
    async def test_a_refused_slot_is_skipped_once(self, redis: FakeRedis) -> None:
        refuse = AsyncMock(side_effect=RateLimitException("full"))
        with (
            patch("src.app.core.utils.rate_limit.enforce_rate_limit", refuse),
            patch.object(outbound, "sleep", new_callable=AsyncMock),
        ):
            assert await outbound.claim_provider_slot("k", counted_as=_SEA) is False
            assert await outbound.claim_provider_slot("k", counted_as=_SEA, wait_seconds=0.5) is False

        assert _buffered(redis) == {("open-meteo", "sea", "skipped"): (2, 0, 0)}

    @pytest.mark.asyncio
    async def test_a_slot_granted_after_the_wait_counts_nothing(self, redis: FakeRedis) -> None:
        second_try = AsyncMock(side_effect=[RateLimitException("full"), None])
        with (
            patch("src.app.core.utils.rate_limit.enforce_rate_limit", second_try),
            patch.object(outbound, "sleep", new_callable=AsyncMock),
        ):
            assert await outbound.claim_provider_slot("k", counted_as=_SEA, wait_seconds=0.5) is True

        assert _buffered(redis) == {}

    @pytest.mark.asyncio
    async def test_a_cache_hit_counts_under_every_request_it_saves_and_a_miss_counts_nothing(
        self, redis: FakeRedis
    ) -> None:
        await outbound.write_cached("k", "[1]", ttl_seconds=60)
        saved: list[ExternalRequest] = [("worms", "by-name"), ("wikidata", "search")]

        assert await outbound.read_cached("k", list, counted_as=saved) == [1]
        assert await outbound.read_cached("missing", list, counted_as=saved) is None

        assert _buffered(redis) == {
            ("worms", "by-name", "cache_hit"): (1, 0, 0),
            ("wikidata", "search", "cache_hit"): (1, 0, 0),
        }


class TestCancellation:
    @staticmethod
    def _hang(monkeypatch: pytest.MonkeyPatch) -> None:
        async def never(request: httpx.Request) -> httpx.Response:
            await anyio.sleep_forever()
            raise AssertionError

        _serve(monkeypatch, never)

    @pytest.mark.asyncio
    async def test_a_budget_cutting_a_request_off_is_counted_and_still_cancels(
        self, monkeypatch: pytest.MonkeyPatch, redis: FakeRedis
    ) -> None:
        self._hang(monkeypatch)

        with anyio.move_on_after(0.05) as budget:
            await _fetch(deadline=10)

        assert budget.cancelled_caught
        ((count, total, slowest),) = _buffered(redis).values()
        assert _outcomes(redis) == {("open-meteo", "sea", "cancelled"): 1}
        assert count == 1
        assert 40 <= total == slowest < 1000

    @pytest.mark.asyncio
    async def test_a_cancelled_task_is_counted_and_sees_its_cancellation(
        self, monkeypatch: pytest.MonkeyPatch, redis: FakeRedis
    ) -> None:
        """How the dive-site search drops its place lookup."""
        self._hang(monkeypatch)

        task = asyncio.create_task(_fetch(deadline=10))
        await asyncio.sleep(0.05)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert _outcomes(redis) == {("open-meteo", "sea", "cancelled"): 1}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("redis_double", ["hanging", "failing"])
    async def test_counting_holds_a_cancellation_a_fraction_of_a_second_at_most(
        self, monkeypatch: pytest.MonkeyPatch, redis_double: str
    ) -> None:
        class Hanging(FakeRedis):
            async def eval(self, *args: object) -> int:
                await anyio.sleep_forever()
                raise AssertionError

        self._hang(monkeypatch)
        double = Hanging() if redis_double == "hanging" else FakeRedis(failing=True)

        started = anyio.current_time()
        with patch("src.app.core.utils.cache.client", double), anyio.move_on_after(0.05) as budget:
            await _fetch(deadline=10)

        assert budget.cancelled_caught
        assert anyio.current_time() - started < 0.05 + 0.5


class TestDurations:
    @pytest.mark.asyncio
    async def test_the_slowest_of_concurrent_requests_is_the_longest_whichever_ends_last(
        self, monkeypatch: pytest.MonkeyPatch, redis: FakeRedis
    ) -> None:
        async def answer(request: httpx.Request) -> httpx.Response:
            await anyio.sleep(float(request.url.params["wait"]))
            return httpx.Response(200)

        _serve(monkeypatch, answer)

        async def short_and_late() -> None:
            await anyio.sleep(0.25)
            await _fetch(params={"wait": "0.1"})

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(lambda: _fetch(params={"wait": "0.3"}))
            tasks.start_soon(short_and_late)

        ((count, total, slowest),) = _buffered(redis).values()
        assert count == 2
        assert 300 <= slowest < 340
        assert total >= slowest + 100

    @pytest.mark.asyncio
    async def test_a_url_carries_nothing_into_the_buffer_or_a_row(
        self, monkeypatch: pytest.MonkeyPatch, redis: FakeRedis
    ) -> None:
        _serve(monkeypatch, lambda request: httpx.Response(200))

        await _fetch(f"{_URL}/marker-in-path", params={"q": "marker-in-query"})

        stored = " ".join(f"{key} {value.decode()}" for key, value in redis.counters.items())
        rows = await external_requests.take_buffered()
        assert len(rows) == 1
        assert "marker" not in stored
        assert "marker" not in repr(rows)


class TestWithoutRedis:
    """Each helper answers as it does uncounted, and counting logs nothing per event."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("client", [None, FakeRedis(failing=True)], ids=["absent", "failing"])
    async def test_each_helper_answers_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, client: FakeRedis | None
    ) -> None:
        _serve(monkeypatch, lambda request: httpx.Response(200, content=b"{}"))

        with (
            patch("src.app.core.utils.cache.client", client),
            patch("src.app.core.utils.rate_limit.enforce_rate_limit", AsyncMock(side_effect=RateLimitException("x"))),
            caplog.at_level(logging.DEBUG, logger=external_requests.logger.name),
        ):
            fetched = await _fetch()
            claimed = await outbound.claim_provider_slot("k", counted_as=_SEA)
            cached = await outbound.read_cached("k", list, counted_as=[_SEA])

        assert isinstance(fetched, Fetched)
        assert fetched.body == b"{}"
        assert (claimed, cached) == (False, None)
        assert [record for record in caplog.records if record.name == external_requests.logger.name] == []


class TestTheVocabulary:
    def test_every_service_with_a_slot_has_a_limit_and_google_has_none(self) -> None:
        assert set(external_requests.DECLARED) == {
            "commons",
            "google",
            "nominatim",
            "open-meteo",
            "photon",
            "wikidata",
            "worms",
        }
        assert external_requests.provider_limit("google") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["open-meteo", "nominatim", "photon", "worms", "wikidata", "commons"])
    async def test_the_reported_limit_is_what_the_slot_claim_passes_the_limiter(
        self, monkeypatch: pytest.MonkeyPatch, redis: FakeRedis, provider: str
    ) -> None:
        monkeypatch.setattr(settings, "GEOCODER_PROVIDER_RATE_LIMIT_REQUESTS", 7)
        monkeypatch.setattr(settings, "SPECIES_COMMONS_RATE_LIMIT_WINDOW_SECONDS", 90)
        # Any of its kinds: the cap is the service's.
        request = next(member for member in _every_declared() if member[0] == provider)
        with patch("src.app.core.utils.rate_limit.enforce_rate_limit", new_callable=AsyncMock) as limiter:
            await outbound.claim_provider_slot("k", counted_as=request)

        _, max_requests, window = limiter.await_args_list[0].args
        body = await _read_route(first=date(2026, 9, 1), last=date(2026, 9, 1))
        (reported,) = [entry["limit"] for entry in body["providers"] if entry["provider"] == provider]
        assert (reported["requests"], reported["window_seconds"]) == (max_requests, window)

    def test_a_pair_outside_the_vocabulary_fails_type_checking(self, tmp_path: Path) -> None:
        """The type checker is what keeps a call site inside the vocabulary; this proves it
        refuses one outside, for both a service and a kind."""
        snippet = tmp_path / "pairs.py"
        snippet.write_text(
            textwrap.dedent(
                """
                from src.app.core.utils.external_requests import ExternalRequest
                from src.app.core.utils.outbound import claim_provider_slot

                declared: ExternalRequest = ("worms", "by-name")
                wrong_kind: ExternalRequest = ("worms", "search")

                async def claim() -> None:
                    await claim_provider_slot("k", counted_as=("ftp", "avatar"))
                """
            )
        )
        stdout, _, status = mypy_api.run(
            ["--follow-imports=silent", "--no-incremental", "--ignore-missing-imports", str(snippet)]
        )

        assert status == 1
        flagged = sorted(int(line.split(":")[1]) for line in stdout.splitlines() if ": error:" in line)
        assert flagged == [6, 9], stdout


def _every_declared() -> list[Any]:
    return [(provider, kind) for provider, kinds in external_requests.DECLARED.items() for kind in kinds]


async def _read_route(*, first: date, last: date, rows: list[DailyExternalRequest] | None = None) -> dict[str, Any]:
    with patch("src.app.api.v1.admin.external_requests_between", AsyncMock(return_value=rows or [])):
        answer = await read_external_requests(Mock(), first=first, last=last)
    return answer.model_dump(mode="json")


def _row(day: date, provider: str, kind: str, outcome: str, count: int, total: int = 0, slowest: int = 0) -> Any:
    return DailyExternalRequest(
        day=day,
        provider=provider,
        kind=kind,
        outcome=outcome,
        count=count,
        duration_ms_total=total,
        duration_ms_max=slowest,
    )


class TestTheRoute:
    @pytest.mark.asyncio
    async def test_the_contracts_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_REQUESTS", 400)
        monkeypatch.setattr(settings, "DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_WINDOW_SECONDS", 3600)
        first = date(2026, 11, 16)
        rows = [
            _row(first, "worms", "by-name", "ok", 3, 900, 500),
            _row(first, "open-meteo", "sea", "status", 1, 120, 120),
            _row(first, "open-meteo", "sea", "ok", 41, 20500, 3100),
            _row(first, "an-old-service", "gone", "ok", 2, 10, 6),
        ]

        body = await _read_route(first=first, last=first + timedelta(days=1), rows=rows)

        assert (body["from"], body["to"]) == ("2026-11-16", "2026-11-17")
        providers = {entry["provider"]: entry for entry in body["providers"]}
        assert list(providers) == list(external_requests.DECLARED)
        assert providers["open-meteo"] == {
            "provider": "open-meteo",
            "kinds": ["forecast", "sea", "temperatures", "weather"],
            "limit": {"requests": 400, "window_seconds": 3600, "per_day": 9600},
        }
        assert providers["google"] == {"provider": "google", "kinds": ["avatar"], "limit": None}
        assert body["days"] == [
            {
                "day": "2026-11-16",
                "requests": [
                    {
                        "provider": "an-old-service",
                        "kind": "gone",
                        "outcome": "ok",
                        "count": 2,
                        "duration_ms_total": 10,
                        "duration_ms_max": 6,
                    },
                    {
                        "provider": "open-meteo",
                        "kind": "sea",
                        "outcome": "ok",
                        "count": 41,
                        "duration_ms_total": 20500,
                        "duration_ms_max": 3100,
                    },
                    {
                        "provider": "open-meteo",
                        "kind": "sea",
                        "outcome": "status",
                        "count": 1,
                        "duration_ms_total": 120,
                        "duration_ms_max": 120,
                    },
                    {
                        "provider": "worms",
                        "kind": "by-name",
                        "outcome": "ok",
                        "count": 3,
                        "duration_ms_total": 900,
                        "duration_ms_max": 500,
                    },
                ],
            },
            {"day": "2026-11-17", "requests": []},
        ]

    @pytest.mark.asyncio
    async def test_per_day_rounds_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "SPECIES_WORMS_RATE_LIMIT_REQUESTS", 5)
        monkeypatch.setattr(settings, "SPECIES_WORMS_RATE_LIMIT_WINDOW_SECONDS", 7)

        body = await _read_route(first=date(2026, 9, 1), last=date(2026, 9, 1))

        (worms,) = [entry for entry in body["providers"] if entry["provider"] == "worms"]
        assert worms["limit"]["per_day"] == 5 * 86400 // 7 == 61714

    @pytest.mark.asyncio
    async def test_the_longest_range_is_answered_and_one_day_more_is_a_422(self) -> None:
        first = date(2026, 7, 1)

        body = await _read_route(first=first, last=first + timedelta(days=MAX_STATS_DAYS - 1))

        assert len(body["days"]) == MAX_STATS_DAYS
        with pytest.raises(UnprocessableEntityException):
            await _read_route(first=first, last=first + timedelta(days=MAX_STATS_DAYS))

    @pytest.mark.asyncio
    async def test_a_range_running_backwards_is_a_422(self) -> None:
        with pytest.raises(UnprocessableEntityException):
            await _read_route(first=date(2026, 9, 2), last=date(2026, 9, 1))


@pytest.fixture(scope="module")
def admin_app() -> Any:
    """No migrations on start: the gate refuses before any query, and the success case stubs it."""
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


class TestTheGateOverTheWire:
    _PARAMS = {"from": "2026-11-01", "to": "2026-11-30"}

    def test_no_token_is_a_401(self, admin_app: Any) -> None:
        with TestClient(admin_app) as client:
            assert client.get("/api/v1/admin/external-requests", params=self._PARAMS).status_code == 401

    def test_a_member_is_a_403(self, admin_app: Any) -> None:
        admin_app.dependency_overrides[get_current_user] = lambda: {"id": 1, "is_superuser": False}
        try:
            with TestClient(admin_app) as client:
                response = client.get("/api/v1/admin/external-requests", params=self._PARAMS)
        finally:
            admin_app.dependency_overrides = {}

        assert response.status_code == 403

    def test_the_operator_reads_from_and_to_by_those_names(self, admin_app: Any) -> None:
        admin_app.dependency_overrides[get_current_user] = lambda: {"id": 1, "is_superuser": True}
        admin_app.dependency_overrides[async_get_db] = lambda: Mock()
        try:
            with (
                patch("src.app.api.v1.admin.external_requests_between", AsyncMock(return_value=[])),
                TestClient(admin_app) as client,
            ):
                response = client.get("/api/v1/admin/external-requests", params=self._PARAMS)
        finally:
            admin_app.dependency_overrides = {}

        assert response.status_code == 200
        body = response.json()
        assert (body["from"], body["to"], len(body["days"])) == ("2026-11-01", "2026-11-30", 30)


class TestTheSchedule:
    def test_the_move_runs_at_start_and_every_five_minutes(self) -> None:
        (entry,) = [job for job in WorkerSettings.cron_jobs if job.coroutine is move_external_request_counts]

        assert entry.run_at_startup
        assert entry.minute == set(range(0, 60, 5))
        assert (entry.hour, entry.second) == (None, 0)


# -------------- the buffer and the move, against Postgres --------------

_DAY = date(2019, 6, 10)


def _stored(db: Session) -> dict[tuple[date, str, str, str], tuple[int, int, int]]:
    db.expire_all()
    rows = db.scalars(select(DailyExternalRequest).where(DailyExternalRequest.day.between(_DAY, _DAY + timedelta(3))))
    return {
        (row.day, row.provider, row.kind, row.outcome): (row.count, row.duration_ms_total, row.duration_ms_max)
        for row in rows
    }


@pytest.fixture
def pinned_days(db: Session) -> Generator[None]:
    def clear() -> None:
        db.execute(delete(DailyExternalRequest).where(DailyExternalRequest.day.between(_DAY, _DAY + timedelta(days=3))))
        db.commit()

    clear()
    yield
    clear()


@pytest_asyncio.fixture
async def _dispose_the_app_engine() -> AsyncGenerator[None]:
    """The move opens its own session from the app's engine, and a pooled asyncpg connection
    belongs to the loop that opened it."""
    await async_engine.dispose()
    yield
    await async_engine.dispose()


def _at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


async def _count(request: ExternalRequest, outcome: Outcome, duration_ms: int = 0, *, day: date = _DAY) -> None:
    with clock(_at(day)):
        await external_requests.count([request], outcome, duration_ms)


@needs_a_database
@pytest.mark.usefixtures("pinned_days", "_dispose_the_app_engine")
class TestTheMove:
    @pytest.mark.asyncio
    async def test_postgres_gains_exactly_what_the_buffer_gave_up(self, db: Session, redis: FakeRedis) -> None:
        await _count(_SEA, Outcome.OK, 120)
        await _count(_SEA, Outcome.OK, 300)
        await _count(_SEA, Outcome.CACHE_HIT)
        await _count(_AVATAR, Outcome.DEADLINE, 3000, day=_DAY + timedelta(days=1))

        assert await move_external_request_counts({}) == "Moved 3 external request count(s)"

        assert _stored(db) == {
            (_DAY, "open-meteo", "sea", "ok"): (2, 420, 300),
            (_DAY, "open-meteo", "sea", "cache_hit"): (1, 0, 0),
            (_DAY + timedelta(days=1), "google", "avatar", "deadline"): (1, 3000, 3000),
        }
        assert _buffered(redis) == {}

    @pytest.mark.asyncio
    async def test_a_second_move_adds_to_the_row_and_keeps_the_slowest(self, db: Session, redis: FakeRedis) -> None:
        await _count(_SEA, Outcome.OK, 900)
        await move_external_request_counts({})
        await _count(_SEA, Outcome.OK, 100)
        await move_external_request_counts({})

        assert _stored(db) == {(_DAY, "open-meteo", "sea", "ok"): (2, 1000, 900)}

    @pytest.mark.asyncio
    async def test_an_event_lands_on_the_day_it_was_counted(self, db: Session, redis: FakeRedis) -> None:
        await _count(_SEA, Outcome.OK, 10, day=_DAY)

        with clock(_at(_DAY + timedelta(days=2))):
            await move_external_request_counts({})

        assert list(_stored(db)) == [(_DAY, "open-meteo", "sea", "ok")]

    @pytest.mark.asyncio
    async def test_a_move_that_finds_nothing_changes_nothing(self, db: Session, redis: FakeRedis) -> None:
        await _count(_SEA, Outcome.OK, 10)
        await move_external_request_counts({})
        before = _stored(db)

        with patch("src.app.core.worker.functions.local_session") as session:
            assert await move_external_request_counts({}) == "No external request counts to move"

        session.assert_not_called()
        assert _stored(db) == before

    @pytest.mark.asyncio
    async def test_an_interrupted_move_loses_what_it_took_and_never_counts_it_twice(
        self, db: Session, redis: FakeRedis
    ) -> None:
        await _count(_SEA, Outcome.OK, 10)

        with (
            patch("sqlalchemy.ext.asyncio.AsyncSession.commit", AsyncMock(side_effect=ConnectionError("cut off"))),
            pytest.raises(ConnectionError),
        ):
            await move_external_request_counts({})
        await move_external_request_counts({})

        assert _stored(db) == {}
        assert _buffered(redis) == {}

    @pytest.mark.asyncio
    async def test_a_row_counting_nothing_is_never_written(self, db: Session, redis: FakeRedis) -> None:
        redis.counters[f"{_PREFIX}{_DAY.isoformat()}:open-meteo:sea:ok"] = b"0 0 0"
        redis.counters[f"{_PREFIX}{_DAY.isoformat()}:open-meteo:sea:status"] = b"not a row"

        assert await move_external_request_counts({}) == "No external request counts to move"

        assert _stored(db) == {}
        assert _buffered(redis) == {}


class TestTheBuffer:
    @pytest.mark.asyncio
    async def test_a_count_outlives_a_day_long_worker_outage(self, redis: FakeRedis) -> None:
        await _count(_SEA, Outcome.OK, 10)

        ((key, seconds),) = redis.counter_expiries.items()
        assert key.startswith(_PREFIX)
        assert seconds > 2 * 86400

    @pytest.mark.asyncio
    async def test_a_rows_count_and_durations_are_one_key(self, redis: FakeRedis) -> None:
        """So an eviction under memory pressure drops the three together."""
        await _count(_SEA, Outcome.OK, 10)
        await _count(_SEA, Outcome.OK, 30)

        assert list(redis.counters.values()) == [b"2 40 30"]

    @pytest.mark.asyncio
    async def test_an_event_counted_during_a_take_waits_for_the_next(self, redis: FakeRedis) -> None:
        await _count(_SEA, Outcome.OK, 10)
        getdel = redis.getdel

        async def counted_meanwhile(key: str) -> bytes | None:
            taken = await getdel(key)
            await _count(_SEA, Outcome.OK, 20)
            return taken

        with patch.object(redis, "getdel", counted_meanwhile):
            (first,) = await external_requests.take_buffered()
        (second,) = await external_requests.take_buffered()

        assert (first.count, first.duration_ms_total, second.count, second.duration_ms_total) == (1, 10, 1, 20)

    @pytest.mark.asyncio
    async def test_a_key_scan_returns_twice_is_taken_once(self, redis: FakeRedis) -> None:
        """Redis's `SCAN` may repeat a key. Taken twice, with a count landing between, it would
        give the upsert two rows of one identity, which Postgres refuses for the whole batch."""
        await _count(_SEA, Outcome.OK, 10)
        (key,) = redis.counters
        getdel = redis.getdel

        async def repeated(cursor: int = 0, match: str = "*", count: int | None = None) -> tuple[int, list[str]]:
            return 0, [key, key]

        async def counted_meanwhile(taken_key: str) -> bytes | None:
            taken = await getdel(taken_key)
            await _count(_SEA, Outcome.OK, 20)
            return taken

        with patch.object(redis, "scan", repeated), patch.object(redis, "getdel", counted_meanwhile):
            rows = await external_requests.take_buffered()

        assert [(row.count, row.duration_ms_total) for row in rows] == [(1, 10)]
        assert _buffered(redis) == {("open-meteo", "sea", "ok"): (1, 20, 20)}
