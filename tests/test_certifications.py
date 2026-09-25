"""Unit tests for the certification feature (`models/certification*.py`,
`schemas/certification.py`, `services/certification_files.py`,
`api/v1/certifications.py`).

Like `test_gear.py`, these cover the pieces that are pure logic and so need no database:
the public/internal shape conversion, the `agency`/`agency_other` pairing rules, the
upload size guard, and - most importantly - the content-type sniffing that decides what
bytes we are willing to store and later serve back. Endpoint behaviour on top of a live
Postgres/Redis is otherwise exercised end to end by hand (see DECISIONS.md), not here.

The one exception is `TestListOrderingAgainstPostgres`, which needs a real database
because what it pins *is* a Postgres semantic - `DESC` defaulting to `NULLS FIRST`. It
skips itself when no database is reachable; on a developer's machine that needs
`POSTGRES_SERVER=localhost`. See CONTRIBUTING.md.
"""

import io
from datetime import UTC, date, datetime
from fnmatch import fnmatch
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Response, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1 import certifications as certifications_module
from src.app.api.v1.certifications import (
    _cached_read_certifications,
    _to_public_certification,
    _validate_agency_pairing,
)
from src.app.core.exceptions.http_exceptions import UnprocessableEntityException
from src.app.core.utils.uploads import content_disposition_attachment, read_upload_within_limit, safe_filename
from src.app.crud.crud_certifications import get_certifications_page, get_expiring_overview_for_user
from src.app.models.user import User
from src.app.schemas.certification import (
    CertificationAgency,
    CertificationBase,
    CertificationCreate,
    CertificationFileInfo,
    CertificationReadInternal,
    CertificationSide,
    CertificationUpdateRequest,
)
from src.app.services.cache_invalidation import invalidate_certification_caches
from src.app.services.certification_files import (
    MAX_CARD_FILE_SIZE,
    UnsupportedCardFileError,
    sniff_content_type,
)
from tests.conftest import db_available
from tests.helpers.generators import create_certification, create_course

# The `@cache` decorator would need Redis and would serve a hit without re-running the
# body, which is the opposite of what the ordering tests assert. `__wrapped__` is the
# undecorated function.
_read_certifications_uncached = cast(Any, _cached_read_certifications).__wrapped__

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
            CertificationCreate(agency=CertificationAgency.PADI, name="Open Water Diver", dive_count=3)

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

    def test_a_stored_agency_outside_the_vocabulary_does_not_five_hundred(self) -> None:
        """The merged check receives the *stored* column, which since the read widening is a
        plain string that may be outside the enum (DECISIONS.md, *"A stored vocabulary is read
        back as a string"*). Reconstructing `CertificationAgency(...)` to call this made
        `PATCH /certification/{uuid}` a 500 on exactly those rows - and, because the
        reconstruction sat in a `dict.get()` default, on the PATCH that would have repaired
        one too.
        """
        _validate_agency_pairing("frobnicator", None)

    def test_such_a_row_still_refuses_a_stray_agency_other(self) -> None:
        """The rule is unchanged by the widening: anything that is not `other` may not carry
        a free-text agency name."""
        with pytest.raises(UnprocessableEntityException):
            _validate_agency_pairing("frobnicator", "FFESSM")

    def test_valid_merges_pass(self) -> None:
        _validate_agency_pairing(CertificationAgency.OTHER, "FFESSM")
        _validate_agency_pairing(CertificationAgency.PADI, None)


