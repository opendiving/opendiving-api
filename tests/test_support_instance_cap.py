"""The support form's instance-wide daily cap (see `api.v1.support`).

Against `FakeRedis` rather than a mocked `enforce_rate_limit`, because what is under test is
the counting: the per-IP and per-address limits both pass for a caller who rotates both, and
only the instance's own bucket stops them.
"""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app.api.v1.support import SUPPORT_REQUESTS_PER_DAY, send_support_request
from src.app.api.v1.support import router as support_router
from src.app.core.config import settings
from tests.helpers.fake_redis import FakeRedis

BODY = {
    "name": "Jacques Cousteau",
    "category": "import",
    "subject": "Suunto export won't import",
    "message": "The JSON my Ocean exports is rejected with a parse error.",
}


@pytest.fixture
def redis() -> Generator[FakeRedis]:
    fake = FakeRedis()
    with (
        patch("src.app.core.utils.cache.client", fake),
        patch.object(settings, "CONTACT_FORM_EMAIL", "support@opendiving.example"),
    ):
        yield fake


def _submit(n: int, *, email: str | None = None) -> int:
    """One submission from its own IP and, unless told otherwise, its own address."""
    app = FastAPI()
    app.include_router(support_router)
    client = TestClient(app, client=(f"203.0.113.{n}", 50000))
    return int(client.post("/support", json={**BODY, "email": email or f"diver{n}@example.com"}).status_code)


class TestTheInstanceWideCap:
    def test_the_submission_past_the_days_allowance_is_refused(self, redis: FakeRedis):
        with patch("src.app.api.v1.support.send_support_request_email", new_callable=AsyncMock) as send:
            statuses = [_submit(n) for n in range(SUPPORT_REQUESTS_PER_DAY + 1)]

        assert statuses[:-1] == [200] * SUPPORT_REQUESTS_PER_DAY
        assert statuses[-1] == 429
        assert send.await_count == SUPPORT_REQUESTS_PER_DAY
        assert redis.expiries["support:instance"] == 86_400

    def test_a_request_the_narrower_limits_refuse_does_not_spend_it(self, redis: FakeRedis):
        """Counted last: an address over its own limit must not burn the instance's day."""
        allowed = settings.CONTACT_FORM_RATE_LIMIT_PER_EMAIL
        with patch("src.app.api.v1.support.send_support_request_email", new_callable=AsyncMock):
            statuses = [_submit(n, email="same@example.com") for n in range(allowed + 2)]

        assert statuses.count(200) == allowed
        assert int(redis.store["support:instance"]) == allowed

    def test_the_published_description_states_the_cap(self):
        """The docstring is the operation description `/developers` renders, and it cannot
        interpolate the constant."""
        assert f"{SUPPORT_REQUESTS_PER_DAY} submissions a day" in " ".join((send_support_request.__doc__ or "").split())
