"""Unit tests for the health check endpoints.

`/health` answers for the process; `/health/ready` answers for the process *and* its two
datastores. The split matters because the image's `HEALTHCHECK` runs the readiness one and
`docker compose ps` reports its verdict, so a wrong answer here is a red status column or,
worse, a green one over a broken instance.
"""

import asyncio
from collections.abc import Generator
from time import perf_counter
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from src.app.api.v1.health import router as health_router
from src.app.core.db.database import async_get_db
from src.app.middleware.client_cache_middleware import ClientCacheMiddleware


def _make_health_client(*, db: AsyncMock | None = None, with_cache_middleware: bool = False) -> TestClient:
    """Build a minimal app exposing only the health router.

    This avoids exercising the full application lifespan (DB/Redis setup),
    keeping the test focused on the health endpoint's own behavior.
    """
    app = FastAPI()
    if with_cache_middleware:
        # `add_middleware` is typed against the middleware's own __init__ signature, which
        # starlette can't infer through BaseHTTPMiddleware's `app` parameter.
        app.add_middleware(ClientCacheMiddleware, max_age=60)  # type: ignore[arg-type]
    app.include_router(health_router)

    session = db if db is not None else AsyncMock()
    app.dependency_overrides[async_get_db] = lambda: session

    return TestClient(app, raise_server_exceptions=False)


def _healthy_db() -> AsyncMock:
    return AsyncMock()


#: What a hung datastore does, and the bound the probe is held to while it does it. The
#: gap between them is the whole assertion: the request has to end on the bound, so any
#: elapsed time near `_HANG_SECONDS` means no bound was applied.
_HANG_SECONDS = 10.0
_BOUND = 0.05


async def _never_answers(*args: Any, **kwargs: Any) -> None:
    await asyncio.sleep(_HANG_SECONDS)


def _unreachable_db() -> AsyncMock:
    session = AsyncMock()
    session.execute.side_effect = OperationalError("SELECT 1", {}, Exception("connection refused"))
    return session


@pytest.fixture
def reachable_redis() -> Generator[AsyncMock, Any]:
    client = AsyncMock()
    with patch("src.app.core.utils.cache.client", client):
        yield client


@pytest.fixture
def unreachable_redis() -> Generator[AsyncMock, Any]:
    client = AsyncMock()
    client.ping.side_effect = RedisConnectionError("Error 111 connecting to redis:6379.")
    with patch("src.app.core.utils.cache.client", client):
        yield client


@pytest.fixture
def unconfigured_redis() -> Generator[None, Any]:
    with patch("src.app.core.utils.cache.client", None):
        yield


class TestLiveness:
    def test_returns_200(self, reachable_redis: AsyncMock):
        client = _make_health_client()

        response = client.get("/health")

        assert response.status_code == 200

    def test_returns_expected_payload(self, reachable_redis: AsyncMock):
        client = _make_health_client()

        response = client.get("/health")
        body = response.json()

        assert body["status"] == "healthy"
        assert body["message"] == "API is running"
        assert "version" in body

    def test_does_not_touch_the_database(self):
        """The whole point of the split: liveness must not fail on a datastore outage."""
        db = _unreachable_db()
        client = _make_health_client(db=db)

        response = client.get("/health")

        assert response.status_code == 200
        db.execute.assert_not_called()

    def test_is_never_cacheable(self):
        client = _make_health_client()

        response = client.get("/health")

        assert response.headers["Cache-Control"] == "no-store"


