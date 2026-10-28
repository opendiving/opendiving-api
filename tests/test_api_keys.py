"""API keys: the branch in `get_current_user`, the refusals a key gets, and the three routes
that create, list and revoke one.

Split as `test_sessions.py` is. The structural and dependency classes at the top need no
database: what they pin is a decision - which routes refuse a key, which methods a read key
may use, what the dict says. Everything under *against Postgres* drives real rows, because
"expired", "revoked" and "the owner asked for deletion" are `WHERE` clauses a mocked session
evaluates none of, and the HTTP half sends a real bearer value - a key this app minted, or a
session token `issue_tokens` minted - through the whole middleware stack with nothing but the
database overridden.

The Postgres-backed classes skip silently without a reachable database -
`POSTGRES_SERVER=localhost` on a developer's machine. CI sets it and fails the job if anything
skips. See CONTRIBUTING.md.
"""

import hashlib
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api import router as api_router
from src.app.api.dependencies import get_current_superuser, get_current_user, refuse_api_keys
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.exceptions.http_exceptions import ForbiddenException, UnauthorizedException
from src.app.core.security import API_KEY_PREFIX, API_KEY_SHOWN_LENGTH, generate_api_key
from src.app.core.setup import create_application
from src.app.core.utils.request_context import RequestContext
from src.app.crud.crud_api_keys import API_KEY_LAST_USED_GRANULARITY, LiveApiKey
from src.app.main import app
from src.app.models.api_key import ApiKey
from src.app.models.auth_audit_event import AuthAuditEvent
from src.app.models.user import User
from src.app.schemas.api_key import ApiKeyLifetime, ApiKeyScope
from src.app.schemas.auth_audit_event import AuthEventType
from src.app.services.auth_service import issue_tokens
from tests.conftest import db_available
from tests.helpers.fake_redis import FakeRedis
from tests.helpers.mocks import awaited_kwargs
from tests.helpers.routes import dependency_calls, iter_api_routes
from tests.test_route_authentication import ANONYMOUS_BY_DESIGN

needs_a_database = pytest.mark.skipif(not db_available(), reason="No database connection available")

CONTEXT = RequestContext(ip="203.0.113.7", user_agent="Mozilla/5.0 (X11; Linux x86_64) TestAgent/1.0")

# The rule `refuse_api_keys` is declared by: every route under one of these that takes a
# session, and `DELETE /user`. A rule rather than a list, so a route added under a prefix is
# held to it without anyone remembering to add it here.
REFUSING_PREFIXES = (
    "/api/v1/user/api-key",
    "/api/v1/user/passkey",
    "/api/v1/user/session",
    "/api/v1/user/checkin-link",
    "/api/v1/user/invite-link",
    "/api/v1/user/email-change",
)
ALSO_REFUSING = {("DELETE", "/api/v1/user")}

READ_ONLY = "This API key is read-only."
NOT_WITH_A_KEY = "This needs a signed-in browser or app, not an API key."
NOT_AUTHENTICATED = {"detail": "User not authenticated."}


def _request(method: str = "GET") -> Mock:
    request = Mock()
    request.method = method
    return request


def _falls_under_the_rule(key: tuple[str, str]) -> bool:
    return key in ALSO_REFUSING or key[1].startswith(REFUSING_PREFIXES)


