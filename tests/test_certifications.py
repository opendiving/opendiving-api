"""Unit tests for the certification feature (`models/certification*.py`,
`schemas/certification.py`, `services/certification_files.py`,
`api/v1/certifications.py`).

Like `test_gear.py`, these cover the pieces that are pure logic and so need no database:
the public/internal shape conversion, the `agency`/`agency_other` pairing rules, the
upload size guard, and - most importantly - the content-type sniffing that decides what
bytes we are willing to store and later serve back. Endpoint behaviour on top of a live
Postgres/Redis is exercised end to end by hand (see DECISIONS.md), not here.
"""

import io
from datetime import UTC, date, datetime
from fnmatch import fnmatch
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, UploadFile
from uuid6 import uuid7

from src.app.api.v1.certifications import _to_public_certification, _validate_agency_pairing
from src.app.core.exceptions.http_exceptions import UnprocessableEntityException
from src.app.core.utils.uploads import read_upload_within_limit, safe_filename
from src.app.crud.crud_certifications import get_expiring_overview_for_user
from src.app.schemas.certification import (
    CertificationAgency,
    CertificationBase,
    CertificationCreate,
    CertificationFileInfo,
    CertificationReadInternal,
    CertificationSide,
)
from src.app.services.cache_invalidation import invalidate_certification_caches
from src.app.services.certification_files import (
    MAX_CARD_FILE_SIZE,
    UnsupportedCardFileError,
    sniff_content_type,
)

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PDF_BYTES = b"%PDF-1.7\n" + b"\x00" * 32
WEBP_BYTES = b"RIFF" + b"\x28\x00\x00\x00" + b"WEBP" + b"\x00" * 32
# HEIC's brand lives in the `ftyp` box at offset 4, not at the start of the file.
HEIC_BYTES = b"\x00\x00\x00\x18" + b"ftyp" + b"heic" + b"\x00" * 32


def _internal_certification(**overrides) -> CertificationReadInternal:
    defaults = {
        "id": 3,
        "user_id": 1,
        "uuid": uuid7(),
        "agency": CertificationAgency.PADI,
        "agency_other": None,
        "name": "Advanced Open Water Diver",
        "certification_number": "1234567",
        "certified_on": date(2019, 6, 14),
        "expires_on": None,
        "instructor_name": "A. Instructor",
        "instructor_number": "654321",
        "training_center": "Blue Ocean, Koh Tao",
        "notes": "",
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
    }
    return CertificationReadInternal(**{**defaults, **overrides})


def _file_info(side: CertificationSide) -> CertificationFileInfo:
    return CertificationFileInfo(
        uuid=uuid7(),
        side=side,
        content_type="image/jpeg",
        byte_size=1234,
        original_filename=f"{side.value}.jpg",
        updated_at=None,
    )


class TestPublicShapeConversion:
    def test_drops_internal_ids_and_resolves_the_owner_uuid(self) -> None:
        user_uuid = uuid7()
        internal = _internal_certification()

        public = _to_public_certification(internal, user_uuid=user_uuid)

        assert public.user_uuid == user_uuid
        assert public.uuid == internal.uuid
        assert public.name == "Advanced Open Water Diver"
        # The sequential internal id/user_id must never reach the public shape.
        assert not hasattr(public, "id")
        assert not hasattr(public, "user_id")

    def test_accepts_a_dict_row(self) -> None:
        """`crud.get_multi` yields dicts rather than models, so both must work."""
        public = _to_public_certification(_internal_certification().model_dump(), user_uuid=uuid7())

        assert public.certification_number == "1234567"

    def test_has_no_files_by_default(self) -> None:
        """`write_certification` passes no `files`, a brand-new certification provably
        having none - so the field has to default rather than blow up."""
        public = _to_public_certification(_internal_certification(), user_uuid=uuid7())

        assert public.files == []

    def test_embeds_the_files_it_is_given(self) -> None:
        """The read paths resolve a whole page's card files in one batched query and hand
        them in here, so the list can show which cards have images without a request per
        row."""
        files = [_file_info(CertificationSide.FRONT), _file_info(CertificationSide.BACK)]

        public = _to_public_certification(_internal_certification(), user_uuid=uuid7(), files=files)

        assert [f.side for f in public.files] == [CertificationSide.FRONT, CertificationSide.BACK]

    def test_never_exposes_file_bytes(self) -> None:
        """The embedded file metadata is metadata only - the bytes come from the separate
        download endpoint, and must not leak into a JSON list response."""
        public = _to_public_certification(
            _internal_certification(), user_uuid=uuid7(), files=[_file_info(CertificationSide.FRONT)]
        )

        assert "data" not in public.model_dump()["files"][0]


