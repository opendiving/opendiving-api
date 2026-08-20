"""What the two file services promise now that the payload lives outside the database.

The contract these pin is an ordering one, and it is the whole consistency story: the file
is written before the row that references it, and the retired file is unlinked after the
transaction that stopped referencing it commits. Every database-visible state therefore
names bytes that exist, and the only thing a crash can leave behind is an unreferenced
file - harmless, and swept.

The sessions here are mocks, which is deliberate rather than a shortcut: it means these
tests assert what the service *does*, in what order, without the commit hook firing at all.
`tests/test_blob_store.py` exercises the hook against a real session, and that split is the
same one `delete_after_commit` is shaped for.
"""

import hashlib
import io
import uuid as uuid_pkg
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import UploadFile
from uuid6 import uuid7

from src.app.core.security import create_dive_file_token
from src.app.schemas.certification import CertificationSide
from src.app.services import blob_store
from src.app.services.certification_files import (
    KEY_KIND as CARD_KIND,
)
from src.app.services.certification_files import (
    load_certification_file,
    store_certification_file,
)
from src.app.services.dive_files import (
    KEY_KIND as DIVE_KIND,
)
from src.app.services.dive_files import (
    load_dive_file,
    store_dive_file,
)
from src.app.services.dive_parsers.suunto_xml import SuuntoXmlParser

# The namespace `SuuntoXmlParser` matches on, copied from `tests/test_dive_files.py`.
SUUNTO_NS = "http://schemas.datacontract.org/2004/07/Suunto.Diving.Dal"

