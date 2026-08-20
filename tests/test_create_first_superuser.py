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

from src.scripts.create_first_superuser import create_first_user


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
