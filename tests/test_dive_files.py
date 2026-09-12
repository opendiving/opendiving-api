"""Unit tests for storing the dive-computer exports a dive's recordings were read from
(`models/dive_file.py`, `services/dive_files.py`, the dive-file token in
`core/security.py`, and the `/dive/{uuid}/file/{fid}` routes).

Like `test_certifications.py`, these cover the pieces that are pure logic and so need no
database: the token that admits a file into storage, the parser metadata that decides
what it is recorded as, and the reconciliation that decides whether an upload is a
replacement, a no-op or a duplicate. Endpoint behaviour on top of a live Postgres/Redis
is exercised end to end by hand (see DECISIONS.md), not here.
"""

import hashlib
import io
import uuid as uuid_pkg
from dataclasses import astuple
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, UploadFile
from jose import jwt
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from uuid6 import uuid7

from src.app.core.security import ALGORITHM, SECRET_KEY, TokenType, create_dive_file_token, verify_dive_file_token
from src.app.core.utils.uploads import read_upload_within_limit
from src.app.crud.crud_dive_mixtures import get_mixtures_for_dive, get_mixtures_for_dives
from src.app.models.dive import Dive
from src.app.schemas.dive import DiveFileInfo, DiveTechScalars
from src.app.schemas.dive_mixture import DiveMixtureRead, GasRole
from src.app.schemas.parsed_dive import DiveMixtureSchema, ParsedDiveSchema
from src.app.services import dive_files as dive_files_module
from src.app.services import dive_parsers as parsers_module
from src.app.services.blob_store import new_key
from src.app.services.cache_invalidation import invalidate_dive_caches
from src.app.services.dive_files import (
    KEY_KIND as DIVE_FILE_KIND,
)
from src.app.services.dive_files import (
    MAX_DIVE_FILE_SIZE,
    TECH_SCALAR_FIELDS,
    LoadedDiveFile,
    RecordingExtraction,
    _ExistingRow,
    _extract_all,
    _rederive_recording,
    backfill_tech_fields,
    extract_recording,
    extract_tech_scalars,
    fill_mixture_fields,
    fill_parsed_mixtures,
    merge_mixture_fields,
    reconcile,
    store_recording_file,
)
from src.app.services.dive_parsers import (
    PARSER_BY_KEY,
    DiveParseError,
    UnsupportedDiveFileError,
    parse_dive_file_with_parser,
)
from src.app.services.dive_parsers.base import DiveParser
from src.app.services.dive_parsers.fit import FitParser
from src.app.services.dive_parsers.suunto_json import SuuntoJsonParser
from src.app.services.dive_parsers.suunto_xml import SuuntoXmlParser
from tests.helpers.fit import dive_fit_file

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


def _existing(*, dive_id: int, content: bytes = VALID_SUUNTO_XML) -> _ExistingRow:
    row_uuid = uuid7()
    return _ExistingRow(
        id=1,
        dive_id=dive_id,
        recording_id=1,
        uuid=row_uuid,
        content_type="application/xml",
        byte_size=len(content),
        original_filename="export.xml",
        parser_key="suunto_xml",
        # A real key for these bytes, so the `noop` branch's self-heal rewrites the file
        # the row actually names rather than an invented path.
        storage_key=new_key(DIVE_FILE_KIND, sha256=_digest(content)),
        updated_at=None,
    )


class TestDiveFileToken:
    """The token is the whole admission control for `POST /dive/{uuid}/recordings`: without it
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
        """`store_recording_file` compares this against a digest of the body it receives, so a
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
        """`store_recording_file` resolves a token's `parser_key` through this map to get the
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
    stored row belongs to a dive the diver can still reach.

    **The comparison stayed on the dive when files moved onto recordings**, and that is a
    decision rather than an oversight: `ux_dive_file_user_id_sha256` is per diver, so one set
    of bytes is one row in one recording of one dive. Asking about the recording would answer
    "insert" for bytes already stored against a *second* recording of the same dive, and the
    insert would then die on that index.
    """

    def test_unseen_bytes_are_inserted(self) -> None:
        assert reconcile(None, dive_id=1) == "insert"

    def test_re_uploading_the_same_file_to_the_same_dive_is_a_noop(self) -> None:
        """Attaching is idempotent for identical bytes, and a form that saves twice must
        not churn the row."""
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
        """`DiveReadWithMixtures` carries `recordings`, so attaching or deleting a file
        makes the cached single-dive read stale."""
        patterns: list[str] = []
        monkeypatch.setattr(
            "src.app.services.cache_invalidation.delete_keys_by_pattern",
            AsyncMock(side_effect=lambda pattern: patterns.append(pattern)),
        )

        await invalidate_dive_caches(7)

        matches = lambda key: any(fnmatch(key, p) for p in patterns)  # noqa: E731
        assert matches("user_7_dive:019f-abc")
        # **A suffixed key too.** The detail read carried a `:v2` for a day and no longer
        # does, but the shape of that sweep is the live part: a suffix after the colon stays
        # inside `user_{id}_dive:*`, where an underscore-joined `user_7_dive_v2:...` falls
        # outside both patterns - and nothing would say so, the invalidation would simply
        # stop working on the detail read.
        assert matches("user_7_dive:v2:019f-abc")
        # Another user's keys, and other resources', must be left alone.
        assert not matches("user_8_dive:019f-abc")
        assert not matches("user_8_dive:v2:019f-abc")
        assert not matches("user_7_certification:019f-abc")
        # The dive *site* list is why there are two literal patterns rather than one
        # `user_{id}_dive*`, and a version suffix must not have quietly widened either.
        assert not matches("user_7_dive_sites:page_1")


