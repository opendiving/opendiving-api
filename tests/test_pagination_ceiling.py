"""The ceiling on `page` (`core/utils/pagination.py`).

`OFFSET` is `(page - 1) * items_per_page`, and Postgres holds it in a `bigint`: past that, an
unclamped `page` is a 500 rather than an empty page. The route test runs against Postgres and
skips silently without one - see CONTRIBUTING.md.
"""

from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.setup import create_application
from src.app.core.utils import cache as cache_module
from src.app.core.utils.pagination import DEFAULT_MAX_ITEMS_PER_PAGE, MAX_PAGE, clamp_pagination
from src.app.crud.crud_users import read_account
from src.app.models.user import User
from tests.conftest import db_available
from tests.helpers.fake_redis import FakeRedis

BEYOND_BIGINT = 10**20


class TestTheCeiling:
    @pytest.mark.parametrize("requested", [MAX_PAGE + 1, BEYOND_BIGINT])
    def test_a_page_past_the_ceiling_is_read_as_the_ceiling(self, requested: int) -> None:
        assert clamp_pagination(requested, 25) == (MAX_PAGE, 25)

    def test_the_ceiling_is_a_page_postgres_can_offset_to(self) -> None:
        assert (MAX_PAGE - 1) * DEFAULT_MAX_ITEMS_PER_PAGE < 2**63

    def test_the_published_description_states_it(self) -> None:
        from src.app.main import app

        parameters = app.openapi()["paths"]["/api/v1/dive-drafts"]["get"]["parameters"]
        page = next(parameter for parameter in parameters if parameter["name"] == "page")

        assert str(MAX_PAGE) in page["description"]


@pytest.mark.skipif(not db_available(), reason="needs Postgres")
class TestAgainstPostgres:
    @pytest_asyncio.fixture
    async def http(
        self, async_sessions: async_sessionmaker[AsyncSession], diver: User, monkeypatch: pytest.MonkeyPatch
    ) -> AsyncGenerator[httpx.AsyncClient]:
        monkeypatch.setattr(cache_module, "client", FakeRedis())
        app: Any = create_application(router=router, settings=settings, apply_migrations_on_start=False)

        async def per_request() -> AsyncGenerator[AsyncSession]:
            async with async_sessions() as session:
                yield session

        async with async_sessions() as db:
            account = await read_account(db, uuid=diver.uuid)
        app.dependency_overrides[async_get_db] = per_request
        app.dependency_overrides[get_current_user] = lambda: account
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1") as client:
            yield client

    @pytest.mark.asyncio
    async def test_a_page_beyond_bigint_is_an_empty_page(self, http: httpx.AsyncClient) -> None:
        response = await http.get("/dive-drafts", params={"page": BEYOND_BIGINT, "items_per_page": 100})

        assert response.status_code == 200
        assert (response.json()["data"], response.json()["page"]) == ([], MAX_PAGE)