class TestAgencyPairing:
    def test_other_requires_a_name(self) -> None:
        with pytest.raises(ValueError):
            CertificationBase(agency=CertificationAgency.OTHER, name="Plongeur Niveau 2")

    def test_other_rejects_a_blank_name(self) -> None:
        """Whitespace is not a name - otherwise `agency_other=" "` would store an
        `other`-agency certification with nothing to display."""
        with pytest.raises(ValueError):
            CertificationBase(agency=CertificationAgency.OTHER, agency_other="   ", name="Plongeur Niveau 2")

    def test_other_accepts_a_name(self) -> None:
        certification = CertificationBase(
            agency=CertificationAgency.OTHER, agency_other="FFESSM", name="Plongeur Niveau 2"
        )

        assert certification.agency_other == "FFESSM"

    def test_named_agency_rejects_a_stray_other(self) -> None:
        """Rejected rather than ignored, so a stored row can never carry a second agency
        name that some future read path might decide to display."""
        with pytest.raises(ValueError):
            CertificationBase(agency=CertificationAgency.PADI, agency_other="FFESSM", name="Open Water Diver")

    def test_create_forbids_unknown_fields(self) -> None:
        with pytest.raises(ValueError):
            CertificationCreate(
                user_uuid=uuid7(), agency=CertificationAgency.PADI, name="Open Water Diver", dive_count=3
            )

    def test_unknown_agency_is_rejected(self) -> None:
        """The `StrEnum` is the single source of truth for the vocabulary - there is no DB
        `CHECK` behind it, so this is the only thing standing between a typo and a stored
        agency nobody's UI knows how to label."""
        with pytest.raises(ValueError):
            CertificationBase(agency="padi-international", name="Open Water Diver")


class TestPatchAgencyPairing:
    """`CertificationUpdate` can't validate the pairing itself - a PATCH may carry either
    field alone - so the route checks the *merged* result. These are that check."""

    def test_switching_to_other_without_a_name_is_rejected(self) -> None:
        with pytest.raises(UnprocessableEntityException):
            _validate_agency_pairing(CertificationAgency.OTHER, None)

    def test_switching_away_from_other_must_clear_the_name(self) -> None:
        """Patching `agency` to `padi` while the stored `agency_other` is still set would
        otherwise leave the row in the state the create schema forbids."""
        with pytest.raises(UnprocessableEntityException):
            _validate_agency_pairing(CertificationAgency.PADI, "FFESSM")

    def test_valid_merges_pass(self) -> None:
        _validate_agency_pairing(CertificationAgency.OTHER, "FFESSM")
        _validate_agency_pairing(CertificationAgency.PADI, None)


