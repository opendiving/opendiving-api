"""Tests for `services/blob_keys.py`, the registry of columns holding blob-store keys.

The sweeper deletes whatever the registry does not name, so the first class is the one that
matters: it fails the moment a model gains a key column nobody added here. The rest pin
what the three readers - the sweeper, the account purge and the startup check - take from
it.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app import models
from src.app.core import setup
from src.app.core.db.database import Base
from src.app.core.worker.functions import _collect_stored_file_keys
from src.app.services import blob_store
from src.app.services.blob_keys import BLOB_KEY_COLUMNS
from src.scripts import sweep_orphaned_files as sweeper
from tests.conftest import db_available
from tests.helpers.generators import (
    create_certification,
    create_dive,
    create_dive_recording,
    create_map_tile,
    create_species,
    create_user,
    create_user_picture,
)

# Columns whose name says "key" and which hold something other than a blob-store key. A new
# entry here is a claim that the column never names a stored file - make it deliberately.
NOT_BLOB_KEYS = {
    "daily_total.key",  # a metric's breakdown dimension
    "dive_file.parser_key",  # which reader parsed the export
    "dive_profile.parser_key",
    "idempotency_hold.key",  # a client's Idempotency-Key
    "webauthn_credential.public_key",  # a passkey's public key, stored inline
}


def _name(table: str, column: str) -> str:
    return f"{table}.{column}"


REGISTERED = {_name(entry.column.table.name, entry.column.name) for entry in BLOB_KEY_COLUMNS}


class TestTheRegistryCoversEveryKeyColumn:
    def test_every_key_named_column_is_registered_or_exempt(self) -> None:
        """A blob column the registry misses is swept as an orphan by `--delete`. Every
        column named like a key has to be accounted for one way or the other, and an
        exemption for a column that no longer exists fails too, so the list cannot rot."""
        assert models  # every model has to be imported for the metadata to be complete
        key_columns = {
            _name(table.name, column.name)
            for table in Base.metadata.tables.values()
            for column in table.columns
            if "key" in column.name.lower()
        }

        assert key_columns - NOT_BLOB_KEYS == REGISTERED
        assert NOT_BLOB_KEYS <= key_columns

    def test_every_registered_column_is_a_string(self) -> None:
        for entry in BLOB_KEY_COLUMNS:
            assert isinstance(entry.column.type, String), entry.column

    def test_only_the_shared_kinds_have_no_owner(self) -> None:
        """Species photos and map tiles belong to no account; everything else is one
        diver's, and a purge that skipped it would leave their files behind."""
        unowned = {_name(e.column.table.name, e.column.name) for e in BLOB_KEY_COLUMNS if e.owner is None}

        assert unowned == {"species.photo_storage_key", "map_tile.storage_key"}


class TestTheVolumeCheckCountsEveryKind:
    @pytest.mark.asyncio
    async def test_the_count_reads_every_registered_column(self, caplog: pytest.LogCaptureFixture) -> None:
        """An instance holding only pictures, species photos or map tiles has to be able to
        trip the warning too, so the count covers every registered column."""
        conn = AsyncMock()
        conn.execute.return_value = MagicMock(scalar_one=MagicMock(return_value=1))
        begin = MagicMock()
        begin.__aenter__ = AsyncMock(return_value=conn)
        begin.__aexit__ = AsyncMock(return_value=False)
        engine = MagicMock()
        engine.begin.return_value = begin

        with (
            patch.object(setup, "engine", engine),
            patch.object(setup.blob_store, "has_any_key", return_value=False),
            caplog.at_level(logging.CRITICAL, logger="src.app.core.setup"),
        ):
            await setup.warn_if_files_volume_looks_empty()

        sql = str(conn.execute.await_args.args[0].compile())
        for name in REGISTERED:
            assert f"count({name})" in sql, name
        assert len(caplog.records) == 1


def _dive_file(db: Session, diver: models.User) -> str:
    dive = create_dive(db, diver)
    key = blob_store.new_key("dive-files", sha256="c" * 64)
    db.add(
        models.DiveFile(
            user_id=diver.id,
            recording_id=create_dive_recording(db, diver, dive).id,
            dive_id=dive.id,
            sha256="c" * 64,
            content_type="application/octet-stream",
            byte_size=1,
            stored_byte_size=1,
            original_filename="dive.uddf",
            parser_key="uddf",
            storage_key=key,
        )
    )
    db.commit()
    return key


def _certification_file(db: Session, diver: models.User) -> str:
    certification = create_certification(db, diver)
    key = blob_store.new_key("certification-files", sha256="d" * 64)
    db.add(
        models.CertificationFile(
            certification_id=certification.id,
            side="front",
            content_type="image/jpeg",
            byte_size=1,
            original_filename="card.jpg",
            sha256="d" * 64,
            storage_key=key,
        )
    )
    db.commit()
    return key


def _diver_with_every_kind(db: Session) -> tuple[models.User, set[str]]:
    """A diver holding one of each per-account kind, and the keys they own."""
    diver = create_user(db)
    avatar = create_user_picture(db, diver, kind="avatar")
    portrait = create_user_picture(db, diver, kind="portrait", with_original=False)
    keys = {
        _dive_file(db, diver),
        _certification_file(db, diver),
        avatar.rendition_storage_key,
        avatar.original_storage_key,
        portrait.rendition_storage_key,
    }
    assert None not in keys
    return diver, keys  # type: ignore[return-value]


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheReadersCollectTheSameKeys:
    @pytest.mark.asyncio
    async def test_the_sweeper_references_every_kind(self, db: Session, async_db: AsyncSession) -> None:
        _, diver_keys = _diver_with_every_kind(db)
        _, other_keys = _diver_with_every_kind(db)
        species_key = f"species-photos/dd/{uuid7()}_{'d' * 64}"
        create_species(db, photo_storage_key=species_key, photo_sha256="d" * 64)
        create_species(db)
        tile = create_map_tile(db)

        referenced = await sweeper._referenced_keys(async_db)

        assert diver_keys | other_keys | {species_key, tile.storage_key} <= referenced
        assert None not in referenced

    @pytest.mark.asyncio
    async def test_the_purge_collects_exactly_the_divers_own(self, db: Session, async_db: AsyncSession) -> None:
        """Every per-account kind, including the certification file reached through its
        certification, and nothing of another diver's or of the shared kinds."""
        diver, diver_keys = _diver_with_every_kind(db)
        _diver_with_every_kind(db)
        create_species(db, photo_storage_key=f"species-photos/ee/{uuid7()}_{'e' * 64}", photo_sha256="e" * 64)
        create_map_tile(db)

        collected = await _collect_stored_file_keys(async_db, diver.id)

        assert sorted(collected) == sorted(diver_keys)
