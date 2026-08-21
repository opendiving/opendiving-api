"""Unit tests for the `ADMIN_EMAIL` guard in `scripts.create_first_superuser`.

The rest of `src/scripts/` is deliberately untested - the scripts are one-shot, they are
not in the published image, and `--cov` is scoped to `src/app`. This one guard earns a
module anyway: `ADMIN_EMAIL` lost its default (`admin@admin.com`, a domain with a real
owner), and what stops `None` reaching the insert is four lines that nothing else would
notice the removal of. Sign-in is passwordless and keyed on the address, so a superuser
row created against a guessed one hands its magic link to a stranger.
"""

import logging
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.app.models.authentication_provider import AuthenticationProvider
from src.app.models.user import User
from src.scripts.create_first_superuser import (
    AUTHENTICATION_PROVIDER_TABLE,
    USER_TABLE,
    create_first_user,
)


class TestAdminEmailIsRequired:
    @pytest.mark.asyncio
    async def test_an_unset_address_creates_nothing(self, caplog):
        session = AsyncMock()

        with (
            patch("src.scripts.create_first_superuser.settings") as mock_settings,
            patch("src.scripts.create_first_superuser.async_engine") as mock_engine,
        ):
            mock_settings.ADMIN_EMAIL = None

            with caplog.at_level(logging.ERROR, logger="src.scripts.create_first_superuser"):
                await create_first_user(session)

            # Not even the lookup: `filter_by(email=None)` is a query nobody meant to run.
            session.execute.assert_not_awaited()
            mock_engine.connect.assert_not_called()

        # Naming the setting is the whole value of failing here rather than at the insert.
        assert "ADMIN_EMAIL" in caplog.text

    @pytest.mark.asyncio
    async def test_a_configured_address_is_the_one_looked_up(self):
        """The other half: a guard that rejected everything would pass the test above.

        `scalar_one_or_none` is pinned to a plain `Mock` rather than left to `AsyncMock`,
        which would make it async too - the script would then bind a coroutine to `user`,
        take the "already exists" branch for the wrong reason, and leak a never-awaited
        coroutine warning while the assertions still passed.
        """
        session = AsyncMock()
        session.execute.return_value = Mock(scalar_one_or_none=Mock(return_value=object()))

        with (
            patch("src.scripts.create_first_superuser.settings") as mock_settings,
            patch("src.scripts.create_first_superuser.async_engine") as mock_engine,
        ):
            mock_settings.ADMIN_EMAIL = "diver@opendiving.example"
            mock_settings.ADMIN_NAME = "Jacques Cousteau"
            mock_settings.ADMIN_USERNAME = "jacques"

            await create_first_user(session)

            session.execute.assert_awaited_once()
            # An account on that address already exists, so there is nothing to insert.
            mock_engine.connect.assert_not_called()

        query = session.execute.await_args.args[0]
        assert "diver@opendiving.example" in query.compile().params.values()


class TestTheHandBuiltTableMatchesTheModel:
    """The other silent failure in this script, and the reason its tables are module-level.

    `USER_TABLE` is a hand-written copy of a real table, and the INSERT it builds includes
    every column carrying a client-side `default=` whether or not `data` names it. So a
    column dropped from the model - `profile_image_url` was, when avatars arrived - turns
    the bootstrap into an `UndefinedColumn` on every fresh install, which the bare
    `except Exception` at the bottom of `create_first_user` logs and swallows. The operator
    gets a running instance they cannot sign in to and one line in the startup log.

    Nothing else covers this: `--cov` is scoped to `src/app`, the script runs from a
    one-shot compose service, and the suite never inserts through it.
    """

    def test_every_column_it_names_exists_on_the_real_table(self) -> None:
        assert set(USER_TABLE.columns.keys()) <= set(User.__table__.columns.keys())
        assert set(AUTHENTICATION_PROVIDER_TABLE.columns.keys()) <= set(AuthenticationProvider.__table__.columns.keys())

    def test_every_column_the_insert_must_supply_is_named(self) -> None:
        """The mirror direction. A new `NOT NULL` column with no server-side default is one
        the INSERT has to provide a value for, and this copy is where that value would come
        from - so a model change that adds one has to be reflected here or the bootstrap
        fails the same silent way, from the opposite cause.
        """
        required = {
            name
            for name, column in User.__table__.columns.items()
            if not column.nullable and column.server_default is None and not column.primary_key
        }

        assert required <= set(USER_TABLE.columns.keys())