class TestWhichRoutesRefuseAKey:
    """The structural half of the refusal, in `test_route_authentication.py`'s shape."""

    def test_every_route_under_the_rule_refuses_a_key(self) -> None:
        missing = sorted(
            route.key
            for route in iter_api_routes(app)
            if _falls_under_the_rule(route.key)
            and route.key not in ANONYMOUS_BY_DESIGN
            and refuse_api_keys not in dependency_calls(route.dependant)
        )

        assert not missing, (
            "these routes manage a credential, the sign-in address or the account and accept an API key:\n"
            + "\n".join(f"  {method:6} {path}" for method, path in missing)
            + "\n\nDeclare `dependencies=[Depends(refuse_api_keys)]` on the route."
        )

    def test_nothing_outside_the_rule_refuses_one(self) -> None:
        """The other direction: a route a key may use, refused by mistake, is a tool that
        breaks for a reason nobody wrote down."""
        stray = sorted(
            route.key
            for route in iter_api_routes(app)
            if refuse_api_keys in dependency_calls(route.dependant) and not _falls_under_the_rule(route.key)
        )

        assert not stray, f"these routes refuse an API key outside the rule: {stray}"

    def test_the_anonymous_email_change_routes_take_no_bearer(self) -> None:
        """`refuse_api_keys` reads the header through `oauth2_scheme`, which 401s a request
        that carries none - so on the link-authorized routes it would break the link."""
        anonymous = [
            route
            for route in iter_api_routes(app)
            if _falls_under_the_rule(route.key) and route.key in ANONYMOUS_BY_DESIGN
        ]

        assert {route.key for route in anonymous} == {
            ("GET", "/api/v1/user/email-change/verify/check"),
            ("POST", "/api/v1/user/email-change/verify"),
        }
        assert all(refuse_api_keys not in dependency_calls(route.dependant) for route in anonymous)

    def test_no_part_of_the_rule_is_stale(self) -> None:
        """A prefix or an entry naming no route guards nothing, and pre-approves whatever is
        registered there next as a route a key is refused on - or, read the other way,
        reports a sweep that has quietly stopped finding anything."""
        keys = {route.key for route in iter_api_routes(app)}

        assert ALSO_REFUSING <= keys
        assert all(any(path.startswith(prefix) for _method, path in keys) for prefix in REFUSING_PREFIXES)

    @pytest.mark.asyncio
    async def test_the_refusal_decides_on_the_prefix(self) -> None:
        with pytest.raises(ForbiddenException) as refused:
            await refuse_api_keys(f"{API_KEY_PREFIX}anything")

        assert refused.value.detail == NOT_WITH_A_KEY
        await refuse_api_keys("eyJhbGciOiJIUzI1NiJ9.e30.signature")


class TestTheBranch:
    """`get_current_user` with the key lookups stood in: what the dependency decides."""

    @staticmethod
    def _live(scope: ApiKeyScope) -> LiveApiKey:
        return LiveApiKey(id=1, scope=scope, owner_uuid=uuid7(), last_used_at=None)

    @staticmethod
    async def _resolve(method: str, scope: ApiKeyScope, account: dict[str, Any] | None = None) -> dict[str, Any] | None:
        with (
            patch("src.app.api.dependencies.live_api_key", new_callable=AsyncMock) as live,
            patch("src.app.api.dependencies.read_account", new_callable=AsyncMock) as read,
            patch("src.app.api.dependencies.stamp_api_key_used", new_callable=AsyncMock),
        ):
            live.return_value = TestTheBranch._live(scope)
            read.return_value = account or {"id": 7, "is_superuser": False}
            return await get_current_user(_request(method), f"{API_KEY_PREFIX}secret", Mock())

    @pytest.mark.asyncio
    async def test_a_key_never_reaches_the_jwt_path(self) -> None:
        """So the blacklist is not queried for it, and an unknown key is refused by the key
        lookup alone."""
        with (
            patch("src.app.api.dependencies.verify_token", new_callable=AsyncMock) as verify,
            patch("src.app.api.dependencies.live_api_key", new_callable=AsyncMock, return_value=None),
            pytest.raises(UnauthorizedException) as refused,
        ):
            await get_current_user(_request(), f"{API_KEY_PREFIX}unknown", Mock())

        verify.assert_not_called()
        assert refused.value.detail == NOT_AUTHENTICATED["detail"]

    @pytest.mark.asyncio
    async def test_any_other_string_takes_the_jwt_path(self) -> None:
        with (
            patch("src.app.api.dependencies.verify_token", new_callable=AsyncMock, return_value=None) as verify,
            patch("src.app.api.dependencies.live_api_key", new_callable=AsyncMock) as live,
            pytest.raises(UnauthorizedException),
        ):
            await get_current_user(_request(), "eyJhbGciOiJIUzI1NiJ9.e30.signature", Mock())

        verify.assert_awaited_once()
        live.assert_not_called()

    @pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
    @pytest.mark.asyncio
    async def test_a_read_key_may_use_the_safe_methods(self, method: str) -> None:
        assert await self._resolve(method, ApiKeyScope.READ) is not None

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    @pytest.mark.asyncio
    async def test_a_read_key_is_refused_every_other_method_before_the_route_runs(self, method: str) -> None:
        with pytest.raises(ForbiddenException) as refused:
            await self._resolve(method, ApiKeyScope.READ)

        assert refused.value.detail == READ_ONLY

    @pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"])
    @pytest.mark.asyncio
    async def test_a_write_key_may_use_every_method(self, method: str) -> None:
        assert await self._resolve(method, ApiKeyScope.WRITE) is not None

    @pytest.mark.asyncio
    async def test_an_unrecognised_scope_is_read_only(self) -> None:
        """Fail closed: only `write` writes, so a value this build does not know cannot."""
        with pytest.raises(ForbiddenException):
            await self._resolve("POST", "admin")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_a_superusers_key_resolves_to_a_diver(self) -> None:
        """Owner decision: the dict says so, so the gate and every inline read of the flag
        see a diver - and `GET /user` reports what the caller may do."""
        resolved = await self._resolve("GET", ApiKeyScope.WRITE, {"id": 7, "is_superuser": True})

        assert resolved is not None
        assert resolved["is_superuser"] is False
        with pytest.raises(ForbiddenException) as refused:
            await get_current_superuser(resolved)
        assert refused.value.detail == "You do not have enough privileges."

    @pytest.mark.asyncio
    async def test_a_key_whose_account_vanished_mid_request_is_refused(self) -> None:
        with (
            patch(
                "src.app.api.dependencies.live_api_key",
                new_callable=AsyncMock,
                return_value=self._live(ApiKeyScope.WRITE),
            ),
            patch("src.app.api.dependencies.read_account", new_callable=AsyncMock, return_value=None),
            pytest.raises(UnauthorizedException),
        ):
            await get_current_user(_request(), f"{API_KEY_PREFIX}secret", Mock())