class TestReadiness:
    def test_returns_200_when_both_datastores_answer(self, reachable_redis: AsyncMock):
        client = _make_health_client(db=_healthy_db())

        response = client.get("/health/ready")

        assert response.status_code == 200
        assert response.json() == {"status": "ready", "database": "ok", "redis": "ok"}
        reachable_redis.ping.assert_awaited_once()

    def test_round_trips_postgres(self, reachable_redis: AsyncMock):
        db = _healthy_db()
        client = _make_health_client(db=db)

        client.get("/health/ready")

        db.execute.assert_awaited_once()
        assert "SELECT 1" in str(db.execute.await_args.args[0])

    def test_503_when_the_database_is_unreachable(self, reachable_redis: AsyncMock):
        client = _make_health_client(db=_unreachable_db())

        response = client.get("/health/ready")

        assert response.status_code == 503
        assert response.json()["detail"] == "Not ready: database unreachable"

    def test_503_when_redis_is_unreachable(self, unreachable_redis: AsyncMock):
        client = _make_health_client(db=_healthy_db())

        response = client.get("/health/ready")

        assert response.status_code == 503
        assert response.json()["detail"] == "Not ready: redis unreachable"

    def test_503_when_redis_was_never_configured(self, unconfigured_redis: None):
        """A missing client is a different fault from a dead server, and says so."""
        client = _make_health_client(db=_healthy_db())

        response = client.get("/health/ready")

        assert response.status_code == 503
        assert response.json()["detail"] == "Not ready: redis is not configured"

    def test_names_every_failing_dependency(self, unreachable_redis: AsyncMock):
        client = _make_health_client(db=_unreachable_db())

        response = client.get("/health/ready")

        assert response.json()["detail"] == "Not ready: database unreachable, redis unreachable"

    def test_checks_redis_even_when_the_database_is_down(self, unreachable_redis: AsyncMock):
        """No short-circuit: an operator wants the whole picture from one request."""
        client = _make_health_client(db=_unreachable_db())

        client.get("/health/ready")

        unreachable_redis.ping.assert_awaited_once()

    def test_the_failure_detail_leaks_no_connection_string(self, reachable_redis: AsyncMock):
        """This endpoint answers anyone who can reach it; the reason goes to the logs."""
        db = AsyncMock()
        db.execute.side_effect = OperationalError(
            "SELECT 1", {}, Exception("could not connect to server at db:5432, user postgres")
        )
        client = _make_health_client(db=db)

        response = client.get("/health/ready")

        assert "5432" not in response.text
        assert "postgres" not in response.text

    def test_a_hanging_database_is_cut_off_rather_than_waited_on(self, reachable_redis: AsyncMock):
        """The probe is bounded, so a hung dependency reports not-ready instead of hanging.

        The hang is real rather than an injected `TimeoutError`: that would be a caught
        exception type with or without the `asyncio.timeout` block, so it would pass
        against code that has no bound at all. Here nothing but the bound can end the
        request, and the elapsed assertion is what says so - without it, a missing bound
        turns this into a ten-second wait for a wrong answer rather than a fast failure.
        """
        db = AsyncMock()
        db.execute.side_effect = _never_answers
        client = _make_health_client(db=db)

        with patch("src.app.api.v1.health._PROBE_TIMEOUT_SECONDS", _BOUND):
            started = perf_counter()
            response = client.get("/health/ready")
            elapsed = perf_counter() - started

        assert response.status_code == 503
        assert response.json()["detail"] == "Not ready: database unreachable"
        assert elapsed < _HANG_SECONDS / 2

    def test_a_hanging_redis_is_cut_off_too(self):
        """Its own `asyncio.timeout` block, so its own test."""
        redis_client = AsyncMock()
        redis_client.ping.side_effect = _never_answers
        client = _make_health_client(db=_healthy_db())

        with (
            patch("src.app.core.utils.cache.client", redis_client),
            patch("src.app.api.v1.health._PROBE_TIMEOUT_SECONDS", _BOUND),
        ):
            started = perf_counter()
            response = client.get("/health/ready")
            elapsed = perf_counter() - started

        assert response.status_code == 503
        assert response.json()["detail"] == "Not ready: redis unreachable"
        assert elapsed < _HANG_SECONDS / 2


class TestReadinessIsNeverCached:
    def test_no_store_when_ready(self, reachable_redis: AsyncMock):
        client = _make_health_client(db=_healthy_db())

        response = client.get("/health/ready")

        assert response.headers["Cache-Control"] == "no-store"

    def test_no_store_when_not_ready(self, unreachable_redis: AsyncMock):
        """The path that most needs it, and the one that loses the injected response."""
        client = _make_health_client(db=_unreachable_db())

        response = client.get("/health/ready")

        assert response.headers["Cache-Control"] == "no-store"

    @pytest.mark.parametrize("path", ["/health", "/health/ready"])
    def test_the_cache_middleware_does_not_relabel_it_public(self, reachable_redis: AsyncMock, path: str):
        """Both are anonymous GETs, which is exactly `ClientCacheMiddleware`'s public case."""
        client = _make_health_client(db=_healthy_db(), with_cache_middleware=True)

        response = client.get(path)

        assert response.headers["Cache-Control"] == "no-store"

    def test_the_cache_middleware_does_not_relabel_a_503_public(self, unreachable_redis: AsyncMock):
        client = _make_health_client(db=_unreachable_db(), with_cache_middleware=True)

        response = client.get("/health/ready")

        assert response.status_code == 503
        assert response.headers["Cache-Control"] == "no-store"
