"""Unit tests for storing the dive-computer export a dive was imported from
(`models/dive_file.py`, `services/dive_files.py`, the dive-file token in
`core/security.py`, and the `/dive/{uuid}/file` routes).

Like `test_certifications.py`, these cover the pieces that are pure logic and so need no
database: the token that admits a file into storage, the parser metadata that decides
what it is recorded as, and the reconciliation that decides whether an upload is a
replacement, a no-op or a duplicate. Endpoint behaviour on top of a live Postgres/Redis
is exercised end to end by hand (see DECISIONS.md), not here.
"""

import hashlib
import io
import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, UploadFile
from jose import jwt
from uuid6 import uuid7

from src.app.core.security import ALGORITHM, SECRET_KEY, TokenType, create_dive_file_token, verify_dive_file_token
from src.app.core.utils.uploads import read_upload_within_limit
from src.app.schemas.dive import DiveFileInfo
from src.app.schemas.parsed_dive import ParsedDiveSchema
from src.app.services import dive_parsers as parsers_module
from src.app.services.cache_invalidation import invalidate_dive_caches
from src.app.services.dive_files import MAX_DIVE_FILE_SIZE, _ExistingRow, reconcile
from src.app.services.dive_parsers import PARSER_BY_KEY, UnsupportedDiveFileError, parse_dive_file_with_parser
from src.app.services.dive_parsers.base import DiveParser
from src.app.services.dive_parsers.suunto_json import SuuntoJsonParser
from src.app.services.dive_parsers.suunto_xml import SuuntoXmlParser

SUUNTO_NS = "http://schemas.datacontract.org/2004/07/Suunto.Diving.Dal"

VALID_SUUNTO_XML = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <MaxDepth>25.5</MaxDepth>
  <Duration>1800</Duration>