class TestTheSecret:
    def test_it_is_the_prefix_and_256_random_bits_and_stored_as_its_sha256(self) -> None:
        secret, token_hash = generate_api_key()

        assert secret.startswith(API_KEY_PREFIX)
        assert len(secret) == 47
        assert token_hash == hashlib.sha256(secret.encode()).hexdigest()
        assert generate_api_key()[0] != secret

    @pytest.mark.parametrize(
        ("lifetime", "days"),
        [(ApiKeyLifetime.THIRTY_DAYS, 30), (ApiKeyLifetime.NINETY_DAYS, 90), (ApiKeyLifetime.ONE_YEAR, 365)],
    )
    def test_a_lifetime_is_a_number_of_days_from_now(self, lifetime: ApiKeyLifetime, days: int) -> None:
        now = datetime(2026, 10, 28, 12, tzinfo=UTC)

        assert lifetime.expires_at(now) == now + timedelta(days=days)

    def test_never_is_no_expiry(self) -> None:
        assert ApiKeyLifetime.NEVER.expires_at(datetime.now(UTC)) is None


# -------------- against Postgres --------------


def _key_row(diver: User, *, scope: str = "write", expires_at: datetime | None = None) -> tuple[str, ApiKey]:
    secret, token_hash = generate_api_key()
    row = ApiKey(
        user_id=diver.id,
        name="laptop",
        prefix=secret[:API_KEY_SHOWN_LENGTH],
        token_hash=token_hash,
        scope=scope,
        expires_at=expires_at,
    )
    return secret, row


def _store_key(db: Session, diver: User, **fields: Any) -> tuple[str, ApiKey]:
    secret, row = _key_row(diver, **fields)
    db.add(row)
    db.commit()
    db.refresh(row)
    return secret, row


class _Clock:
    """Stand-in for `datetime` in `api.dependencies`, which reaches for `now` alone."""

    def __init__(self, now: datetime) -> None:
        self.at = now

    def now(self, tz: Any = None) -> datetime:
        return self.at


