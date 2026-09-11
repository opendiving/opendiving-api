"""Tests for `src/scripts/migrate_blobs.py` - the copy an operator runs when switching
backends.

Three properties carry the whole script, and none of them is the copy loop:

- **It is resumable**, because a key ends in the hash of its own content, so an object
  already present under that key is already the right bytes. A second run is cheap and
  correct rather than merely tolerated - which matters, because the documented procedure is
  to run it, switch, and run it again for whatever arrived in between.
- **It never deletes the source.** A switch that goes wrong has to be one restart away from
  working.
- **It refuses to carry corruption.** A local file whose bytes do not hash to what its own
  key claims is not copied into a store with no older copy to compare against.
"""

from pathlib import Path

import pytest

from src.app.core.config import FileStorageBackendOption
from src.app.services import blob_store
from src.scripts import migrate_blobs
from tests.helpers.fake_s3 import FakeS3Client, select_s3_backend

DATA = b"a dive-computer export, more or less"
#: The real sha256 of `DATA`, because `_copy_one` checks the bytes against the key's own
#: tail before carrying them across.
DIGEST = "3ceed9efd24c38bad2fc8750c5131ca6a6694c50d8d37ca8470f9f3c43a58bc2"


@pytest.fixture
def both_backends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeS3Client:
    """A volume and a bucket configured at once, with `local` still selected.

    That is the state the script is run in and the reason `backend_for` takes an argument:
    an operator sets the `S3_*` group, runs this, and only then flips
    `FILE_STORAGE_BACKEND`.
    """
    client = select_s3_backend(monkeypatch)
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_BACKEND", FileStorageBackendOption.LOCAL)
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return client


def _local_key(sha256: str = DIGEST) -> str:
    return blob_store.new_key("dive-files", sha256=sha256)


class TestCopyingToTheObjectStore:
    @pytest.mark.asyncio
    async def test_every_blob_arrives_under_the_same_key(self, both_backends: FakeS3Client) -> None:
        """The property the whole switch rests on: the `storage_key` column is untouched by
        the move, so nothing in the database has to be rewritten."""
        keys = [_local_key(), _local_key()]
        for key in keys:
            await blob_store.put(key, DATA)

        report = await migrate_blobs.migrate(to=FileStorageBackendOption.S3)

        assert (report.total, report.copied, report.failed) == (2, 2, 0)
        assert sorted(both_backends.objects) == sorted(keys)
        assert both_backends.objects[keys[0]] == DATA

    @pytest.mark.asyncio
    async def test_the_source_is_left_exactly_as_it_was(self, both_backends: FakeS3Client, tmp_path: Path) -> None:
        """Reclaiming the volume is a separate, deliberate act, so that a switch that goes
        wrong is a restart away from working again."""
        key = _local_key()
        await blob_store.put(key, DATA)

        await migrate_blobs.migrate(to=FileStorageBackendOption.S3)

        assert (tmp_path / key).read_bytes() == DATA

    @pytest.mark.asyncio
    async def test_a_second_run_copies_nothing_and_is_not_a_failure(self, both_backends: FakeS3Client) -> None:
        """What makes it resumable. An object already there under a key that ends in the
        content's own hash cannot be different bytes, so skipping is the correct answer."""
        key = _local_key()
        await blob_store.put(key, DATA)
        await migrate_blobs.migrate(to=FileStorageBackendOption.S3)
        both_backends.calls.clear()

        report = await migrate_blobs.migrate(to=FileStorageBackendOption.S3)

        assert (report.total, report.copied, report.skipped, report.failed) == (1, 0, 1, 0)
        assert "put_object" not in both_backends.operations()

    @pytest.mark.asyncio
    async def test_an_interrupted_run_is_finished_by_the_next_one(self, both_backends: FakeS3Client) -> None:
        keys = [_local_key() for _ in range(3)]
        for key in keys:
            await blob_store.put(key, DATA)
        both_backends.objects[keys[0]] = DATA  # as though the first run stopped after one

        report = await migrate_blobs.migrate(to=FileStorageBackendOption.S3)

        assert (report.copied, report.skipped) == (2, 1)
        assert sorted(both_backends.objects) == sorted(keys)

    @pytest.mark.asyncio
    async def test_the_temp_directory_is_not_carried_across(self, both_backends: FakeS3Client, tmp_path: Path) -> None:
        """`.part` files are the remains of interrupted writes, and the store they would be
        copied into has no pass that would ever reclaim them."""
        leftover = tmp_path / blob_store.TMP_DIRNAME / ".1234-abc.part"
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.write_bytes(b"half an upload")

        report = await migrate_blobs.migrate(to=FileStorageBackendOption.S3)

        assert (report.total, report.copied) == (0, 0)
        assert both_backends.objects == {}

    @pytest.mark.asyncio
    async def test_a_file_that_does_not_match_its_own_key_is_refused(self, both_backends: FakeS3Client) -> None:
        """Copying it would launder a corrupted file into a store with no older copy beside
        it. The run reports a failure and the operator's next act is to look, not to switch.
        """
        key = _local_key()
        await blob_store.put(key, b"not the bytes this key claims")

        report = await migrate_blobs.migrate(to=FileStorageBackendOption.S3)

        assert (report.copied, report.failed) == (0, 1)
        assert both_backends.objects == {}

    @pytest.mark.asyncio
    async def test_a_key_with_no_digest_in_it_is_copied_anyway(self, both_backends: FakeS3Client) -> None:
        """The check is anchored on the key format, so anything written before that format
        settled simply skips it rather than failing it. Refusing to move a blob because its
        name is old would be the worse error."""
        key = "dive-files/ab/legacy-name"
        await blob_store.put(key, DATA)

        report = await migrate_blobs.migrate(to=FileStorageBackendOption.S3)

        assert (report.copied, report.failed) == (1, 0)
        assert both_backends.objects[key] == DATA


class TestCopyingBack:
    @pytest.mark.asyncio
    async def test_the_move_runs_in_the_other_direction_too(self, both_backends: FakeS3Client, tmp_path: Path) -> None:
        """An operator who tries the object store and wants the volume back is in the same
        position as one who switched to it, and for the same reason - the keys are identical
        - it is the same copy with the ends swapped."""
        key = _local_key()
        both_backends.objects[key] = DATA

        report = await migrate_blobs.migrate(to=FileStorageBackendOption.LOCAL)

        assert (report.total, report.copied) == (1, 1)
        assert (tmp_path / key).read_bytes() == DATA
        assert both_backends.objects[key] == DATA, "the source was deleted"


class TestAskingForABackendThatIsNotConfigured:
    def test_a_half_configured_object_store_names_what_is_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`Settings._require_s3_credentials` cannot catch this one: it only fires when
        `FILE_STORAGE_BACKEND` is already `s3`, and this script is run while it is still
        `local`. Without the guard the failure is a `NoneType` deep inside botocore.
        """
        select_s3_backend(monkeypatch)
        monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_BACKEND", FileStorageBackendOption.LOCAL)
        monkeypatch.setattr(blob_store.settings, "S3_BUCKET", None)

        with pytest.raises(RuntimeError, match="S3_BUCKET"):
            blob_store.backend_for(FileStorageBackendOption.S3)