XML = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><MaxDepth>25.5</MaxDepth><Duration>1800</Duration></Dive>
""".encode()
XML_DIGEST = hashlib.sha256(XML).hexdigest()

JPEG = b"\xff\xd8\xff" + b"a card, notionally"
JPEG_DIGEST = hashlib.sha256(JPEG).hexdigest()


@pytest.fixture
def volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


def _upload(content: bytes, filename: str) -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(content))


def _token() -> tuple[str, uuid_pkg.UUID]:
    user_uuid = uuid7()
    return create_dive_file_token(user_uuid=user_uuid, sha256=XML_DIGEST, parser_key=SuuntoXmlParser.key), user_uuid


class TestDiveFileWriteOrdering:
    @staticmethod
    def _session(*, existing_row: tuple | None = None, replaced_keys: list[str] | None = None) -> AsyncMock:
        """A session whose dedupe lookup returns `existing_row` and whose `DELETE ...
        RETURNING` hands back `replaced_keys`."""
        result = MagicMock()
        result.one_or_none.return_value = existing_row
        result.one.return_value = SimpleNamespace(uuid=uuid7(), updated_at=None)
        result.scalars.return_value = replaced_keys or []
        # `delete_profile_for_dive` reads `rowcount`; one result object answers every
        # statement here, so it has to be plausible for all of them.
        result.rowcount = 0

        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)
        db.info = {}
        return db

    @pytest.mark.asyncio
    async def test_the_file_is_on_the_volume_before_the_commit(self, volume: Path) -> None:
        """The ordering rule itself. A crash after this point leaves an unreferenced file;
        the reverse order would leave a committed row naming bytes that never existed."""
        when_committed: list[bool] = []
        db = self._session()
        db.commit = AsyncMock(side_effect=lambda: when_committed.append(any(volume.rglob("dive-files/*/*"))))

        token, user_uuid = _token()
        await store_dive_file(
            db, user_id=1, user_uuid=user_uuid, dive_id=7, upload=_upload(XML, "export.xml"), file_token=token
        )

        assert when_committed == [True]

    @pytest.mark.asyncio
    async def test_the_stored_key_names_the_kind_the_shard_and_the_content(self, volume: Path) -> None:
        """Not the row: the uuid in a key is a per-write nonce, deliberately not the owning
        row's - see `blob_store.new_key`."""
        db = self._session()
        token, user_uuid = _token()
        await store_dive_file(
            db, user_id=1, user_uuid=user_uuid, dive_id=7, upload=_upload(XML, "export.xml"), file_token=token
        )

        written = list(blob_store.iter_keys())
        assert len(written) == 1
        kind, shard, name = written[0].split("/")
        assert kind == DIVE_KIND
        assert shard == XML_DIGEST[:2]
        assert name.endswith(f"_{XML_DIGEST}")

    @pytest.mark.asyncio
    async def test_the_replaced_file_is_registered_for_unlinking_and_not_unlinked_yet(self, volume: Path) -> None:
        """Registered before the commit, because the hook fires *on* the commit - and the
        old file has to survive a rollback, since the row referencing it would too."""
        old_key = blob_store.new_key(DIVE_KIND, sha256="cd" + "0" * 62)
        await blob_store.put(old_key, b"the previous export")

        db = self._session(replaced_keys=[old_key])
        token, user_uuid = _token()
        await store_dive_file(
            db, user_id=1, user_uuid=user_uuid, dive_id=7, upload=_upload(XML, "export.xml"), file_token=token
        )

        assert db.info[blob_store._PENDING_DELETES] == [old_key]
        assert (volume / old_key).is_file()

    @pytest.fixture
    def no_reextraction(self, monkeypatch: pytest.MonkeyPatch):
        """Switch off the `noop` branch's opportunistic profile re-extraction.

        One mock result answers every statement on these sessions, so `get_existing_profile`
        would otherwise be handed the dedupe lookup's row. That branch has its own tests in
        `tests/test_dive_files.py`; what these two are about is the file on the volume.
        """
        monkeypatch.setattr("src.app.services.dive_files.should_extract", lambda *a, **k: "skip")
        monkeypatch.setattr("src.app.services.dive_files.get_existing_profile", AsyncMock(return_value=None))

    @pytest.mark.asyncio
    async def test_a_re_upload_of_the_same_bytes_rewrites_a_file_that_went_missing(
        self, volume: Path, no_reextraction
    ) -> None:
        """ "Just upload it again" is the natural repair after a partial volume loss, and
        without this it does nothing: the row already says "stored", so the download keeps
        500ing while the server refuses the very bytes that would fix it."""
        row_uuid = uuid7()
        key = blob_store.new_key(DIVE_KIND, sha256=XML_DIGEST)
        existing = (1, 7, row_uuid, "application/xml", len(XML), "export.xml", "suunto_xml", key, None)

        db = self._session(existing_row=existing)
        token, user_uuid = _token()
        await store_dive_file(
            db, user_id=1, user_uuid=user_uuid, dive_id=7, upload=_upload(XML, "export.xml"), file_token=token
        )

        assert (volume / key).read_bytes() == XML

    @pytest.mark.asyncio
    async def test_a_re_upload_leaves_an_intact_file_alone(self, volume: Path, no_reextraction) -> None:
        """The normal `noop` path costs one `stat` and touches nothing."""
        row_uuid = uuid7()
        key = blob_store.new_key(DIVE_KIND, sha256=XML_DIGEST)
        await blob_store.put(key, XML)
        before = (volume / key).stat().st_mtime_ns

        existing = (1, 7, row_uuid, "application/xml", len(XML), "export.xml", "suunto_xml", key, None)
        db = self._session(existing_row=existing)
        token, user_uuid = _token()
        await store_dive_file(
            db, user_id=1, user_uuid=user_uuid, dive_id=7, upload=_upload(XML, "export.xml"), file_token=token
        )

        assert (volume / key).stat().st_mtime_ns == before


