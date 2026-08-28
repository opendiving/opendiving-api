"""move uploaded file payloads onto the files volume

Uploaded dive-computer exports and c-card images stop being `bytea` columns and become
ordinary files under `FILE_STORAGE_DIR`, named by a `storage_key` the row now carries.
See *"File payloads live on the files volume, not in Postgres"* in `DECISIONS.md` for why.

The data move is hand-written; autogenerate drafted the DDL and does not see a backfill at
all. Three things shape it:

- **It must survive offline rendering.** `tests/test_migrations.py` runs
  `alembic upgrade head --sql` against no database, so the move is guarded with
  `context.is_offline_mode()` and skipped there. The rendered DDL stays complete.
- **Every filesystem touch is lazy, per row written.** At zero rows this revision must not
  create so much as a directory: CI runs `alembic upgrade head` on a bare runner, and a
  self-hoster installing fresh has nothing to move either.
- **The key layout and the atomic-write helper are inlined, not imported.** A revision is
  frozen history. If it called into `services/blob_store.py`, a later change to how keys
  are laid out would silently rewrite what this migration did in the past.
  `FILE_STORAGE_DIR` is the exception and is read from the live settings: where the volume
  is mounted is the operator's answer and must match the app's.

Retry-safe: the whole revision runs in one transaction (Postgres DDL is transactional), so
a failure mid-move rolls back every `storage_key` and both column adds, leaving only files
already written - which the retry rewrites byte-identically to the same deterministic keys,
and which the sweeper would reclaim in any case.

`downgrade` raises. Moving bytes back into `bytea` is a path nobody will ever run, and
pretending otherwise ships untested code.

Revision ID: c3c2c4dd4c27
Revises: e30bd5792bbe
Create Date: 2026-08-20 19:29:23.342815

"""

import os
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import context, op

from app.core.config import settings

# revision identifiers, used by Alembic.
revision: str = "c3c2c4dd4c27"
down_revision: str | None = "e30bd5792bbe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of what `services/blob_store` held when this revision was written.
# Deliberately not imported from it: this migration has to keep producing the keys it
# produced on the day it ran, whatever that module becomes. `FILE_STORAGE_DIR` is read from
# the live settings, because *where* is the operator's answer and has to match the app's -
# only *how the key is spelled* is frozen here.
_TMP_DIRNAME = "tmp"
_KINDS = {"dive_file": "dive-files", "certification_file": "certification-files"}


def _storage_root() -> Path:
    return Path(settings.FILE_STORAGE_DIR)


def _key_for(table: str, row_uuid: object, sha256: str) -> str:
    return f"{_KINDS[table]}/{sha256[:2]}/{row_uuid}_{sha256}"


def _write_atomically(root: Path, key: str, data: bytes) -> None:
    """Temp file on the same filesystem, fsync, rename, fsync the directory.

    The temp directory lives *inside* the storage root because `os.replace` raises `EXDEV`
    across filesystems, and in a container `/tmp` (overlayfs) and a mounted volume always
    are two filesystems.
    """
    destination = root / key
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = root / _TMP_DIRNAME
    tmp_dir.mkdir(parents=True, exist_ok=True)

    tmp_path = tmp_dir / f".migration-{os.getpid()}-{id(data):x}.part"
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, destination)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    dir_fd = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _move_payloads(table: str) -> None:
    """One table's `data` column onto the volume, one row at a time.

    Two passes - collect the ids, then read and write each row - rather than interleaving
    `UPDATE`s under an open `stream_results` cursor. The interleaving would probably work
    on the single connection `migrations/env.py` provides, and "probably" is the wrong word
    inside a migration. One row's payload is in memory at a time either way.

    `table` is interpolated into the SQL below, which is safe here and only here: it is one
    of two literals this module names, never anything a caller supplies.
    """
    connection = op.get_bind()
    ids = list(connection.scalars(sa.text(f"SELECT id FROM {table} ORDER BY id")))
    if not ids:
        return

    root = _storage_root()
    for row_id in ids:
        row = connection.execute(
            sa.text(f"SELECT uuid, sha256, data FROM {table} WHERE id = :id"),
            {"id": row_id},
        ).one()
        key = _key_for(table, row.uuid, row.sha256)
        _write_atomically(root, key, bytes(row.data))
        connection.execute(
            sa.text(f"UPDATE {table} SET storage_key = :key WHERE id = :id"),
            {"key": key, "id": row_id},
        )


def upgrade() -> None:
    # Nullable first, so existing rows survive the add; tightened to NOT NULL once every
    # one of them has a key.
    op.add_column("certification_file", sa.Column("storage_key", sa.String(length=255), nullable=True))
    op.add_column("dive_file", sa.Column("storage_key", sa.String(length=255), nullable=True))

    # Skipped when rendering SQL with no database: there is nothing to read and nowhere to
    # write it. Standard Alembic practice for a data migration, and what keeps
    # `tests/test_migrations.py`'s offline render working.
    if not context.is_offline_mode():
        _move_payloads("certification_file")
        _move_payloads("dive_file")

    op.alter_column("certification_file", "storage_key", nullable=False)
    op.alter_column("dive_file", "storage_key", nullable=False)

    op.create_index("ux_certification_file_storage_key", "certification_file", ["storage_key"], unique=True)
    op.create_index("ux_dive_file_storage_key", "dive_file", ["storage_key"], unique=True)

    # Postgres does not reclaim the pages these occupied until a `VACUUM FULL`. Irrelevant
    # on a fresh install and not worth automating on an existing one - a rewrite of both
    # tables under an exclusive lock is the operator's call, not a migration's.
    op.drop_column("certification_file", "data")
    op.drop_column("dive_file", "data")


def downgrade() -> None:
    raise NotImplementedError(
        "There is no path back from the files volume into bytea columns. Restore from a "
        "backup taken before the upgrade - see "
        "https://github.com/opendiving/opendiving/blob/main/docs/backup-restore.md"
    )
