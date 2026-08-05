"""Unit tests for the Arq worker background tasks."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app.core.worker.functions import purge_expired_tokens


class _FakeSessionContext:
    """Minimal async context manager mimicking `local_session()`."""

    def __init__(self, db: AsyncMock) -> None:
        self._db = db

    async def __aenter__(self) -> AsyncMock:
        return self._db

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class TestPurgeExpiredTokens:
    """Test the periodic token_blacklist cleanup job."""

    @pytest.mark.asyncio
    async def test_purge_deletes_expired_rows(self):
        mock_db = AsyncMock()

        with (
            patch("src.app.core.worker.functions.local_session", return_value=_FakeSessionContext(mock_db)),
            patch("src.app.core.worker.functions.crud_token_blacklist") as mock_blacklist,
        ):
            mock_blacklist.count = AsyncMock(return_value=3)
            mock_blacklist.delete = AsyncMock(return_value=None)

            result = await purge_expired_tokens(MagicMock())

            mock_blacklist.count.assert_called_once()
            count_args, count_kwargs = mock_blacklist.count.call_args
            assert count_args[0] is mock_db
            assert isinstance(count_kwargs["expires_at__lt"], datetime)

            mock_blacklist.delete.assert_called_once()
            delete_args, delete_kwargs = mock_blacklist.delete.call_args
            assert delete_args[0] is mock_db
            assert delete_kwargs["allow_multiple"] is True
            assert isinstance(delete_kwargs["expires_at__lt"], datetime)

            assert "3" in result

    @pytest.mark.asyncio
    async def test_purge_skips_delete_when_nothing_expired(self):
        mock_db = AsyncMock()

        with (
            patch("src.app.core.worker.functions.local_session", return_value=_FakeSessionContext(mock_db)),
            patch("src.app.core.worker.functions.crud_token_blacklist") as mock_blacklist,
        ):
            mock_blacklist.count = AsyncMock(return_value=0)
            mock_blacklist.delete = AsyncMock(return_value=None)

            result = await purge_expired_tokens(MagicMock())

            mock_blacklist.delete.assert_not_called()
            assert "No expired" in result