def _attach_result() -> MagicMock:
    """One mock result that is plausible for every statement the attach path issues.

    The dedupe lookup finds nothing (`one_or_none`), the dive has no recordings yet
    (iteration), the recording insert returns an id (`scalar_one`), and the recording holds
    no files when the re-derivation reads them back (`all`) - which is the shape a mocked
    session can honestly represent, since no row it "wrote" is really there.
    """
    result = MagicMock()
    result.one_or_none.return_value = None
    result.one.return_value = SimpleNamespace(uuid=uuid7(), updated_at=None)
    result.scalar_one.return_value = 1
    result.scalar_one_or_none.return_value = 0
    result.scalars.return_value = []
    result.all.return_value = []
    result.__iter__.return_value = iter(())
    result.rowcount = 0
    return result


class TestProfileExtractionReleasesTheTransaction:
    """Sampling a FIT file is up to ~1.5 s of CPU in a worker thread. The event loop is
    free for that - `run_in_threadpool` bought that much - but the connection the lookups
    rode in on would otherwise sit idle-in-transaction for the whole of it, so a burst of
    FIT uploads ties up pool connections doing nothing.

    An ordering test rather than a behavioural one: what can regress here is somebody
    moving an extraction back above the release, and this is what would catch it.

    **The attach path releases twice, and the assertion below is written to see the second
    one.** Recordings gave it a second hop - the incoming file is parsed to decide where it
    lands, and then the whole recording is parsed to derive what comes off it - and the
    lookups in between reopen the transaction the first release closed. `calls.index()`
    reports the *first* occurrence of each, so a test written that way is satisfied by the
    first release alone and blind to a second hop taken with a connection pinned.
    """

    @staticmethod
    def _session(calls: list[str]) -> AsyncMock:
        result = _attach_result()

        async def execute(*args: object, **kwargs: object) -> MagicMock:
            calls.append("query")
            return result

        db = AsyncMock()
        db.execute = execute
        db.rollback = AsyncMock(side_effect=lambda: calls.append("release"))
        db.commit = AsyncMock(side_effect=lambda: calls.append("commit"))
        return db

    @pytest.mark.asyncio
    async def test_releases_before_handing_the_file_to_the_thread(self, monkeypatch) -> None:
        calls: list[str] = []
        real_extract_all = dive_files_module._extract_all
        real_extract_recording = dive_files_module.extract_recording

        def first_hop(parser, data):
            calls.append("extract")
            return real_extract_all(parser, data)

        def second_hop(files, known=None):
            calls.append("extract")
            return real_extract_recording(files, known)

        monkeypatch.setattr("src.app.services.dive_files._extract_all", first_hop)
        monkeypatch.setattr("src.app.services.dive_files.extract_recording", second_hop)

        user_uuid = uuid7()
        content = b"<Dive/>"
        await store_recording_file(
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

        # Both hops, and each against the release that precedes *it*: what regresses here is a
        # query issued between a release and the extraction after it, which reopens the
        # transaction and pins the connection for the parse. A positional `index()` cannot
        # express that pairing - it reports the first release and the first extraction and is
        # satisfied by them however the rest is ordered.
        assert calls.count("extract") == 2, f"the attach path takes two extraction hops: {calls}"
        for position, call in enumerate(calls):
            if call == "extract":
                assert calls[position - 1] in ("release", "extract"), (
                    f"an extraction ran with the read transaction reopened: {calls}"
                )


class TestExtractAllSharesOneDecode:
    """`_extract_all` goes through `parse_all`, and falls back when that fails.

    The point of the fallback is the property the two extractions have separately and
    `parse_all` cannot: a file whose samples are malformed still yields its header
    scalars. Losing it would be invisible - the dive would just quietly stop carrying
    CNS and OTU whenever its profile was unreadable.
    """

    XML = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><CnsEnd>20</CnsEnd><SurfacePressure>105700</SurfacePressure></Dive>
""".encode()

    def test_prefers_the_shared_decode(self) -> None:
        calls: list[str] = []

        class Sharing(SuuntoXmlParser):
            @classmethod
            def parse_all(cls, content):
                calls.append("parse_all")
                return super().parse_all(content)

        extraction = _extract_all(Sharing, self.XML)

        assert calls == ["parse_all"]
        assert extraction.scalars is not None and extraction.scalars["cns_end"] == 20.0
        assert extraction.profile is None  # this file carries no samples

    def test_a_failed_shared_decode_still_yields_the_half_that_works(self) -> None:
        """The case the fallback exists for. A `parse_all` that dies takes both halves
        with it; the two methods behind it do not, so the header survives."""

        class BrokenTogether(SuuntoXmlParser):
            @classmethod
            def parse_all(cls, content):
                raise DiveParseError("samples are unreadable, and this took the header too")

        extraction = _extract_all(BrokenTogether, self.XML)

        assert extraction.profile is None
        assert extraction.scalars is not None and extraction.scalars["cns_end"] == 20.0

    def test_the_fallback_covers_an_unexpected_failure_too(self) -> None:
        """Not just the two parser exceptions: an override is third-party code as far as
        this function is concerned, and a `TypeError` out of it must not fail an upload
        that the two methods behind it would have served."""

        class Exploding(SuuntoXmlParser):
            @classmethod
            def parse_all(cls, content):
                raise TypeError("an override with a bug in it")

        extraction = _extract_all(Exploding, self.XML)

        assert extraction.scalars is not None and extraction.scalars["cns_end"] == 20.0

    def test_fit_decodes_once_where_it_used_to_decode_twice(self) -> None:
        """The whole point of the override. Counted rather than timed - a wall-clock
        assertion would be flaky on a loaded machine, and the scan count is the actual
        claim."""
        scans = 0
        original = FitParser._scan.__func__  # type: ignore[attr-defined]

        class Counting(FitParser):
            @classmethod
            def _scan(cls, content):
                nonlocal scans
                scans += 1
                return original(cls, content)

        _extract_all(Counting, dive_fit_file(end_cns=9, o2_toxicity=23))

        assert scans == 1

    def test_the_shared_decode_returns_what_the_two_methods_would_have(self) -> None:
        """Pinned on FIT specifically, since it is the one parser where `parse_all` is a
        different code path rather than a delegation."""
        content = dive_fit_file(end_cns=9, o2_toxicity=23)

        dive, samples = FitParser.parse_all(content)

        assert dive == FitParser.parse(content)
        assert samples == FitParser.parse_profile(content)


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
            # A DM5 XML export carries no GPS at all - no file in the 384-export corpus
            # has a coordinate anywhere in it - so this format contributes the columns
            # and never a value.
            "entry_latitude": None,
            "entry_longitude": None,
            "exit_latitude": None,
            "exit_longitude": None,
        }

    def test_covers_exactly_the_columns_the_read_schema_publishes(self) -> None:
        """`TECH_SCALAR_FIELDS` is derived from `DiveTechScalars` rather than listed, so
        adding a field to the schema without a parser writing it would show up here
        rather than as a column that silently stays null forever."""
        assert set(TECH_SCALAR_FIELDS) == set(DiveTechScalars.model_fields)
        assert set(TECH_SCALAR_FIELDS) <= set(ParsedDiveSchema.model_fields)
        # The other direction, and the one that fails in production rather than in CI:
        # these names are spread into `update(Dive).values(**scalars)`, so a field on
        # `DiveTechScalars` that is not a `Dive` column raises at attach time, on a real
        # upload, rather than anywhere a developer would see it first.
        assert set(TECH_SCALAR_FIELDS) <= set(Dive.__table__.columns.keys())

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
        """The first cylinder's `role` is absent from its update rather than written as
        `None` - see `test_a_field_the_file_does_not_record_is_never_written`."""
        parsed = [self._parsed(), self._parsed(oxygen=50.0, gas_number=2, po2_limit=1.6, role=GasRole.DECO)]
        stored = [self._stored(11), self._stored(12, oxygen=50.0)]

        assert merge_mixture_fields(parsed, stored) == [
            (11, {"po2_limit": 1.4, "gas_number": 1}),
            (12, {"po2_limit": 1.6, "gas_number": 2, "role": GasRole.DECO}),
        ]

    def test_a_field_the_file_does_not_record_is_never_written(self) -> None:
        """Fill-only, and it is `DiveMixtureSchema`'s own rule applied to the write: `None`
        means "the file did not record this", so spreading one into an `UPDATE` turns an
        absent reading into a value.

        All three fields are client-writable, which is what makes it data loss rather than
        a tidiness point. A FIT import produces `po2_limit=None` always and `role=None` for
        any open-circuit gas; the diver then sets 1.6 and `deco` on their stage bottle
        through the form, touching neither fraction - so the guard above still admits the
        join, and an overwriting backfill would put both back to `NULL` on a script whose
        docstring calls it safe to run repeatedly.
        """
        parsed = [self._parsed(po2_limit=None, role=None, gas_number=None)]
        stored = [self._stored(11, po2_limit=1.6, role=GasRole.DECO, gas_number=0)]

        # Nothing to write at all, so the row is absent rather than carrying an empty dict.
        assert merge_mixture_fields(parsed, stored) == []

    def test_a_recorded_value_still_overwrites_what_is_stored(self) -> None:
        """What fill-only does *not* cost: where the file records a value the backfill
        still owns it, so a parser correction lands on the next run. It declines only where
        the file has nothing to say."""
        parsed = [self._parsed(po2_limit=1.6, role=GasRole.DECO)]
        stored = [self._stored(11, po2_limit=1.4, role=GasRole.BOTTOM)]

        assert merge_mixture_fields(parsed, stored) == [(11, {"po2_limit": 1.6, "gas_number": 1, "role": GasRole.DECO})]

    def test_refuses_when_a_gas_fraction_no_longer_matches(self) -> None:
        """The diver swapped their deco bottle. Applying positionally would write the
        parsed second gas's ppO2 onto a cylinder that isn't it."""
        parsed = [self._parsed(), self._parsed(oxygen=50.0, gas_number=2)]
        stored = [self._stored(11), self._stored(12, oxygen=32.0)]

        assert merge_mixture_fields(parsed, stored) is None

    def test_refuses_when_the_counts_disagree(self) -> None:
        assert merge_mixture_fields([self._parsed()], [self._stored(11), self._stored(12)]) is None
        assert merge_mixture_fields([], []) is None
        # The diver deleted every cylinder. Refused like any other count mismatch - and
        # the one the report used to count as zero mixtures skipped, because it sized the
        # skip off `stored`, which is empty here. See `backfill_tech_fields`.
        assert merge_mixture_fields([self._parsed()], []) is None

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

        assert merge_mixture_fields(parsed, stored) == [(11, {"po2_limit": 1.4, "gas_number": 1})]

    def test_a_fraction_the_stored_row_never_recorded_is_not_a_mismatch_either(self) -> None:
        """The mirror of the case above, and the one the columns becoming nullable created.
        The import no longer defaults a mix the document never carried, so a stored row can
        say "not recorded" in exactly the way a parsed one always could - and a guard that
        only looked at the parsed side would read `parsed 21` against `stored NULL` as two
        different gases and refuse every such dive.
        """
        parsed = [self._parsed(oxygen=21.0, helium=0.0)]
        stored = [self._stored(11, oxygen=None, helium=None)]

        assert merge_mixture_fields(parsed, stored) == [(11, {"po2_limit": 1.4, "gas_number": 1})]

    def test_a_fraction_both_sides_recorded_is_still_compared(self) -> None:
        """What the widened guard does not cost: a real disagreement is still a refusal."""
        parsed = [self._parsed(oxygen=50.0)]
        stored = [self._stored(11, oxygen=32.0)]

        assert merge_mixture_fields(parsed, stored) is None

    def test_the_fraction_guard_cannot_catch_a_mis_ordered_all_null_list(self) -> None:
        """Why `get_mixtures_for_dive` has to order by `id`, stated as a test.

        A 2026 Suunto Ocean export reconstructs its cylinders from sample data, which
        carries gas numbers and pressures but no fractions at all - so every parsed row is
        `oxygen=None, helium=None`, the guard above compares nothing, and *both* orderings
        below are accepted. The ordering of `stored` is the only thing deciding which
        cylinder gets `gas_number=0`, and `gas_number` is the join key to the profile's
        per-cylinder pressure channels: swap it and each tank's curve is attributed to the
        other one. Four exports in the corpus are exactly this shape.
        """
        parsed = [
            self._parsed(oxygen=None, helium=None, gas_number=0),
            self._parsed(oxygen=None, helium=None, gas_number=1),
        ]

        in_order = merge_mixture_fields(parsed, [self._stored(11), self._stored(12)])
        reversed_order = merge_mixture_fields(parsed, [self._stored(12), self._stored(11)])

        assert in_order is not None and reversed_order is not None
        assert [(mixture_id, values["gas_number"]) for mixture_id, values in in_order] == [(11, 0), (12, 1)]
        assert [(mixture_id, values["gas_number"]) for mixture_id, values in reversed_order] == [(12, 0), (11, 1)]


class TestMixtureFieldFill:
    """`fill_mixture_fields` decides what a second reading of one recording may *add*.

    The other half of the pair, and the difference is the whole reason there are two: the
    backfill above asks whether it may write over stored cylinders, this asks what it may put
    into the blanks of cylinders that already exist. The corpus case is a Suunto Ocean JSON
    whose one cylinder carries pressures and no gas fraction, meeting the same computer's FIT
    which carries `oxygen` 33 and no pressures.
    """

    @staticmethod
    def _parsed(**overrides: object) -> DiveMixtureSchema:
        defaults: dict[str, object] = {
            "end_pressure": None,
            "gas_number": 1,
            "helium": None,
            "oxygen": None,
            "po2_limit": None,
            "role": None,
            "start_pressure": None,
            "volume": None,
        }
        return DiveMixtureSchema(**(defaults | overrides))  # type: ignore[arg-type]

    @staticmethod
    def _stored(mixture_id: int, **overrides: object) -> DiveMixtureRead:
        defaults: dict[str, object] = {"id": mixture_id, "gas_number": 0}
        return DiveMixtureRead(**(defaults | overrides))  # type: ignore[arg-type]

    def test_a_blank_member_fills_and_a_recorded_one_does_not(self) -> None:
        """The corpus pair, as one call. `oxygen` lands because the row has none; the
        pressures do not, because it has them - and they differ, which is what makes this an
        assertion about the rule rather than about two equal numbers."""
        parsed = [self._parsed(oxygen=33.0, helium=0.0, start_pressure=210.0, end_pressure=50.0, volume=11.1)]
        stored = [self._stored(11, start_pressure=207.34, end_pressure=47.47)]

        assert fill_mixture_fields(parsed, stored) == [{"oxygen": 33.0, "helium": 0.0, "volume": 11.1}]

    def test_the_three_members_it_never_writes(self) -> None:
        """`po2_limit` and `role` are the diver's plan rather than the tank's contents, and
        `gas_number` is the join key the stored profile's pressure channels are already
        attributed under - a second file's own labelling would rename the cylinder those
        curves hang off. All three are blank on the stored row here and stay blank."""
        parsed = [self._parsed(po2_limit=1.4, role=GasRole.DECO, gas_number=1)]
        stored = [self._stored(11, gas_number=None)]

        assert fill_mixture_fields(parsed, stored) == [{}]

    def test_one_answer_per_stored_row_in_order(self) -> None:
        """The result indexes alongside `stored`, so a row with nothing to add is an empty
        dict rather than an absence - which is what lets the caller zip the two."""
        parsed = [self._parsed(), self._parsed(oxygen=50.0, gas_number=2)]
        stored = [self._stored(11), self._stored(12)]

        assert fill_mixture_fields(parsed, stored) == [{}, {"oxygen": 50.0}]

    def test_refuses_when_the_counts_disagree(self) -> None:
        assert fill_mixture_fields([self._parsed()], [self._stored(11), self._stored(12)]) is None
        assert fill_mixture_fields([], []) is None
        assert fill_mixture_fields([self._parsed()], []) is None

    def test_refuses_when_a_fraction_both_sides_recorded_disagrees(self) -> None:
        """A different gas in that position is a different cylinder, and the positional join
        has nothing else to go on."""
        parsed = [self._parsed(oxygen=50.0, volume=11.1)]
        stored = [self._stored(11, oxygen=32.0)]

        assert fill_mixture_fields(parsed, stored) is None

    def test_a_fraction_only_one_side_recorded_is_not_a_disagreement(self) -> None:
        """Which is the case this function exists for: the stored row's `oxygen` being null
        is precisely why there is something to fill."""
        parsed = [self._parsed(oxygen=33.0)]
        stored = [self._stored(11, oxygen=None, helium=0.0)]

        assert fill_mixture_fields(parsed, stored) == [{"oxygen": 33.0}]

    def test_a_fill_the_table_would_reject_is_dropped(self) -> None:
        """`end_pressure <= start_pressure` and `oxygen + helium <= 100` are pair
        constraints, so filling one half against a stored other half can compose a row the
        database refuses - and `CHECK` is not deferrable, so it would arrive as an
        `IntegrityError` mid-attach rather than anywhere either caller could recover.
        """
        pressures = fill_mixture_fields([self._parsed(end_pressure=220.0)], [self._stored(11, start_pressure=200.0)])
        fractions = fill_mixture_fields([self._parsed(helium=60.0)], [self._stored(11, oxygen=50.0)])

        assert pressures == [{}]
        assert fractions == [{}]

    def test_a_rejected_fill_costs_only_its_own_row(self) -> None:
        """Per row, unlike `merge_mixture_fields`' all-or-nothing refusal, and the difference
        is that nothing here is being overwritten: a filled cylinder beside an unfilled one is
        two rows each carrying what it always did."""
        parsed = [self._parsed(end_pressure=220.0), self._parsed(oxygen=33.0, gas_number=2)]
        stored = [self._stored(11, start_pressure=200.0), self._stored(12)]

        assert fill_mixture_fields(parsed, stored) == [{}, {"oxygen": 33.0}]

    def test_a_stored_row_with_nothing_at_all_takes_the_whole_cylinder(self) -> None:
        """The bound checks are on the *result*, not on the fill, so a row that fills every
        member is admitted as long as the cylinder it composes is one the table would take."""
        parsed = [self._parsed(oxygen=33.0, helium=0.0, volume=11.1, start_pressure=207.34, end_pressure=47.47)]

        assert fill_mixture_fields(parsed, [self._stored(11)]) == [
            {"oxygen": 33.0, "helium": 0.0, "volume": 11.1, "start_pressure": 207.34, "end_pressure": 47.47}
        ]


class TestARecordingsCylindersFillMemberByMember:
    """`fill_parsed_mixtures`, which is the same rule one level up: across a recording's own
    files rather than between a file and the dive's rows.

    Per member rather than per list, and that is the whole of it. Taking the first list whole
    lands the JSON's pressures and drops the FIT's `oxygen` - the one gas fraction the corpus
    pair records anywhere - so the dive's only cylinder ends up with no mix at all.
    """

    @staticmethod
    def _mix(**overrides: object) -> DiveMixtureSchema:
        return TestMixtureFieldFill._parsed(**overrides)

    def test_each_member_comes_from_the_first_file_that_recorded_it(self) -> None:
        earlier = [self._mix(gas_number=0, start_pressure=207.34, end_pressure=47.47)]
        later = [self._mix(gas_number=1, oxygen=33.0, start_pressure=210.0)]

        filled = fill_parsed_mixtures(earlier, later)

        assert [(row.oxygen, row.start_pressure, row.end_pressure, row.gas_number) for row in filled] == [
            (33.0, 207.34, 47.47, 0)
        ]

    def test_a_first_file_with_no_cylinders_takes_the_later_ones_whole(self) -> None:
        later = [self._mix(gas_number=1, oxygen=33.0)]

        assert fill_parsed_mixtures([], later) == later

    def test_cylinders_that_cannot_be_joined_leave_the_earlier_list_alone(self) -> None:
        """The earlier list is what the recording's stored profile is already labelled
        against, so a later file describing other cylinders contributes nothing rather than
        replacing it."""
        earlier = [self._mix(gas_number=0, oxygen=32.0)]
        later = [self._mix(gas_number=1, oxygen=50.0), self._mix(gas_number=2, oxygen=21.0)]

        assert fill_parsed_mixtures(earlier, later) == earlier


class TestStoredMixturesAreReadInSavedOrder:
    """The `ORDER BY` that `merge_mixture_fields`' positional join rests on.

    Asserted on the compiled SQL rather than against a live Postgres, like the rest of
    this suite. That is the whole of the guarantee anyway: without the clause Postgres may
    return heap order, and heap order stops matching insertion order as soon as a row is
    updated in place - which `backfill_tech_fields` does to these very rows.
    """

    class _RecordingSession:
        """Captures the statements handed to `execute` and returns an empty result."""

        def __init__(self) -> None:
            self.statements: list[object] = []

        async def execute(self, statement: object) -> MagicMock:
            self.statements.append(statement)
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

    @staticmethod
    def _sql(statement: object) -> str:
        return str(
            statement.compile(  # type: ignore[attr-defined]
                dialect=postgresql.dialect(paramstyle="named"), compile_kwargs={"literal_binds": True}
            )
        )

    @pytest.mark.asyncio
    async def test_the_single_dive_read_orders_by_id(self) -> None:
        session = self._RecordingSession()

        await get_mixtures_for_dive(session, 7)  # type: ignore[arg-type]

        assert "ORDER BY dive_mixture.id" in self._sql(session.statements[0])

    @pytest.mark.asyncio
    async def test_the_batched_read_orders_by_id_too(self) -> None:
        """So a dive's cylinders come back the same way whether the list endpoint or the
        detail endpoint asked for them."""
        session = self._RecordingSession()

        await get_mixtures_for_dives(session, [7, 8])  # type: ignore[arg-type]

        assert "ORDER BY dive_mixture.id" in self._sql(session.statements[0])


class TestReExtractionFailureDoesNotFailTheRequest:
    """The `noop` branch's handler has to sit around the *writes*, not the commit.

    A `CHECK` is not deferrable in Postgres, so it is evaluated as the `UPDATE` runs and
    `IntegrityError` comes out of `execute()`. A `try` wrapped around `commit()` alone -
    which is what the first version of this handler did - catches nothing, the request
    500s, and the session is left in a failed transaction for whatever runs next.
    """

    XML = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><CnsEnd>20</CnsEnd></Dive>
""".encode()

    @staticmethod
    def _session_for_reupload(*, files: list[object] | None = None) -> AsyncMock:
        """A session whose dedupe lookup already holds this recording's file, so the attach
        takes the `noop` branch.

        `files` is what the re-derivation reads back for the recording; by default one row
        naming the very bytes being re-uploaded, which is what that branch is about.
        """
        existing = _existing(dive_id=7, content=TestReExtractionFailureDoesNotFailTheRequest.XML)
        result = MagicMock()
        result.one_or_none.return_value = astuple(existing)
        result.scalar_one_or_none.return_value = 0
        result.all.return_value = (
            files
            if files is not None
            else [
                SimpleNamespace(
                    storage_key=existing.storage_key,
                    content_type="application/xml",
                    original_filename="export.xml",
                    sha256=_digest(TestReExtractionFailureDoesNotFailTheRequest.XML),
                    parser_key="suunto_xml",
                )
            ]
        )
        result.rowcount = 0

        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)
        return db

    @pytest.mark.asyncio
    async def test_a_rejected_write_is_swallowed_and_rolled_back(self, monkeypatch) -> None:
        db = self._session_for_reupload()

        async def rejecting_fill(db, *, dive_id, scalars):
            raise IntegrityError("UPDATE dive ...", {}, Exception("ck_dive_cns_start_non_negative"))

        monkeypatch.setattr("src.app.services.dive_files.fill_tech_scalars", rejecting_fill)
        monkeypatch.setattr("src.app.services.dive_files.should_extract", lambda *a, **k: "extract")
        monkeypatch.setattr("src.app.services.dive_files.get_existing_profile", AsyncMock(return_value=None))
        monkeypatch.setattr("src.app.services.dive_files.store_profile", AsyncMock())
        monkeypatch.setattr("src.app.services.blob_store.get", AsyncMock(return_value=self.XML))
        monkeypatch.setattr("src.app.services.blob_store.has", AsyncMock(return_value=True))

        user_uuid = uuid7()
        stored = await store_recording_file(
            db,
            user_id=1,
            user_uuid=user_uuid,
            dive_id=7,
            upload=UploadFile(filename="export.xml", file=io.BytesIO(self.XML)),
            file_token=create_dive_file_token(
                user_uuid=user_uuid,
                sha256=hashlib.sha256(self.XML).hexdigest(),
                parser_key=SuuntoXmlParser.key,
            ),
        )

        # The caller re-uploaded bytes that are already stored; the right answer to that
        # is still "you already have this", not a 500.
        assert stored.recording_id == 1
        # And the session is usable afterwards, which is the half a missing handler cost.
        db.rollback.assert_awaited()

    @pytest.mark.asyncio
    async def test_an_unreadable_header_leaves_the_dive_alone_on_this_branch(self, monkeypatch) -> None:
        """The asymmetry with a recording's *first* file, and it is deliberate. There nothing
        was read off this recording before, so the write is outright and a reading nothing
        yields is cleared; here the recording already had files, so "couldn't read it *this*
        build" is not "the files say nothing" - and a later backfill, or a re-upload after a
        parser fix, can still get them.

        **The asymmetry is now structural rather than conditional**, which is the change worth
        pinning: the re-derivation picks `store_tech_scalars` (which clears) or
        `fill_tech_scalars` (which cannot) on its `fresh` parameter - "this upload created the
        recording" - so this branch cannot reach the clearing write at all.
        """
        writes: list[dict] = []
        cleared: list[dict] = []

        async def capture(db, *, dive_id, scalars):
            writes.append(scalars)

        async def clearing(db, *, dive_id, scalars, commit=False):
            cleared.append(scalars)

        monkeypatch.setattr("src.app.services.dive_files.fill_tech_scalars", capture)
        monkeypatch.setattr("src.app.services.dive_files.store_tech_scalars", clearing)
        monkeypatch.setattr("src.app.services.dive_files.should_extract", lambda *a, **k: "extract")
        monkeypatch.setattr("src.app.services.dive_files.get_existing_profile", AsyncMock(return_value=None))
        monkeypatch.setattr("src.app.services.dive_files.store_profile", AsyncMock())
        monkeypatch.setattr("src.app.services.blob_store.get", AsyncMock(return_value=self.XML))
        monkeypatch.setattr("src.app.services.blob_store.has", AsyncMock(return_value=True))
        monkeypatch.setattr(
            "src.app.services.dive_files.extract_recording",
            lambda files, known=None: RecordingExtraction(unreadable=True),
        )

        user_uuid = uuid7()
        await store_recording_file(
            self._session_for_reupload(),
            user_id=1,
            user_uuid=user_uuid,
            dive_id=7,
            upload=UploadFile(filename="export.xml", file=io.BytesIO(self.XML)),
            file_token=create_dive_file_token(
                user_uuid=user_uuid,
                sha256=hashlib.sha256(self.XML).hexdigest(),
                parser_key=SuuntoXmlParser.key,
            ),
        )

        # Nothing to write, and - the half that matters - nothing cleared either.
        assert [value for scalars in writes for value in scalars.values() if value is not None] == []
        assert cleared == []


