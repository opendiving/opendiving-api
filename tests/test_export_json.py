"""Tests for the DiveJSON writer (`services/export/envelope.py`).

Three jobs, and they are not interchangeable.

**Conformance.** The app claims to be DiveJSON's reference implementation, so the streamed
bytes are checked against the published JSON Schema *and* against the rules spec §3 says
the schema cannot express - identifier closure, cross-member arithmetic, profile-series
integrity, the `exported_at` offset, the member order. `divejson.validate_document` is that
whole rule set, from the reference implementation itself rather than a port of it; a
schema-only check would pass documents `divejson validate` rejects, which is not a
hypothetical (see `test_the_checker_is_not_vacuous`).

**The declared shape.** `envelope.py` streams the file a record at a time rather than
serializing an `ExportEnvelope`, so the declared shape is *not* on the write path and
nothing but a test stops the two drifting apart. Validating the streamed bytes back
through the model is what makes `schemas/export.py` a specification rather than
documentation that happens to be Python.

**The promise the format makes**: everything UDDF cannot hold is in here, and what is here
refers to other records by public uuid rather than by an internal id that means nothing
outside this database.
"""

import json
import uuid as uuid_pkg
from dataclasses import replace
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import divejson
import pytest

from src.app.models.certification import Certification
from src.app.models.course import Course
from src.app.models.gear_item import GearItem
from src.app.models.gear_service_record import GearServiceRecord
from src.app.models.gear_service_schedule import GearServiceSchedule
from src.app.models.trip import Trip
from src.app.schemas.export import DIVEJSON_FORMAT, DIVEJSON_VERSION, ExportCourse, ExportEnvelope
from src.app.schemas.location import LocationRead
from src.app.schemas.trip import TripPartRead
from src.app.schemas.user import CHECK_IN_FIELDS
from src.app.services.dive_profiles import MERGE_PARSER_KEY, LoadedProfile
from src.app.services.export.envelope import write_divejson
from src.app.services.export.paths import plan_archive_paths
from src.app.services.export.tabular import CSV_WRITERS
from src.app.services.export.uddf import write_uddf
from src.app.services.logbook_import import parse_document
from tests.helpers.export import (
    CREATED_AT,
    EXPORTED_AT,
    PRIMARY_RECORDING_ID,
    TRIMIX_PROFILE,
    UUIDS,
    _with_id,
    build_bundle,
    full_bundle,
    make_dive,
    make_recording,
    make_user,
    mixture,
)


async def _stream(bundle: Any, monkeypatch: Any, profiles: dict[int, dict] | None = None, duration: int = 90) -> bytes:
    payloads = profiles or {}

    async def fake_load_profile(db: Any, *, recording_id: int) -> LoadedProfile | None:
        data = payloads.get(recording_id)
        return None if data is None else LoadedProfile(duration=duration, data=data, parser_key="suunto_xml")

    monkeypatch.setattr("src.app.services.export.envelope.load_profile", fake_load_profile)
    return b"".join([chunk async for chunk in write_divejson(AsyncMock(), bundle, exported_at=EXPORTED_AT)])


async def _render(
    bundle: Any,
    monkeypatch: Any,
    profiles: dict[int, dict] | None = None,
    paths: Any = None,
    duration: int = 90,
    parser_key: str = "suunto_xml",
) -> dict:
    payloads = profiles or {}

    async def fake_load_profile(db: Any, *, recording_id: int) -> LoadedProfile | None:
        data = payloads.get(recording_id)
        return None if data is None else LoadedProfile(duration=duration, data=data, parser_key=parser_key)

    monkeypatch.setattr("src.app.services.export.envelope.load_profile", fake_load_profile)
    chunks = [chunk async for chunk in write_divejson(AsyncMock(), bundle, exported_at=EXPORTED_AT, paths=paths)]
    # Through the format's own parser, which refuses duplicate member names (spec §9) -
    # so a writer that emitted a key twice fails here rather than silently losing one.
    document: dict = parse_document(b"".join(chunks))
    return document


def _issues(document: Any) -> list[str]:
    """Every way `document` fails DiveJSON 1.0, exactly as `divejson validate` reports it."""
    return [str(issue) for issue in divejson.validate_document(document)]


def _assert_conforms(document: Any) -> None:
    issues = _issues(document)
    assert not issues, "document is not conforming DiveJSON:\n" + "\n".join(issues)


def _nulls(value: Any, path: str = "$") -> list[str]:
    """Every path in the document holding an explicit null."""
    if value is None:
        return [path]
    if isinstance(value, dict):
        return [issue for key, item in value.items() for issue in _nulls(item, f"{path}/{key}")]
    if isinstance(value, list):
        return [issue for index, item in enumerate(value) for issue in _nulls(item, f"{path}/{index}")]
    return []