</Dive>
""".encode()

VALID_SUUNTO_JSON = b'{"DeviceLog": {"Header": {"Depth": {"Max": 25.5}, "Duration": 1800}}}'

USER_UUID = uuid_pkg.UUID("00000000-0000-0000-0000-0000000000aa")


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _existing(*, dive_id: int) -> _ExistingRow:
    return _ExistingRow(
        id=1,
        dive_id=dive_id,
        uuid=uuid7(),
        content_type="application/xml",
        byte_size=len(VALID_SUUNTO_XML),
        original_filename="export.xml",
        parser_key="suunto_xml",
        updated_at=None,
    )


class TestDiveFileToken:
    """The token is the whole admission control for `PUT /dive/{uuid}/file`: without it
    the endpoint would store any blob shaped like an export."""

    def test_round_trips_what_it_attests(self) -> None:
        token = create_dive_file_token(user_uuid=USER_UUID, sha256=_digest(VALID_SUUNTO_XML), parser_key="suunto_xml")

        claims = verify_dive_file_token(token)

        assert claims is not None
        assert claims.user_uuid == str(USER_UUID)
        assert claims.sha256 == _digest(VALID_SUUNTO_XML)
        assert claims.parser_key == "suunto_xml"

    def test_rejects_a_token_of_another_type(self) -> None:
        """An access token is signed with the same key and is held by the same client -
        only the `token_type` claim stops one being presented as a parse receipt."""
        access_like = jwt.encode(
            {
                "user_uuid": str(USER_UUID),
                "sha256": _digest(VALID_SUUNTO_XML),
                "parser_key": "suunto_xml",
                "exp": datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=30),
                "token_type": TokenType.ACCESS,
            },
            SECRET_KEY.get_secret_value(),
            algorithm=ALGORITHM,
        )

        assert verify_dive_file_token(access_like) is None

    def test_rejects_an_expired_token(self) -> None:
        expired = jwt.encode(
            {
                "user_uuid": str(USER_UUID),
                "sha256": _digest(VALID_SUUNTO_XML),
                "parser_key": "suunto_xml",
                "exp": datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1),
                "token_type": TokenType.DIVE_FILE,
            },
            SECRET_KEY.get_secret_value(),
            algorithm=ALGORITHM,
        )

        assert verify_dive_file_token(expired) is None

    def test_rejects_a_token_signed_with_another_key(self) -> None:
        """Forging one is the only way to have this server store bytes it never parsed."""
        forged = jwt.encode(
            {
                "user_uuid": str(USER_UUID),
                "sha256": _digest(b"arbitrary bytes"),
                "parser_key": "suunto_xml",
                "exp": datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=30),
                "token_type": TokenType.DIVE_FILE,
            },
            "not-the-servers-key",
            algorithm=ALGORITHM,
        )

        assert verify_dive_file_token(forged) is None

    def test_rejects_a_well_signed_token_missing_a_claim(self) -> None:
        incomplete = jwt.encode(
            {
                "user_uuid": str(USER_UUID),
                "exp": datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=30),
                "token_type": TokenType.DIVE_FILE,
            },
            SECRET_KEY.get_secret_value(),
            algorithm=ALGORITHM,
        )

        assert verify_dive_file_token(incomplete) is None

    def test_rejects_garbage(self) -> None:
        assert verify_dive_file_token("") is None
        assert verify_dive_file_token("not.a.jwt") is None

    def test_binds_the_hash_of_the_specific_bytes_parsed(self) -> None:
        """`store_dive_file` compares this against a digest of the body it receives, so a
        token for one file cannot admit another."""
        token = create_dive_file_token(user_uuid=USER_UUID, sha256=_digest(VALID_SUUNTO_XML), parser_key="suunto_xml")

        claims = verify_dive_file_token(token)

        assert claims is not None
        assert claims.sha256 != _digest(VALID_SUUNTO_JSON)


class TestParserMetadata:
    """`parser_key` is stored on every row and read back by future backfills, so the
    registry has to declare it consistently."""

    def test_every_parser_declares_a_key_and_a_content_type(self) -> None:
        for parser in parsers_module._PARSERS:
            assert parser.key, f"{parser.__name__} has no key"
            assert parser.content_type, f"{parser.__name__} has no content_type"

    def test_keys_are_unique(self) -> None:
        keys = [parser.key for parser in parsers_module._PARSERS]

        assert len(keys) == len(set(keys))

    def test_parser_by_key_covers_the_registry(self) -> None:
        """`store_dive_file` resolves a token's `parser_key` through this map to get the
        `content_type` it serves the file back as; a gap would reject a valid import."""
        assert PARSER_BY_KEY == {parser.key: parser for parser in parsers_module._PARSERS}

    def test_content_types_are_ones_we_are_willing_to_serve(self) -> None:
        for parser in parsers_module._PARSERS:
            assert parser.content_type in {"application/xml", "application/json"}


class TestParseDiveFileWithParser:
    def test_returns_the_parser_that_read_the_file(self) -> None:
        parser, parsed = parse_dive_file_with_parser("export.xml", VALID_SUUNTO_XML)

        assert parser is SuuntoXmlParser
        assert parsed.max_depth == 25.5

    def test_picks_the_json_parser_for_a_json_export(self) -> None:
        parser, _ = parse_dive_file_with_parser("export.json", VALID_SUUNTO_JSON)

        assert parser is SuuntoJsonParser

    def test_reports_the_parser_that_succeeded_not_the_one_that_matched(self, monkeypatch) -> None:
        """A parser may recognize a file and then find it isn't really its format, in
        which case the next candidate gets a turn. Recording the first *match* would
        label the stored file with a parser that never read it."""

        class GreedyParser(DiveParser):
            key = "greedy"
            content_type = "application/xml"

            @classmethod
            def can_parse(cls, filename: str, content: bytes) -> bool:
                return True

            @classmethod
            def parse(cls, content: bytes) -> ParsedDiveSchema:
                raise UnsupportedDiveFileError("not mine after all")

        monkeypatch.setattr(parsers_module, "_PARSERS", [GreedyParser, SuuntoXmlParser])

        parser, parsed = parse_dive_file_with_parser("export.xml", VALID_SUUNTO_XML)

        assert parser is SuuntoXmlParser
        assert parsed.max_depth == 25.5

    def test_raises_when_nothing_recognizes_the_file(self) -> None:
        with pytest.raises(UnsupportedDiveFileError):
            parse_dive_file_with_parser("notes.csv", b"time,depth\n0,0\n")


class TestReconciliation:
    """Three outcomes, and only three, because `dive_file.dive_id` is NOT NULL: every
    stored row belongs to a dive the diver can still reach."""

    def test_unseen_bytes_are_inserted(self) -> None:
        assert reconcile(None, dive_id=1) == "insert"

    def test_re_uploading_the_same_file_to_the_same_dive_is_a_noop(self) -> None:
        """`PUT` is idempotent, and a form that saves twice must not churn the row."""
        assert reconcile(_existing(dive_id=1), dive_id=1) == "noop"

    def test_the_same_file_on_another_dive_is_a_conflict(self) -> None:
        """The realistic cause is logging one export as two dives. Reported rather than
        resolved: re-pointing would silently strip the file off the dive that has it."""
        assert reconcile(_existing(dive_id=2), dive_id=1) == "conflict"


class TestDiveFileInfoShape:
    def test_never_exposes_file_bytes(self) -> None:
        """The read schema is metadata only - the bytes have exactly one way out, and it
        is the download route."""
        assert "data" not in DiveFileInfo.model_fields

    def test_carries_what_the_ui_needs_to_describe_the_file(self) -> None:
        assert {"original_filename", "byte_size", "parser_key"} <= set(DiveFileInfo.model_fields)


class TestUploadSizeGuard:
    """Shared with card uploads; asserted here too because a dive file's limit differs."""

    @staticmethod
    def _upload(content: bytes) -> UploadFile:
        return UploadFile(filename="export.xml", file=io.BytesIO(content))

    @pytest.mark.asyncio
    async def test_accepts_a_file_within_the_limit(self) -> None:
        content = b"a" * 1024

        assert await read_upload_within_limit(self._upload(content), MAX_DIVE_FILE_SIZE) == content

    @pytest.mark.asyncio
    async def test_rejects_a_file_over_the_limit_with_413(self) -> None:
        upload = self._upload(b"a" * (MAX_DIVE_FILE_SIZE + 1))

        with pytest.raises(HTTPException) as exc_info:
            await read_upload_within_limit(upload, MAX_DIVE_FILE_SIZE)

        assert exc_info.value.status_code == 413

    def test_matches_the_limit_the_parse_endpoint_reads_under(self) -> None:
        """A lower limit here would let a file pre-fill a form and then be refused
        storage - the diver would have no way to tell why."""
        assert MAX_DIVE_FILE_SIZE == 5 * 1024 * 1024


class TestCacheInvalidation:
    @pytest.mark.asyncio
    async def test_a_file_change_drops_the_dive_reads_that_embed_it(self, monkeypatch) -> None:
        """`DiveReadWithMixtures` carries `source_file`, so attaching or deleting one
        makes the cached single-dive read stale."""
        patterns: list[str] = []
        monkeypatch.setattr(
            "src.app.services.cache_invalidation.delete_keys_by_pattern",
            AsyncMock(side_effect=lambda pattern: patterns.append(pattern)),
        )

        await invalidate_dive_caches(7)

        matches = lambda key: any(fnmatch(key, p) for p in patterns)  # noqa: E731
        assert matches("user_7_dive:019f-abc")
        # Another user's keys, and other resources', must be left alone.
        assert not matches("user_8_dive:019f-abc")
        assert not matches("user_7_certification:019f-abc")
