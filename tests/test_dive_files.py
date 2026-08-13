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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, UploadFile
from jose import jwt
from uuid6 import uuid7

from src.app.core.security import ALGORITHM, SECRET_KEY, TokenType, create_dive_file_token, verify_dive_file_token
from src.app.core.utils.uploads import read_upload_within_limit
from src.app.schemas.dive import DiveFileInfo, DiveTechScalars
from src.app.schemas.dive_mixture import DiveMixtureRead, GasRole
from src.app.schemas.parsed_dive import DiveMixtureSchema, ParsedDiveSchema
from src.app.services import dive_parsers as parsers_module
from src.app.services.cache_invalidation import invalidate_dive_caches
from src.app.services.dive_files import (
    MAX_DIVE_FILE_SIZE,
    TECH_SCALAR_FIELDS,
    _ExistingRow,
    extract_tech_scalars,
    merge_mixture_fields,
    reconcile,
    store_dive_file,
)
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
        """The closed set `read_dive_file` may put in a `Content-Type` header.

        The bar is that a browser handed one of these can't be talked into *executing* it
        at the app's own origin - so nothing in the `text/*` family, and nothing
        `nosniff` wouldn't already pin down. `application/vnd.ant.fit` (the ANT+
        registered type for a FIT file) clears it more easily than the two before it: it
        is opaque binary with no renderer at all.
        """
        assert {parser.content_type for parser in parsers_module._PARSERS} <= {
            "application/xml",
            "application/json",
            "application/vnd.ant.fit",
        }


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