class TestConformance:
    """The claim the whole exporter exists to make, and the only test here that can catch a
    writer producing something `divejson validate` would reject."""

    @pytest.mark.asyncio
    async def test_the_awkward_case_logbook_is_a_conforming_divejson_document(self, monkeypatch):
        _assert_conforms(await _render(full_bundle(), monkeypatch, {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}))

    @pytest.mark.asyncio
    async def test_an_empty_logbook_is_a_conforming_document_too(self, monkeypatch):
        """The floor case: a fresh account with nothing in it still exports something a
        reader can dispatch on."""
        _assert_conforms(await _render(build_bundle(), monkeypatch))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("label", "parts"),
        [
            ("no dates anywhere", [TripPartRead(location=LocationRead(name="Dahab"))]),
            ("an end and no start", [TripPartRead(end_date=date(2026, 6, 8))]),
            ("no parts at all", []),
        ],
    )
    async def test_a_trip_no_part_of_which_starts_is_a_document_like_any_other(self, monkeypatch, label, parts):
        """The three shapes the envelope could not express while a trip carried a span.

        `starts_on` was REQUIRED on a trip and §5.4 forbids inventing one, so each of these
        used to fail the export outright rather than produce a document. A trip's span is
        its parts' now, and a trip that has none simply has none.
        """
        trip = _with_id(Trip(user_id=1, name=f"Egypt, {label}", notes="", uuid=UUIDS["trip"], created_at=CREATED_AT), 1)
        document = await _render(build_bundle(trips=[trip], parts_by_trip={1: parts}), monkeypatch)

        _assert_conforms(document)
        exported = document["trips"][0]
        assert len(exported["parts"]) == len(parts)
        assert "starts_on" not in exported

    @pytest.mark.asyncio
    async def test_an_archive_layout_stays_conforming(self, monkeypatch):
        """`archive_path` is the one member a document grows inside a zip, and it has its
        own constraints (spec §6.7 and Appendix A)."""
        bundle = full_bundle()
        document = await _render(
            bundle, monkeypatch, {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, paths=plan_archive_paths(bundle)
        )
        _assert_conforms(document)

    @pytest.mark.asyncio
    async def test_the_checker_is_not_vacuous(self, monkeypatch):
        """A conformance assertion nobody has watched fail proves nothing. Breaking one
        rule from each of spec §3's checkable classes has to be caught."""
        document = await _render(full_bundle(), monkeypatch, {PRIMARY_RECORDING_ID: TRIMIX_PROFILE})

        broken = json.loads(json.dumps(document))
        broken["dives"][0]["avg_depth"] = broken["dives"][0]["max_depth"] + 1
        assert any("avg_depth exceeds max_depth" in issue for issue in _issues(broken))

        broken = json.loads(json.dumps(document))
        broken["dives"][0]["site_uuids"] = ["019f0000-0000-7000-8000-000000000099"]
        assert any("not present in sites" in issue for issue in _issues(broken))

        broken = json.loads(json.dumps(document))
        broken["dives"][1]["recordings"][0]["profile"]["depth"]["values"].append(1)
        assert any("samples but values has" in issue for issue in _issues(broken))

        broken = json.loads(json.dumps(document))
        broken["exported_at"] = broken["exported_at"].replace("+00:00", "")
        assert any("must carry a UTC offset" in issue for issue in _issues(broken))

        broken = {"version": document["version"], **document}
        del broken["format"]
        assert any('"format" MUST come first' in issue for issue in _issues(broken))

        broken = json.loads(json.dumps(document))
        broken["dives"][0]["mixtures"] = []
        assert any("Additional properties" in issue for issue in _issues(broken))

    @pytest.mark.asyncio
    async def test_the_document_holds_no_explicit_nulls(self, monkeypatch):
        """Absence is the only spelling of "not recorded" (spec §5.4), and the schema
        rejects a null wherever one could appear - but only on a member that is *there*
        to be typed. This says it for the document as a whole, including the extension
        payloads the schema leaves open.
        """
        document = await _render(full_bundle(), monkeypatch, {PRIMARY_RECORDING_ID: TRIMIX_PROFILE})
        assert _nulls(document) == []

    @pytest.mark.asyncio
    async def test_an_event_after_the_last_sample_is_left_where_it_is(self, monkeypatch):
        """The case the specification was amended for.

        `services/dive_profiles.py` deliberately does not clamp the high end - a FIT
        `user_marker` can be pressed after the final `record`, and dragging it back onto
        the last sample would invent a time to keep it on screen. `profile.duration` spans
        the *samples*, so an event past it is conforming (spec §6.4) and must travel
        unmoved: neither clamped, nor dropped, nor swallowed by a widened `duration`.
        """
        profile = {
            "depth": {"t": [0, 30, 60], "v": [0, 1800, 300]},
            "events": [{"t": 95, "type": "bookmark"}],
        }
        document = await _render(full_bundle(), monkeypatch, {PRIMARY_RECORDING_ID: profile}, duration=60)

        assert document["dives"][1]["recordings"][0]["profile"]["duration"] == 60
        assert document["dives"][1]["recordings"][0]["profile"]["events"] == [{"time": 95, "type": "bookmark"}]
        assert _issues(document) == []


class TestTheDeclaredShape:
    @pytest.mark.asyncio
    async def test_the_streamed_bytes_validate_against_export_envelope(self, monkeypatch):
        """The whole reason `ExportEnvelope` is worth declaring - see the module docstring."""
        document = await _render(full_bundle(), monkeypatch, {PRIMARY_RECORDING_ID: TRIMIX_PROFILE})
        envelope = ExportEnvelope.model_validate(document)
        assert len(envelope.dives) == 3

    @pytest.mark.asyncio
    async def test_an_empty_logbook_is_still_a_valid_envelope(self, monkeypatch):
        document = await _render(build_bundle(), monkeypatch)
        envelope = ExportEnvelope.model_validate(document)
        assert envelope.dives == []
        assert envelope.diver.username == "ada"

    @pytest.mark.asyncio
    async def test_format_and_version_come_first(self, monkeypatch):
        """A reader has to be able to dispatch on them before parsing anything else, which
        is why the spec makes it a rule about the document's text rather than a courtesy."""
        document = await _render(full_bundle(), monkeypatch)
        assert list(document)[:2] == ["format", "version"]
        assert (document["format"], document["version"]) == (DIVEJSON_FORMAT, DIVEJSON_VERSION)
        assert (document["format"], document["version"]) == ("divejson", "1.0")

    @pytest.mark.asyncio
    async def test_the_version_is_a_string_so_a_minor_bump_can_be_additive(self, monkeypatch):
        """The old format's bare integer could not signal an additive change without
        either lying or breaking every reader (spec §7)."""
        document = await _render(full_bundle(), monkeypatch)
        assert isinstance(document["version"], str)

    @pytest.mark.asyncio
    async def test_every_declared_collection_is_present(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch)
        assert set(document) == set(ExportEnvelope.model_fields)


class TestTheLayout:
    """Whitespace only - which JSON gives no meaning and a person reading the file does.

    Every top-level member opens a line and every record sits on one, so `head`, a pager or
    a diff of two exports shows the document's structure instead of one unbounded line per
    collection. It is pinned here because it is invisible to every other test in this file:
    they all parse the bytes first, and a parser cannot tell the two layouts apart.
    """

    @pytest.mark.asyncio
    async def test_every_top_level_member_opens_a_line(self, monkeypatch):
        body = (await _stream(full_bundle(), monkeypatch)).decode()
        lines = body.splitlines()
        assert lines[0].startswith('{"format": ')
        for member in list(ExportEnvelope.model_fields)[1:]:
            assert any(line.startswith(f'"{member}": ') for line in lines), member

    @pytest.mark.asyncio
    async def test_every_collection_writes_one_record_to_a_line(self, monkeypatch):
        """`dives` has been written this way since it was streamed a dive at a time; the
        other collections are encoded in one go and used to arrive as a single line."""
        body = (await _stream(full_bundle(), monkeypatch, {PRIMARY_RECORDING_ID: TRIMIX_PROFILE})).decode()
        lines = body.splitlines()
        collections = {
            member: records for member, records in parse_document(body.encode()).items() if isinstance(records, list)
        }
        # Off the document rather than a written-out list, so a collection added later is
        # covered by this the day it is written.
        assert "dives" in collections
        for member, records in collections.items():
            opening = next(index for index, line in enumerate(lines) if line.startswith(f'"{member}": ['))
            if not records:
                assert lines[opening].startswith(f'"{member}": []')
                continue
            written = lines[opening + 1 : opening + 1 + len(records)]
            assert all(line.startswith("{") for line in written), member
            assert lines[opening + 1 + len(records)].startswith("]"), member


class TestWhatUddfCannotHold:
    """The reason this document exists beside `dives.uddf`."""

    @pytest.mark.asyncio
    async def test_gear_sets_service_history_and_c_cards_are_all_here(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch)
        assert [s["name"] for s in document["gear_sets"]] == ["Tech"]
        assert len(document["gear_service_schedules"]) == 1
        assert len(document["gear_service_records"]) == 1
        assert [c["name"] for c in document["certifications"]] == ["Open Water Diver"]

    @pytest.mark.asyncio
    async def test_training_courses_are_here_with_every_attribute(self, monkeypatch):
        """UDDF's nearest element is `<divetrip>`, which a training course is not - so this
        document is the only place a course survives the export at all.

        The key-set assertion is what makes the name a promise rather than a hope: the
        spot checks below only ever grew when someone remembered to add one, so a field
        added to `ExportCourse` and never written - or one removed from the schema and
        still written here - passed unnoticed. It is the same shape
        `test_every_declared_collection_is_present` uses one level up. `agency_other` is
        the one exception: the format forbids it on a course whose agency is not `other`
        (spec §6.17), so it is absent here by rule rather than by omission.
        """
        document = await _render(full_bundle(), monkeypatch)
        (course,) = document["courses"]

        assert set(course) == set(ExportCourse.model_fields) - {"agency_other"}
        assert course["name"] == "Advanced Nitrox + Decompression Procedures"
        assert (course["agency"], course["status"]) == ("tdi", "completed")
        assert (course["starts_on"], course["ends_on"]) == ("2026-03-02", "2026-03-06")
        assert (course["instructor_name"], course["instructor_number"]) == ("Jae Kim", "TDI-88121")
        assert course["training_center"] == "Blue Ocean"

    @pytest.mark.asyncio
    async def test_the_account_preferences_travel_under_this_producer_s_key(self, monkeypatch):
        """`/export/archive` promises nothing in the account is reachable only through the
        app, and these are the whole of what a diver can set *as a preference*.

        They ride `extensions.opendiving` because they are application preferences, not
        logbook data, and the format gives them no core member (spec §6.1) - a writer may
        not invent one. `units` in particular travels as *account data*: it says which
        system the diver reads in, and every measurement in this document stays metric
        regardless.

        The dive-form settings are here for that promise and nothing else - they configure
        a form no reader of this document has. A preset travels as `{name, hidden_fields}`:
        its uuid and timestamps identify a row in *this* instance and mean nothing anywhere
        else. Asserting the whole extension object rather than its keys one at a time is the
        point - a preference added without a decision fails here.
        """
        document = await _render(full_bundle(), monkeypatch)
        assert document["diver"]["extensions"] == {
            "opendiving": {
                "units": "metric",
                "gear_service_emails": True,
                "dive_form_hidden_fields": ["altitude", "mixture.po2_limit"],
                "dive_form_presets": [
                    {"name": "Recreational", "hidden_fields": ["altitude", "mixture.po2_limit"]},
                    # The empty set is written as `[]`, not omitted: "Technical hides
                    # nothing" is a preset, and a reader that saw no key could not tell it
                    # from a preset that failed to export.
                    {"name": "Technical", "hidden_fields": []},
                ],
            }
        }
        assert document["dives"][0]["max_depth"] == 28.4

    @pytest.mark.asyncio
    async def test_the_per_cylinder_role_ppo2_limit_and_usage_survive(self, monkeypatch):
        """`role` and `usage` are the two UDDF has no slot for, which is why this document
        exists. `po2_limit` is asserted beside them because it completes the cylinder's gas
        planning, not because it is lost - it maps to `<mix><maximumpo2>`, which is why
        `_MixKey` dedupes on it and why `test_the_planned_ppo2_lands_in_maximumpo2` pins it.

        `usage` rides in for free on `DiveMixtureBase` - the envelope re-wraps every read
        as that schema - so this is what would catch it silently not doing so. The absent
        `usage` is *absent* rather than null, which is the format's only spelling of it.
        """
        document = await _render(full_bundle(), monkeypatch)
        cylinders = document["dives"][1]["cylinders"]
        assert [(c["role"], c["po2_limit"], c.get("usage")) for c in cylinders] == [
            ("bottom", 1.4, None),
            ("deco", 1.6, "staged"),
        ]
        assert "usage" not in cylinders[0]

    @pytest.mark.asyncio
    async def test_a_cylinder_omits_the_size_and_mix_it_never_recorded(self, monkeypatch):
        """All three are OPTIONAL in the format (§6.3), and absent is how it spells "not
        recorded" - `oxygen` explicitly so, where the absence must not be read as 21. The
        encoder's `exclude_none=True` is what makes this true for free, which is also what
        makes it worth pinning: nothing else in this file would notice a `"volume": null`
        appearing in a document that promises to invent nothing.
        """
        bundle = build_bundle(
            dives=[make_dive(1, UUIDS["dive-air"])],
            mixtures_by_dive={1: [mixture(volume=None, oxygen=None, helium=None, start_pressure=200.0)]},
        )
        document = await _render(bundle, monkeypatch)

        cylinder = document["dives"][0]["cylinders"][0]
        assert cylinder == {"start_pressure": 200.0}
        _assert_conforms(await _render(bundle, monkeypatch))

    @pytest.mark.asyncio
    async def test_cns_and_otu_are_here_since_uddf_has_no_slot_for_them(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch)
        assert (document["dives"][0]["cns_end"], document["dives"][0]["otu_end"]) == (8.0, 21.0)

    @pytest.mark.asyncio
    async def test_the_entry_and_exit_positions_are_here_for_the_same_reason(self, monkeypatch):
        """UDDF 3.2.2 hangs `<geography>` off a `<site>` and nowhere else - neither
        `informationbeforedive` nor `waypoint` has a coordinate element - so a dive's own
        two positions survive only in this document and in `dives.csv`. They are Position
        *objects*: half a coordinate is unrepresentable, so the format groups the pair
        rather than trusting a writer to emit both."""
        document = await _render(full_bundle(), monkeypatch)
        dive = document["dives"][0]
        assert dive["entry_position"] == {"latitude": 27.7278, "longitude": 34.2564}
        assert dive["exit_position"] == {"latitude": 27.7291, "longitude": 34.2572}

    @pytest.mark.asyncio
    async def test_a_dive_with_only_an_exit_position_exports_one(self, monkeypatch):
        """Which is the ordinary shape: no wrist computer gets a fix before the descent."""
        document = await _render(full_bundle(), monkeypatch)
        dive = document["dives"][1]
        assert "entry_position" not in dive
        assert dive["exit_position"] == {"latitude": 27.7315, "longitude": 34.259}

    @pytest.mark.asyncio
    async def test_the_water_type_is_here_because_uddf_has_no_per_dive_slot(self, monkeypatch):
        """3.2.2's `density` elements are site-level (`sitedata`) and deco-planner input
        (`baseCalculationType`) - neither is a fact about one dive - so this document and
        `dives.csv` are the only two that carry it. `altitude` is the counter-example: it
        has a real slot, and the UDDF gets it too - and a *recorded* 0 is written, since
        absence is what "not recorded" means and 0 is a reading."""
        document = await _render(full_bundle(), monkeypatch)
        assert document["dives"][0]["water_type"] == "salt"
        assert document["dives"][0]["altitude"] == 0
        assert "water_type" not in document["dives"][1]
        assert "altitude" not in document["dives"][1]

    @pytest.mark.asyncio
    async def test_the_ceiling_channel_survives_in_the_embedded_profile(self, monkeypatch):
        """UDDF drops it (`<decostop>` needs a duration we do not have); this must not."""
        document = await _render(full_bundle(), monkeypatch, {PRIMARY_RECORDING_ID: TRIMIX_PROFILE})
        assert document["dives"][1]["recordings"][0]["profile"]["ceiling"] == {"times": [60, 90], "values": [600, 300]}


class TestTheCheckInDetails:
    """What a dive shop's desk asks for, as core Diver members (spec §6.1)."""

    @pytest.mark.asyncio
    async def test_they_are_core_diver_members(self, monkeypatch):
        """The contact and the insurance are one-element arrays, the account holding one of
        each. The two members left unset are absent rather than null, the format's one
        spelling of "not recorded". The producer entry keeps the preferences and none of
        these, and the document conforms - no other test here validates a diver carrying
        them, which is what makes this the check on the `divejson` floor.
        """
        bundle = replace(
            full_bundle(),
            user=make_user(
                dive_form_hidden_fields=["altitude", "mixture.po2_limit"],
                date_of_birth=date(1988, 4, 12),
                phone="+20 100 123 4567",
                emergency_contact_name="Grace Hopper",
                emergency_contact_phone="+1 202 555 0143",
                insurance_provider="DAN Europe",
                insurance_expires_on=date(2027, 6, 30),
            ),
        )

        document = await _render(bundle, monkeypatch)
        diver = document["diver"]

        assert diver["born_on"] == "1988-04-12"
        assert diver["phone"] == "+20 100 123 4567"
        assert diver["emergency_contacts"] == [{"name": "Grace Hopper", "phone": "+1 202 555 0143"}]
        assert diver["insurances"] == [{"provider": "DAN Europe", "expires_on": "2027-06-30"}]
        assert set(diver["extensions"]["opendiving"]).isdisjoint(CHECK_IN_FIELDS)
        _assert_conforms(document)

    @pytest.mark.asyncio
    async def test_an_account_that_filled_in_none_carries_none(self, monkeypatch):
        diver = (await _render(full_bundle(), monkeypatch))["diver"]

        assert set(diver).isdisjoint({"born_on", "phone", "emergency_contacts", "insurances"})

    @pytest.mark.asyncio
    async def test_a_contact_with_no_name_and_a_policy_with_no_provider_are_left_out(self, monkeypatch):
        """Their anchors are REQUIRED in the format, and a row saved before `PATCH /user`
        required them can lack one. A blank string is no anchor either, and a blank phone
        is no phone."""
        bundle = replace(
            full_bundle(),
            user=make_user(
                phone=" ",
                emergency_contact_name="",
                emergency_contact_phone="+1 202 555 0143",
                insurance_policy_number="DE-4471902",
            ),
        )

        document = await _render(bundle, monkeypatch)

        assert set(document["diver"]).isdisjoint({"phone", "emergency_contacts", "insurances"})
        _assert_conforms(document)


class TestTheProfileVocabulary:
    """One profile vocabulary across the app: `GET /dive/{uuid}/recording/{rid}/profile` and this document
    both serve `DiveProfileRead`, whose member names are the format's.

    The route serves a subclass of it carrying the profile's provenance, which is where the
    one vocabulary stops: the schema's `profile` object is `additionalProperties: false`, so
    a member DiveJSON has no slot for makes every document invalid rather than merely
    verbose. The exact-set assertion below is what holds the line.
    """

    @pytest.mark.asyncio
    async def test_the_channels_and_events_use_the_format_s_member_names(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch, {PRIMARY_RECORDING_ID: TRIMIX_PROFILE})
        profile = document["dives"][1]["recordings"][0]["profile"]

        assert set(profile) == {
            "duration",
            "depth",
            "ceiling",
            "temperature",
            "pressures",
            "ndl",
            "tts",
            "ppo2",
            "cns",
            "gradient_factor",
            "surface_gradient_factor",
            "events",
        }
        assert profile["depth"] == {"times": [0, 30, 60, 90], "values": [0, 1800, 5200, 300]}
        assert profile["pressures"][0] == {"times": [0, 60], "values": [2320, 1400], "gas_number": 1}
        assert profile["events"][0] == {"time": 0, "type": "gas_switch", "gas_number": 1}

    @pytest.mark.asyncio
    async def test_the_members_are_written_in_the_formats_order(self, monkeypatch):
        """§6.4 puts `pressures` between `temperature` and `ndl`, so a writer emitting
        channels in whatever order it happened to build them produces a profile that reads
        down nothing. The order comes off `DiveProfileRead`'s own declaration."""
        document = await _render(full_bundle(), monkeypatch, {PRIMARY_RECORDING_ID: TRIMIX_PROFILE})

        assert list(document["dives"][1]["recordings"][0]["profile"]) == [
            "duration",
            "depth",
            "ceiling",
            "temperature",
            "pressures",
            "ndl",
            "tts",
            "ppo2",
            "cns",
            "gradient_factor",
            "surface_gradient_factor",
            "events",
        ]

    @pytest.mark.asyncio
    async def test_the_decompression_channels_keep_their_own_scales(self, monkeypatch):
        """Seconds, hundredths of a bar, tenths of a percent and whole percent - all fixed by
        the format (spec §5.1), and none of them depth's or temperature's."""
        profile = (await _render(full_bundle(), monkeypatch, {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}))["dives"][1][
            "recordings"
        ][0]["profile"]

        assert profile["ndl"] == {"times": [0, 30, 60], "values": [5940, 1260, 0]}
        assert profile["tts"]["values"] == [268, 120]
        assert profile["ppo2"]["values"] == [34, 96]
        assert profile["cns"]["values"] == [100, 800]
        # Unbounded above: 398 is a compartment past its M-value, and a writer that clamped
        # would be deciding what the device should have written.
        assert profile["gradient_factor"]["values"] == [17, 398]
        assert profile["surface_gradient_factor"]["values"] == [90, 116]

    @pytest.mark.asyncio
    async def test_an_unclassified_event_is_written_with_no_type(self, monkeypatch):
        """§6.6 spells "the device recorded something and nothing in the vocabulary says
        what" as an *absent* `type` beside a required `label`; storage spells it `other`.
        A document carrying `"type": "other"` is invalid, which is what makes this the one
        place the two vocabularies have to meet."""
        document = await _render(full_bundle(), monkeypatch, {PRIMARY_RECORDING_ID: TRIMIX_PROFILE})
        events = document["dives"][1]["recordings"][0]["profile"]["events"]

        unclassified = [event for event in events if "type" not in event]
        assert unclassified == [{"time": 60, "label": "Ceiling Broken"}]
        assert all(event.get("type") != "other" for event in events)
        _assert_conforms(document)


class TestTheRecordingsModeAndDecoModel:
    """§6.4a's `mode` and the whole of §6.4c, which are the device's and not the dive's."""

    @pytest.mark.asyncio
    async def test_both_members_ride_the_recording(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch, {PRIMARY_RECORDING_ID: TRIMIX_PROFILE})
        recording = document["dives"][1]["recordings"][0]

        assert recording["mode"] == "open_circuit"
        assert recording["deco_model"] == {
            "algorithm": "buhlmann",
            "name": "ZHL-16C",
            "gf_low": 50,
            "gf_high": 85,
            # Unfloored: a negative is the device's own P-1 rather than an absent-marker.
            "conservatism": -1,
        }
        assert "mode" not in document["dives"][1]
        assert "deco_model" not in document["dives"][1]

    @pytest.mark.asyncio
    async def test_a_recording_that_recorded_neither_writes_neither(self, monkeypatch):
        """Absence is the only spelling of "not recorded", and an empty `deco_model` object
        says nothing a missing one does not (§6.4c)."""
        bundle = full_bundle()
        bundle.recordings_by_dive[2][0] = replace(bundle.recordings_by_dive[2][0], mode=None, deco_model={})

        recording = (await _render(bundle, monkeypatch, {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}))["dives"][1][
            "recordings"
        ][0]

        assert "mode" not in recording
        assert "deco_model" not in recording

    @pytest.mark.asyncio
    async def test_a_stored_mode_the_format_has_no_word_for_drops_the_field_not_the_record(self, monkeypatch):
        """`_sayable`'s rule for an OPTIONAL member: the column carries no `CHECK`, so it
        really can hold a value outside the vocabulary - and a diver's profile must not
        vanish from their export over how a mode is spelt."""
        bundle = full_bundle()
        bundle.recordings_by_dive[2][0] = replace(bundle.recordings_by_dive[2][0], mode="rebreather")

        document = await _render(bundle, monkeypatch, {PRIMARY_RECORDING_ID: TRIMIX_PROFILE})
        recording = document["dives"][1]["recordings"][0]

        assert "mode" not in recording
        assert recording["profile"]["depth"]["values"] == [0, 1800, 5200, 300]
        _assert_conforms(document)

    @pytest.mark.asyncio
    async def test_a_stored_algorithm_the_format_has_no_word_for_keeps_the_rest_of_the_model(self, monkeypatch):
        bundle = full_bundle()
        bundle.recordings_by_dive[2][0] = replace(
            bundle.recordings_by_dive[2][0],
            deco_model={"algorithm": "vpm", "name": "VPM-B", "conservatism": 3},
        )

        document = await _render(bundle, monkeypatch, {PRIMARY_RECORDING_ID: TRIMIX_PROFILE})

        assert document["dives"][1]["recordings"][0]["deco_model"] == {"name": "VPM-B", "conservatism": 3}
        _assert_conforms(document)

    @pytest.mark.asyncio
    async def test_neither_member_satisfies_the_rule_that_a_recording_describes_something(self, monkeypatch):
        """§3's rule 4 counts three members and these two are not among them: a mode with no
        device, no samples and no file behind it is a setting nothing recorded a dive with.
        """
        bundle = full_bundle()
        bundle.recordings_by_dive[2] = [
            make_recording(9, UUIDS["recording-second"], mode="gauge", deco_model={"algorithm": "buhlmann"})
        ]

        document = await _render(bundle, monkeypatch)

        assert document["dives"][1]["recordings"] == []
        _assert_conforms(document)

    @pytest.mark.asyncio
    async def test_the_samples_keep_the_stored_integer_scales(self, monkeypatch):
        """Depth in centimeters, temperature in tenths of a degree - the scales are part of
        the format (spec §5.1), chosen so a round trip cannot introduce float noise."""
        document = await _render(full_bundle(), monkeypatch, {PRIMARY_RECORDING_ID: TRIMIX_PROFILE})
        profile = document["dives"][1]["recordings"][0]["profile"]
        assert profile["depth"]["values"] == [0, 1800, 5200, 300]
        assert profile["temperature"] == {"times": [0, 60], "values": [249, 181]}

    @pytest.mark.asyncio
    async def test_a_merged_profile_exports_without_its_provenance(self, monkeypatch):
        """The interesting half of the previous test's exact set, spelled out: a merge is the
        provenance most worth carrying and the document still may not carry it. The value the
        app publishes on its own routes has no core member here, and a writer may not invent
        one - `extensions.opendiving` is the sanctioned slot if it ever earns a place.
        """
        document = await _render(
            full_bundle(), monkeypatch, {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, parser_key=MERGE_PARSER_KEY
        )

        profile = document["dives"][1]["recordings"][0]["profile"]
        assert "provenance" not in profile
        _assert_conforms(document)


class TestReferences:
    @pytest.mark.asyncio
    async def test_records_reference_each_other_by_public_uuid(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch)
        assert document["dives"][0]["trip_uuid"] == str(UUIDS["trip"])
        assert document["gear_sets"][0]["gear_uuids"] == [
            str(UUIDS["gear-regulator"]),
            str(UUIDS["gear-suit"]),
        ]
        assert document["gear_service_records"][0]["gear_service_schedule_uuid"] == str(UUIDS["schedule"])

    @pytest.mark.asyncio
    async def test_both_kinds_of_child_point_at_the_course_by_uuid(self, monkeypatch):
        """The grouping is expressed on the children, not as a list on the course - so a
        reader rebuilds it by walking `dives` and `certifications`. Both directions are
        asserted because they are two independent mappings in `envelope.py`."""
        document = await _render(full_bundle(), monkeypatch)

        assert document["dives"][0]["course_uuid"] == str(UUIDS["course"])
        assert document["certifications"][0]["course_uuid"] == str(UUIDS["course"])
        # And the dives that were not on it say so by absence, rather than inheriting the
        # reference or carrying a null.
        assert all("course_uuid" not in dive for dive in document["dives"][1:])

    @pytest.mark.asyncio
    async def test_a_dive_site_carries_its_position(self, monkeypatch):
        """`ExportEnvelope` defaults it to `None`, so dropping the mapping in
        `envelope.py` would leave every other assertion here green."""
        document = await _render(full_bundle(), monkeypatch)
        assert document["sites"][0]["position"] == {"latitude": 27.7278, "longitude": 34.2564}

    @pytest.mark.asyncio
    async def test_a_dive_sites_locality_is_the_whole_place_and_not_its_pin(self, monkeypatch):
        """§6.9's object on a site, and §6.10's rule that its two positions are different
        facts: the locality's centre and box are the *place's*, and the site's own pin is
        the `position` beside them. A writer that filled either from the other would pass
        every other assertion in this file.
        """
        document = await _render(full_bundle(), monkeypatch)
        site = document["sites"][0]

        assert site["location"] == {
            "name": "Ras Mohammed, Egypt",
            "full_name": "Ras Muhammad National Park, South Sinai, Egypt",
            "position": {"latitude": 27.7333, "longitude": 34.25},
            "bbox": {"south": 27.68, "north": 27.83, "west": 34.18, "east": 34.3},
        }
        assert site["location"]["position"] != site["position"]

    @pytest.mark.asyncio
    async def test_a_dive_site_with_no_locality_carries_no_location_member(self, monkeypatch):
        """The common shape, and absence is how the format spells it (§5.4) - not an empty
        object and not a null."""
        document = await _render(full_bundle(), monkeypatch)

        assert "location" not in document["sites"][1]

    @pytest.mark.asyncio
    async def test_multi_site_visit_order_is_a_list_not_a_primary_site(self, monkeypatch):
        """A dive's sites are an ordered list with the primary at index 0, which is what a
        drift dive needs and what a single `site_uuid` could not express.

        This lived in `TestWhatUddfCannotHold` until the class stopped being true of it:
        `informationbeforedive/link` is `maxOccurs="unbounded"`, and
        `test_every_site_is_linked_in_visit_order` pins the same itinerary in `dives.uddf`.
        The ordering is the claim here, not the survival.
        """
        document = await _render(full_bundle(), monkeypatch)
        assert document["dives"][0]["site_uuids"] == [str(UUIDS["site-reef"]), str(UUIDS["site-wall"])]

    @pytest.mark.asyncio
    async def test_a_trip_is_its_parts_each_with_its_own_dates_and_place(self, monkeypatch):
        """The flat formats join the places into a string and collapse the dates into one
        range; this is the document that keeps each stretch whole, box included, so a
        reader can redraw the trip's map and say which week was where."""
        document = await _render(full_bundle(), monkeypatch)
        trip = document["trips"][0]
        # No span of its own, derived or otherwise - the trip's dates are its parts' now.
        assert "starts_on" not in trip and "ends_on" not in trip and "locations" not in trip

        parts = trip["parts"]
        assert [part.get("starts_on") for part in parts] == ["2026-05-30", "2026-06-02", None]
        assert [part.get("ends_on") for part in parts] == ["2026-06-02", "2026-06-04", "2026-06-06"]
        assert parts[0]["location"]["name"] == "Sharm el-Sheikh, Egypt"
        assert parts[0]["location"]["full_name"] == "Sharm el-Sheikh, South Sinai, Egypt"
        assert parts[0]["location"]["position"] == {"latitude": 27.9158, "longitude": 34.33}
        assert parts[0]["location"]["bbox"] == {"south": 27.8, "north": 28.0, "west": 34.2, "east": 34.4}
        # The free-text one: a place the geocoder had no answer for is still a place, and
        # it says so by carrying nothing but its name.
        assert parts[1]["location"] == {"name": "Ras Mohammed"}
        # The placeless one: an end date and nothing else, which is a transit day home.
        assert parts[2] == {"ends_on": "2026-06-06"}

    @pytest.mark.asyncio
    async def test_the_species_a_dive_saw_are_a_list_of_uuids_the_document_defines(self, monkeypatch):
        """The catalog is global, so `species` is the one collection here that is not the
        diver's own rows - it is the slice their dives reference. The point of exporting it
        at all is that the document stays self-contained: every uuid a dive names is
        defined in it, which the format requires (spec §5.3) and the conformance check
        enforces.
        """
        document = await _render(full_bundle(), monkeypatch)

        assert document["dives"][0]["species_uuids"] == [
            str(UUIDS["species-clownfish"]),
            str(UUIDS["species-manta"]),
        ]
        # The dive with no sightings says so as an empty list rather than by omitting it -
        # an absent collection and an empty one mean the same thing (spec §4).
        assert document["dives"][2]["species_uuids"] == []
        defined = {species["uuid"] for species in document["species"]}
        for dive in document["dives"]:
            assert set(dive["species_uuids"]) <= defined

    @pytest.mark.asyncio
    async def test_a_species_carries_the_identifier_that_means_something_elsewhere(self, monkeypatch):
        """The uuids are this instance's; `aphia_id` is the World Register of Marine
        Species' own, and the format names it the interchange identity a reader matches
        its catalog on."""
        document = await _render(full_bundle(), monkeypatch)
        clownfish, morays = document["species"]

        assert (clownfish["aphia_id"], clownfish["common_name"]) == (278400, "ocellaris clownfish")
        assert clownfish["wikidata_qid"] == "Q1126155"
        # A family-rank sighting with no common name: both facts survive, so a reader does
        # not render "Muraenidae" as though the diver identified a species.
        assert (morays["rank"], "common_name" in morays) == ("Family", False)

    @pytest.mark.asyncio
    async def test_no_internal_integer_id_leaks(self, monkeypatch):
        """They are an implementation detail of this database and actively misleading in
        a document meant to outlive it."""
        document = await _render(full_bundle(), monkeypatch, {PRIMARY_RECORDING_ID: TRIMIX_PROFILE})
        assert "id" not in document["dives"][0]
        assert "user_id" not in document["diver"]
        assert "gear_uuid" in document["gear_service_records"][0]
        assert "gear_item_id" not in document["gear_service_records"][0]
        # Cylinders too: `DiveMixtureRead` carries the row `id` and the API serves it,
        # but nothing in this document references a cylinder, so it would be the one
        # integer key in it.
        assert "id" not in document["dives"][1]["cylinders"][0]

    @pytest.mark.asyncio
    async def test_a_record_whose_schedule_was_deleted_keeps_its_history(self, monkeypatch):
        """`gear_service_record` outlives the rule it was logged against by design. Deleting
        the schedule nulls `gear_service_schedule_id` through the FK's `ON DELETE SET NULL`,
        so the record keeps every denormalized field and loses only the reference; this
        clears the bundle instead, which is the same thing from the writer's side."""
        bundle = full_bundle()
        bundle.schedules.clear()
        bundle.schedule_by_id.clear()
        document = await _render(bundle, monkeypatch)
        assert "gear_service_schedule_uuid" not in document["gear_service_records"][0]

    @pytest.mark.asyncio
    async def test_started_at_is_the_combined_offset_aware_string(self, monkeypatch):
        """One string carrying both the wall clock and the instant, which is the failure
        every tested UDDF consumer produces and the reason spec §5.2 says so explicitly."""
        document = await _render(full_bundle(), monkeypatch)
        assert document["dives"][0]["started_at"] == "2026-06-01T08:15:00+02:00"


class TestAbsence:
    """ "Not recorded" has exactly one spelling in this format, and it is omission."""

    @pytest.mark.asyncio
    async def test_an_empty_note_is_absent_rather_than_an_empty_string(self, monkeypatch):
        """The column is `NOT NULL` with `""` standing for "the diver wrote nothing", so
        this app cannot tell a blank note from no note. The format can, and writing `""`
        would claim the stronger of the two readings about a distinction the data never
        carried."""
        document = await _render(full_bundle(), monkeypatch)
        assert "notes" not in document["dives"][2]
        assert document["dives"][0]["notes"].startswith("Strong current")

    @pytest.mark.asyncio
    async def test_a_dive_that_recorded_nothing_optional_carries_nothing_optional(self, monkeypatch):
        """The bare dive: no depth, no cylinders, no site, no trip. Every one of those is
        a member the old format wrote as an explicit null."""
        document = await _render(full_bundle(), monkeypatch)
        bare = document["dives"][2]
        assert set(bare) == {
            "uuid",
            "number",
            "started_at",
            "duration",
            "site_uuids",
            "gear_uuids",
            "species_uuids",
            "cylinders",
            # An empty array, like the four above it: a hand-entered dive was recorded by
            # nothing, and the collection members are written empty rather than omitted so a
            # reader never has to tell "no recordings" from "this writer omits the member".
            "recordings",
            "created_at",
        }

    @pytest.mark.asyncio
    async def test_a_course_with_no_agency_omits_the_member(self, monkeypatch):
        """A course taught by a private instructor ran under no agency, and the column now
        says so. The dive logged on it keeps its `course_uuid`: nothing about the course is
        unwritable, so there is nothing for a reference to lose."""
        course = _with_id(
            Course(
                user_id=1,
                name="Sidemount Fundamentals",
                status="completed",
                uuid=UUIDS["course"],
                notes="",
                created_at=CREATED_AT,
            ),
            1,
        )
        bundle = build_bundle(dives=[make_dive(1, UUIDS["dive-air"], course_id=1)], courses=[course])

        document = await _render(bundle, monkeypatch)

        exported = document["courses"][0]
        assert exported["name"] == "Sidemount Fundamentals"
        assert "agency" not in exported and "agency_other" not in exported
        assert document["dives"][0]["course_uuid"] == str(UUIDS["course"])
        _assert_conforms(document)

    @pytest.mark.asyncio
    async def test_an_agency_the_enum_has_no_member_for_is_still_written_as_other(self, monkeypatch):
        """The pair the format does admit, unchanged by the agency becoming optional: a
        national body the vocabulary has no member for travels as `other` plus the name."""
        course = _with_id(
            Course(
                user_id=1,
                name="Plongeur Niveau 2",
                agency="other",
                agency_other="FFESSM",
                status="completed",
                uuid=UUIDS["course"],
                notes="",
                created_at=CREATED_AT,
            ),
            1,
        )
        document = await _render(build_bundle(courses=[course]), monkeypatch)

        assert document["courses"][0]["agency"] == "other"
        assert document["courses"][0]["agency_other"] == "FFESSM"
        _assert_conforms(document)


class TestAnUnrecognizedVocabularyValueDoesNotFiveHundredTheExport:
    """The three writers, and each answers differently because each is bound differently.

    A stored `kind`/`type` outside its enum is legal data - the columns carry no DB `CHECK`
    on purpose (DECISIONS.md, *"A stored vocabulary is read back as a string"*), and revision
    `f9d04a823776` deliberately leaves one behind when repairing it would collide with
    `ux_gear_service_schedule_item_kind_label`. So every reader of those columns has to have
    an answer, and "raise" is not one: the export streams inside the request handler, so a
    `ValueError` or a `ValidationError` there is a 500 on `GET /export/divejson`,
    `GET /export/uddf` and `GET /export/archive` alike.
    """

    @staticmethod
    def _bundle() -> Any:
        """One legal schedule and one carrying the value the migration can strand, on gear
        whose `type` is outside `GearType` the same way."""
        item = _with_id(
            GearItem(
                user_id=1,
                name="AL80",
                brand="Luxfer",
                type="frobnicator",
                uuid=UUIDS["gear-other"],
                created_at=CREATED_AT,
            ),
            1,
        )

        def schedule(row_id: int, kind: str) -> GearServiceSchedule:
            return _with_id(
                GearServiceSchedule(
                    user_id=1,
                    gear_item_id=1,
                    kind=kind,
                    starts_on=date(2026, 1, 1),
                    interval_months=12,
                    # Distinct labels: `ux_gear_service_schedule_item_kind_label` is over
                    # (item, kind, label), so these two coexist on one item in a real
                    # database - which is the collision case the migration declines to
                    # rename, and so the state this whole class is about.
                    label=kind,
                    uuid=uuid_pkg.UUID(f"019f0000-0000-7000-8000-{900 + row_id:012d}"),
                    created_at=CREATED_AT,
                ),
                row_id,
            )

        return build_bundle(
            gear_items=[item],
            schedules=[schedule(1, "service"), schedule(2, "inspection")],
        )

    @pytest.mark.asyncio
    async def test_divejson_omits_the_record_it_cannot_express_and_keeps_the_rest(self, monkeypatch) -> None:
        """`ExportGearServiceSchedule.type` is the format's own enum on an object the schema
        closes, so the value cannot go into the document - but the *other* schedule can, and
        a writer that raised would have taken it with it.
        """
        document = await _render(self._bundle(), monkeypatch)

        assert [s["type"] for s in document["gear_service_schedules"]] == ["service"]
        _assert_conforms(document)

    @pytest.mark.asyncio
    async def test_the_gear_item_itself_still_exports(self, monkeypatch) -> None:
        """`gear_item.type` is optional in the format, so an unrepresentable one costs the
        field, never the item - a diver's cylinder does not disappear from their export
        because of how its category is spelt."""
        document = await _render(self._bundle(), monkeypatch)

        assert [g["name"] for g in document["gear"]] == ["AL80"]
        assert "type" not in document["gear"][0]
        _assert_conforms(document)

    @pytest.mark.asyncio
    async def test_uddf_files_it_under_the_catch_all_element(self) -> None:
        """UDDF has no vocabulary of ours to keep: the value only picks which typed element
        the piece is written into, and `<variouspieces>` is where `OTHER` already goes."""
        xml = b"".join([chunk async for chunk in write_uddf(AsyncMock(), self._bundle(), exported_at=EXPORTED_AT)])

        assert b"frobnicator" not in xml
        assert b"<variouspieces" in xml
        assert b"AL80" in xml

    @pytest.mark.asyncio
    async def test_a_cylinder_keeps_its_pressures_when_its_role_is_unspeakable(self, monkeypatch) -> None:
        """`role`/`usage` are OPTIONAL, and the cylinder is rebuilt as `DiveMixtureBase` on
        the way out - a *write* base, still enum-typed - so the value the read schema carried
        through has to be dropped here rather than handed over."""
        dive = make_dive(1, UUIDS["dive-air"])
        bundle = build_bundle(
            dives=[dive],
            mixtures_by_dive={1: [mixture(role="frobnicator", usage="frobnicator", start_pressure=200.0)]},
        )

        document = await _render(bundle, monkeypatch)

        cylinder = document["dives"][0]["cylinders"][0]
        assert cylinder["start_pressure"] == 200.0
        assert "role" not in cylinder and "usage" not in cylinder
        _assert_conforms(document)

    @pytest.mark.asyncio
    async def test_a_record_pointing_at_an_omitted_schedule_loses_the_link_not_itself(self, monkeypatch) -> None:
        """The state revision `f9d04a823776` actually produces: it renames a record's `kind`
        unconditionally while leaving a colliding schedule at the old value. DiveJSON checks
        referential closure, so a record still naming the omitted schedule would make the
        whole document non-conforming - which fails at the far end, in someone else's
        importer, rather than here.
        """
        bundle = self._bundle()
        bundle.service_records.append(
            _with_id(
                GearServiceRecord(
                    user_id=1,
                    gear_item_id=1,
                    kind="visual_inspection",
                    serviced_on=date(2026, 1, 1),
                    dive_count_at_service=0,
                    # The schedule that `_speakable` drops.
                    gear_service_schedule_id=2,
                    notes="",
                    uuid=UUIDS["record"],
                    created_at=CREATED_AT,
                ),
                1,
            )
        )

        document = await _render(bundle, monkeypatch)

        record = document["gear_service_records"][0]
        assert record["type"] == "visual_inspection"
        assert "gear_service_schedule_uuid" not in record
        _assert_conforms(document)

    @pytest.mark.asyncio
    async def test_an_unspeakable_agency_costs_the_field_not_the_course(self, monkeypatch) -> None:
        """`course.agency` is OPTIONAL (spec §6.17), so the course survives its own agency
        being unreadable - and so do the dive and the card that point at it. Losing a dive
        from an export over the spelling of its course's agency would be the worst answer
        available."""
        course = _with_id(
            Course(
                user_id=1,
                name="Deco Procedures",
                agency="frobnicator",
                status="completed",
                uuid=UUIDS["course"],
                notes="",
                created_at=CREATED_AT,
            ),
            1,
        )
        certification = _with_id(
            Certification(
                user_id=1,
                agency="padi",
                name="Open Water Diver",
                course_id=1,
                notes="",
                uuid=UUIDS["certification"],
                created_at=CREATED_AT,
            ),
            1,
        )
        bundle = build_bundle(
            dives=[make_dive(1, UUIDS["dive-air"], course_id=1)],
            courses=[course],
            certifications=[certification],
        )

        document = await _render(bundle, monkeypatch)

        assert [c["name"] for c in document["courses"]] == ["Deco Procedures"]
        assert "agency" not in document["courses"][0]
        assert document["dives"][0]["course_uuid"] == str(UUIDS["course"])
        assert document["certifications"][0]["course_uuid"] == str(UUIDS["course"])
        _assert_conforms(document)

    @pytest.mark.asyncio
    async def test_an_unspeakable_agency_takes_its_agency_other_with_it(self, monkeypatch) -> None:
        """The pair is written together or not at all: `$defs/course` forbids `agency_other`
        beside anything but `other`, an absent agency included, so a leftover row carrying
        both would export as a document the validator rejects at the far end."""
        course = _with_id(
            Course(
                user_id=1,
                name="Cave 1",
                agency="frobnicator",
                agency_other="NSS-CDS",
                status="completed",
                uuid=UUIDS["course"],
                notes="",
                created_at=CREATED_AT,
            ),
            1,
        )
        bundle = build_bundle(dives=[make_dive(1, UUIDS["dive-air"], course_id=1)], courses=[course])

        document = await _render(bundle, monkeypatch)

        assert [c["name"] for c in document["courses"]] == ["Cave 1"]
        assert "agency" not in document["courses"][0] and "agency_other" not in document["courses"][0]
        _assert_conforms(document)

    @pytest.mark.asyncio
    async def test_other_with_nothing_to_name_is_not_an_agency(self, monkeypatch) -> None:
        """`other` is a promise to name the agency in `agency_other`, and the schema's `then`
        branch requires it. A row that broke the promise - only a direct write can, the
        pairing rule refusing it everywhere else - has claimed nothing, so nothing is
        written."""
        course = _with_id(
            Course(
                user_id=1,
                name="Cave 1",
                agency="other",
                agency_other="   ",
                status="completed",
                uuid=UUIDS["course"],
                notes="",
                created_at=CREATED_AT,
            ),
            1,
        )
        bundle = build_bundle(courses=[course])

        document = await _render(bundle, monkeypatch)

        assert "agency" not in document["courses"][0] and "agency_other" not in document["courses"][0]
        _assert_conforms(document)

    @pytest.mark.asyncio
    async def test_a_named_agency_drops_a_stray_agency_other(self, monkeypatch) -> None:
        """The mirror of the case above, and the one the reader already answers this way
        (`logbook_import/planner.py::_agency`): the agency is the load-bearing half, and the
        name beside it is unwritable next to anything but `other`."""
        course = _with_id(
            Course(
                user_id=1,
                name="Advanced Nitrox",
                agency="tdi",
                agency_other="NSS-CDS",
                status="completed",
                uuid=UUIDS["course"],
                notes="",
                created_at=CREATED_AT,
            ),
            1,
        )
        bundle = build_bundle(courses=[course])

        document = await _render(bundle, monkeypatch)

        assert document["courses"][0]["agency"] == "tdi"
        assert "agency_other" not in document["courses"][0]
        _assert_conforms(document)

    @pytest.mark.asyncio
    async def test_a_card_with_an_unspeakable_agency_is_still_omitted(self, monkeypatch) -> None:
        """The side of the asymmetry that does not move: §6.16 keeps a certification's
        `agency` REQUIRED, so a card the format has no word for is uninterpretable and goes,
        while the course beside it stays. See *A course may have no agency, and a
        certification may not* in DECISIONS.md."""
        course = _with_id(
            Course(
                user_id=1,
                name="Deco Procedures",
                agency="tdi",
                status="completed",
                uuid=UUIDS["course"],
                notes="",
                created_at=CREATED_AT,
            ),
            1,
        )
        certification = _with_id(
            Certification(
                user_id=1,
                agency="frobnicator",
                name="Open Water Diver",
                course_id=1,
                notes="",
                uuid=UUIDS["certification"],
                created_at=CREATED_AT,
            ),
            1,
        )
        bundle = build_bundle(courses=[course], certifications=[certification])

        document = await _render(bundle, monkeypatch)

        assert document["certifications"] == []
        assert [c["name"] for c in document["courses"]] == ["Deco Procedures"]
        _assert_conforms(document)

    @pytest.mark.asyncio
    async def test_an_unspeakable_status_costs_the_field_not_the_course(self, monkeypatch) -> None:
        """`status` reads like a REQUIRED member and is not - `$defs/course` requires only
        uuid/name, and spec §6.17 marks it O. Classifying it by intuition dropped the
        diver's whole course."""
        course = _with_id(
            Course(
                user_id=1,
                name="Deco Procedures",
                agency="tdi",
                status="frobnicator",
                uuid=UUIDS["course"],
                notes="",
                created_at=CREATED_AT,
            ),
            1,
        )
        bundle = build_bundle(dives=[make_dive(1, UUIDS["dive-air"], course_id=1)], courses=[course])

        document = await _render(bundle, monkeypatch)

        assert [c["name"] for c in document["courses"]] == ["Deco Procedures"]
        assert "status" not in document["courses"][0]
        # The reference survives too, because the course did.
        assert document["dives"][0]["course_uuid"] == str(UUIDS["course"])
        _assert_conforms(document)

    def test_the_csv_carries_the_stored_value_verbatim(self) -> None:
        """A CSV column has no vocabulary to keep, so this is where the value survives - which
        is what makes the DiveJSON omission above a re-encoding rather than a loss."""
        writer = dict(CSV_WRITERS)["gear-service.csv"]
        rows = "".join(writer(self._bundle()))

        assert "inspection" in rows
        assert "service" in rows


class TestUnresolvableReferences:
    """Unreachable through the API - a join row cannot outlive the row it points at, now
    that the five referenced tables are hard-deleted - but the export's stated policy is
    that a row nobody can see must never cost a diver their download, and only a test keeps
    that true. It is also what keeps the document referentially closed, which the format
    requires."""

    @pytest.mark.asyncio
    async def test_a_schedule_and_record_whose_gear_item_is_missing_are_skipped(self, monkeypatch):
        bundle = full_bundle()
        bundle.gear_items.clear()
        bundle.gear_item_by_id.clear()
        document = await _render(bundle, monkeypatch)
        assert document["gear"] == []
        assert document["gear_service_schedules"] == []
        assert document["gear_service_records"] == []

    @pytest.mark.asyncio
    async def test_a_gear_set_drops_members_it_cannot_resolve(self, monkeypatch):
        bundle = full_bundle()
        bundle.gear_items.clear()
        bundle.gear_item_by_id.clear()
        document = await _render(bundle, monkeypatch)
        assert document["gear_sets"][0]["gear_uuids"] == []


class TestStoredFiles:
    @pytest.mark.asyncio
    async def test_the_stored_digest_travels_with_the_metadata(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch)
        assert document["dives"][1]["recordings"][0]["source_files"][0]["sha256"] == "a" * 64

    @pytest.mark.asyncio
    async def test_which_parser_read_the_file_rides_this_producer_s_key(self, monkeypatch):
        """Parser registries are application-specific, so the format has no core member for
        one (spec §6.7) - and the archive-restore path reads it back from here."""
        document = await _render(full_bundle(), monkeypatch)
        source_file = document["dives"][1]["recordings"][0]["source_files"][0]
        assert source_file["extensions"] == {"opendiving": {"parser_key": "suunto_json"}}
        assert "parser_key" not in source_file

    @pytest.mark.asyncio
    async def test_a_card_has_one_front_and_one_back_rather_than_a_list(self, monkeypatch):
        """A list with a side discriminator would let a document claim two fronts."""
        document = await _render(full_bundle(), monkeypatch)
        certification = document["certifications"][0]
        assert certification["front_file"]["original_filename"] == "card front.jpg"
        assert certification["back_file"]["original_filename"] == "card back.png"
        assert (certification["front_file"]["sha256"], certification["back_file"]["sha256"]) == ("b" * 64, "c" * 64)
        assert "files" not in certification

    @pytest.mark.asyncio
    async def test_archive_paths_are_absent_outside_an_archive(self, monkeypatch):
        """There is no zip for them to point into when the document is served alone, and
        the format has one spelling of that (spec §6.7)."""
        document = await _render(full_bundle(), monkeypatch)
        assert "archive_path" not in document["dives"][1]["recordings"][0]["source_files"][0]
        certification = document["certifications"][0]
        assert all("archive_path" not in certification[side] for side in ("front_file", "back_file"))

    @pytest.mark.asyncio
    async def test_archive_paths_are_filled_in_inside_one(self, monkeypatch):
        bundle = full_bundle()
        document = await _render(bundle, monkeypatch, paths=plan_archive_paths(bundle))
        # `{dive number}-{recording ordinal}-{stem}`: the ordinal is what tells two members
        # of one dive apart, and it is the same number the document's `recordings[]` is
        # ordered by, so a member can be matched to its recording by name alone.
        assert (
            document["dives"][1]["recordings"][0]["source_files"][0]["archive_path"]
            == "files/0002-0-Suunto-Ocean-2026-06-01.json"
        )
        certification = document["certifications"][0]
        assert certification["front_file"]["archive_path"] == "certifications/open-water-diver-front.jpg"
        assert certification["back_file"]["archive_path"] == "certifications/open-water-diver-back.png"


class TestEncoding:
    @pytest.mark.asyncio
    async def test_non_ascii_notes_are_written_as_themselves(self, monkeypatch):
        """`ensure_ascii=False`: the file is declared UTF-8, and a diver's notes read
        better as themselves than as `\\u00e4`-escapes."""
        bundle = full_bundle()
        bundle.dives[0].notes = "Zackenbarsch, Blaupunktrochen — ünïcode"
        assert "Blaupunktrochen — ünïcode".encode() in await _stream(bundle, monkeypatch)