class TestTheCourseLink:
    """`course_uuid` on a certification - the first reference a card has ever carried, and
    the only one whose *whole* path is new rather than mirrored from a dive.

    The two things worth pinning here are the ones a symmetry argument would skip: the
    field sits on `CertificationCreate`/`CertificationUpdateRequest` rather than on
    `CertificationBase`/`CertificationUpdate`, because those two are CRUDAdmin's form
    schemas and a non-column field on either lands in the admin form; and
    `_to_public_certification` takes the resolved uuid as an argument rather than looking
    it up, which is what keeps it a synchronous pure function with three callers.
    """

    def test_the_admin_form_schemas_carry_no_course_uuid(self) -> None:
        """The trap `TripUpdateRequest`'s docstring records. `CertificationUpdate` is the
        panel's Certification form and `CertificationCreateInternal` inherits
        `CertificationBase`, so a `course_uuid` on either would be a field the panel
        renders and cannot resolve."""
        from src.app.schemas.certification import CertificationCreateInternal, CertificationUpdate

        assert "course_uuid" not in CertificationUpdate.model_fields
        assert "course_uuid" not in CertificationBase.model_fields
        assert "course_uuid" not in CertificationCreateInternal.model_fields
        # The column, though, is exactly what the admin *create* form should offer -
        # matching how `DiveCreateInternal` exposes `trip_id`.
        assert "course_id" in CertificationCreateInternal.model_fields

    def test_the_request_schemas_carry_it(self) -> None:
        assert "course_uuid" in CertificationCreate.model_fields
        assert "course_uuid" in CertificationUpdateRequest.model_fields

    def test_the_public_shape_reports_the_course_it_is_given(self) -> None:
        course_uuid = uuid7()

        public = _to_public_certification(
            _internal_certification(course_id=5), user_uuid=uuid7(), course_uuid=course_uuid
        )

        assert public.course_uuid == course_uuid
        # The internal FK must never reach the public shape, exactly as `user_id` does not.
        assert "course_id" not in public.model_dump()

    def test_a_certification_with_no_course_reports_none(self) -> None:
        public = _to_public_certification(_internal_certification(), user_uuid=uuid7())

        assert public.course_uuid is None