class TestContentTypeSniffing:
    """What we are willing to store, decided from the bytes rather than from the client's
    claimed `Content-Type` - the sniffed value is what the download route later serves
    the file back as."""

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (JPEG_BYTES, "image/jpeg"),
            (PNG_BYTES, "image/png"),
            (PDF_BYTES, "application/pdf"),
            (WEBP_BYTES, "image/webp"),
        ],
    )
    def test_accepts_supported_formats(self, data: bytes, expected: str) -> None:
        assert sniff_content_type(data) == expected

    def test_rejects_html_masquerading_as_a_jpeg(self) -> None:
        """The attack this exists to stop: were we to trust the upload's `Content-Type`,
        this would be stored as `image/jpeg` but served back as whatever the uploader
        asked for."""
        with pytest.raises(UnsupportedCardFileError):
            sniff_content_type(b"<html><script>alert(1)</script></html>")

    def test_rejects_a_riff_container_that_is_not_webp(self) -> None:
        """RIFF also wraps WAV and AVI; only the WEBP form is an image we can render."""
        with pytest.raises(UnsupportedCardFileError):
            sniff_content_type(b"RIFF" + b"\x28\x00\x00\x00" + b"WAVE" + b"\x00" * 32)

    def test_rejects_empty_input(self) -> None:
        with pytest.raises(UnsupportedCardFileError):
            sniff_content_type(b"")

    def test_heic_is_rejected_with_an_actionable_message(self) -> None:
        """HEIC is what an iPhone stores natively, so a bare "unsupported file type" here
        would read as a bug to anyone who just photographed their card."""
        with pytest.raises(UnsupportedCardFileError, match="HEIC"):
            sniff_content_type(HEIC_BYTES)

    def test_heic_message_says_what_to_do(self) -> None:
        with pytest.raises(UnsupportedCardFileError, match="JPEG or PNG"):
            sniff_content_type(HEIC_BYTES)


class TestSafeFilename:
    """`original_filename` is echoed back in a `Content-Disposition` header, so it has to
    be safe to put there. Nothing here ever touches the filesystem."""

    def test_strips_directory_components(self) -> None:
        assert safe_filename("../../etc/passwd") == "passwd"
        assert safe_filename(r"C:\Users\me\card.jpg") == "card.jpg"

    def test_strips_quotes_and_newlines_that_would_break_the_header(self) -> None:
        cleaned = safe_filename('ca"rd\r\nX-Injected: yes.jpg')

        assert '"' not in cleaned
        assert "\r" not in cleaned
        assert "\n" not in cleaned

    def test_falls_back_when_there_is_nothing_usable(self) -> None:
        assert safe_filename(None, default="card") == "card"
        assert safe_filename("", default="card") == "card"
        assert safe_filename("   ", default="card") == "card"

    def test_the_fallback_is_per_caller(self) -> None:
        """Each kind of upload names its own placeholder, so a download with no usable
        original name still says what it is."""
        assert safe_filename(None) == "file"
        assert safe_filename(None, default="dive-file") == "dive-file"

    def test_truncates_to_the_column_width(self) -> None:
        assert len(safe_filename("a" * 400 + ".jpg")) == 255


class TestUploadSizeGuard:
    """The guard promoted out of `/dive/parse` and now shared with card uploads."""

    @staticmethod
    def _upload(content: bytes) -> UploadFile:
        return UploadFile(filename="card.jpg", file=io.BytesIO(content))

    @pytest.mark.asyncio
    async def test_accepts_a_file_within_the_limit(self) -> None:
        content = b"a" * 1024

        assert await read_upload_within_limit(self._upload(content), MAX_CARD_FILE_SIZE) == content

    @pytest.mark.asyncio
    async def test_rejects_a_file_over_the_limit_with_413(self) -> None:
        upload = self._upload(b"a" * (MAX_CARD_FILE_SIZE + 1))

        with pytest.raises(HTTPException) as exc_info:
            await read_upload_within_limit(upload, MAX_CARD_FILE_SIZE)

        assert exc_info.value.status_code == 413

    @pytest.mark.asyncio
    async def test_reports_the_limit_it_was_given(self) -> None:
        """The message is built from `max_size` rather than a module constant, so the two
        callers (5 MB dive files, 10 MB cards) can't report each other's limit."""
        upload = self._upload(b"a" * (5 * 1024 * 1024 + 1))

        with pytest.raises(HTTPException) as exc_info:
            await read_upload_within_limit(upload, 5 * 1024 * 1024)

        assert "5 MB" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_rounds_a_fractional_limit_up_rather_than_down(self) -> None:
        """A 1.5 MB limit reported as "1 MB" would be a lie in the safe direction, but
        reporting it as "2 MB" invites a retry that fails again - round up so the number
        is never smaller than the real limit."""
        limit = 1024 * 1024 + 512 * 1024
        upload = self._upload(b"a" * (limit + 1))

        with pytest.raises(HTTPException) as exc_info:
            await read_upload_within_limit(upload, limit)

        assert "2 MB" in exc_info.value.detail


