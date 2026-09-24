"""Tests for `src/scripts/sweep_orphaned_files.py`.

The interesting behaviour is all refusal. A sweeper that deletes what it should is a loop
over `unlink`; a sweeper that deletes what it shouldn't is unrecoverable, and the way that
happens in practice is not a bug in the diff - it is being pointed at the wrong database,
where *everything* scans as orphaned. That is the Gitea `doctor --fix` failure this is
shaped against, and the grace window does nothing about it, because the files are old and
the database is simply wrong.
"""

import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.services import blob_store
from src.scripts import sweep_orphaned_files as sweeper
from tests.conftest import db_available
from tests.helpers.fake_s3 import FakeS3Client, select_s3_backend
from tests.helpers.generators import create_species, create_user, create_user_picture

REFERENCED = "dive-files/aa/referenced"
ORPHAN = "dive-files/bb/orphan"


@pytest.fixture
def volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


def _write(volume: Path, key: str, *, age_hours: float = 0.0) -> Path:
    path = volume / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"payload")
    if age_hours:
        stamp = time.time() - age_hours * 3600
        import os

        os.utime(path, (stamp, stamp))
    return path


def _with_referenced(keys: set[str]):
    """Patch out the database read, so these tests need no Postgres to say what is
    referenced - which is the whole input the diff runs on."""
    return patch.object(sweeper, "_referenced_keys", AsyncMock(return_value=keys))


class TestReporting:
    @pytest.mark.asyncio
    async def test_a_dry_run_finds_the_orphan_and_deletes_nothing(self, volume: Path) -> None:
        _write(volume, REFERENCED, age_hours=48)
        _write(volume, ORPHAN, age_hours=48)

        with _with_referenced({REFERENCED}):
            report = await sweeper.sweep()

        assert (report.on_disk, report.orphaned, report.deleted) == (2, 1, 0)
        assert (volume / ORPHAN).is_file()

    @pytest.mark.asyncio
    async def test_delete_removes_only_the_unreferenced_one(self, volume: Path) -> None:
        _write(volume, REFERENCED, age_hours=48)
        _write(volume, ORPHAN, age_hours=48)

        with _with_referenced({REFERENCED}):
            report = await sweeper.sweep(delete=True)

        assert report.deleted == 1
        assert (volume / REFERENCED).is_file()
        assert not (volume / ORPHAN).exists()

    @pytest.mark.asyncio
    async def test_a_file_younger_than_the_grace_window_is_left_alone(self, volume: Path) -> None:
        """This is what makes an *online* sweep safe: a file is written before the row that
        references it commits, so a fresh unreferenced file may simply be an upload in
        flight."""
        _write(volume, REFERENCED, age_hours=48)
        _write(volume, ORPHAN)

        with _with_referenced({REFERENCED}):
            report = await sweeper.sweep(delete=True)

        assert (report.orphaned, report.within_grace, report.deleted) == (0, 1, 0)
        assert (volume / ORPHAN).is_file()


class TestTheWrongDatabaseGuard:
    @pytest.mark.asyncio
    async def test_it_refuses_when_the_database_references_nothing_at_all(self, volume: Path) -> None:
        """Mid-restore, or `POSTGRES_*` pointed at the wrong host. Every file scans as
        orphaned and every one of them is real."""
        _write(volume, REFERENCED, age_hours=48)
        _write(volume, ORPHAN, age_hours=48)

        with _with_referenced(set()):
            report = await sweeper.sweep(delete=True)

        assert report.deleted == 0
        assert report.refused is not None
        assert (volume / REFERENCED).is_file()

    @pytest.mark.asyncio
    async def test_it_refuses_when_too_much_of_the_volume_is_unreferenced(self, volume: Path) -> None:
        for index in range(30):
            _write(volume, f"dive-files/cc/orphan-{index}", age_hours=48)
        referenced = {f"dive-files/dd/kept-{index}" for index in range(5)}
        for key in referenced:
            _write(volume, key, age_hours=48)

        with _with_referenced(referenced):
            report = await sweeper.sweep(delete=True)

        assert report.deleted == 0
        assert report.refused is not None

    @pytest.mark.asyncio
    async def test_a_small_tree_is_not_judged_by_its_fraction(self, volume: Path) -> None:
        """One orphan out of four is 25% and says nothing. A guard that fired here would
        teach whoever runs this to reach for `--force`, which is how a guard stops working
        on the day it matters."""
        for index in range(3):
            _write(volume, f"dive-files/dd/kept-{index}", age_hours=48)
        _write(volume, ORPHAN, age_hours=48)
        referenced = {f"dive-files/dd/kept-{index}" for index in range(3)}

        with _with_referenced(referenced):
            report = await sweeper.sweep(delete=True)

        assert report.refused is None
        assert report.deleted == 1

    @pytest.mark.asyncio
    async def test_force_overrides_the_refusal(self, volume: Path) -> None:
        _write(volume, ORPHAN, age_hours=48)

        with _with_referenced(set()):
            report = await sweeper.sweep(delete=True, force=True)

        assert report.deleted == 1
        assert report.refused is None

    @pytest.mark.asyncio
    async def test_an_empty_database_and_an_empty_volume_is_not_a_refusal(self, volume: Path) -> None:
        """A fresh install has no rows and no files, and reporting that as suspicious would
        train whoever runs this to reach for `--force`."""
        with _with_referenced(set()):
            report = await sweeper.sweep(delete=True)

        assert report.refused is None
        assert (report.on_disk, report.orphaned) == (0, 0)

    @pytest.mark.asyncio
    async def test_a_dry_run_never_reports_a_refusal(self, volume: Path) -> None:
        """There is nothing to refuse - the run was only ever going to print."""
        _write(volume, ORPHAN, age_hours=48)

        with _with_referenced(set()):
            report = await sweeper.sweep()

        assert report.refused is None