@needs_a_database
class TestAKeyAgainstTheDatabase:
    """`get_current_user` over real rows, in `TestARevokedSessionEndsItsAccessToken`'s shape."""

    @pytest.mark.asyncio
    async def test_a_live_key_resolves_to_its_account(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        secret, _ = _store_key(db, diver)

        user = await get_current_user(_request(), secret, async_db)

        assert user is not None
        assert user["id"] == diver.id
        assert user["uuid"] == diver.uuid

    @pytest.mark.asyncio
    async def test_a_superusers_key_resolves_with_the_flag_false(self, db: Session, async_db: AsyncSession) -> None:
        from tests.helpers.generators import create_user

        operator = create_user(db, is_super_user=True)
        secret, _ = _store_key(db, operator)

        user = await get_current_user(_request(), secret, async_db)

        assert user is not None
        assert user["id"] == operator.id
        assert user["is_superuser"] is False

    @pytest.mark.asyncio
    async def test_an_unknown_key_is_refused(self, async_db: AsyncSession) -> None:
        with pytest.raises(UnauthorizedException) as refused:
            await get_current_user(_request(), generate_api_key()[0], async_db)

        assert refused.value.detail == NOT_AUTHENTICATED["detail"]

    @pytest.mark.asyncio
    async def test_an_expired_key_is_refused(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        secret, _ = _store_key(db, diver, expires_at=datetime.now(UTC) - timedelta(minutes=1))

        with pytest.raises(UnauthorizedException) as refused:
            await get_current_user(_request(), secret, async_db)

        assert refused.value.detail == NOT_AUTHENTICATED["detail"]

    @pytest.mark.asyncio
    async def test_a_key_that_expires_later_authenticates(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The other half of the clause above: a check that refused every key with an expiry
        would pass it."""
        secret, _ = _store_key(db, diver, expires_at=datetime.now(UTC) + timedelta(days=1))

        assert await get_current_user(_request(), secret, async_db) is not None

    @pytest.mark.asyncio
    async def test_a_revoked_key_stops_on_the_very_next_request(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        secret, row = _store_key(db, diver)
        assert await get_current_user(_request(), secret, async_db) is not None

        db.delete(row)
        db.commit()

        with pytest.raises(UnauthorizedException) as refused:
            await get_current_user(_request(), secret, async_db)
        assert refused.value.detail == NOT_AUTHENTICATED["detail"]

    @pytest.mark.asyncio
    async def test_a_key_whose_owner_asked_for_deletion_is_refused(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """Dark through the grace period, as the account's sessions are."""
        secret, _ = _store_key(db, diver)
        db.execute(update(User).where(User.id == diver.id).values(is_deleted=True))
        db.commit()

        with pytest.raises(UnauthorizedException) as refused:
            await get_current_user(_request(), secret, async_db)
        assert refused.value.detail == NOT_AUTHENTICATED["detail"]

    @pytest.mark.asyncio
    async def test_another_accounts_key_names_that_account(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        secret, _ = _store_key(db, other_diver)

        user = await get_current_user(_request(), secret, async_db)

        assert user is not None
        assert user["id"] == other_diver.id


@needs_a_database
class TestLastUsedIsThrottled:
    """At most one write per `API_KEY_LAST_USED_GRANULARITY`, and every write committed before
    the route runs - read back through a second session, which sees only committed rows."""

    @staticmethod
    async def _stamp(async_sessions: async_sessionmaker[AsyncSession], key_id: int) -> datetime | None:
        async with async_sessions() as reader:
            return (await reader.execute(select(ApiKey.last_used_at).where(ApiKey.id == key_id))).scalar_one()

    @pytest.mark.asyncio
    async def test_uses_inside_the_window_write_once_and_one_past_it_writes_again(
        self, db: Session, async_sessions: async_sessionmaker[AsyncSession], diver: User
    ) -> None:
        secret, row = _store_key(db, diver)
        assert row.last_used_at is None
        start = datetime.now(UTC).replace(microsecond=0)
        clock = _Clock(start)

        async def use(at: datetime) -> None:
            clock.at = at
            async with async_sessions() as session:
                await get_current_user(_request(), secret, session)

        with patch("src.app.api.dependencies.datetime", clock):
            await use(start)
            assert await self._stamp(async_sessions, row.id) == start

            await use(start + API_KEY_LAST_USED_GRANULARITY - timedelta(seconds=1))
            assert await self._stamp(async_sessions, row.id) == start

            later = start + API_KEY_LAST_USED_GRANULARITY + timedelta(seconds=1)
            await use(later)
            assert await self._stamp(async_sessions, row.id) == later


@pytest.fixture(scope="module")
def keys_app() -> Any:
    return create_application(router=api_router, settings=settings, apply_migrations_on_start=False)


@pytest_asyncio.fixture
async def http(keys_app: Any, async_db: AsyncSession) -> AsyncGenerator[httpx.AsyncClient]:
    """The real app with only the database overridden: every credential below is a real
    bearer value, so `get_current_user` and `refuse_api_keys` run as they do in production."""

    async def the_tests_session() -> AsyncGenerator[AsyncSession]:
        yield async_db

    keys_app.dependency_overrides[async_get_db] = the_tests_session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=keys_app), base_url="http://test") as client:
        yield client
    keys_app.dependency_overrides = {}


async def _browser(async_db: AsyncSession, diver: User) -> dict[str, str]:
    """A signed-in browser's header, minted by the same `issue_tokens` sign-in uses."""
    tokens = await issue_tokens(Response(), diver.uuid, db=async_db, context=CONTEXT, user_id=diver.id)
    return {"Authorization": f"Bearer {tokens.access_token}"}


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


async def _create(
    http: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    scope: str = "write",
    lifetime: str = "1y",
    name: str = "laptop",
) -> httpx.Response:
    with patch("src.app.api.v1.api_keys.send_api_key_created_email", new_callable=AsyncMock):
        return await http.post(
            "/api/v1/user/api-keys", json={"name": name, "scope": scope, "lifetime": lifetime}, headers=headers
        )


@needs_a_database
class TestCreatingAKey:
    @pytest.mark.asyncio
    async def test_the_secret_is_in_the_201_and_only_its_hash_is_stored(
        self, http: httpx.AsyncClient, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        with patch("src.app.api.v1.api_keys.send_api_key_created_email", new_callable=AsyncMock) as notice:
            response = await http.post(
                "/api/v1/user/api-keys",
                json={"name": "laptop", "scope": "read", "lifetime": "30d"},
                headers=await _browser(async_db, diver),
            )

        assert response.status_code == 201, response.text
        body = response.json()
        assert set(body) == {"uuid", "name", "prefix", "scope", "created_at", "last_used_at", "expires_at", "secret"}
        secret = body["secret"]
        assert secret.startswith(API_KEY_PREFIX) and len(secret) == 47
        assert body["prefix"] == secret[:API_KEY_SHOWN_LENGTH]
        assert body["scope"] == "read"
        assert body["last_used_at"] is None
        created = datetime.fromisoformat(body["created_at"])
        assert datetime.fromisoformat(body["expires_at"]) - created == pytest.approx(
            timedelta(days=30), abs=timedelta(seconds=5)
        )

        row = db.execute(select(ApiKey).where(ApiKey.user_id == diver.id)).scalar_one()
        assert row.token_hash == hashlib.sha256(secret.encode()).hexdigest()
        assert secret not in {getattr(row, column) for column in ApiKey.__table__.columns.keys()}

        sent = awaited_kwargs(notice)
        assert sent == {
            "email": diver.email,
            "key_name": "laptop",
            "prefix": body["prefix"],
            "scope": "read",
            "expires_at": datetime.fromisoformat(body["expires_at"]),
        }
        assert secret not in {str(value) for value in sent.values()}

    @pytest.mark.asyncio
    async def test_it_writes_one_audit_event_naming_the_provider_and_no_part_of_the_key(
        self, http: httpx.AsyncClient, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        response = await _create(http, await _browser(async_db, diver))
        assert response.status_code == 201

        events = (
            db.execute(
                select(AuthAuditEvent).where(
                    AuthAuditEvent.user_id == diver.id, AuthAuditEvent.event_type.like("api_key%")
                )
            )
            .scalars()
            .all()
        )
        assert [(event.event_type, event.provider) for event in events] == [(AuthEventType.API_KEY_CREATED, "api_key")]
        written = {str(getattr(events[0], column)) for column in AuthAuditEvent.__table__.columns.keys()}
        assert not any(response.json()["prefix"] in value for value in written)

    @pytest.mark.asyncio
    async def test_a_key_made_to_last_has_no_expiry(
        self, http: httpx.AsyncClient, async_db: AsyncSession, diver: User
    ) -> None:
        response = await _create(http, await _browser(async_db, diver), lifetime="never")

        assert response.status_code == 201
        assert response.json()["expires_at"] is None

    @pytest.mark.asyncio
    async def test_a_client_cannot_send_a_date(
        self, http: httpx.AsyncClient, async_db: AsyncSession, diver: User
    ) -> None:
        response = await http.post(
            "/api/v1/user/api-keys",
            json={"name": "laptop", "scope": "read", "lifetime": "1y", "expires_at": "2099-01-01T00:00:00Z"},
            headers=await _browser(async_db, diver),
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_the_cap_counts_expired_keys_and_answers_409(
        self, http: httpx.AsyncClient, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        _store_key(db, diver, expires_at=datetime.now(UTC) - timedelta(days=1))

        with patch.object(settings, "API_KEY_MAX_PER_USER", 1):
            response = await _create(http, await _browser(async_db, diver))

        assert response.status_code == 409
        assert response.json() == {"detail": "You already have 1 API keys. Revoke one to create another."}

    @pytest.mark.asyncio
    async def test_creation_is_limited_per_account(
        self, http: httpx.AsyncClient, async_db: AsyncSession, diver: User
    ) -> None:
        """The limiter fails open with no Redis, which is how the rest of this module runs -
        so one stands in here, and the real counter counts."""
        headers = await _browser(async_db, diver)

        with (
            patch("src.app.core.utils.cache.client", FakeRedis()),
            patch.object(settings, "API_KEY_CREATE_RATE_LIMIT_PER_USER", 2),
        ):
            statuses = [(await _create(http, headers, name=f"tool {n}")).status_code for n in range(3)]

        assert statuses == [201, 201, 429]


@needs_a_database
class TestListingKeys:
    @pytest.mark.asyncio
    async def test_every_key_oldest_first_expired_ones_included_and_never_the_secret(
        self, http: httpx.AsyncClient, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        first_secret, first = _store_key(db, diver, expires_at=datetime.now(UTC) - timedelta(days=1))
        second_secret, second = _store_key(db, diver, scope="read")
        _store_key(db, other_diver)

        response = await http.get("/api/v1/user/api-keys", headers=await _browser(async_db, diver))

        assert response.status_code == 200
        rows = response.json()
        assert [row["uuid"] for row in rows] == [str(first.uuid), str(second.uuid)]
        assert all(
            set(row) == {"uuid", "name", "prefix", "scope", "created_at", "last_used_at", "expires_at"} for row in rows
        )
        assert first_secret not in response.text and second_secret not in response.text
        assert first.token_hash not in response.text


@needs_a_database
class TestRevokingAKey:
    @pytest.mark.asyncio
    async def test_the_row_goes_with_one_audit_event_and_the_key_is_refused_next(
        self, http: httpx.AsyncClient, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        secret, row = _store_key(db, diver)
        assert (await http.get("/api/v1/user", headers=_bearer(secret))).status_code == 200

        with patch("src.app.api.v1.api_keys.send_api_key_revoked_email", new_callable=AsyncMock) as notice:
            response = await http.delete(f"/api/v1/user/api-key/{row.uuid}", headers=await _browser(async_db, diver))

        assert response.status_code == 200
        assert response.json() == {"message": "API key revoked"}
        assert db.execute(select(ApiKey).where(ApiKey.id == row.id)).scalar_one_or_none() is None
        events = db.execute(
            select(AuthAuditEvent.event_type, AuthAuditEvent.provider).where(
                AuthAuditEvent.user_id == diver.id, AuthAuditEvent.event_type.like("api_key%")
            )
        ).all()
        assert [tuple(event) for event in events] == [(AuthEventType.API_KEY_REVOKED, "api_key")]
        assert awaited_kwargs(notice) == {"email": diver.email, "key_name": "laptop"}

        refused = await http.get("/api/v1/user", headers=_bearer(secret))
        assert refused.status_code == 401
        assert refused.json() == NOT_AUTHENTICATED

    @pytest.mark.asyncio
    async def test_a_strangers_key_is_a_404_and_stays(
        self, http: httpx.AsyncClient, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        _, theirs = _store_key(db, other_diver)

        response = await http.delete(f"/api/v1/user/api-key/{theirs.uuid}", headers=await _browser(async_db, diver))

        assert response.status_code == 404
        assert response.json() == {"detail": "API key not found"}
        assert db.execute(select(ApiKey).where(ApiKey.id == theirs.id)).scalar_one_or_none() is not None

    @pytest.mark.asyncio
    async def test_a_second_revoke_is_a_404(
        self, http: httpx.AsyncClient, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        _, row = _store_key(db, diver)
        headers = await _browser(async_db, diver)

        first = await http.delete(f"/api/v1/user/api-key/{row.uuid}", headers=headers)
        second = await http.delete(f"/api/v1/user/api-key/{row.uuid}", headers=headers)

        assert (first.status_code, second.status_code) == (200, 404)


@needs_a_database
class TestAKeyOnTheWire:
    """What a tool holding a key sees, through the whole stack."""

    @pytest.mark.asyncio
    async def test_a_read_key_reads_the_account_and_is_labelled_private(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        secret, _ = _store_key(db, diver, scope="read")

        response = await http.get("/api/v1/user", headers=_bearer(secret))

        assert response.status_code == 200
        assert response.json()["uuid"] == str(diver.uuid)
        assert "public" not in response.headers.get("cache-control", "")

    @pytest.mark.asyncio
    async def test_a_read_key_cannot_write(self, http: httpx.AsyncClient, db: Session, diver: User) -> None:
        secret, _ = _store_key(db, diver, scope="read")

        response = await http.patch("/api/v1/user", json={"name": "Renamed"}, headers=_bearer(secret))

        assert response.status_code == 403
        assert response.json() == {"detail": READ_ONLY}

    @pytest.mark.asyncio
    async def test_a_write_key_writes(self, http: httpx.AsyncClient, db: Session, diver: User) -> None:
        secret, _ = _store_key(db, diver)

        response = await http.patch("/api/v1/user", json={"name": "Renamed"}, headers=_bearer(secret))

        assert response.status_code == 200, response.text
        db.expire_all()
        assert db.execute(select(User.name).where(User.id == diver.id)).scalar_one() == "Renamed"

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/api/v1/user/api-keys"),
            ("GET", "/api/v1/user/api-keys"),
            ("GET", "/api/v1/user/sessions"),
            ("GET", "/api/v1/user/passkeys"),
            ("DELETE", "/api/v1/user"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_write_key_cannot_touch_credentials_or_the_account(
        self, http: httpx.AsyncClient, db: Session, diver: User, method: str, path: str
    ) -> None:
        secret, _ = _store_key(db, diver)

        response = await http.request(
            method,
            path,
            json={"name": "x", "scope": "write", "lifetime": "1y"} if method == "POST" else None,
            headers=_bearer(secret),
        )

        assert response.status_code == 403
        assert response.json() == {"detail": NOT_WITH_A_KEY}
        db.expire_all()
        assert db.execute(select(User.is_deleted).where(User.id == diver.id)).scalar_one() is False

    @pytest.mark.asyncio
    async def test_a_superusers_key_is_a_diver_to_the_gate_and_to_get_user(
        self, http: httpx.AsyncClient, db: Session
    ) -> None:
        from tests.helpers.generators import create_user

        operator = create_user(db, is_super_user=True)
        secret, _ = _store_key(db, operator)

        stats = await http.get("/api/v1/admin/stats", headers=_bearer(secret))
        account = await http.get("/api/v1/user", headers=_bearer(secret))

        assert stats.status_code == 403
        assert stats.json() == {"detail": "You do not have enough privileges."}
        assert account.status_code == 200
        assert account.json()["is_superuser"] is False

    @pytest.mark.asyncio
    async def test_using_a_key_writes_no_audit_event(self, http: httpx.AsyncClient, db: Session, diver: User) -> None:
        """A named exclusion: use is recurring and expected, and `last_used_at` on the key is
        the liveness a row here would restate."""
        secret, _ = _store_key(db, diver, scope="read")

        await http.get("/api/v1/user", headers=_bearer(secret))
        await http.post("/api/v1/dive-site", json={"name": "Refused"}, headers=_bearer(secret))
        await http.get("/api/v1/user", headers=_bearer(f"{API_KEY_PREFIX}unknown"))

        written = db.execute(select(AuthAuditEvent).where(AuthAuditEvent.user_id == diver.id)).scalars().all()
        assert written == []
