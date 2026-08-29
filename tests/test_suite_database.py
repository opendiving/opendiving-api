"""The suite writes to a database of its own, never the one the dev stack serves from.

`tests/conftest.py` sets `POSTGRES_DB` before the first `src.app` import, which is the only
moment at which it can be set at all - `settings.POSTGRES_URI` is assembled when
`core.config` is imported, and `core/db/database.py` builds the app's engine from it. Delete
those lines and everything here still runs, quietly, against the developer's `opendive`: the
fixtures write real rows into it and `alembic upgrade head` builds tables the `api`
container's own startup migration then dies on. That failure surfaces at the next
`docker compose restart api`, hours later and with nothing connecting it back to a test run,
which is why the redirection gets a test rather than a comment.

Nothing here needs a database - it asserts what the settings resolved to, not what answered.
See *"The suite has its own database, and builds it with the migrations"* in DECISIONS.md.
"""

from src.app.core.config import settings
from src.app.core.db.database import DATABASE_URL
from tests.conftest import CONFIGURED_DATABASE, TEST_DATABASE


class TestTheSuiteHasItsOwnDatabase:
    def test_it_is_not_the_configured_one(self):
        """The whole invariant, in the form the dev stack cares about."""
        assert TEST_DATABASE != CONFIGURED_DATABASE

    def test_it_is_the_configured_name_with_a_suffix(self):
        """Derived rather than a literal, so the two cannot coincide whatever the operator
        called theirs - `opendive_test` against the stock `src/.env`, `postgres_test` in CI.
        """
        assert TEST_DATABASE == f"{CONFIGURED_DATABASE}_test"

    def test_the_settings_the_app_was_built_from_name_it(self):
        """`settings` is what every fixture and every route reaches the database through, so
        this is the assertion that the redirection happened early enough to matter.
        """
        assert settings.POSTGRES_DB == TEST_DATABASE

    def test_the_apps_own_engine_points_at_it(self):
        """`core/db/database.py` builds `async_engine` from `POSTGRES_URI` at import - the
        engine the lifespan migrates through and every endpoint test writes through.
        """
        assert DATABASE_URL.endswith(f"/{TEST_DATABASE}")
