"""The two messages a self-hoster reads at runtime, and the property they share.

Both are emitted on a machine running the published image: the files-volume check shouts
when a restore brought the database back and left the files behind, and revision
`c3c2c4dd4c27` refuses a rollback. That reader has no checkout of this repository, so a
repo-relative `docs/...` path resolves to nothing for them - both messages name an absolute
URL instead.

Every other doc path left in `src/` is a comment or a config-template line, read by someone
who does have a tree in front of them. These two are the only ones the running app says out
loud, which is why one module pins both rather than each sitting beside its own subsystem.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from alembic.script import ScriptDirectory

from src.app.core import setup
from src.app.core.db.migrations import alembic_config

#: Where `backup-restore.md` is published: the product repository, not this one.
BACKUP_RESTORE_URL = "https://github.com/opendiving/opendiving/blob/main/docs/backup-restore.md"

#: The revision that moved file payloads onto the volume, and refuses to move them back.
FILES_VOLUME_REVISION = "c3c2c4dd4c27"


def _assert_points_at_the_docs(message: str) -> None:
    """The URL is present, and no bare repo-relative path survives beside it."""
    assert BACKUP_RESTORE_URL in message
    assert "docs/" not in message.replace(BACKUP_RESTORE_URL, "")


class TestTheFilesVolumeWarning:
    @staticmethod
    def _engine_counting(rows: int) -> MagicMock:
        """An `engine` whose one query reports `rows` stored file rows."""
        conn = AsyncMock()
        conn.execute.return_value = MagicMock(scalar_one=MagicMock(return_value=rows))

        begin = MagicMock()
        begin.__aenter__ = AsyncMock(return_value=conn)
        begin.__aexit__ = AsyncMock(return_value=False)

        engine = MagicMock()
        engine.begin.return_value = begin
        return engine

    @pytest.mark.asyncio
    async def test_the_critical_sends_the_operator_to_a_url(self, caplog):
        with (
            patch.object(setup, "engine", self._engine_counting(3)),
            patch.object(setup.blob_store, "has_any_key", return_value=False),
            caplog.at_level(logging.CRITICAL, logger="src.app.core.setup"),
        ):
            await setup.warn_if_files_volume_looks_empty()

        assert len(caplog.records) == 1
        _assert_points_at_the_docs(caplog.records[0].getMessage())

    @pytest.mark.asyncio
    async def test_a_volume_with_files_says_nothing(self, caplog):
        """The other half: a check that shouted unconditionally would pass the test above."""
        with (
            patch.object(setup, "engine", self._engine_counting(3)),
            patch.object(setup.blob_store, "has_any_key", return_value=True),
            caplog.at_level(logging.CRITICAL, logger="src.app.core.setup"),
        ):
            await setup.warn_if_files_volume_looks_empty()

        assert caplog.records == []


class TestTheMigrationRefusal:
    def test_the_downgrade_sends_the_operator_to_a_url(self):
        """`downgrade` is the one place this revision talks to a person, and it talks to
        them at the worst possible moment - mid-rollback, with the payloads already moved.
        """
        revision = ScriptDirectory.from_config(alembic_config()).get_revision(FILES_VOLUME_REVISION)

        with pytest.raises(NotImplementedError) as raised:
            revision.module.downgrade()

        _assert_points_at_the_docs(str(raised.value))