class TestPatchCourseLink:
    """The `model_fields_set` branch on `patch_certification`, which is what tells an
    explicit `course_uuid: null` (detach) from an omitted key (leave alone). Without it a
    diver clearing the field gets "Certification updated" and no change at all."""

    @pytest.fixture
    def captured(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        seen: dict[str, Any] = {}

        async def fake_update(*, db: Any, object: dict, uuid: Any) -> None:
            seen["update_data"] = object

        stored = _internal_certification()
        monkeypatch.setattr(certifications_module, "_get_owned_certification", AsyncMock(return_value=stored))
        monkeypatch.setattr(certifications_module.crud_certifications, "update", fake_update)
        monkeypatch.setattr(certifications_module, "resolve_course_id_for_user", AsyncMock(return_value=88))
        monkeypatch.setattr(certifications_module, "invalidate_certification_caches", AsyncMock())
        return seen

    async def _patch(self, body: dict[str, Any]) -> None:
        await certifications_module.patch_certification(
            request=MagicMock(),
            uuid=uuid7(),
            values=CertificationUpdateRequest.model_validate(body),
            current_user={"id": 1, "uuid": uuid7()},
            db=MagicMock(),
        )

    @pytest.mark.asyncio
    async def test_explicit_null_detaches_the_card(self, captured: dict[str, Any]) -> None:
        await self._patch({"course_uuid": None})

        assert captured["update_data"]["course_id"] is None

    @pytest.mark.asyncio
    async def test_an_omitted_key_leaves_the_course_alone(self, captured: dict[str, Any]) -> None:
        # `notes` is here only so `update_data` is non-empty - the route skips the write
        # otherwise, which would pass this test for the wrong reason.
        await self._patch({"notes": "Card reprinted 2024"})

        assert "course_id" not in captured["update_data"]

    @pytest.mark.asyncio
    async def test_a_uuid_is_translated_to_the_internal_id(self, captured: dict[str, Any]) -> None:
        await self._patch({"course_uuid": str(uuid7())})

        assert captured["update_data"]["course_id"] == 88
        assert "course_uuid" not in captured["update_data"]

    @pytest.mark.asyncio
    async def test_an_unknown_course_is_rejected_before_the_write(
        self, captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(certifications_module, "resolve_course_id_for_user", AsyncMock(return_value=None))

        with pytest.raises(UnprocessableEntityException, match="Course not found"):
            await self._patch({"course_uuid": str(uuid7())})

        assert "update_data" not in captured


class TestACourseThatVanishesMidWrite:
    """The race the resolve-then-write shape leaves open: `resolve_course_id_for_user`
    answers, a concurrent `DELETE /course/{uuid}` hard-deletes the row, and the write then
    violates `certification_course_id_fkey`.

    Narrow, but the answer has to be the 422 a foreign or missing uuid already gets rather
    than a raw 500 - from the caller's side the two are the same thing, and the dive routes
    have answered that way for the identical FK since `_fk_error_detail` gained its branch.
    A constraint the handler does not recognize has to come back out untouched, or a real
    bug elsewhere would be reported as a missing course.
    """

    @staticmethod
    def _integrity_error(constraint: str) -> IntegrityError:
        return IntegrityError("write", {}, Exception(f'violates foreign key constraint "{constraint}"'))

    @pytest.fixture
    def failing_write(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        stubs: dict[str, Any] = {"db": MagicMock(), "invalidate": AsyncMock()}
        stubs["db"].rollback = AsyncMock()
        monkeypatch.setattr(
            certifications_module, "_get_owned_certification", AsyncMock(return_value=_internal_certification())
        )
        monkeypatch.setattr(certifications_module, "resolve_course_id_for_user", AsyncMock(return_value=88))
        monkeypatch.setattr(certifications_module, "invalidate_certification_caches", stubs["invalidate"])
        return stubs

    @pytest.mark.asyncio
    async def test_the_create_path_answers_422_and_rolls_back(
        self, failing_write: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            certifications_module.crud_certifications,
            "create",
            AsyncMock(side_effect=self._integrity_error("certification_course_id_fkey")),
        )
        user_uuid = uuid7()
        body = CertificationCreate.model_validate(
            {"agency": "tdi", "name": "Advanced Nitrox", "course_uuid": str(uuid7())}
        )

        with pytest.raises(UnprocessableEntityException, match="Course not found"):
            await certifications_module.write_certification(
                request=MagicMock(),
                certification=body,
                current_user={"id": 1, "uuid": user_uuid},
                db=failing_write["db"],
            )

        # The rollback is not decoration: the aborted transaction would refuse every
        # command the route ran afterwards, the cache invalidation included.
        failing_write["db"].rollback.assert_awaited_once()
        failing_write["invalidate"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_patch_path_answers_422_and_rolls_back(
        self, failing_write: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            certifications_module.crud_certifications,
            "update",
            AsyncMock(side_effect=self._integrity_error("certification_course_id_fkey")),
        )

        with pytest.raises(UnprocessableEntityException, match="Course not found"):
            await certifications_module.patch_certification(
                request=MagicMock(),
                uuid=uuid7(),
                values=CertificationUpdateRequest.model_validate({"course_uuid": str(uuid7())}),
                current_user={"id": 1, "uuid": uuid7()},
                db=failing_write["db"],
            )

        failing_write["db"].rollback.assert_awaited_once()
        failing_write["invalidate"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_any_other_constraint_comes_back_out_untouched(
        self, failing_write: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reporting an unrecognized violation as "Course not found." would send the caller
        after the wrong thing entirely - the failure `_fk_error_detail`'s own comment
        records for the dive routes."""
        monkeypatch.setattr(
            certifications_module.crud_certifications,
            "update",
            AsyncMock(side_effect=self._integrity_error("certification_user_id_fkey")),
        )

        with pytest.raises(IntegrityError):
            await certifications_module.patch_certification(
                request=MagicMock(),
                uuid=uuid7(),
                values=CertificationUpdateRequest.model_validate({"course_uuid": str(uuid7())}),
                current_user={"id": 1, "uuid": uuid7()},
                db=failing_write["db"],
            )


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

    def test_keeps_non_ascii_names(self) -> None:
        """`original_filename` is what the clients display, so a name that is entirely
        CJK or accented is stored as the diver wrote it. Making it safe for the download
        header is `content_disposition_attachment`'s job, not this one's."""
        assert safe_filename("潜水カード.jpg") == "潜水カード.jpg"


class TestContentDispositionHeader:
    """Starlette encodes header values as latin-1, so the non-ASCII names `safe_filename`
    keeps used to raise `UnicodeEncodeError` while building the response - a permanent 500
    on every download of that file, not a garbled filename."""

    def test_a_non_ascii_name_survives_starlette_header_encoding(self) -> None:
        """The regression itself: the value has to be latin-1 encodable, which is what
        `Response` does to every header on the way out."""
        header = content_disposition_attachment("潜水カード.jpg", default="card")

        response = Response(content=b"x", headers={"Content-Disposition": header})

        assert (b"content-disposition", header.encode("latin-1")) in response.raw_headers

    def test_the_real_name_rides_in_the_rfc_5987_parameter(self) -> None:
        header = content_disposition_attachment("潜水.jpg", default="card")

        assert "filename*=UTF-8''%E6%BD%9C%E6%B0%B4.jpg" in header

    def test_an_ascii_name_is_left_readable(self) -> None:
        header = content_disposition_attachment("card.jpg", default="card")

        assert header == "attachment; filename=\"card.jpg\"; filename*=UTF-8''card.jpg"

    def test_accents_fold_rather_than_vanish(self) -> None:
        """The plain parameter is all a client that ignores `filename*` gets (`curl -OJ`,
        notably), so it should still resemble the name the diver chose."""
        header = content_disposition_attachment("café.jpg", default="card")

        assert 'filename="cafe.jpg"' in header

    def test_a_name_that_folds_away_keeps_its_extension(self) -> None:
        """Nothing ASCII survives here, and a bare ".jpg" would download as a dotfile."""
        header = content_disposition_attachment("潜水.jpg", default="card")

        assert 'filename="card.jpg"' in header

    def test_fullwidth_punctuation_cannot_break_out_of_the_quoted_string(self) -> None:
        """NFKD maps `＂` onto a plain `"`, so folding re-introduces the character
        `safe_filename` had already stripped."""
        header = content_disposition_attachment("ca＂rd.jpg", default="card")

        assert header.count('"') == 2

    def test_falls_back_when_there_is_no_usable_name(self) -> None:
        header = content_disposition_attachment(None, default="dive-file")

        assert header == "attachment; filename=\"dive-file\"; filename*=UTF-8''dive-file"


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


class TestListOrderingSql:
    """`GET /certifications` orders `certified_on DESC NULLS LAST`, and the `NULLS LAST`
    is the whole point: Postgres's default for `DESC` is `NULLS FIRST`, which floats every
    dateless card above the diver's most recent one *and* leaves
    `ix_certification_user_id_certified_on` - built `NULLS LAST` - unable to serve the
    query.

    Asserts on the compiled statement, so it runs with no database. The behaviour that
    statement produces is pinned separately by `TestListOrderingAgainstPostgres`.
    """

    def _db(self) -> MagicMock:
        db = MagicMock()
        db.scalar = AsyncMock(return_value=0)
        db.execute = AsyncMock(return_value=MagicMock(mappings=lambda: []))
        return db

    @pytest.mark.asyncio
    async def test_orders_newest_first_with_dateless_cards_last(self) -> None:
        db = self._db()

        await get_certifications_page(db, user_id=1, offset=0, limit=10)

        statement = str(db.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
        assert "ORDER BY certification.certified_on DESC NULLS LAST, certification.uuid DESC" in statement

    @pytest.mark.asyncio
    async def test_is_scoped_to_the_caller_and_excludes_deleted_rows(self) -> None:
        db = self._db()

        await get_certifications_page(db, user_id=7, offset=0, limit=10)

        statement = str(db.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
        assert "certification.user_id = 7" in statement
        assert "certification.is_deleted IS false" in statement

    @pytest.mark.asyncio
    async def test_counts_the_same_rows_it_pages_over(self) -> None:
        # A `total_count` taken over a different set than the page would make
        # `has_more` lie - the count query has to carry the identical conditions.
        db = self._db()

        await get_certifications_page(db, user_id=7, offset=20, limit=10)

        count_statement = str(db.scalar.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
        assert "count(*)" in count_statement
        assert "certification.user_id = 7" in count_statement
        assert "certification.is_deleted IS false" in count_statement


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestListOrderingAgainstPostgres:
    """The same ordering, through the real reader and a real Postgres.

    `TestListOrderingSql` can only say what SQL we emit; whether `NULLS LAST` puts the
    dateless cards where a diver expects them is a property of the database, and this is
    the half that would have caught the bug had the list been written this way from the
    start.
    """

    @pytest.mark.asyncio
    async def test_dateless_cards_sort_below_every_dated_one(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        undated_first = create_certification(db, diver)
        older = create_certification(db, diver, certified_on=date(2015, 3, 2))
        undated_second = create_certification(db, diver)
        newest = create_certification(db, diver, certified_on=date(2024, 7, 19))

        page = await _read_certifications_uncached(
            request=None,
            user_id=diver.id,
            user_uuid=diver.uuid,
            db=async_db,
            page=1,
            items_per_page=10,
            course_id=None,
        )

        # Dated cards newest first, then the dateless ones - which tie on `certified_on`
        # and so fall back to `uuid DESC`, i.e. most recently created first.
        assert [row["name"] for row in page["data"]] == [
            newest.name,
            older.name,
            undated_second.name,
            undated_first.name,
        ]

    @pytest.mark.asyncio
    async def test_the_course_filter_narrows_to_the_cards_that_course_issued(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """What the course page reads. One course can issue several cards - TDI's combined
        Advanced Nitrox + Deco Procedures is the case - so the filter has to keep both and
        drop the unrelated one."""
        course = create_course(db, diver)
        first = create_certification(db, diver, course=course)
        second = create_certification(db, diver, course=course)
        unrelated = create_certification(db, diver)

        page = await get_certifications_page(db=async_db, user_id=diver.id, offset=0, limit=10, course_id=course.id)

        assert {row["name"] for row in page["data"]} == {first.name, second.name}
        assert unrelated.name not in {row["name"] for row in page["data"]}
        assert page["total_count"] == 2

    @pytest.mark.asyncio
    async def test_no_course_filter_returns_the_whole_list(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The default has to stay "all of them" - a filter that leaked into the unfiltered
        path would empty every diver's certification list."""
        course = create_course(db, diver)
        create_certification(db, diver, course=course)
        create_certification(db, diver)

        page = await get_certifications_page(db=async_db, user_id=diver.id, offset=0, limit=10)

        assert page["total_count"] == 2

    @pytest.mark.asyncio
    async def test_the_first_page_is_not_all_dateless_cards(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The user-visible shape of the bug: with `NULLS FIRST` a diver holding a few
        undated cards opens their list and sees only those, with the certification they
        were actually asked for pushed onto page two."""
        for _ in range(3):
            create_certification(db, diver)
        newest = create_certification(db, diver, certified_on=date(2024, 7, 19))

        page = await _read_certifications_uncached(
            request=None,
            user_id=diver.id,
            user_uuid=diver.uuid,
            db=async_db,
            page=1,
            items_per_page=2,
            course_id=None,
        )

        assert [row["name"] for row in page["data"]][0] == newest.name
        assert page["total_count"] == 4
        assert page["has_more"] is True
