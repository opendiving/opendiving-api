"""Revision `36f9496a7b33`, which labels the phone's untagged originals for their reader.

The revision's own SQL, loaded from the module rather than retyped, run against a live Postgres:
what is worth pinning is which rows it reaches and what it writes there, on a revision that
rewrites stored rows at startup with no way back. The reads run the real `divejson-raw`, and skip
where the host has none: CI installs it and fails on any skip.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import hashlib
import importlib.util
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.core.db.migrations import MIGRATIONS_PATH
from src.app.models.dive import Dive
from src.app.models.dive_file import DiveFile
from src.app.models.dive_profile import DiveProfile
from src.app.models.dive_recording import DiveRecording
from src.app.models.user import User
from src.app.services import blob_store, libdivecomputer
from src.app.services.dive_files import extract_file, read_unread_file_fixes
from src.app.services.dive_profiles import IMPORT_PARSER_KEY, MERGE_PARSER_KEY
from src.app.services.libdivecomputer import LIBDIVECOMPUTER, Descriptor, descriptor_of_columns
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_user

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

needs_the_reader = pytest.mark.skipif(
    shutil.which(libdivecomputer.CLI) is None or sys.platform != "linux",
    reason="no divejson-raw on PATH, or no Linux address-space ceiling to run it under",
)

_REVISION = "36f9496a7b33_a_phone_original_stored_unlabelled_is_"
FIXTURES = Path(__file__).parent / "fixtures" / "dive_files"
PERDIX_NAME = "shearwater-perdix-3_D9772626_2026-10-26T1503.bin"
OCEAN_NAME = "suunto-ocean_253810000400_2026-10-26T1501.bin"
PERDIX = (FIXTURES / PERDIX_NAME).read_bytes()
OCEAN = (FIXTURES / OCEAN_NAME).read_bytes()

# What the phone tags each with, as the iOS app's `perdix-3-download.divejson` and
# `ocean-download.divejson` fixtures record it.
PERDIX_TAG = Descriptor(vendor="Shearwater", product="Perdix 3", model=14)
OCEAN_TAG = Descriptor(vendor="Suunto", product="Ocean", model=1)

# The Ocean's dive, at Dahab on UTC+3: 15:01 on the wall clock its file's name spells.
OCEAN_START = datetime(2026, 10, 26, 12, 1, 31, tzinfo=UTC)
PERDIX_START = datetime(2026, 10, 26, 12, 3, 23, tzinfo=UTC)
DAHAB = 180


def _label() -> str:
    """The revision's statement, loaded by path: `migrations/versions/` is not a package."""
    spec = importlib.util.spec_from_file_location(_REVISION, MIGRATIONS_PATH / "versions" / f"{_REVISION}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(str, module.LABEL)


def _apply(db: Session) -> int:
    count = db.execute(text(_label())).rowcount  # type: ignore[attr-defined]
    db.commit()
    return cast(int, count)


@dataclass(frozen=True)
class Stored:
    dive: Dive
    recording: DiveRecording
    file: DiveFile


def _store(
    db: Session,
    user: User,
    *,
    model: str | None = "Suunto Ocean",
    brand: str | None = "Suunto",
    name: str = OCEAN_NAME,
    data: bytes = OCEAN,
    content_type: str = "application/octet-stream",
    parser_key: str = IMPORT_PARSER_KEY,
    start: datetime | None = OCEAN_START,
    offset: int | None = DAHAB,
    profile: str | None = None,
    fixes_read: bool = True,
    position: tuple[float, float] | None = None,
    storage_key: str | None = None,
) -> Stored:
    """A dive holding one recording and one file of it, as the phone's early upload stored it."""
    dive = create_dive(db, user)
    if position is not None:
        dive.entry_latitude, dive.entry_longitude = position
    recording = DiveRecording(
        dive_id=dive.id,
        user_id=user.id,
        ordinal=0,
        device_brand=brand,
        device_model=model,
        device_serial="253810000400",
        start_time=start,
        utc_offset_minutes=offset,
    )
    db.add(recording)
    db.flush()
    digest = hashlib.sha256(data).hexdigest()
    file = DiveFile(
        user_id=user.id,
        recording_id=recording.id,
        dive_id=dive.id,
        sha256=digest,
        content_type=content_type,
        byte_size=len(data),
        stored_byte_size=len(data),
        original_filename=name,
        parser_key=parser_key,
        storage_key=storage_key or blob_store.new_key("dive-files", sha256=digest),
        fixes_read=fixes_read,
    )
    db.add(file)
    if profile is not None:
        db.add(
            DiveProfile(
                recording_id=recording.id,
                dive_id=dive.id,
                source_sha256=digest,
                parser_key=profile,
                extractor_version=3,
                duration=2400,
                depth_sample_count=2,
                data={"depth": {"t": [0, 10], "v": [100, 500]}},
            )
        )
    db.commit()
    return Stored(dive=dive, recording=recording, file=file)


def _row(db: Session, file: DiveFile) -> dict[str, Any]:
    db.expire_all()
    row = db.get(DiveFile, file.id)
    assert row is not None
    return {column.key: getattr(row, column.key) for column in DiveFile.__table__.columns}


def _tag(row: dict[str, Any]) -> Descriptor | None:
    return descriptor_of_columns(row["descriptor_vendor"], row["descriptor_product"], row["descriptor_model"])


@pytest.fixture
def owner(db: Session) -> User:
    return create_user(db)


class TestWhatIsLabelled:
    def test_a_perdix_3_original_takes_the_phone_s_tag_and_no_fallback_start(self, db: Session, owner: User) -> None:
        stored = _store(
            db,
            owner,
            model="Perdix 3",
            brand="Shearwater Research, Inc",
            name=PERDIX_NAME,
            data=PERDIX,
            start=PERDIX_START,
        )

        _apply(db)

        row = _row(db, stored.file)
        assert row["parser_key"] == LIBDIVECOMPUTER
        assert _tag(row) == PERDIX_TAG
        assert row["fallback_start"] is None

    def test_an_ocean_original_takes_the_phone_s_tag_and_its_recording_s_start(self, db: Session, owner: User) -> None:
        stored = _store(db, owner)

        _apply(db)

        row = _row(db, stored.file)
        assert row["parser_key"] == LIBDIVECOMPUTER
        assert _tag(row) == OCEAN_TAG
        assert row["fallback_start"] == OCEAN_START

    @pytest.mark.parametrize("brand", [None, "", "suunto", "Suunto", "Shearwater Research, Inc"])
    @pytest.mark.parametrize("model", ["Suunto Ocean", " suunto ocean\t", "SUUNTO OCEAN"])
    def test_the_model_is_folded_as_the_gates_fold_it_and_the_brand_is_not_read(
        self, db: Session, owner: User, model: str, brand: str | None
    ) -> None:
        stored = _store(db, owner, model=model, brand=brand)

        _apply(db)

        assert _row(db, stored.file)["descriptor_product"] == "Ocean"

    def test_a_wall_clock_start_is_compared_as_the_name_spells_it(self, db: Session, owner: User) -> None:
        """A recording with no offset stores its wall clock as the instant, which the name spells."""
        start = datetime(2026, 10, 26, 15, 1, 31, tzinfo=UTC)
        stored = _store(db, owner, start=start, offset=None)

        _apply(db)

        assert _row(db, stored.file)["fallback_start"] == start

    def test_the_bytes_digest_storage_recording_and_dive_are_untouched(self, db: Session, owner: User) -> None:
        stored = _store(db, owner)
        before = _row(db, stored.file)

        _apply(db)

        after = _row(db, stored.file)
        labelled = {"parser_key", "descriptor_vendor", "descriptor_product", "descriptor_model", "fallback_start"}
        assert {key: value for key, value in after.items() if key not in labelled | {"fixes_read"}} == {
            key: value for key, value in before.items() if key not in labelled | {"fixes_read"}
        }


class TestTheFallbackStart:
    def test_none_on_a_merge_s_recording(self, db: Session, owner: User) -> None:
        """Its start is the merged dive's, so it is no record's own."""
        stored = _store(db, owner, profile=MERGE_PARSER_KEY)

        _apply(db)

        row = _row(db, stored.file)
        assert (row["parser_key"], row["descriptor_product"], row["fallback_start"]) == (LIBDIVECOMPUTER, "Ocean", None)

    def test_none_where_the_file_s_name_spells_another_minute(self, db: Session, owner: User) -> None:
        """A merge whose `merge` profile a re-derivation replaced holds the later record's file
        under the earlier record's start."""
        stored = _store(db, owner, name="suunto-ocean_253810000400_2026-10-26T1544.bin", profile="suunto_json")

        _apply(db)

        row = _row(db, stored.file)
        assert (row["parser_key"], row["fallback_start"]) == (LIBDIVECOMPUTER, None)

    def test_none_where_the_recording_has_no_start(self, db: Session, owner: User) -> None:
        stored = _store(db, owner, start=None)

        _apply(db)

        row = _row(db, stored.file)
        assert (row["parser_key"], row["fallback_start"]) == (LIBDIVECOMPUTER, None)


class TestWhatIsLeftAlone:
    @pytest.mark.parametrize(
        "values",
        [
            {"model": "Suunto D5"},
            {"model": "Perdix 2"},
            {"model": "Ocean"},
            {"model": None},
            {"name": "suunto-ocean_253810000400_2026-10-26T1501.json"},
            {"name": "suunto-ocean_253810000400_2026-10-26T1501.BIN"},
            {"content_type": "application/json"},
            {"parser_key": "suunto_json"},
            {"parser_key": "fit"},
        ],
        ids=lambda values: "-".join(f"{key}={value}" for key, value in values.items()),
    )
    def test_any_other_file_stays_as_it_is(self, db: Session, owner: User, values: dict[str, Any]) -> None:
        stored = _store(db, owner, **values)
        before = _row(db, stored.file)

        _apply(db)

        assert _row(db, stored.file) == before

    def test_an_original_the_phone_tagged_keeps_its_tag(self, db: Session, owner: User) -> None:
        stored = _store(db, owner, parser_key=LIBDIVECOMPUTER)
        db.execute(
            update(DiveFile)
            .where(DiveFile.id == stored.file.id)
            .values(
                descriptor_vendor="Suunto",
                descriptor_product="Ocean",
                descriptor_model=1,
                fallback_start=OCEAN_START + timedelta(seconds=1),
            )
        )
        db.commit()
        before = _row(db, stored.file)

        _apply(db)

        assert _row(db, stored.file) == before

    def test_a_second_run_changes_nothing(self, db: Session, owner: User) -> None:
        stored = [
            _store(db, owner),
            _store(db, owner, model="Perdix 3", name=PERDIX_NAME, data=PERDIX, start=PERDIX_START),
        ]
        _apply(db)
        before = [_row(db, each.file) for each in stored]

        assert _apply(db) == 0
        assert [_row(db, each.file) for each in stored] == before


class TestTheFixes:
    def test_a_dive_with_a_position_keeps_it_and_its_file_stays_read(self, db: Session, owner: User) -> None:
        stored = _store(db, owner, position=(27.0, 33.0))

        _apply(db)

        row = _row(db, stored.file)
        assert (row["fixes_read"], row["entry_latitude"]) == (True, None)

    def test_a_file_never_read_on_a_dive_with_a_position_is_marked_read(self, db: Session, owner: User) -> None:
        """Read now, a file first by id would replace the position the dive shows."""
        stored = _store(db, owner, position=(27.0, 33.0), fixes_read=False)

        _apply(db)

        assert _row(db, stored.file)["fixes_read"] is True

    def test_a_dive_without_a_position_has_its_file_read(self, db: Session, owner: User) -> None:
        stored = _store(db, owner)

        _apply(db)

        assert _row(db, stored.file)["fixes_read"] is False

    @pytest.mark.asyncio
    async def test_the_worker_moves_no_position_a_dive_has(
        self, db: Session, async_db: AsyncSession, owner: User, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("src.app.services.cache_invalidation.invalidate_dive_caches", AsyncMock())
        monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
        stored = _store(db, owner, position=(27.0, 33.0))
        await blob_store.put(stored.file.storage_key, OCEAN)
        _apply(db)

        while await read_unread_file_fixes(async_db, limit=50) == 50:
            pass

        dive = (
            await async_db.execute(select(Dive.entry_latitude, Dive.entry_longitude).where(Dive.id == stored.dive.id))
        ).one()
        assert tuple(dive) == (27.0, 33.0)

    @needs_the_reader
    @pytest.mark.asyncio
    async def test_the_worker_fills_a_dive_without_one_from_its_labelled_file(
        self, db: Session, async_db: AsyncSession, owner: User, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("src.app.services.cache_invalidation.invalidate_dive_caches", AsyncMock())
        monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
        stored = _store(db, owner)
        await blob_store.put(stored.file.storage_key, OCEAN)
        _apply(db)
        expected = extract_file(OCEAN, LIBDIVECOMPUTER, descriptor=OCEAN_TAG, fallback_start=None).scalars

        while await read_unread_file_fixes(async_db, limit=50) == 50:
            pass

        dive = (
            await async_db.execute(select(Dive.entry_latitude, Dive.entry_longitude).where(Dive.id == stored.dive.id))
        ).one()
        assert expected is not None and expected["entry_latitude"] is not None
        assert tuple(dive) == (expected["entry_latitude"], expected["entry_longitude"])


@needs_the_reader
class TestALabelledFileReads:
    """Through the reader every labelled original goes through, to the dive a labelled upload of
    the same bytes gives."""

    @pytest.mark.parametrize(
        ("model", "name", "data", "start", "tag", "phone_fallback"),
        [
            ("Perdix 3", PERDIX_NAME, PERDIX, PERDIX_START, PERDIX_TAG, None),
            ("Suunto Ocean", OCEAN_NAME, OCEAN, OCEAN_START, OCEAN_TAG, OCEAN_START + timedelta(seconds=1)),
        ],
        ids=["perdix-3", "ocean"],
    )
    def test_as_the_phone_s_upload_reads(
        self,
        db: Session,
        owner: User,
        model: str,
        name: str,
        data: bytes,
        start: datetime,
        tag: Descriptor,
        phone_fallback: datetime | None,
    ) -> None:
        stored = _store(db, owner, model=model, name=name, data=data, start=start)

        _apply(db)

        row = _row(db, stored.file)
        read = extract_file(
            data,
            row["parser_key"],
            descriptor=_tag(row),
            fallback_start=row["fallback_start"],
        )
        assert read.profile is not None
        assert read == extract_file(data, LIBDIVECOMPUTER, descriptor=tag, fallback_start=phone_fallback)