class TestBackfillDoesNotStopOnOneBadDive:
    """A dive the database refuses costs that dive, not the run and not the batch.

    Nothing here advances a version column - `backfill_tech_fields` re-reads every
    candidate on every run by design - so a dive that raises on one run raises on the next
    one too. Without the savepoint the first such dive would be permanently fatal: the
    enclosing session rolls back up to `_BACKFILL_BATCH_SIZE` dives of finished work, and
    re-running walks into the same dive and dies the same way.
    """

    XML = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><CnsEnd>20</CnsEnd></Dive>
""".encode()

    class _Savepoint:
        """Stands in for `begin_nested`: lets the exception out, as the real one does
        after rolling the savepoint back."""

        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    def _db(self, candidates: list[SimpleNamespace]) -> AsyncMock:
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[candidates, *[MagicMock() for _ in range(20)]])
        db.begin_nested = MagicMock(return_value=self._Savepoint())
        return db

    @pytest.mark.asyncio
    async def test_a_constraint_violation_is_counted_and_the_run_continues(self, monkeypatch) -> None:
        candidates = [SimpleNamespace(recording_id=n, dive_id=n, user_id=1) for n in (1, 2, 3)]
        written: list[int] = []

        async def flaky_fill(db, *, dive_id, scalars):
            if dive_id == 2:
                raise IntegrityError("UPDATE dive ...", {}, Exception("ck_dive_surface_pressure_range"))
            written.append(dive_id)

        monkeypatch.setattr("src.app.services.dive_files.fill_tech_scalars", flaky_fill)
        monkeypatch.setattr(
            "src.app.services.dive_files.load_recording_files",
            AsyncMock(
                return_value=[
                    SimpleNamespace(
                        data=self.XML,
                        sha256=_digest(self.XML),
                        parser_key=SuuntoXmlParser.key,
                        content_type="application/xml",
                        original_filename="export.xml",
                    )
                ]
            ),
        )
        monkeypatch.setattr("src.app.services.dive_recordings.fill_device_fields", AsyncMock())
        monkeypatch.setattr("src.app.crud.crud_dive_mixtures.get_mixtures_for_dive", AsyncMock(return_value=[]))
        monkeypatch.setattr("src.app.services.cache_invalidation.invalidate_dive_caches", AsyncMock())

        report = await backfill_tech_fields(self._db(candidates))

        # The dive after the bad one is the assertion that matters: the run got past it.
        assert written == [1, 3]
        assert (report.examined, report.dives_updated, report.failed) == (3, 2, 1)


class TestScalarsAreWrittenAtAttach:
    """The import path owns these columns outright - the form cannot set them at all
    (`DiveTechScalars` is on the read shapes only), so this is the only write.

    **Outright for the upload that created the recording, filled for every other file**, and
    that split is what the tests below are about. A dive with nothing yet on this recording
    has nothing to lose by a write that clears what the files no longer yield; a recording
    gaining a file it did not begin with has earlier readings on the dive and must not
    overwrite them. The condition is `_rederive_recording`'s `fresh`, which is *not* "the
    recording had no files" - see its docstring for the case where the two differ.
    """

    XML_WITH_EXPOSURE = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><CnsEnd>20</CnsEnd><SurfacePressure>105700</SurfacePressure></Dive>
""".encode()

    @staticmethod
    def _files(*contents: bytes) -> list[LoadedDiveFile]:
        return [
            LoadedDiveFile(
                data=content,
                content_type="application/xml",
                original_filename="export.xml",
                sha256=_digest(content),
                parser_key=SuuntoXmlParser.key,
            )
            for content in contents
        ]

    @staticmethod
    async def _rederive(
        files: list[LoadedDiveFile], monkeypatch, *, fresh: bool, ordinal: int = 0, joined: bool = False
    ) -> dict:
        """Run the re-derivation over `files` and report which write it chose and with what.

        Captured at the two `store_tech_scalars`/`fill_tech_scalars` seams rather than by
        inspecting the emitted `UPDATE`: the decision under test is *what the attach decided
        to write*, and reading it back off SQLAlchemy's statement internals would pin the
        assertion to how the write is spelled rather than to what it says. The cylinder fill
        is captured the same way, under `"cylinders"`.
        """
        chosen: dict = {}

        async def outright(db, *, dive_id, scalars, commit=False):
            chosen["outright"] = scalars

        async def fill(db, *, dive_id, scalars):
            chosen["fill"] = scalars

        async def cylinders(db, *, dive_id, parsed):
            chosen["cylinders"] = list(parsed)

        monkeypatch.setattr("src.app.services.dive_files.store_tech_scalars", outright)
        monkeypatch.setattr("src.app.services.dive_files.fill_tech_scalars", fill)
        monkeypatch.setattr("src.app.services.dive_files.fill_dive_mixtures", cylinders)
        monkeypatch.setattr("src.app.services.dive_files.store_profile", AsyncMock())
        monkeypatch.setattr("src.app.services.dive_files.delete_profile_for_recording", AsyncMock())

        await _rederive_recording(
            AsyncMock(),
            recording_id=1,
            dive_id=7,
            ordinal=ordinal,
            fresh=fresh,
            joined=joined,
            files=files,
            extraction=extract_recording(files),
        )
        return chosen

    @pytest.mark.asyncio
    async def test_an_export_that_records_exposure_writes_it(self, monkeypatch) -> None:
        chosen = await self._rederive(self._files(self.XML_WITH_EXPOSURE), monkeypatch, fresh=True)

        assert chosen["outright"] == {
            "cns_start": None,
            "cns_end": 20.0,
            "otu_start": None,
            "otu_end": None,
            "surface_pressure_bar": 1.057,
            "entry_latitude": None,
            "entry_longitude": None,
            "exit_latitude": None,
            "exit_longitude": None,
        }

    @pytest.mark.asyncio
    async def test_an_export_that_records_none_clears_what_was_there(self, monkeypatch) -> None:
        """Unconditional on the `fresh` branch, unlike the profile write beside it: leaving a
        previous export's CNS on a recording whose files have changed would attribute a
        reading to bytes it didn't come from."""
        empty = f'<?xml version="1.0" encoding="utf-8"?><Dive xmlns="{SUUNTO_NS}"/>'.encode()

        chosen = await self._rederive(self._files(empty), monkeypatch, fresh=True)

        assert chosen["outright"] == dict.fromkeys(TECH_SCALAR_FIELDS)

    @pytest.mark.asyncio
    async def test_a_second_file_of_one_recording_fills_and_never_clears(self, monkeypatch) -> None:
        """The rule a recording exists to make expressible. The FIT beside the JSON of one
        Ocean dive contributes what the JSON had none of and takes nothing away - so the
        write is the filling one, and a value the second file does not carry is simply
        absent from it rather than present as `None`."""
        empty = f'<?xml version="1.0" encoding="utf-8"?><Dive xmlns="{SUUNTO_NS}"/>'.encode()

        chosen = await self._rederive(self._files(empty, self.XML_WITH_EXPOSURE), monkeypatch, fresh=False, joined=True)

        assert "outright" not in chosen
        assert chosen["fill"] == dict.fromkeys(TECH_SCALAR_FIELDS) | {"cns_end": 20.0, "surface_pressure_bar": 1.057}

    @pytest.mark.asyncio
    async def test_re_reading_the_same_files_fills_the_scalars_and_not_the_cylinders(self, monkeypatch) -> None:
        """**The two questions `fresh` and `joined` answer are different**, and this is where
        they part company: `_repeat_upload` re-reads bytes the recording already had, so a
        re-parse yielding less must not clear the dive's readings (`fresh=False`) - but
        nothing arrived that could put a value into a cylinder (`joined=False`).

        Gating the cylinders on `fresh` instead made re-uploading a file undo an edit: the
        diver attaches an export, clears the `oxygen` the form pre-filled from it, uploads
        the same file again, and the fill reads it straight back off those very bytes. A
        sample-less export reaches this on *every* repeat upload, `should_extract` answering
        "extract" unconditionally where there is no stored profile.
        """
        chosen = await self._rederive(self._files(self.XML_WITH_EXPOSURE), monkeypatch, fresh=False, joined=False)

        assert "fill" in chosen
        assert "cylinders" not in chosen

    @pytest.mark.asyncio
    async def test_a_secondary_recording_never_touches_them(self, monkeypatch) -> None:
        """A second computer's CNS clock is its own device's arithmetic. Writing it onto the
        dive would attribute one machine's numbers to another's record, which is why the
        dive's readings are the *primary* recording's and nothing else's."""
        chosen = await self._rederive(self._files(self.XML_WITH_EXPOSURE), monkeypatch, fresh=True, ordinal=1)

        assert chosen == {}

    @pytest.mark.asyncio
    async def test_the_first_file_that_records_a_reading_wins(self, monkeypatch) -> None:
        """The fill rule across a recording's files, stated on the value rather than on the
        write: a diver who corrected a reading between two uploads keeps the correction, and
        the later file supplies only what the earlier one was silent about."""
        second = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><CnsEnd>99</CnsEnd><OtuEnd>44</OtuEnd></Dive>