class TestProfileExtractionReleasesTheTransaction:
    """Sampling a FIT file is up to ~1.5 s of CPU in a worker thread. The event loop is
    free for that - `run_in_threadpool` bought that much - but the connection the lookups
    rode in on would otherwise sit idle-in-transaction for the whole of it, so a burst of
    FIT uploads ties up pool connections doing nothing.

    An ordering test rather than a behavioural one: what can regress here is somebody
    moving the extraction back above the release, and this is what would catch it.
    """

    @staticmethod
    def _session(calls: list[str]) -> AsyncMock:
        row = SimpleNamespace(uuid=uuid7(), updated_at=None)
        result = MagicMock()
        # `_find_by_digest` finds nothing, so the upload takes the insert path; the
        # `RETURNING` at the end of it hands back the new row.
        result.one_or_none.return_value = None
        result.one.return_value = row
        result.rowcount = 0

        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)
        db.rollback = AsyncMock(side_effect=lambda: calls.append("release"))
        db.commit = AsyncMock(side_effect=lambda: calls.append("commit"))
        return db

    @pytest.mark.asyncio
    async def test_releases_before_handing_the_file_to_the_thread(self, monkeypatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(
            "src.app.services.dive_files.extract_profile",
            lambda parser, data: calls.append("extract"),
        )

        user_uuid = uuid7()
        content = b"<Dive/>"
        await store_dive_file(
            self._session(calls),
            user_id=1,
            user_uuid=user_uuid,
            dive_id=7,
            upload=UploadFile(filename="export.xml", file=io.BytesIO(content)),
            file_token=create_dive_file_token(
                user_uuid=user_uuid,
                sha256=hashlib.sha256(content).hexdigest(),
                parser_key=SuuntoXmlParser.key,
            ),
        )

        assert calls.index("release") < calls.index("extract"), (
            f"the read transaction is still open during extraction: {calls}"
        )


class TestTechScalarExtraction:
    """`extract_tech_scalars` mirrors `extract_profile`'s contract: it reads the header
    off already-stored bytes, and it never fails the upload it rode in on."""

    def test_reads_the_scalars_off_a_parseable_file(self) -> None:
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <CnsStart>8</CnsStart><CnsEnd>9</CnsEnd>
  <OtuStart>22</OtuStart><OtuEnd>23</OtuEnd>
  <SurfacePressure>105700</SurfacePressure>
</Dive>
""".encode()

        assert extract_tech_scalars(SuuntoXmlParser, content) == {
            "cns_start": 8.0,
            "cns_end": 9.0,
            "otu_start": 22.0,
            "otu_end": 23.0,
            "surface_pressure_bar": 1.057,
        }

    def test_covers_exactly_the_columns_the_read_schema_publishes(self) -> None:
        """`TECH_SCALAR_FIELDS` is derived from `DiveTechScalars` rather than listed, so
        adding a field to the schema without a parser writing it would show up here
        rather than as a column that silently stays null forever."""
        assert set(TECH_SCALAR_FIELDS) == set(DiveTechScalars.model_fields)
        assert set(TECH_SCALAR_FIELDS) <= set(ParsedDiveSchema.model_fields)

    def test_an_unreadable_file_returns_none_rather_than_raising(self) -> None:
        """The upload must survive a header this build can't read: the file is the
        durable artifact, and refusing the attach would discard the very corpus entry
        needed to fix the parser. Note the catch is `DiveParseError`, *not*
        `EXTRACTION_ERRORS` - that tuple is what parsers catch internally before
        re-raising, and does not contain the parser errors themselves."""
        assert extract_tech_scalars(SuuntoXmlParser, b"<Dive><Unclosed>") is None

    def test_a_file_that_records_nothing_yields_an_all_null_write(self) -> None:
        """Distinct from the `None` above, and the distinction is load-bearing: this is
        "the file says nothing", which *clears* a previous export's readings, whereas
        `None` is "couldn't read" and leaves them alone."""
        content = f'<?xml version="1.0" encoding="utf-8"?><Dive xmlns="{SUUNTO_NS}"/>'.encode()

        assert extract_tech_scalars(SuuntoXmlParser, content) == dict.fromkeys(TECH_SCALAR_FIELDS)


class TestMixtureFieldMerge:
    """`merge_mixture_fields` decides whether a backfill may write onto stored cylinders.

    Mixtures are replaced wholesale on every save, so a stored row's `id` postdates the
    import and can't say which parsed cylinder it came from. Position is the only join
    available, and it is only trusted when the gas fractions still agree.
    """

    @staticmethod
    def _parsed(**overrides: object) -> DiveMixtureSchema:
        defaults: dict[str, object] = {
            "end_pressure": None,
            "gas_number": 1,
            "helium": 0.0,
            "name": None,
            "oxygen": 21.0,
            "po2_limit": 1.4,
            "role": None,
            "start_pressure": None,
            "volume": None,
        }
        return DiveMixtureSchema(**(defaults | overrides))  # type: ignore[arg-type]

    @staticmethod
    def _stored(mixture_id: int, **overrides: object) -> DiveMixtureRead:
        defaults: dict[str, object] = {"id": mixture_id, "volume": 12.0, "oxygen": 21.0, "helium": 0.0}
        return DiveMixtureRead(**(defaults | overrides))  # type: ignore[arg-type]

    def test_applies_positionally_when_every_pair_still_matches(self) -> None:
        parsed = [self._parsed(), self._parsed(oxygen=50.0, gas_number=2, po2_limit=1.6, role=GasRole.DECO)]
        stored = [self._stored(11), self._stored(12, oxygen=50.0)]

        assert merge_mixture_fields(parsed, stored) == [
            (11, {"po2_limit": 1.4, "gas_number": 1, "role": None}),
            (12, {"po2_limit": 1.6, "gas_number": 2, "role": GasRole.DECO}),
        ]

    def test_refuses_when_a_gas_fraction_no_longer_matches(self) -> None:
        """The diver swapped their deco bottle. Applying positionally would write the
        parsed second gas's ppO2 onto a cylinder that isn't it."""
        parsed = [self._parsed(), self._parsed(oxygen=50.0, gas_number=2)]
        stored = [self._stored(11), self._stored(12, oxygen=32.0)]

        assert merge_mixture_fields(parsed, stored) is None

    def test_refuses_when_the_counts_disagree(self) -> None:
        assert merge_mixture_fields([self._parsed()], [self._stored(11), self._stored(12)]) is None
        assert merge_mixture_fields([], []) is None

    def test_is_all_or_nothing_rather_than_per_row(self) -> None:
        """A half-matching list is an edited list, and half-applying to it would leave
        cylinders sourced from two different places with nothing recording which is which."""
        parsed = [self._parsed(), self._parsed(oxygen=50.0, gas_number=2)]
        stored = [self._stored(11), self._stored(12, oxygen=99.0)]

        assert merge_mixture_fields(parsed, stored) is None

    def test_a_fraction_the_file_never_recorded_is_not_a_mismatch(self) -> None:
        """The form filled in `DEFAULT_MIXTURE` because the file said nothing. That
        difference is not evidence the diver edited anything."""
        parsed = [self._parsed(oxygen=None, helium=None)]
        stored = [self._stored(11, oxygen=21.0)]

        assert merge_mixture_fields(parsed, stored) == [(11, {"po2_limit": 1.4, "gas_number": 1, "role": None})]


class TestScalarsAreWrittenAtAttach:
    """The import path owns these columns outright - the form cannot set them at all
    (`DiveTechScalars` is on the read shapes only), so this is the only write."""

    @staticmethod
    def _session() -> AsyncMock:
        row = SimpleNamespace(uuid=uuid7(), updated_at=None)
        result = MagicMock()
        result.one_or_none.return_value = None
        result.one.return_value = row
        result.rowcount = 0

        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)
        return db

    @staticmethod
    async def _attach(db: AsyncMock, content: bytes) -> None:
        user_uuid = uuid7()
        await store_dive_file(
            db,
            user_id=1,
            user_uuid=user_uuid,
            dive_id=7,
            upload=UploadFile(filename="export.xml", file=io.BytesIO(content)),
            file_token=create_dive_file_token(
                user_uuid=user_uuid,
                sha256=hashlib.sha256(content).hexdigest(),
                parser_key=SuuntoXmlParser.key,
            ),
        )

    @staticmethod
    def _captured_writes(monkeypatch) -> list[dict]:
        """What the attach path handed `store_tech_scalars`, if anything.

        Captured at that seam rather than by inspecting the emitted `UPDATE`: the
        decision under test is *what the import decided to write*, and reading it back
        off SQLAlchemy's statement internals would pin the assertion to how the write is
        spelled rather than to what it says.
        """
        writes: list[dict] = []

        async def capture(db, *, dive_id, scalars, commit=False):
            writes.append(scalars)

        monkeypatch.setattr("src.app.services.dive_files.store_tech_scalars", capture)
        return writes

    @pytest.mark.asyncio
    async def test_an_export_that_records_exposure_writes_it(self, monkeypatch) -> None:
        writes = self._captured_writes(monkeypatch)
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><CnsEnd>20</CnsEnd><SurfacePressure>105700</SurfacePressure></Dive>
""".encode()

        await self._attach(self._session(), content)

        assert writes == [
            {
                "cns_start": None,
                "cns_end": 20.0,
                "otu_start": None,
                "otu_end": None,
                "surface_pressure_bar": 1.057,
            }
        ]

    @pytest.mark.asyncio
    async def test_an_export_that_records_none_clears_what_was_there(self, monkeypatch) -> None:
        """Unconditional, unlike the profile write beside it: leaving a previous
        export's CNS on a dive whose file has been replaced would attribute a reading to
        bytes it didn't come from."""
        writes = self._captured_writes(monkeypatch)
        content = f'<?xml version="1.0" encoding="utf-8"?><Dive xmlns="{SUUNTO_NS}"/>'.encode()

        await self._attach(self._session(), content)

        assert writes == [dict.fromkeys(TECH_SCALAR_FIELDS)]

    @pytest.mark.asyncio
    async def test_an_unreadable_header_leaves_the_dive_alone(self, monkeypatch) -> None:
        """ "Couldn't read" is not "the file says nothing", so nothing is written and the
        upload still succeeds - the file is what a later backfill needs."""
        writes = self._captured_writes(monkeypatch)
        monkeypatch.setattr("src.app.services.dive_files.extract_tech_scalars", lambda parser, data: None)

        await self._attach(self._session(), b"<Dive/>")

        assert writes == []
