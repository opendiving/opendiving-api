"""Tests for `services/blob_store.py` - the files volume, and the only filesystem in here.

Three things are worth pinning and the rest is plumbing:

- **The write is atomic and lands on the volume**, temp file included, because the temp
  directory being on another filesystem is the classic way an "atomic" rename becomes an
  `EXDEV` that only fires in a container.
- **`delete_after_commit` fires on commit and only on commit.** It is what keeps every
  `commit: bool = False` composition in the services working: a caller that rolls back must
  keep its files.
- **A missing file raises rather than reading as absent.** A row whose payload is gone is
  data loss, and the one report that would stop anyone investigating is a 404.
"""

import os
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.app.services import blob_store
from tests.conftest import db_available

DATA = b"a dive-computer export, more or less"
KEY = "dive-files/ab/abcdef"


@pytest.fixture
def volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Repoint the storage root at a temp directory for one test.

    Patched on `settings` rather than through the environment because `blob_store` reads
    `settings.FILE_STORAGE_DIR` at call time - which is exactly why it reads it at call time
    rather than capturing a `Path` at import.
    """
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


class TestKeys:
    def test_a_key_carries_the_kind_the_shard_and_the_content(self) -> None:
        digest = "ff" + "0" * 62
        kind, shard, name = blob_store.new_key("dive-files", sha256=digest).split("/")
        assert (kind, shard) == ("dive-files", "ff")
        assert name.endswith(f"_{digest}")

    def test_the_shard_comes_from_the_hash_not_the_nonce(self) -> None:
        """uuid7's leading hex is a millisecond timestamp, so sharding on it would put every
        key minted in one month in a handful of directories. sha256's first byte is
        uniformly random."""
        shards = {blob_store.new_key("dive-files", sha256=f"{n:02x}" + "0" * 62).split("/")[1] for n in range(256)}
        assert len(shards) == 256

    def test_the_same_content_never_mints_the_same_key_twice(self) -> None:
        """The invariant the whole post-commit unlink rests on: a retired key can never be
        minted again, so an unlink scheduled for it cannot destroy a file some concurrent
        write has since put there.

        This is why `new_key` is deliberately impure. It used to take the owning row's uuid,
        which was per-write for dive files (each content change inserts a new row) but *not*
        for cards, whose row survives replacement - so a card key was really keyed on (slot,
        content) and was re-mintable.
        """
        digest = "ab" + "0" * 62
        keys = {blob_store.new_key("certification-files", sha256=digest) for _ in range(200)}
        assert len(keys) == 200

    def test_the_kind_is_the_only_thing_a_caller_chooses(self) -> None:
        """Kinds are separate prefixes so a later one (dive photos, species images) can pick
        its own layout without moving anything already stored."""
        digest = "ab" + "0" * 62
        assert blob_store.new_key("species-images", sha256=digest).startswith("species-images/ab/")


class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_what_goes_in_comes_back_out(self, volume: Path) -> None:
        await blob_store.put(KEY, DATA)
        assert await blob_store.get(KEY) == DATA

    @pytest.mark.asyncio
    async def test_the_shard_directory_is_created_on_demand(self, volume: Path) -> None:
        await blob_store.put(KEY, DATA)
        assert (volume / "dive-files" / "ab" / "abcdef").is_file()

    @pytest.mark.asyncio
    async def test_the_temp_file_lives_inside_the_storage_root(self, volume: Path, monkeypatch) -> None:
        """`os.replace` raises `EXDEV` across filesystems, and in a container `/tmp` is
        overlayfs while the volume is a mount - two filesystems. Writing the temp file
        anywhere but under the root turns every upload into a container-only failure."""
        seen: list[str] = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append(str(src))
            return real_replace(src, dst)

        monkeypatch.setattr(blob_store.os, "replace", spy)
        await blob_store.put(KEY, DATA)

        assert seen and all(Path(src).parent == volume / blob_store.TMP_DIRNAME for src in seen)

    @pytest.mark.asyncio
    async def test_rewriting_a_key_leaves_no_temp_file_behind(self, volume: Path) -> None:
        await blob_store.put(KEY, DATA)
        await blob_store.put(KEY, DATA)
        assert list((volume / blob_store.TMP_DIRNAME).iterdir()) == []

    @pytest.mark.asyncio
    async def test_a_failed_write_cleans_up_its_temp_file(self, volume: Path, monkeypatch) -> None:
        def explode(src, dst):
            raise OSError("no")

        monkeypatch.setattr(blob_store.os, "replace", explode)
        with pytest.raises(OSError):
            await blob_store.put(KEY, DATA)

        assert list((volume / blob_store.TMP_DIRNAME).iterdir()) == []


class TestMissingFiles:
    @pytest.mark.asyncio
    async def test_reading_a_key_with_no_file_raises(self, volume: Path) -> None:
        with pytest.raises(blob_store.BlobMissingError) as exc:
            await blob_store.get(KEY)
        assert exc.value.key == KEY

    @pytest.mark.asyncio
    async def test_deleting_a_key_with_no_file_is_a_no_op(self, volume: Path) -> None:
        """So a retried delete, and the post-commit unlink of something the sweeper already
        took, cost nothing."""
        await blob_store.delete(KEY)

    @pytest.mark.asyncio
    async def test_has_answers_without_reading_the_file(self, volume: Path) -> None:
        assert await blob_store.has(KEY) is False
        await blob_store.put(KEY, DATA)
        assert await blob_store.has(KEY) is True


class TestEnsureRootWritable:
    def test_it_creates_the_root_and_its_temp_directory(self, tmp_path: Path, monkeypatch) -> None:
        root = tmp_path / "nested" / "files"
        monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(root))

        blob_store.ensure_root_writable()

        assert (root / blob_store.TMP_DIRNAME).is_dir()

    def test_it_leaves_no_probe_file_behind(self, volume: Path) -> None:
        blob_store.ensure_root_writable()
        assert list((volume / blob_store.TMP_DIRNAME).iterdir()) == []

    def test_concurrent_workers_do_not_fail_each_other(self, volume: Path) -> None:
        """The shipped image runs `gunicorn -w 4` and the lifespan runs once per worker, so
        all four reach this within milliseconds of each other at container start.

        With a shared probe filename the second worker to finish unlinks a file the first
        already took, and the `FileNotFoundError` - an `OSError` - is reported as a volume
        that "is not writable". A flaky startup failure accusing the wrong thing. Threads
        rather than processes here because the failure is a filesystem race, not a process
        one; what matters is several calls interleaving over the same directory.
        """
        errors: list[BaseException] = []

        def probe() -> None:
            try:
                for _ in range(40):
                    blob_store.ensure_root_writable()
            except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised by the assert
                errors.append(exc)

        threads = [threading.Thread(target=probe) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors, f"a healthy volume was reported unwritable: {errors[0]}"

    def test_the_probe_is_named_per_process(self, volume: Path, monkeypatch) -> None:
        """The property the test above rests on, asserted directly - because that one passes
        for the wrong reason if the race window simply never opens on a fast machine."""
        written: list[str] = []
        real_write_bytes = Path.write_bytes

        def spy(self: Path, data: bytes) -> int:
            written.append(self.name)
            return real_write_bytes(self, data)

        monkeypatch.setattr(Path, "write_bytes", spy)
        blob_store.ensure_root_writable()

        assert written == [f".writable-{os.getpid()}"]

    def test_an_unwritable_root_fails_loudly_and_names_the_setting(self, tmp_path: Path, monkeypatch) -> None:
        """Fail-fast at startup beats four gunicorn workers each discovering this
        per-upload, hours later, one diver at a time - and the message has to name the
        setting, because the cause is almost always a volume that isn't mounted."""
        root = tmp_path / "readonly" / "files"
        (tmp_path / "readonly").mkdir()
        (tmp_path / "readonly").chmod(0o500)
        monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(root))

        try:
            with pytest.raises(RuntimeError, match="FILE_STORAGE_DIR"):
                blob_store.ensure_root_writable()
        finally:
            (tmp_path / "readonly").chmod(0o700)