class TestCacheInvalidation:
    @pytest.mark.asyncio
    async def test_covers_every_certification_key_and_nothing_else(self, monkeypatch) -> None:
        patterns: list[str] = []
        monkeypatch.setattr(
            "src.app.services.cache_invalidation.delete_keys_by_pattern",
            AsyncMock(side_effect=lambda pattern: patterns.append(pattern)),
        )

        await invalidate_certification_caches(7)

        matches = lambda key: any(fnmatch(key, p) for p in patterns)  # noqa: E731
        assert matches("user_7_certifications:page_1:items_per_page:10")
        assert matches("user_7_certification:019f-abc")
        # Another user's keys, and other resources', must be left alone.
        assert not matches("user_8_certifications:page_1:items_per_page:10")
        assert not matches("user_7_gear_items:page_1:items_per_page:10:archived_False")
        assert not matches("user_7_dives:page_1:items_per_page:10")


class TestExpiringOverview:
    """`GET /certifications-expiring` is the certification twin of `/gear-service-due`,
    and exists so the dashboard's renewal card stops paging a diver's whole certification
    list client-side just to find the few with dates on them.
    """

    def _db(self, rows: list) -> MagicMock:
        db = MagicMock()
        db.execute = AsyncMock(return_value=rows)
        return db

    def _row(self, name: str = "Rescue Diver", expires_on: date = date(2026, 9, 1)) -> SimpleNamespace:
        return SimpleNamespace(
            uuid=uuid7(),
            agency=CertificationAgency.PADI,
            agency_other=None,
            name=name,
            expires_on=expires_on,
        )

    @pytest.mark.asyncio
    async def test_returns_the_rows_and_no_truncation_flag(self) -> None:
        db = self._db([self._row("Rescue Diver"), self._row("EFR")])

        data, truncated = await get_expiring_overview_for_user(db, user_id=1, limit=200)

        assert [item.name for item in data] == ["Rescue Diver", "EFR"]
        assert truncated is False

    @pytest.mark.asyncio
    async def test_excludes_cards_with_no_expiry_and_other_users(self) -> None:
        db = self._db([])

        await get_expiring_overview_for_user(db, user_id=7, limit=200)

        statement = str(db.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
        # Most recreational certifications never expire, so a dated-only filter is most
        # of the point - none of the undated ones can ever appear on a renewals card.
        assert "certification.expires_on IS NOT NULL" in statement
        assert "certification.user_id = 7" in statement
        assert "certification.is_deleted IS false" in statement

    @pytest.mark.asyncio
    async def test_sorts_soonest_first(self) -> None:
        db = self._db([])

        await get_expiring_overview_for_user(db, user_id=1, limit=200)

        statement = str(db.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
        assert "ORDER BY certification.expires_on ASC, certification.name" in statement

    @pytest.mark.asyncio
    async def test_flags_truncation_and_trims_to_the_limit(self) -> None:
        # One row past the cap is what the query deliberately asks for, so `truncated`
        # is exact rather than the "we got exactly `limit` rows, so probably" guess.
        db = self._db([self._row() for _ in range(4)])

        data, truncated = await get_expiring_overview_for_user(db, user_id=1, limit=3)

        assert len(data) == 3
        assert truncated is True

    @pytest.mark.asyncio
    async def test_does_not_flag_truncation_at_exactly_the_limit(self) -> None:
        db = self._db([self._row() for _ in range(3)])

        data, truncated = await get_expiring_overview_for_user(db, user_id=1, limit=3)

        assert len(data) == 3
        assert truncated is False

    @pytest.mark.asyncio
    async def test_selects_one_row_past_the_limit(self) -> None:
        db = self._db([])

        await get_expiring_overview_for_user(db, user_id=1, limit=200)

        statement = str(db.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
        assert "LIMIT 201" in statement

    def test_the_cache_key_is_covered_by_the_existing_invalidation_pattern(self) -> None:
        # The route's key prefix must start with `user_{id}_certification` or a card edit
        # would leave a stale renewals list behind - `invalidate_certification_caches`
        # sweeps exactly that one pattern and nothing calls anything extra for this route.
        assert fnmatch("user_1_certifications_expiring", "user_1_certification*")
