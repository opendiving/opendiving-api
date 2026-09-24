"""`services/blob_store.py` on its `s3` backend, against an in-memory stub.

The other half of `tests/test_blob_store.py`, which covers the same module on the local
volume. Everything here runs with no database, no network and no skip, which is the
property that matters: the object store is what the hosted instance runs on, and a test
suite that only exercised it where a bucket happens to exist would exercise it nowhere.

Four things are worth pinning and the rest is plumbing:

- **A key means the same thing on both backends.** `S3_PREFIX` is prepended on the way out
  and stripped on the way back, so the `storage_key` a row carries is portable and a switch
  between backends rewrites nothing.
- **A missing object is `BlobMissingError`, and a broken store is not.** `NoSuchKey` and a
  `404` from a HEAD mean gone; anything else has to keep propagating, or a credential
  failure reads as data loss.
- **The startup probe fails loudly and names what to check**, because the alternative is a
  misconfigured bucket discovered by a diver.
- **A committed transaction's deletes actually reach the store**, which on this backend
  happens on a thread rather than inline - the one place the two backends behave
  differently.

`tests/helpers/fake_s3.py` says what the stub does and does not prove.
"""

import asyncio

import pytest
from botocore.exceptions import ClientError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.services import blob_store
from tests.helpers.fake_s3 import FakeS3Client, select_s3_backend

DATA = b"a dive-computer export, more or less"
KEY = "dive-files/ab/abcdef"
BUCKET = "opendiving-files"
PREFIX = "instance-one"


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> FakeS3Client:
    """The S3 backend selected, with no key prefix."""
    return select_s3_backend(monkeypatch, bucket=BUCKET)


@pytest.fixture
def prefixed_s3(monkeypatch: pytest.MonkeyPatch) -> FakeS3Client:
    """The S3 backend selected, sharing a bucket with other instances."""
    return select_s3_backend(monkeypatch, bucket=BUCKET, prefix=PREFIX)