class TestCardFileWriteOrdering:
    @staticmethod
    def _session(*, existing: str | None = None) -> AsyncMock:
        """`existing` is the `storage_key` the side currently holds, which is all the upsert
        path reads about it."""
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        result.one.return_value = SimpleNamespace(uuid=uuid7(), updated_at=None)

        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)
        db.info = {}
        return db

    @pytest.mark.asyncio
    async def test_the_file_is_on_the_volume_before_the_commit(self, volume: Path) -> None:
        when_committed: list[bool] = []
        db = self._session()
        db.commit = AsyncMock(side_effect=lambda: when_committed.append(any(volume.rglob(f"{CARD_KIND}/*/*"))))

        await store_certification_file(
            db, certification_id=3, side=CertificationSide.FRONT, upload=_upload(JPEG, "card.jpg")
        )

        assert when_committed == [True]

    @pytest.mark.asyncio
    async def test_replacing_a_side_retires_the_old_key(self, volume: Path) -> None:
        old_key = blob_store.new_key(CARD_KIND, sha256="ef" + "0" * 62)
        await blob_store.put(old_key, b"the blurry one")

        db = self._session(existing=old_key)
        await store_certification_file(
            db, certification_id=3, side=CertificationSide.FRONT, upload=_upload(JPEG, "card.jpg")
        )

        assert db.info[blob_store._PENDING_DELETES] == [old_key]
        # The new file is already on the volume, under a key of its own.
        written = [k for k in blob_store.iter_keys() if k != old_key]
        assert len(written) == 1 and written[0].endswith(f"_{JPEG_DIGEST}")

    @pytest.mark.asyncio
    async def test_re_uploading_identical_bytes_mints_a_new_key_and_retires_the_old(self, volume: Path) -> None:
        """The case that used to be a filesystem no-op, and had to stop being one.

        Deriving the key from the row made re-uploading a card's existing bytes reproduce the
        key it already had - which meant a *retired* key could be minted again, and a
        concurrent replacement's post-commit unlink could then delete the file this write had
        just put there. Now every write mints its own key, so the old one is unambiguously
        retired and the new one is unambiguously live.
        """
        old_key = blob_store.new_key(CARD_KIND, sha256=JPEG_DIGEST)
        await blob_store.put(old_key, JPEG)

        db = self._session(existing=old_key)
        await store_certification_file(
            db, certification_id=3, side=CertificationSide.FRONT, upload=_upload(JPEG, "card.jpg")
        )

        assert db.info[blob_store._PENDING_DELETES] == [old_key]
        written = [k for k in blob_store.iter_keys() if k != old_key]
        assert len(written) == 1, "the re-upload must land under a key of its own, not the retired one"
        assert (volume / written[0]).read_bytes() == JPEG