class TestTempDirectory:
    @pytest.mark.asyncio
    async def test_a_stale_part_file_is_counted_and_removed(self, volume: Path) -> None:
        """A `.part` file is live only between the write and the rename, so one a day old
        is the remains of a process that died in between."""
        _write(volume, REFERENCED, age_hours=48)
        stale = _write(volume, f"{blob_store.TMP_DIRNAME}/.dead.part", age_hours=48)

        with _with_referenced({REFERENCED}):
            report = await sweeper.sweep(delete=True)

        assert report.stale_temp_files == 1
        assert not stale.exists()

    @pytest.mark.asyncio
    async def test_a_fresh_part_file_is_left_alone(self, volume: Path) -> None:
        _write(volume, REFERENCED, age_hours=48)
        fresh = _write(volume, f"{blob_store.TMP_DIRNAME}/.inflight.part")

        with _with_referenced({REFERENCED}):
            report = await sweeper.sweep(delete=True)

        assert report.stale_temp_files == 0
        assert fresh.is_file()

    @pytest.mark.asyncio
    async def test_temp_files_are_never_counted_as_orphans(self, volume: Path) -> None:
        _write(volume, REFERENCED, age_hours=48)
        _write(volume, f"{blob_store.TMP_DIRNAME}/.dead.part", age_hours=48)

        with _with_referenced({REFERENCED}):
            report = await sweeper.sweep()

        assert (report.on_disk, report.orphaned) == (1, 0)


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestReferencedKeys:
    """The query the whole diff runs on, against a real database.

    Every test above stubs `_referenced_keys` out, which is right for testing the refusal
    logic and wrong for the one thing that makes this script dangerous: a key source the
    query forgets is a live file the sweep offers to delete. The nullable-column sources are
    covered here, the pictures and species photos, because they are also the only ones that
    can quietly contribute a `None` to the set instead of a key.

    Species photos are the case worth having a test for rather than a note: they are the only
    kind on a **global** table, so forgetting them would offer to delete a photo shared by
    every account on the instance, and the suspicious-fraction brake does not engage at all
    below `_SUSPICIOUS_MIN_FILES` files - which is exactly the instance least likely to
    notice.
    """

    @pytest.mark.asyncio
    async def test_every_picture_key_counts_as_referenced(self, db: Session, async_db: AsyncSession) -> None:
        """Both files of both pictures."""
        diver = create_user(db)
        avatar = create_user_picture(db, diver, kind="avatar")
        portrait = create_user_picture(db, diver, kind="portrait")

        referenced = await sweeper._referenced_keys(async_db)

        assert {
            avatar.rendition_storage_key,
            avatar.original_storage_key,
            portrait.rendition_storage_key,
            portrait.original_storage_key,
        } <= referenced

    @pytest.mark.asyncio
    async def test_a_picture_without_an_original_contributes_nothing_more(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        create_user_picture(db, create_user(db), with_original=False)

        referenced = await sweeper._referenced_keys(async_db)

        assert None not in referenced

    @pytest.mark.asyncio
    async def test_a_species_photo_key_counts_as_referenced(self, db: Session, async_db: AsyncSession) -> None:
        key = f"species-photos/bb/{uuid7()}_{'b' * 64}"
        create_species(db, photo_storage_key=key, photo_sha256="b" * 64)

        referenced = await sweeper._referenced_keys(async_db)

        assert key in referenced

    @pytest.mark.asyncio
    async def test_a_species_without_a_photo_contributes_nothing(self, db: Session, async_db: AsyncSession) -> None:
        create_species(db)

        referenced = await sweeper._referenced_keys(async_db)

        assert None not in referenced

    @pytest.mark.asyncio
    async def test_a_stored_species_photo_is_not_swept(self, db: Session, async_db: AsyncSession, volume: Path) -> None:
        """The end-to-end shape of the destructive failure, rather than only the query.

        `iter_keys()` walks the whole volume, so a kind missing from `_referenced_keys` is
        on-disk and referenced by nothing - and `--delete` unlinks it once it is past the grace
        window. This drives the real query rather than the stub every test above uses, which is
        the only way the two halves can be checked against each other.
        """
        key = f"species-photos/cc/{uuid7()}_{'c' * 64}"
        create_species(db, photo_storage_key=key, photo_sha256="c" * 64)
        _write(volume, key, age_hours=48)
        _write(volume, ORPHAN, age_hours=48)

        # The set comes from the real query rather than from a literal - which is the half the
        # tests above stub out - and is then fed to the real classification. Run through the
        # test's own session, because the one `sweep` opens for itself would point at the same
        # database but not see rows this test committed on another connection.
        with _with_referenced(await sweeper._referenced_keys(async_db)):
            report = await sweeper.sweep(delete=True)

        assert (volume / key).is_file()
        assert not (volume / ORPHAN).exists()
        assert report.deleted == 1


class TestTheObjectStoreBackend:
    """The same diff, against a bucket instead of a volume.

    Everything the sweeper does is written in `blob_store`'s vocabulary, so the interesting
    question is not whether the diff still works - it is the one half that cannot: the
    stale-temp-file pass, which has nothing to reclaim on a store where `PutObject` is
    atomic.
    """

    @staticmethod
    def _store(client: FakeS3Client, key: str, *, age_hours: float = 0.0) -> None:
        client.seed(key, modified=datetime.fromtimestamp(time.time() - age_hours * 3600, UTC))

    @pytest.fixture
    def bucket(self, monkeypatch: pytest.MonkeyPatch) -> FakeS3Client:
        return select_s3_backend(monkeypatch)

    @pytest.mark.asyncio
    async def test_it_finds_and_deletes_the_unreferenced_object(self, bucket: FakeS3Client) -> None:
        self._store(bucket, REFERENCED, age_hours=48)
        self._store(bucket, ORPHAN, age_hours=48)

        with _with_referenced({REFERENCED}):
            report = await sweeper.sweep(delete=True)

        assert (report.on_disk, report.orphaned, report.deleted) == (2, 1, 1)
        assert list(bucket.objects) == [REFERENCED]

    @pytest.mark.asyncio
    async def test_an_object_inside_the_grace_window_is_left_alone(self, bucket: FakeS3Client) -> None:
        """`LastModified` is what stands in for the mtime, and an upload in flight is exactly
        as possible here as on the volume: the object is written before the row commits."""
        self._store(bucket, ORPHAN)

        with _with_referenced(set()):
            report = await sweeper.sweep(delete=True)

        assert (report.orphaned, report.within_grace, report.deleted) == (0, 1, 0)
        assert list(bucket.objects) == [ORPHAN]

    @pytest.mark.asyncio
    async def test_the_wrong_database_guard_still_refuses(self, bucket: FakeS3Client) -> None:
        """The refusal is the whole point of this script and is backend-independent, so it
        has to be shown to fire here rather than assumed to."""
        self._store(bucket, ORPHAN, age_hours=48)

        with _with_referenced(set()):
            report = await sweeper.sweep(delete=True)

        assert report.refused is not None
        assert list(bucket.objects) == [ORPHAN]

    @pytest.mark.asyncio
    async def test_the_temp_pass_does_nothing_and_reads_no_path(self, bucket: FakeS3Client) -> None:
        """There is no temp prefix to age out on an object store, and `storage_root()` would
        hand back a `Path` that has nothing to do with the bucket - so the pass skips itself
        rather than reporting a count from a directory nobody writes to."""
        self._store(bucket, f"{blob_store.TMP_DIRNAME}/.dead.part", age_hours=48)

        with _with_referenced(set()):
            report = await sweeper.sweep()

        assert report.stale_temp_files == 0
        assert report.on_disk == 0, "the temp prefix is not part of the store the sweeper diffs"