class TestIterKeys:
    @pytest.mark.asyncio
    async def test_it_yields_keys_not_paths(self, volume: Path) -> None:
        await blob_store.put(KEY, DATA)
        assert list(blob_store.iter_keys()) == [KEY]

    @pytest.mark.asyncio
    async def test_it_skips_the_temp_directory(self, volume: Path) -> None:
        """Otherwise the sweeper counts an upload in flight as an orphan."""
        (volume / blob_store.TMP_DIRNAME).mkdir(parents=True, exist_ok=True)
        (volume / blob_store.TMP_DIRNAME / ".leftover.part").write_bytes(b"x")
        await blob_store.put(KEY, DATA)

        assert list(blob_store.iter_keys()) == [KEY]

    def test_an_absent_root_yields_nothing_rather_than_raising(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path / "never-created"))
        assert list(blob_store.iter_keys()) == []

    @pytest.mark.asyncio
    async def test_stat_mtime_reports_the_file_and_none_for_a_missing_one(self, volume: Path) -> None:
        await blob_store.put(KEY, DATA)
        mtime = blob_store.stat_mtime(KEY)
        assert mtime is not None and mtime <= time.time() + 1
        assert blob_store.stat_mtime("dive-files/zz/nothing") is None


@pytest.mark.skipif(not db_available(), reason="Postgres not reachable")
class TestDeleteAfterCommit:
    """The hook that ties an unlink to a transaction, against a real session.

    A real one, because the whole point is what SQLAlchemy's `after_commit` and
    `after_rollback` do - which a mock session neither fires nor could be trusted to fire
    faithfully. The counterpart is that the service tests drive mock sessions and therefore
    never fire it, which is exactly the property `delete_after_commit` is shaped for: it
    only appends to `session.info`.
    """

    @pytest.fixture
    def sync_session(self):
        from tests.conftest import sync_engine

        with Session(sync_engine) as session:
            yield session

    @pytest.mark.asyncio
    async def test_a_commit_unlinks_what_was_registered(self, volume: Path, sync_session: Session) -> None:
        await blob_store.put(KEY, DATA)

        blob_store.delete_after_commit(sync_session, KEY)
        assert (volume / KEY).is_file(), "registering must not unlink anything on its own"

        sync_session.commit()
        assert not (volume / KEY).exists()

    @pytest.mark.asyncio
    async def test_a_rollback_keeps_the_file(self, volume: Path, sync_session: Session) -> None:
        """The row is still there after a rollback, so its file has to be too."""
        await blob_store.put(KEY, DATA)
        sync_session.execute(select(1))

        blob_store.delete_after_commit(sync_session, KEY)
        sync_session.rollback()

        assert (volume / KEY).is_file()

    @pytest.mark.asyncio
    async def test_a_rollback_clears_the_registration_for_the_next_commit(
        self, volume: Path, sync_session: Session
    ) -> None:
        """The nastier half: a stale registration surviving a rollback would unlink the
        file on whatever the session committed *next*.

        The statement before the registration is not scene-setting - it is the precondition
        that makes this work at all. SQLAlchemy autobegins on the first statement, and a
        `rollback()` on a session with no transaction open is a no-op that fires no event.
        Every real caller registers keys immediately after the `DELETE` or `INSERT` that
        retired them, so there is always a transaction to roll back.
        """
        await blob_store.put(KEY, DATA)
        sync_session.execute(select(1))

        blob_store.delete_after_commit(sync_session, KEY)
        sync_session.rollback()

        sync_session.execute(select(1))
        sync_session.commit()

        assert (volume / KEY).is_file()

    @pytest.mark.asyncio
    async def test_registering_nothing_is_harmless(self, volume: Path, sync_session: Session) -> None:
        blob_store.delete_after_commit(sync_session, [])
        sync_session.commit()

    @pytest.mark.asyncio
    async def test_an_already_unlinked_key_does_not_break_the_commit(self, volume: Path, sync_session: Session) -> None:
        """The sweeper may have got there first, and a commit that raised over a file
        already gone would fail a request whose database work is done."""
        blob_store.delete_after_commit(sync_session, "dive-files/zz/never-existed")
        sync_session.commit()


