"""Unit tests for storing the dive-computer exports a dive's recordings were read from
(`models/dive_file.py`, `services/dive_files.py`, the dive-file token in
`core/security.py`, and the `/dive/{uuid}/file/{fid}` routes).

Like `test_certifications.py`, these cover the pieces that are pure logic and so need no
database: the token that admits a file into storage, the format it is recorded and served
as, and the reconciliation that decides whether an upload is a replacement, a no-op or a
duplicate. Endpoint behaviour on top of a live Postgres/Redis
is exercised end to end by hand (see DECISIONS.md), not here.
"""

import hashlib
import io
import json
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
from src.app.models.dive_recording import DiveRecording
from src.app.schemas.dive import DiveFileInfo, DiveTechScalars, RecordingReadouts
from src.app.schemas.dive_mixture import DiveMixtureRead, GasRole
from src.app.schemas.parsed_dive import DiveMixtureSchema, ParsedDiveSchema
from src.app.services import dive_files as dive_files_module
from src.app.services.blob_store import new_key
from src.app.services.cache_invalidation import invalidate_dive_caches
from src.app.services.dive_files import (
    KEY_KIND as DIVE_FILE_KIND,
)
from src.app.services.dive_files import (
    MAX_DIVE_FILE_SIZE,
    READOUT_FIELDS,
    SCALAR_FIELDS,
    TECH_SCALAR_FIELDS,
    InvalidDiveFileTokenError,
    LoadedDiveFile,
    RecordingExtraction,
    _admit,
    _ExistingRow,
    backfill_tech_fields,
    extract_file,
    extract_recording,
    fill_mixture_fields,
    join_file_mixtures,
    merge_mixture_fields,
    reconcile,
    rederive_recording,
    renumber_onto_labels,
    store_recording_file,
)
from src.app.services.dive_reader import FORMAT_CONTENT_TYPES

SUUNTO_NS = "http://schemas.datacontract.org/2004/07/Suunto.Diving.Dal"

VALID_SUUNTO_XML = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <StartTime>2026-06-03T12:15:00</StartTime>
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


class TestAdmittingAToken:
    """`_admit` checks the claim against the formats this build's reader reads."""

    def test_a_token_minted_before_the_reader_changed_still_admits_its_file(self) -> None:
        """The deploy-skew case: the previous build minted its tokens with its parsers' keys,
        and the three formats the form read then are the same three strings as the reader's
        format ids - so a form the old build prefilled still attaches on this one."""
        for key in ("fit", "suunto_json", "suunto_xml"):
            token = create_dive_file_token(user_uuid=USER_UUID, sha256=_digest(VALID_SUUNTO_XML), parser_key=key)

            assert _admit(user_uuid=USER_UUID, digest=_digest(VALID_SUUNTO_XML), file_token=token) == key

    def test_a_format_this_build_does_not_read_is_refused(self) -> None:
        token = create_dive_file_token(user_uuid=USER_UUID, sha256=_digest(VALID_SUUNTO_XML), parser_key="garmin_fitx")

        with pytest.raises(InvalidDiveFileTokenError, match="no longer supported"):
            _admit(user_uuid=USER_UUID, digest=_digest(VALID_SUUNTO_XML), file_token=token)