class TestTheReadTransactionIsReleasedBeforeTheBlobWrite:
    """`blob_store.put` is a threadpool write plus an `fsync` of up to 10 MB, and the lookup
    that precedes it autobegins a transaction.

    Without an explicit release the connection that ran a sub-millisecond `SELECT` is held
    idle-in-transaction for the whole of that write, against a pool of five plus ten
    overflow. The event loop is free throughout, which is exactly what makes it invisible
    until the pool runs dry and unrelated endpoints start timing out.

    An ordering test rather than a behavioural one, matching
    `test_dive_files.py::TestProfileExtractionReleasesTheTransaction` and
    `test_species.py::TestTheReadTransactionIsReleasedBeforeGoingOutbound`: what regresses is
    somebody moving a query back above the release, and nothing else would notice.
    """

    @staticmethod
    def _tracking_session(calls: list[str], *, existing: str | None = None) -> AsyncMock:
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        result.one.return_value = SimpleNamespace(uuid=uuid7(), updated_at=None)

        def record_query(*args: object, **kwargs: object) -> MagicMock:
            calls.append("query")
            return result

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=record_query)
        db.rollback = AsyncMock(side_effect=lambda: calls.append("release"))
        db.commit = AsyncMock(side_effect=lambda: calls.append("commit"))
        db.info = {}
        return db

    @staticmethod
    def _assert_no_query_is_held_open(calls: list[str]) -> None:
        """No `query` may sit between a release and the blob write that follows it.

        Positional rather than `calls.index(...)`, which returns the *first* occurrence and
        so cannot see the regression this class exists for: a read added back between the
        release and the write leaves `["query", "release", "query", "write"]`, where every
        index-based comparison still holds while the connection is pinned open again. The
        same trap is written up under *"The read transaction is released before either
        endpoint goes outbound"* in `DECISIONS.md`, where the first version of the sibling
        test had exactly this bug.
        """
        assert "write" in calls, "nothing was written; the test is not exercising the path"
        assert "release" in calls, "the lookup's transaction is never released"

        for position, call in enumerate(calls):
            if call != "write":
                continue
            preceding = calls[:position]
            assert "release" in preceding, f"wrote at {position} before any release: {calls}"
            window = preceding[len(preceding) - preceding[::-1].index("release") :]
            assert "query" not in window, f"a query is held open across the blob write: {calls}"

    @pytest.fixture
    def recording_put(self, monkeypatch: pytest.MonkeyPatch):
        """Marks the write in the same list the session writes into, so one sequence carries
        both sides of the ordering."""
        calls: list[str] = []
        real_put = blob_store.put

        async def record(key: str, data: bytes) -> None:
            calls.append("write")
            await real_put(key, data)

        monkeypatch.setattr("src.app.services.certification_files.blob_store.put", record)
        return calls

    @pytest.mark.asyncio
    async def test_a_first_upload_releases_before_writing(self, volume: Path, recording_put: list[str]) -> None:
        db = self._tracking_session(recording_put)

        await store_certification_file(
            db, certification_id=3, side=CertificationSide.FRONT, upload=_upload(JPEG, "card.jpg")
        )

        self._assert_no_query_is_held_open(recording_put)

    @pytest.mark.asyncio
    async def test_a_replacement_releases_before_writing_too(self, volume: Path, recording_put: list[str]) -> None:
        """The branch where the lookup actually found something, and so has a `Row` that has
        to survive the rollback."""
        db = self._tracking_session(recording_put, existing=blob_store.new_key(CARD_KIND, sha256="ef" + "0" * 62))

        await store_certification_file(
            db, certification_id=3, side=CertificationSide.FRONT, upload=_upload(JPEG, "card.jpg")
        )

        self._assert_no_query_is_held_open(recording_put)


class TestAMissingFileIsNotAMissingRow:
    """`None` means "no row"; a row whose file is gone raises.

    Collapsing the two would report data loss as a 404, which is the one answer that stops
    anyone investigating. The read routes turn the exception into a 500 by not catching it;
    the export writer and both backfills catch it and carry on.
    """

    @staticmethod
    def _session(row: object | None) -> AsyncMock:
        result = MagicMock()
        result.one_or_none.return_value = row
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)
        return db

    @pytest.mark.asyncio
    async def test_no_row_reads_as_none_for_a_dive_file(self, volume: Path) -> None:
        assert await load_dive_file(self._session(None), dive_id=7) is None

    @pytest.mark.asyncio
    async def test_no_row_reads_as_none_for_a_card(self, volume: Path) -> None:
        card = await load_certification_file(self._session(None), certification_id=3, side=CertificationSide.FRONT)
        assert card is None

    @pytest.mark.asyncio
    async def test_a_row_whose_file_is_gone_raises_for_a_dive_file(self, volume: Path) -> None:
        row = SimpleNamespace(
            storage_key="dive-files/ab/gone", content_type="application/xml", original_filename="x", sha256="ab"
        )
        with pytest.raises(blob_store.BlobMissingError):
            await load_dive_file(self._session(row), dive_id=7)

    @pytest.mark.asyncio
    async def test_a_row_whose_file_is_gone_raises_for_a_card(self, volume: Path) -> None:
        row = SimpleNamespace(
            storage_key="certification-files/ab/gone",
            content_type="image/jpeg",
            original_filename="card.jpg",
            sha256="ab",
        )
        with pytest.raises(blob_store.BlobMissingError):
            await load_certification_file(self._session(row), certification_id=3, side=CertificationSide.FRONT)