class TestKeysAndThePrefix:
    @pytest.mark.asyncio
    async def test_an_unprefixed_key_is_the_object_name(self, s3: FakeS3Client) -> None:
        await blob_store.put(KEY, DATA)
        assert list(s3.objects) == [KEY]

    @pytest.mark.asyncio
    async def test_the_prefix_is_prepended_and_stripped_back_off(self, prefixed_s3: FakeS3Client) -> None:
        """The property that makes a `storage_key` portable: the database never sees the
        prefix, so adding or removing one is a decision about the bucket rather than a
        migration of the rows."""
        await blob_store.put(KEY, DATA)

        assert list(prefixed_s3.objects) == [f"{PREFIX}/{KEY}"]
        assert list(blob_store.iter_keys()) == [KEY]
        assert await blob_store.get(KEY) == DATA

    @pytest.mark.asyncio
    async def test_a_slash_heavy_prefix_is_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`/instance-one/` is what an operator types. A leading slash gives an empty first
        path segment on stores that tolerate it, and a doubled one gives an empty middle."""
        client = select_s3_backend(monkeypatch, bucket=BUCKET, prefix="/instance-one/")
        await blob_store.put(KEY, DATA)

        assert list(client.objects) == [f"{PREFIX}/{KEY}"]

    @pytest.mark.asyncio
    async def test_iter_keys_skips_the_temp_prefix(self, s3: FakeS3Client) -> None:
        """The startup probe writes there, and the sweeper must not read a probe as an
        orphaned blob and delete it out from under a booting worker."""
        await blob_store.put(KEY, DATA)
        s3.objects[f"{blob_store.TMP_DIRNAME}/.writable-1"] = b""

        assert list(blob_store.iter_keys()) == [KEY]

    @pytest.mark.asyncio
    async def test_iter_keys_pages_rather_than_asking_for_everything(self, s3: FakeS3Client) -> None:
        """A bucket with a real corpus does not fit in one response, and a walk that read
        only the first page would hand the sweeper a list of orphans that is mostly wrong."""
        keys = [f"dive-files/{n:02x}/file-{n}" for n in range(5)]
        for key in keys:
            await blob_store.put(key, DATA)

        assert sorted(blob_store.iter_keys()) == sorted(keys)
        assert "list_objects_v2_paginate" in s3.operations()


class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_what_goes_in_comes_back_out(self, s3: FakeS3Client) -> None:
        await blob_store.put(KEY, DATA)
        assert await blob_store.get(KEY) == DATA

    @pytest.mark.asyncio
    async def test_rewriting_a_key_is_one_request_and_no_temp_object(self, s3: FakeS3Client) -> None:
        """`PutObject` is atomic at the store, so the whole temp-file-and-rename dance the
        local backend needs has no counterpart here."""
        await blob_store.put(KEY, DATA)
        await blob_store.put(KEY, DATA)

        assert list(s3.objects) == [KEY]
        assert s3.operations() == ["put_object", "put_object"]

    @pytest.mark.asyncio
    async def test_has_answers_without_fetching_the_bytes(self, s3: FakeS3Client) -> None:
        assert await blob_store.has(KEY) is False
        await blob_store.put(KEY, DATA)

        assert await blob_store.has(KEY) is True
        assert "get_object" not in s3.operations()

    @pytest.mark.asyncio
    async def test_deleting_a_key_with_no_object_is_a_no_op(self, s3: FakeS3Client) -> None:
        """So a retried delete, and the post-commit delete of something the sweeper already
        took, cost one request and nothing else."""
        await blob_store.delete(KEY)

    @pytest.mark.asyncio
    async def test_stat_mtime_reports_the_object_and_none_for_a_missing_one(self, s3: FakeS3Client) -> None:
        await blob_store.put(KEY, DATA)
        assert blob_store.stat_mtime(KEY) is not None
        assert blob_store.stat_mtime("dive-files/zz/nothing") is None


class TestMissingObjectsAndRealFailures:
    """A row whose bytes are gone is data loss and says so; a store that is broken must not
    be reported as one."""

    @pytest.mark.asyncio
    async def test_reading_a_key_with_no_object_raises_blob_missing(self, s3: FakeS3Client) -> None:
        with pytest.raises(blob_store.BlobMissingError) as exc:
            await blob_store.get(KEY)
        assert exc.value.key == KEY

    @pytest.mark.asyncio
    async def test_a_head_on_a_missing_object_is_absence_not_an_error(self, s3: FakeS3Client) -> None:
        """`HeadObject` has no response body to carry an error code, so a store answers it
        with the bare status. Reading only `NoSuchKey` as absence turns every `exists()` on
        a missing object into a 500."""
        assert await blob_store.has(KEY) is False

    @pytest.mark.asyncio
    async def test_the_wrong_bucket_propagates_rather_than_reading_as_data_loss(self, s3: FakeS3Client) -> None:
        """The failure this separates out is the expensive one: a typo in `S3_BUCKET`
        answered as `BlobMissingError` would have the export archive quietly skip every
        member and the download routes report a lost file, for a bucket that is simply not
        the one that exists."""
        s3.bucket = "somebody-elses-bucket"

        with pytest.raises(ClientError):
            await blob_store.get(KEY)
        with pytest.raises(ClientError):
            await blob_store.has(KEY)


class TestEnsureStorageReady:
    def test_the_probe_is_written_and_taken_away_again(self, s3: FakeS3Client) -> None:
        blob_store.ensure_storage_ready()

        assert s3.objects == {}
        assert s3.operations() == ["put_object", "delete_object"]

    def test_the_probe_lives_where_iter_keys_does_not_look(self, s3: FakeS3Client) -> None:
        """A probe leaked by a crash between the put and the delete must never be swept as an
        orphan, and must never be counted as a stored file."""
        blob_store.ensure_storage_ready()
        written = [kwargs["Key"] for name, kwargs in s3.calls if name == "put_object"]

        assert written and all(key.startswith(f"{blob_store.TMP_DIRNAME}/") for key in written)

    def test_a_store_that_refuses_the_write_fails_loudly(self, s3: FakeS3Client) -> None:
        """Fail-fast at startup beats a diver discovering it, and the message has to name
        the settings because the cause is always one of four of them."""
        from botocore.exceptions import ClientError

        s3.explode_on["put_object"] = ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")

        with pytest.raises(RuntimeError, match="S3_ACCESS_KEY_ID"):
            blob_store.ensure_storage_ready()

    def test_the_failure_names_the_bucket_it_could_not_write_to(self, s3: FakeS3Client) -> None:
        s3.explode_on["put_object"] = ClientError({"Error": {"Code": "NoSuchBucket"}}, "PutObject")

        with pytest.raises(RuntimeError, match=BUCKET):
            blob_store.ensure_storage_ready()


class TestHasAnyKey:
    """The startup emptiness check, which is a network request on this backend and so has to
    be a bounded one."""

    @pytest.mark.asyncio
    async def test_it_answers_empty_and_not_empty(self, s3: FakeS3Client) -> None:
        assert blob_store.has_any_key() is False
        await blob_store.put(KEY, DATA)
        assert blob_store.has_any_key() is True

    @pytest.mark.asyncio
    async def test_it_asks_for_one_key_rather_than_listing(self, s3: FakeS3Client) -> None:
        """On an instance with a real corpus an unbounded listing would pull a thousand keys
        per page, on every boot of every container."""
        await blob_store.put(KEY, DATA)
        s3.calls.clear()

        blob_store.has_any_key()

        assert [(name, kwargs.get("MaxKeys")) for name, kwargs in s3.calls] == [("list_objects_v2", 1)]

    def test_a_leaked_probe_does_not_make_an_empty_store_look_populated(self, s3: FakeS3Client) -> None:
        """Otherwise the "your volume looks unmounted" warning is silenced for good by a
        single object nobody can read - which is the one direction this check must not fail
        in. The second request is still bounded."""
        s3.objects[f"{blob_store.TMP_DIRNAME}/.writable-1"] = b""

        assert blob_store.has_any_key() is False
        assert [kwargs.get("MaxKeys") for name, kwargs in s3.calls if name == "list_objects_v2"] == [1, 1]

    @pytest.mark.asyncio
    async def test_a_blob_beyond_the_temp_prefix_is_still_found(self, s3: FakeS3Client) -> None:
        """`user-avatars/...` sorts after `tmp/...`, so the first single-key request returns
        the probe and only the second one sees the avatar."""
        s3.objects[f"{blob_store.TMP_DIRNAME}/.writable-1"] = b""
        await blob_store.put("user-avatars/ab/avatar", DATA)

        assert blob_store.has_any_key() is True

    @pytest.mark.asyncio
    async def test_it_only_sees_this_instance_when_a_prefix_is_set(self, prefixed_s3: FakeS3Client) -> None:
        """A shared bucket's other tenant must not answer this instance's question."""
        prefixed_s3.objects["instance-two/dive-files/ab/theirs"] = DATA

        assert blob_store.has_any_key() is False


class TestDeletesRideTheCommit:
    """`delete_after_commit` on the backend where deleting is a network round trip.

    Driven through real SQLAlchemy sessions with no bind at all: the whole point is what
    `after_commit` does, which a mock session neither fires nor could be trusted to fire
    faithfully, and an unbound session commits without needing a database. `AsyncSession` is
    the shape every production caller has - the API's request sessions and the worker's
    purge alike - and it is the one that matters here, because its commit runs the sync
    session in a greenlet on the event loop thread.
    """

    @pytest.mark.asyncio
    async def test_a_commit_deletes_the_objects_it_registered(self, s3: FakeS3Client) -> None:
        await blob_store.put(KEY, DATA)
        session = AsyncSession()

        blob_store.delete_after_commit(session, KEY)
        assert list(s3.objects) == [KEY], "registering must not delete anything on its own"

        await session.commit()
        await blob_store._await_pending_removals()

        assert s3.objects == {}

    @pytest.mark.asyncio
    async def test_a_rollback_keeps_them(self, s3: FakeS3Client) -> None:
        """The rows are still there after a rollback, so their objects have to be too - and
        the registration must not survive into whatever that session commits next, which is
        the nastier half.

        `begin()` stands in for the statement every real caller has already run. SQLAlchemy
        autobegins on the first one, and a `rollback()` on a session with no transaction open
        is a no-op that fires no event at all - which is why `delete_after_commit`'s rule is
        to register immediately *after* the `DELETE` that retired the rows.
        """
        await blob_store.put(KEY, DATA)
        session = AsyncSession()
        await session.begin()

        blob_store.delete_after_commit(session, KEY)
        await session.rollback()
        await session.commit()
        await blob_store._await_pending_removals()

        assert list(s3.objects) == [KEY]

    @pytest.mark.asyncio
    async def test_a_purges_worth_of_keys_goes_out_as_one_request(self, s3: FakeS3Client) -> None:
        """`purge_deleted_accounts` hands over every blob one diver ever uploaded. One
        `DeleteObjects` per commit rather than one `DeleteObject` per key is the difference
        between one round trip and five hundred."""
        keys = [f"dive-files/{n:02x}/export-{n}" for n in range(12)]
        for key in keys:
            await blob_store.put(key, DATA)
        session = AsyncSession()
        blob_store.delete_after_commit(session, keys)

        await session.commit()
        await blob_store._await_pending_removals()

        assert s3.objects == {}
        assert s3.operations().count("delete_objects") == 1

    @pytest.mark.asyncio
    async def test_the_prefix_is_applied_to_what_the_commit_deletes(self, prefixed_s3: FakeS3Client) -> None:
        """The one place a forgotten prefix would be silent: the delete would name an object
        that does not exist, succeed, and leave the diver's file in the bucket."""
        await blob_store.put(KEY, DATA)
        session = AsyncSession()
        blob_store.delete_after_commit(session, KEY)

        await session.commit()
        await blob_store._await_pending_removals()

        assert prefixed_s3.objects == {}

    @pytest.mark.asyncio
    async def test_a_refused_delete_is_logged_and_does_not_break_the_commit(
        self, s3: FakeS3Client, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The transaction has already committed and the rows are already gone. Raising here
        would fail a request whose work is done, over a blob the sweeper reclaims."""
        await blob_store.put(KEY, DATA)
        s3.undeletable.add(KEY)
        session = AsyncSession()
        blob_store.delete_after_commit(session, KEY)

        with caplog.at_level("WARNING"):
            await session.commit()
            await blob_store._await_pending_removals()

        assert KEY in caplog.text
        assert list(s3.objects) == [KEY]

    @pytest.mark.asyncio
    async def test_the_commit_does_not_wait_for_the_store(self, s3: FakeS3Client) -> None:
        """The property the thread hop buys, asserted rather than assumed: `commit()` returns
        before the delete has been made, so a round trip to the object store never blocks the
        event loop on a path whose database work is finished.

        A local `unlink` is the opposite - inline and done by the time `commit()` returns -
        which is what `tests/test_blob_store.py` asserts for that backend.
        """
        started = asyncio.Event()
        release = asyncio.Event()
        loop = asyncio.get_running_loop()

        def block_until_released(**kwargs: object) -> dict[str, object]:
            loop.call_soon_threadsafe(started.set)
            # Blocking a worker thread, not the loop: if the delete were inline this would
            # deadlock, because nothing would be left to set the event.
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)
            return {}

        await blob_store.put(KEY, DATA)
        s3.delete_objects = block_until_released  # type: ignore[method-assign]
        session = AsyncSession()
        blob_store.delete_after_commit(session, KEY)

        await session.commit()
        await asyncio.wait_for(started.wait(), timeout=5)
        assert list(s3.objects) == [KEY], "the commit waited for the store"

        release.set()
        await blob_store._await_pending_removals()

    @pytest.mark.asyncio
    async def test_a_sync_session_outside_a_loop_deletes_inline(self, s3: FakeS3Client) -> None:
        """The scripts and the suite commit sync sessions with no event loop to protect, and
        a thread hop there would be a delete nothing ever waits for."""
        await blob_store.put(KEY, DATA)

        def commit_without_a_loop() -> None:
            session = Session()
            blob_store.delete_after_commit(session, KEY)
            session.commit()

        await asyncio.to_thread(commit_without_a_loop)

        assert s3.objects == {}