class TestTheContentTypesAreOnesWeServe:
    def test_content_types_are_ones_we_are_willing_to_serve(self) -> None:
        """The closed set `read_dive_file` may put in a `Content-Type` header.

        The bar is that a browser handed one of these can't be talked into *executing* it
        at the app's own origin - so nothing in the `text/*` family, and nothing `nosniff`
        wouldn't already pin down. `application/vnd.ant.fit` (the ANT+ registered type for
        a FIT file) clears it more easily than the XML and JSON types: it is opaque binary
        with no renderer at all. Every value also fits `dive_file.content_type`'s 32
        characters.
        """
        assert set(FORMAT_CONTENT_TYPES.values()) <= {
            "application/xml",
            "application/json",
            "application/vnd.ant.fit",
        }


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

    The dedupe lookup finds nothing (`one_or_none`), the account stores nothing yet (the
    storage sum's `one`), the dive has no recordings yet (iteration), the recording insert
    returns an id (`scalar_one`), and the recording holds no files when the re-derivation
    reads them back (`all`) - which is the shape a mocked session can honestly represent,
    since no row it "wrote" is really there.
    """
    result = MagicMock()
    result.one_or_none.return_value = None
    result.one.return_value = (0, 0, 0)
    result.scalar_one.return_value = 1
    result.scalar_one_or_none.return_value = 0
    result.scalars.return_value = []
    result.all.return_value = []
    result.__iter__.return_value = iter(())
    result.rowcount = 0
    return result


class TestProfileExtractionReleasesTheTransaction:
    """Decoding a FIT file is pure-Python CPU in a worker thread. The event loop is free for
    that - `run_in_threadpool` bought that much - but the connection the lookups
    rode in on would otherwise sit idle-in-transaction for the whole of it, so a burst of
    FIT uploads ties up pool connections doing nothing.

    An ordering test rather than a behavioural one: what can regress here is somebody
    moving an extraction back above the release, and this is what would catch it.

    **The attach path releases twice, and the assertion below is written to see the second
    one.** Recordings gave it a second hop - the incoming file is read to decide where it
    lands, and then the whole recording is read to derive what comes off it - and the
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
        real_extract_file = dive_files_module.extract_file
        real_extract_recording = dive_files_module.extract_recording

        def first_hop(data, format_id):
            calls.append("extract")
            return real_extract_file(data, format_id)

        def second_hop(files, known=None, **start):
            calls.append("extract")
            return real_extract_recording(files, known, **start)

        monkeypatch.setattr("src.app.services.dive_files.extract_file", first_hop)
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
                parser_key="suunto_xml",
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


class TestFileExtraction:
    """`extract_file` reads a stored file as the format it was admitted under, and never
    fails the upload or the backfill run it rode in on."""

    def test_reads_the_readouts_off_a_readable_file(self) -> None:
        content = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <StartTime>2026-06-03T12:15:00</StartTime>
  <CnsStart>8</CnsStart><CnsEnd>9</CnsEnd>
  <OtuStart>22</OtuStart><OtuEnd>23</OtuEnd>
  <SurfacePressure>105700</SurfacePressure>
</Dive>
""".encode()

        assert extract_file(content, "suunto_xml").scalars == {
            "cns_start": 8.0,
            "cns_end": 9.0,
            "otu_start": 22.0,
            "otu_end": 23.0,
            "surface_pressure_bar": 1.057,
            # A DM5 XML export carries no GPS at all, so this format contributes the columns
            # and never a value.
            "entry_latitude": None,
            "entry_longitude": None,
            "exit_latitude": None,
            "exit_longitude": None,
        }

    def test_covers_exactly_the_columns_the_read_schema_publishes(self) -> None:
        """`TECH_SCALAR_FIELDS` is derived from `DiveTechScalars` rather than listed, so
        adding a field to the schema without the projection writing it would show up here
        rather than as a column that silently stays null forever."""
        assert set(TECH_SCALAR_FIELDS) == set(DiveTechScalars.model_fields)
        assert set(TECH_SCALAR_FIELDS) <= set(ParsedDiveSchema.model_fields)
        # The other direction, and the one that fails in production rather than in CI:
        # these names are spread into `update(Dive).values(**scalars)`, so a field on
        # `DiveTechScalars` that is not a `Dive` column raises at attach time, on a real
        # upload, rather than anywhere a developer would see it first.
        assert set(TECH_SCALAR_FIELDS) <= set(Dive.__table__.columns.keys())

    def test_an_unreadable_file_comes_back_empty_rather_than_raising(self) -> None:
        """The upload must survive a file this build can't read: the file is the durable
        artifact, and refusing the attach would discard the very corpus entry needed to fix
        the reader. Empty is what `extract_recording` counts as unreadable."""
        extraction = extract_file(b"<Dive><Unclosed>", "suunto_xml")

        assert (extraction.parsed, extraction.profile, extraction.scalars) == (None, None, None)

    def test_a_file_that_records_nothing_yields_an_all_null_write(self) -> None:
        """Distinct from the empty result above, and the distinction is load-bearing: this is
        "the file says nothing", which *clears* a previous export's readings, whereas empty is
        "couldn't read" and leaves them alone."""
        content = (
            f'<?xml version="1.0" encoding="utf-8"?><Dive xmlns="{SUUNTO_NS}">'
            "<StartTime>2026-06-03T12:15:00</StartTime></Dive>"
        ).encode()

        assert extract_file(content, "suunto_xml").scalars == dict.fromkeys(SCALAR_FIELDS)


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
            (11, {"po2_limit": 1.4}),
            (12, {"po2_limit": 1.6, "role": GasRole.DECO}),
        ]

    def test_a_field_the_file_does_not_record_is_never_written(self) -> None:
        """Fill-only, and it is `DiveMixtureSchema`'s own rule applied to the write: `None`
        means "the file did not record this", so spreading one into an `UPDATE` turns an
        absent reading into a value.

        Both fields are client-writable, which is what makes it data loss rather than a
        tidiness point. A FIT import produces `po2_limit=None` always and `role=None` for
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
        still owns it, so a reader correction lands on the next run. It declines only where
        the file has nothing to say."""
        parsed = [self._parsed(po2_limit=1.6, role=GasRole.DECO)]
        stored = [self._stored(11, po2_limit=1.4, role=GasRole.BOTTOM)]

        assert merge_mixture_fields(parsed, stored) == [(11, {"po2_limit": 1.6, "role": GasRole.DECO})]

    def test_never_writes_a_cylinder_label(self) -> None:
        """The join is positional, and a label written by it would put the reader's labels
        on an old dive by a rule other than the labelling's - breaking the dive's join to its
        stored channels until the profile backfill reached it. A label is the profile path's
        to write."""
        parsed = [self._parsed(gas_number=0, po2_limit=None)]
        stored = [self._stored(11, gas_number=1)]

        assert merge_mixture_fields(parsed, stored) == []

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

        assert merge_mixture_fields(parsed, stored) == [(11, {"po2_limit": 1.4})]

    def test_a_fraction_the_stored_row_never_recorded_is_not_a_mismatch_either(self) -> None:
        """The mirror of the case above, and the one the columns becoming nullable created.
        The import no longer defaults a mix the document never carried, so a stored row can
        say "not recorded" in exactly the way a parsed one always could - and a guard that
        only looked at the parsed side would read `parsed 21` against `stored NULL` as two
        different gases and refuse every such dive.
        """
        parsed = [self._parsed(oxygen=21.0, helium=0.0)]
        stored = [self._stored(11, oxygen=None, helium=None)]

        assert merge_mixture_fields(parsed, stored) == [(11, {"po2_limit": 1.4})]

    def test_a_fraction_both_sides_recorded_is_still_compared(self) -> None:
        """What the widened guard does not cost: a real disagreement is still a refusal."""
        parsed = [self._parsed(oxygen=50.0)]
        stored = [self._stored(11, oxygen=32.0)]

        assert merge_mixture_fields(parsed, stored) is None

    def test_the_fraction_guard_cannot_catch_a_mis_ordered_all_null_list(self) -> None:
        """Why `get_mixtures_for_dive` has to order by `id`, stated as a test.

        A file whose cylinders record no fractions at all makes every parsed row
        `oxygen=None, helium=None`, the guard above compares nothing, and *both* orderings
        below are accepted. The ordering of `stored` is then the only thing deciding which
        cylinder gets which ppO2 limit: swap it and the back gas carries the deco bottle's.
        """
        parsed = [
            self._parsed(oxygen=None, helium=None, po2_limit=1.4),
            self._parsed(oxygen=None, helium=None, po2_limit=1.6),
        ]

        in_order = merge_mixture_fields(parsed, [self._stored(11), self._stored(12)])
        reversed_order = merge_mixture_fields(parsed, [self._stored(12), self._stored(11)])

        assert in_order is not None and reversed_order is not None
        assert [(mixture_id, values["po2_limit"]) for mixture_id, values in in_order] == [(11, 1.4), (12, 1.6)]
        assert [(mixture_id, values["po2_limit"]) for mixture_id, values in reversed_order] == [(12, 1.4), (11, 1.6)]


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


class TestARecordingsCylindersJoinMemberByMember:
    """`join_file_mixtures`, the fill rule one level up: across a recording's own files rather
    than between a file and the dive's rows - and the map a later file's channels are
    rewritten through before they join.

    Per member rather than per list, and that is the whole of it. Taking the first list whole
    lands the JSON's pressures and drops the FIT's `oxygen` - the one gas fraction the corpus
    pair records anywhere - so the dive's only cylinder ends up with no mix at all.
    """

    @staticmethod
    def _mix(**overrides: object) -> DiveMixtureSchema:
        return TestMixtureFieldFill._parsed(**overrides)

    def test_each_member_comes_from_the_first_file_that_recorded_it(self) -> None:
        earlier = [self._mix(gas_number=0, start_pressure=207.34, end_pressure=47.47)]
        later = [self._mix(gas_number=None, oxygen=33.0, start_pressure=210.0)]

        joined, labels = join_file_mixtures(earlier, later)

        assert [(row.oxygen, row.start_pressure, row.end_pressure, row.gas_number) for row in joined] == [
            (33.0, 207.34, 47.47, 0)
        ]
        assert labels == {}

    def test_a_first_file_with_no_cylinders_takes_the_later_ones_whole(self) -> None:
        later = [self._mix(gas_number=1, oxygen=33.0)]

        assert join_file_mixtures([], later) == (later, {})

    def test_a_later_files_label_is_the_recordings_where_the_first_labelled_nothing(self) -> None:
        """The corpus pair FIT first: the FIT labels no cylinder - it has no channel to point
        one at - and the JSON's transmitter channel names its cylinder `0`. The recording
        takes the label, so the channel names the cylinder whichever file came first."""
        earlier = [self._mix(gas_number=None, oxygen=33.0)]
        later = [self._mix(gas_number=0, oxygen=None, start_pressure=207.34)]

        joined, labels = join_file_mixtures(earlier, later)

        assert [(row.oxygen, row.start_pressure, row.gas_number) for row in joined] == [(33.0, 207.34, 0)]
        assert labels == {0: 0}

    def test_a_file_with_no_mix_joins_by_position(self) -> None:
        """The two-cylinder Ocean pair: its JSON records no mix, so its two cylinders join
        the FIT's two by position, and its transmitter's channel lands on the first."""
        earlier = [self._mix(gas_number=None, oxygen=21.0), self._mix(gas_number=None, oxygen=54.0)]
        later = [
            self._mix(gas_number=0, oxygen=None, start_pressure=211.63),
            self._mix(gas_number=1, oxygen=None),
        ]

        joined, labels = join_file_mixtures(earlier, later)

        assert [(row.oxygen, row.gas_number, row.start_pressure) for row in joined] == [
            (21.0, 0, 211.63),
            (54.0, 1, None),
        ]
        assert labels == {0: 0, 1: 1}

    def test_a_later_label_is_mapped_onto_the_recordings_own(self) -> None:
        """By mix first: the later file lists the deco bottle first and labels it 0, and the
        recording already calls it 1. Its channels are rewritten through the map."""
        earlier = [self._mix(gas_number=0, oxygen=21.0), self._mix(gas_number=1, oxygen=50.0)]
        later = [self._mix(gas_number=0, oxygen=50.0), self._mix(gas_number=1, oxygen=21.0)]

        joined, labels = join_file_mixtures(earlier, later)

        assert [row.gas_number for row in joined] == [0, 1]
        assert labels == {0: 1, 1: 0}

    def test_a_cylinder_only_the_later_file_saw_is_appended_under_a_free_label(self) -> None:
        """A positional pair whose recorded fractions disagree is not a pair, and the later
        cylinder is one the later file saw: appended, under the next free label, so none of
        its channels names another tank."""
        earlier = [self._mix(gas_number=0, oxygen=32.0)]
        later = [self._mix(gas_number=0, oxygen=50.0)]

        joined, labels = join_file_mixtures(earlier, later)

        assert [(row.oxygen, row.gas_number) for row in joined] == [(32.0, 0), (50.0, 1)]
        assert labels == {0: 1}


class TestRenumberingOntoTheReadersLabels:
    """`renumber_onto_labels`: a primary recording's labels onto the dive's own rows, and the
    map the dive's other recordings are rewritten through."""

    @staticmethod
    def _stored(mixture_id: int, oxygen: float | None, gas_number: int | None) -> DiveMixtureRead:
        return DiveMixtureRead(
            id=mixture_id, oxygen=oxygen, helium=None if oxygen is None else 0.0, gas_number=gas_number
        )

    @staticmethod
    def _read(oxygen: float | None, gas_number: int | None) -> DiveMixtureSchema:
        return TestMixtureFieldFill._parsed(
            oxygen=oxygen, helium=None if oxygen is None else 0.0, gas_number=gas_number
        )

    def test_rows_already_on_the_readers_labels_are_left_alone(self) -> None:
        stored = [self._stored(11, 21.0, 0), self._stored(12, 49.0, 1)]

        assert renumber_onto_labels([self._read(21.0, 0), self._read(49.0, 1)], stored) is None

    def test_a_previous_readers_labels_move_onto_this_ones(self) -> None:
        """The D5-shape JSON the previous parsers numbered from 1."""
        stored = [self._stored(11, 21.0, 1), self._stored(12, 49.0, 2)]

        labels, siblings = renumber_onto_labels([self._read(21.0, 0), self._read(49.0, 1)], stored) or ({}, {})

        assert labels == {11: 0, 12: 1}
        assert siblings == {1: 0, 2: 1}

    def test_a_row_the_reader_does_not_have_keeps_its_label_unless_a_new_one_claims_it(self) -> None:
        """Cleared rather than kept, since two rows sharing a label would join one channel to
        both - and a sibling that pointed at it is sent to a label no row carries rather than
        to the cylinder that took its number."""
        stored = [
            self._stored(11, 21.0, 1),
            self._stored(12, 49.0, 2),
            self._stored(13, 32.0, 0),
            self._stored(14, 36.0, 7),
            self._stored(15, 30.0, None),
        ]

        labels, siblings = renumber_onto_labels([self._read(21.0, 0), self._read(49.0, 1)], stored) or ({}, {})

        assert labels == {11: 0, 12: 1, 13: None, 14: 7, 15: None}
        assert siblings == {1: 0, 2: 1, 0: 8}

    def test_a_file_with_no_mix_joins_the_rows_by_position(self) -> None:
        """The Ocean JSON the previous parser labelled `[1, 0]` where the reader says `[0, 1]`
        - the one place the two readings differed on a real dive."""
        stored = [self._stored(11, None, 1), self._stored(12, None, 0)]

        labels, siblings = renumber_onto_labels([self._read(None, 0), self._read(None, 1)], stored) or ({}, {})

        assert labels == {11: 0, 12: 1}
        assert siblings == {1: 0, 0: 1}


class TestAFileThisBuildCannotRead:
    def test_a_stored_file_under_a_format_the_reader_does_not_name_makes_the_recording_unreadable(self) -> None:
        """A restored file this build does not read keeps the import's key, and a recording
        holding one cannot be re-derived - which the backfill counts rather than swallows."""
        extraction = extract_recording(
            [
                LoadedDiveFile(
                    data=b"anything",
                    content_type="application/octet-stream",
                    original_filename="dive.bin",
                    sha256=_digest(b"anything"),
                    parser_key="divejson_import",
                )
            ],
            start_time=None,
            utc_offset_minutes=None,
        )

        assert extraction.unreadable is True
        assert extraction.profile is None


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
<Dive xmlns="{SUUNTO_NS}"><StartTime>2026-06-03T12:15:00</StartTime><CnsEnd>20</CnsEnd></Dive>
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

        async def rejecting_fill(db, *, recording_id, readouts):
            raise IntegrityError("UPDATE dive_recording ...", {}, Exception("ck_dive_recording_cns_start_non_negative"))

        monkeypatch.setattr("src.app.services.dive_recordings.fill_readouts", rejecting_fill)
        monkeypatch.setattr("src.app.services.dive_recordings.recording_start", AsyncMock(return_value=(None, None)))
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
                parser_key="suunto_xml",
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
        reader fix, can still get them.

        **The asymmetry is now structural rather than conditional**, which is the change worth
        pinning: the re-derivation picks `store_tech_scalars` (which clears) or
        `fill_tech_scalars` (which cannot) on its `fresh` parameter - "this upload created the
        recording" - so this branch cannot reach the clearing write at all.
        """
        writes: list[dict] = []
        cleared: list[dict] = []

        async def capture(db, *, dive_id, scalars):
            writes.append(scalars)

        async def capture_readouts(db, *, recording_id, readouts):
            writes.append(readouts)

        async def clearing(db, *, dive_id, scalars, commit=False):
            cleared.append(scalars)

        monkeypatch.setattr("src.app.services.dive_files.fill_tech_scalars", capture)
        monkeypatch.setattr("src.app.services.dive_files.store_tech_scalars", clearing)
        monkeypatch.setattr("src.app.services.dive_files.should_extract", lambda *a, **k: "extract")
        monkeypatch.setattr("src.app.services.dive_files.get_existing_profile", AsyncMock(return_value=None))
        monkeypatch.setattr("src.app.services.dive_files.store_profile", AsyncMock())
        monkeypatch.setattr("src.app.services.blob_store.get", AsyncMock(return_value=self.XML))
        monkeypatch.setattr("src.app.services.blob_store.has", AsyncMock(return_value=True))
        monkeypatch.setattr("src.app.services.dive_recordings.recording_start", AsyncMock(return_value=(None, None)))
        monkeypatch.setattr("src.app.services.dive_recordings.fill_readouts", capture_readouts)
        monkeypatch.setattr(
            "src.app.services.dive_files.extract_recording",
            lambda files, known=None, **start: RecordingExtraction(unreadable=True),
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
                parser_key="suunto_xml",
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
<Dive xmlns="{SUUNTO_NS}"><StartTime>2026-06-03T12:15:00</StartTime><CnsEnd>20</CnsEnd></Dive>
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
        candidates = [
            SimpleNamespace(recording_id=n, dive_id=n, user_id=1, ordinal=0, start_time=None, utc_offset_minutes=None)
            for n in (1, 2, 3)
        ]
        written: list[int] = []

        async def flaky_fill(db, *, recording_id, readouts):
            if recording_id == 2:
                raise IntegrityError(
                    "UPDATE dive_recording ...", {}, Exception("ck_dive_recording_surface_pressure_range")
                )
            written.append(recording_id)

        monkeypatch.setattr("src.app.services.dive_recordings.fill_readouts", flaky_fill)
        monkeypatch.setattr(
            "src.app.services.dive_files.load_recording_files",
            AsyncMock(
                return_value=[
                    SimpleNamespace(
                        data=self.XML,
                        sha256=_digest(self.XML),
                        parser_key="suunto_xml",
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
        assert (report.examined, report.recordings_updated, report.failed) == (3, 2, 1)


class TestScalarsAreWrittenAtAttach:
    """The import path owns these columns outright - the form cannot set them at all, so this
    is the only write: a recording's readouts onto that recording, and the primary's entry
    and exit fixes onto the dive.

    **Outright for the upload that created the recording, filled for every other file**, and
    that split is what the tests below are about. A recording with nothing yet has nothing to
    lose by a write that clears what the files no longer yield; a recording gaining a file it
    did not begin with has earlier readings and must not overwrite them. The condition is
    `rederive_recording`'s `fresh`, which is *not* "the recording had no files" - see its
    docstring for the case where the two differ.
    """

    XML_WITH_EXPOSURE = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><StartTime>2026-06-03T12:15:00</StartTime><CnsEnd>20</CnsEnd>
<SurfacePressure>105700</SurfacePressure></Dive>
""".encode()

    @staticmethod
    def _files(*contents: bytes) -> list[LoadedDiveFile]:
        return [
            LoadedDiveFile(
                data=content,
                content_type="application/xml",
                original_filename="export.xml",
                sha256=_digest(content),
                parser_key="suunto_xml",
            )
            for content in contents
        ]

    @staticmethod
    async def _rederive(
        files: list[LoadedDiveFile], monkeypatch, *, fresh: bool, ordinal: int = 0, joined: bool = False
    ) -> dict:
        """Run the re-derivation over `files` and report which writes it chose and with what.

        Captured at the seams rather than by inspecting the emitted `UPDATE`: the decision
        under test is *what the attach decided to write*. `readouts` is the recording's write
        and `dive` the dive's, each under `outright` or `fill`; the cylinder fill is captured
        under `cylinders`.
        """
        chosen: dict = {}

        async def readouts_outright(db, *, recording_id, readouts):
            chosen["readouts outright"] = {name: readouts.get(name) for name in READOUT_FIELDS}

        async def readouts_fill(db, *, recording_id, readouts):
            chosen["readouts fill"] = {name: readouts.get(name) for name in READOUT_FIELDS}

        async def outright(db, *, dive_id, scalars, commit=False):
            chosen["dive outright"] = {name: scalars.get(name) for name in TECH_SCALAR_FIELDS}

        async def fill(db, *, dive_id, scalars):
            chosen["dive fill"] = {name: scalars.get(name) for name in TECH_SCALAR_FIELDS}

        async def cylinders(db, *, dive_id, parsed):
            chosen["cylinders"] = list(parsed)

        monkeypatch.setattr("src.app.services.dive_recordings.store_readouts", readouts_outright)
        monkeypatch.setattr("src.app.services.dive_recordings.fill_readouts", readouts_fill)
        monkeypatch.setattr("src.app.services.dive_files.store_tech_scalars", outright)
        monkeypatch.setattr("src.app.services.dive_files.fill_tech_scalars", fill)
        monkeypatch.setattr("src.app.services.dive_files.fill_dive_mixtures", cylinders)
        monkeypatch.setattr("src.app.services.dive_files.store_profile", AsyncMock())
        monkeypatch.setattr("src.app.services.dive_files.delete_profile_for_recording", AsyncMock())

        await rederive_recording(
            AsyncMock(),
            recording_id=1,
            dive_id=7,
            ordinal=ordinal,
            fresh=fresh,
            joined=joined,
            files=files,
            extraction=extract_recording(files, start_time=None, utc_offset_minutes=None),
        )
        return chosen

    @pytest.mark.asyncio
    async def test_an_export_that_records_exposure_writes_it_onto_the_recording(self, monkeypatch) -> None:
        chosen = await self._rederive(self._files(self.XML_WITH_EXPOSURE), monkeypatch, fresh=True)

        assert chosen["readouts outright"] == {
            "cns_start": None,
            "cns_end": 20.0,
            "otu_start": None,
            "otu_end": None,
            "surface_pressure_bar": 1.057,
        }
        assert chosen["dive outright"] == dict.fromkeys(TECH_SCALAR_FIELDS)

    @pytest.mark.asyncio
    async def test_an_export_that_records_none_clears_what_was_there(self, monkeypatch) -> None:
        """Unconditional on the `fresh` branch, unlike the profile write beside it: leaving a
        previous export's CNS on a recording whose files have changed would attribute a
        reading to bytes it didn't come from."""
        empty = (
            f'<?xml version="1.0" encoding="utf-8"?><Dive xmlns="{SUUNTO_NS}">'
            "<StartTime>2026-06-03T12:15:00</StartTime></Dive>"
        ).encode()

        chosen = await self._rederive(self._files(empty), monkeypatch, fresh=True)

        assert chosen["readouts outright"] == dict.fromkeys(READOUT_FIELDS)
        assert chosen["dive outright"] == dict.fromkeys(TECH_SCALAR_FIELDS)

    @pytest.mark.asyncio
    async def test_a_second_file_of_one_recording_fills_and_never_clears(self, monkeypatch) -> None:
        """The rule a recording exists to make expressible. The FIT beside the JSON of one
        Ocean dive contributes what the JSON had none of and takes nothing away - so the
        write is the filling one."""
        empty = (
            f'<?xml version="1.0" encoding="utf-8"?><Dive xmlns="{SUUNTO_NS}">'
            "<StartTime>2026-06-03T12:15:00</StartTime></Dive>"
        ).encode()

        chosen = await self._rederive(self._files(empty, self.XML_WITH_EXPOSURE), monkeypatch, fresh=False, joined=True)

        assert "readouts outright" not in chosen
        assert "dive outright" not in chosen
        assert chosen["readouts fill"] == dict.fromkeys(READOUT_FIELDS) | {
            "cns_end": 20.0,
            "surface_pressure_bar": 1.057,
        }

    @pytest.mark.asyncio
    async def test_re_reading_the_same_files_fills_the_scalars_and_not_the_cylinders(self, monkeypatch) -> None:
        """**The two questions `fresh` and `joined` answer are different**, and this is where
        they part company: `_repeat_upload` re-reads bytes the recording already had, so a
        re-parse yielding less must not clear the readings (`fresh=False`) - but nothing
        arrived that could put a value into a cylinder (`joined=False`)."""
        chosen = await self._rederive(self._files(self.XML_WITH_EXPOSURE), monkeypatch, fresh=False, joined=False)

        assert "readouts fill" in chosen
        assert "dive fill" in chosen
        assert "cylinders" not in chosen

    @pytest.mark.asyncio
    async def test_a_secondary_recording_writes_its_own_readouts_and_nothing_of_the_dives(self, monkeypatch) -> None:
        """A second computer's CNS clock is its own device's arithmetic, so it lands on its
        own recording - and its positions never reach the dive, whose fixes are the
        *primary* recording's and nothing else's."""
        chosen = await self._rederive(self._files(self.XML_WITH_EXPOSURE), monkeypatch, fresh=True, ordinal=1)

        assert chosen.keys() == {"readouts outright"}
        assert chosen["readouts outright"]["cns_end"] == 20.0

    @pytest.mark.asyncio
    async def test_the_first_file_that_records_a_reading_wins(self, monkeypatch) -> None:
        """The fill rule across a recording's files, stated on the value rather than on the
        write: a diver who corrected a reading between two uploads keeps the correction, and
        the later file supplies only what the earlier one was silent about."""
        second = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><StartTime>2026-06-03T12:15:00</StartTime><CnsEnd>99</CnsEnd><OtuEnd>44</OtuEnd></Dive>
""".encode()

        extraction = extract_recording(
            self._files(self.XML_WITH_EXPOSURE, second), start_time=None, utc_offset_minutes=None
        )

        assert extraction.scalars["cns_end"] == 20.0
        assert extraction.scalars["otu_end"] == 44.0


class TestARecordingsProfileIsAttributedBeforeItIsCapped:
    """`extract_recording` runs `attribute_and_cap`, whose two steps run in that order.

    The ordering `derive_gas_attribution`'s own docstring states and `attribute_and_cap`
    implements: attribution reads a mean depth off the channel, and `downsample` keeps each
    bucket's extremes and throws the rest away, so attributing afterwards is a mean of the
    dive's peaks and troughs rather than of the dive.

    **It regressed once and nothing caught it**, because it is invisible below
    `MAX_POINTS_PER_CHANNEL` and because the wrong number is a summary column no other test
    re-derives. So this is asserted against the *dive*'s own mean rather than against
    `attribute_and_cap`'s output - two implementations agreeing is not evidence when the same
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
<Dive xmlns="{SUUNTO_NS}"><StartTime>2026-06-03T12:15:00</StartTime><DiveMixtures><DiveMixture><Oxygen>21</Oxygen>
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
                    parser_key="suunto_xml",
                )
            ],
            start_time=None,
            utc_offset_minutes=None,
        )

        assert extraction.profile is not None
        assert extraction.profile.depth is not None
        assert extraction.profile.gas_attribution, "the file records a gas switch, so there is one to attribute"
        stored_depth = extraction.profile.depth

        assert extraction.profile.gas_attribution[0].mean_depth_cm == round(sum(depths) / len(depths))
        # And the channel really was thinned, so the mean could not have come off it.
        assert len(stored_depth.t) < len(depths)
        assert extraction.profile.gas_attribution[0].mean_depth_cm != round(sum(stored_depth.v) / len(stored_depth.v))