""".encode()

        extraction = extract_recording(self._files(self.XML_WITH_EXPOSURE, second))

        assert extraction.scalars["cns_end"] == 20.0
        assert extraction.scalars["otu_end"] == 44.0


class TestARecordingsProfileIsAttributedBeforeItIsCapped:
    """`extract_recording` runs `finalize_profile`'s last two steps, in that order.

    The ordering `derive_gas_attribution`'s own docstring states and `finalize_profile`
    implements: attribution reads a mean depth off the channel, and `downsample` keeps each
    bucket's extremes and throws the rest away, so attributing afterwards is a mean of the
    dive's peaks and troughs rather than of the dive.

    **It regressed once and nothing caught it**, because it is invisible below
    `MAX_POINTS_PER_CHANNEL` and because the wrong number is a summary column no other test
    re-derives. So this is asserted against the *dive*'s own mean rather than against
    `finalize_profile`'s output - two implementations agreeing is not evidence when the same
    hand wrote both.
    """

    # Whole metres, so the stored centimetres are exact and the expected mean has no
    # rounding of its own to argue with.
    DEPTHS_M = [second // 80 for second in range(2_400)]

    @classmethod
    def _one_hertz_dive(cls) -> bytes:
        """A 1 Hz descent well past the cap - the cadence every FIT export uses, which
        reaches 1 200 samples inside twenty minutes."""
        samples = "".join(
            f"<Dive.Sample><Time>{second}</Time><Depth>{depth}</Depth></Dive.Sample>"
            for second, depth in enumerate(cls.DEPTHS_M)
        )
        return f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><DiveMixtures><DiveMixture><Oxygen>0.21</Oxygen>
<DiveGasChanges><DiveGasChange><GasChangeTime>0</GasChangeTime></DiveGasChange></DiveGasChanges>
</DiveMixture></DiveMixtures><DiveSamples>{samples}</DiveSamples></Dive>
""".encode()

    def test_the_mean_depth_is_the_dives_and_not_the_thinned_channels(self) -> None:
        content = self._one_hertz_dive()
        depths = [metres * 100 for metres in self.DEPTHS_M]

        extraction = extract_recording(
            [
                LoadedDiveFile(
                    data=content,
                    content_type="application/xml",
                    original_filename="export.xml",
                    sha256=_digest(content),
                    parser_key=SuuntoXmlParser.key,
                )
            ]
        )

        assert extraction.profile is not None
        assert extraction.profile.depth is not None
        assert extraction.profile.gas_attribution, "the file records a gas switch, so there is one to attribute"
        stored_depth = extraction.profile.depth

        assert extraction.profile.gas_attribution[0].mean_depth_cm == round(sum(depths) / len(depths))
        # And the channel really was thinned, so the mean could not have come off it.
        assert len(stored_depth.t) < len(depths)
        assert extraction.profile.gas_attribution[0].mean_depth_cm != round(sum(stored_depth.v) / len(stored_depth.v))