@pytest.mark.skipif(not db_available(), reason="Postgres not reachable")
class TestTheHookFiresThroughARealAsyncSession:
    """The one path every production caller actually takes.

    `delete_after_commit` writes into `db.info`, and the listeners are registered on the
    *sync* `Session` class - so the entire delete path rests on `AsyncSession.info` being
    the same dict object the listener pops from. It is (SQLAlchemy proxies it to
    `sync_session.info`), but that is a fact about a library, and the tests above assert it
    only through a sync `Session` while the service tests use `AsyncMock`s whose `info` is a
    plain dict no listener ever sees. Between them, that arrangement would stay green while
    every dive file and card leaked its blob on disk. This is the test that would not.
    """

    @pytest.mark.asyncio
    async def test_a_commit_on_an_async_session_unlinks(self, volume: Path, async_db) -> None:
        await blob_store.put(KEY, DATA)
        await async_db.execute(select(1))

        blob_store.delete_after_commit(async_db, KEY)
        assert (volume / KEY).is_file(), "registering must not unlink anything on its own"

        await async_db.commit()
        assert not (volume / KEY).exists()

    @pytest.mark.asyncio
    async def test_a_rollback_on_an_async_session_keeps_the_file(self, volume: Path, async_db) -> None:
        await blob_store.put(KEY, DATA)
        await async_db.execute(select(1))

        blob_store.delete_after_commit(async_db, KEY)
        await async_db.rollback()

        await async_db.execute(select(1))
        await async_db.commit()

        assert (volume / KEY).is_file()

    @pytest.mark.asyncio
    async def test_async_session_info_is_the_dict_the_listener_reads(self, volume: Path, async_db) -> None:
        """The proxy relationship itself, named rather than left implicit - so a SQLAlchemy
        upgrade that broke it fails here, pointing at the cause, instead of failing the two
        tests above and pointing at the hook."""
        assert async_db.info is async_db.sync_session.info


@pytest.mark.skipif(not db_available(), reason="Postgres not reachable")
class TestTheHookDoesNotLeakBetweenSessions:
    def test_one_sessions_registration_is_not_another_sessions_business(self, volume: Path) -> None:
        """The listeners are registered against the `Session` class, so every session in
        the process runs them - and each has to take only its own keys."""
        from tests.conftest import sync_engine

        (volume / "dive-files" / "ab").mkdir(parents=True)
        (volume / KEY).write_bytes(DATA)

        with Session(sync_engine) as registering, Session(sync_engine) as innocent:
            blob_store.delete_after_commit(registering, KEY)
            innocent.commit()
            assert (volume / KEY).is_file()

            registering.commit()
            assert not (volume / KEY).exists()