def _suunto_app_export(header: str, *first_depth_at: str) -> bytes:
    """A Suunto app export whose `Header.DateTime` is `header` and whose depth samples fall
    at the given instants - the only format whose samples carry an offset below a second."""
    samples = [{"Depth": 1.2 + index, "TimeISO8601": moment} for index, moment in enumerate(first_depth_at)]
    body = {"DeviceLog": {"Header": {"DateTime": header, "Duration": 1800}, "Samples": samples}}
    return json.dumps(body).encode()


def _loaded(content: bytes) -> LoadedDiveFile:
    return LoadedDiveFile(
        data=content,
        content_type="application/json",
        original_filename="export.json",
        sha256=_digest(content),
        parser_key="suunto_json",
    )


class TestEachFileIsPlacedOnTheRecordingsStart:
    """A recording's axis counts from its stored start - the `started_at` the export writes -
    and each file is moved onto it by its own start's offset, measured on the clock
    `delta_seconds` uses."""

    HEADER = "2025-05-31T12:59:06.000+02:00"
    START = datetime(2025, 5, 31, 10, 59, 6, tzinfo=UTC)

    def _depth_times(self, content: bytes, start_time: datetime | None, offset: int | None) -> list[int]:
        extraction = extract_recording([_loaded(content)], start_time=start_time, utc_offset_minutes=offset)
        assert extraction.profile is not None and extraction.profile.depth is not None
        return extraction.profile.depth.t

    def test_a_first_reading_keeps_the_offset_its_file_states(self) -> None:
        content = _suunto_app_export(self.HEADER, "2025-05-31T12:59:06.160+02:00", "2025-05-31T13:19:06.160+02:00")

        assert self._depth_times(content, self.START, 120) == [160, 1_200_160]

    def test_an_offset_less_imported_recording_keeps_the_profile_intact(self) -> None:
        """The recording came in from a document with a wall clock and no offset; the file
        carries one. Compared as instants the two are two hours apart and the profile would
        be pushed off its own dive - the wall clocks agree, which is what is compared."""
        content = _suunto_app_export(self.HEADER, "2025-05-31T12:59:06.160+02:00")
        wall_clock = datetime(2025, 5, 31, 12, 59, 6, tzinfo=UTC)

        assert self._depth_times(content, wall_clock, None) == [160]

    def test_a_file_whose_clock_started_later_is_moved_later(self) -> None:
        """The corpus's Ocean: its JSON starts at `.67` of the second its FIT starts on."""
        content = _suunto_app_export("2025-05-31T12:59:06.670+02:00", "2025-05-31T12:59:06.830+02:00")

        assert self._depth_times(content, self.START, 120) == [830]

    def test_a_reading_before_the_recordings_start_is_clamped_to_it(self) -> None:
        content = _suunto_app_export(
            "2025-05-31T12:59:05.000+02:00", "2025-05-31T12:59:05.500+02:00", "2025-05-31T12:59:16.000+02:00"
        )

        assert self._depth_times(content, self.START, 120) == [0, 10_000]

    def test_a_recording_with_no_stored_start_takes_the_first_files(self) -> None:
        first = _suunto_app_export(self.HEADER, "2025-05-31T12:59:06.160+02:00")
        second = json.dumps(
            {
                "DeviceLog": {
                    "Header": {"DateTime": "2025-05-31T12:59:08.000+02:00"},
                    "Samples": [{"Temperature": 299.15, "TimeISO8601": "2025-05-31T12:59:08.000+02:00"}],
                }
            }
        ).encode()

        extraction = extract_recording([_loaded(first), _loaded(second)], start_time=None, utc_offset_minutes=None)

        assert extraction.profile is not None
        assert extraction.profile.depth is not None and extraction.profile.temperature is not None
        assert extraction.profile.depth.t == [160]
        assert extraction.profile.temperature.t == [2000]


class TestTheReadoutFieldsAreTheRecordings:
    def test_they_are_the_read_schemas_and_the_recordings_columns(self) -> None:
        """Written onto `dive_recording` by name, so a readout that is not one of its columns
        would raise at attach time, on a real upload."""
        assert set(READOUT_FIELDS) == set(RecordingReadouts.model_fields)
        assert set(READOUT_FIELDS) <= set(ParsedDiveSchema.model_fields)
        assert set(READOUT_FIELDS) <= set(DiveRecording.__table__.columns.keys())
        assert not set(READOUT_FIELDS) & set(Dive.__table__.columns.keys())
