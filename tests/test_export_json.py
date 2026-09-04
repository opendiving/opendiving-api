"""Tests for the DiveJSON writer (`services/export/envelope.py`).

Three jobs, and they are not interchangeable.

**Conformance.** The app claims to be DiveJSON's reference implementation, so the streamed
bytes are checked against the vendored JSON Schema *and* against the rules spec §3 says
the schema cannot express - identifier closure, cross-member arithmetic, profile-series
integrity, the `exported_at` offset, the member order. `tests/helpers/divejson.py` is that
whole rule set, ported from the reference validator; a schema-only check would pass
documents `divejson validate` rejects, which is not a hypothetical (see that module).

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
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.app.schemas.export import DIVEJSON_FORMAT, DIVEJSON_VERSION, ExportCourse, ExportEnvelope
from src.app.services.dive_profiles import LoadedProfile
from src.app.services.export.envelope import write_divejson
from src.app.services.export.paths import plan_archive_paths
from tests.helpers.divejson import assert_conforms, conformance_issues, parse_document
from tests.helpers.export import EXPORTED_AT, TRIMIX_PROFILE, UUIDS, build_bundle, full_bundle


async def _stream(bundle: Any, monkeypatch: Any, profiles: dict[int, dict] | None = None, duration: int = 90) -> bytes:
    payloads = profiles or {}

    async def fake_load_profile(db: Any, *, dive_id: int) -> LoadedProfile | None:
        data = payloads.get(dive_id)
        return None if data is None else LoadedProfile(duration=duration, data=data)

    monkeypatch.setattr("src.app.services.export.envelope.load_profile", fake_load_profile)
    return b"".join([chunk async for chunk in write_divejson(AsyncMock(), bundle, exported_at=EXPORTED_AT)])


async def _render(
    bundle: Any,
    monkeypatch: Any,
    profiles: dict[int, dict] | None = None,
    paths: Any = None,
    duration: int = 90,
) -> dict:
    payloads = profiles or {}

    async def fake_load_profile(db: Any, *, dive_id: int) -> LoadedProfile | None:
        data = payloads.get(dive_id)
        return None if data is None else LoadedProfile(duration=duration, data=data)

    monkeypatch.setattr("src.app.services.export.envelope.load_profile", fake_load_profile)
    chunks = [chunk async for chunk in write_divejson(AsyncMock(), bundle, exported_at=EXPORTED_AT, paths=paths)]
    # Through the format's own parser, which refuses duplicate member names (spec §9) -
    # so a writer that emitted a key twice fails here rather than silently losing one.
    document: dict = parse_document(b"".join(chunks))
    return document


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
    """The claim the whole node exists to make, and the only test here that can catch a
    writer producing something `divejson validate` would reject."""

    @pytest.mark.asyncio
    async def test_the_awkward_case_logbook_is_a_conforming_divejson_document(self, monkeypatch):
        assert_conforms(await _render(full_bundle(), monkeypatch, {2: TRIMIX_PROFILE}))

    @pytest.mark.asyncio
    async def test_an_empty_logbook_is_a_conforming_document_too(self, monkeypatch):
        """The floor case: a fresh account with nothing in it still exports something a
        reader can dispatch on."""
        assert_conforms(await _render(build_bundle(), monkeypatch))

    @pytest.mark.asyncio
    async def test_an_archive_layout_stays_conforming(self, monkeypatch):
        """`archive_path` is the one member a document grows inside a zip, and it has its
        own constraints (spec §6.7 and Appendix A)."""
        bundle = full_bundle()
        document = await _render(bundle, monkeypatch, {2: TRIMIX_PROFILE}, paths=plan_archive_paths(bundle))
        assert_conforms(document)

    @pytest.mark.asyncio
    async def test_the_checker_is_not_vacuous(self, monkeypatch):
        """A conformance assertion nobody has watched fail proves nothing. Breaking one
        rule from each of spec §3's checkable classes has to be caught."""
        document = await _render(full_bundle(), monkeypatch, {2: TRIMIX_PROFILE})

        broken = json.loads(json.dumps(document))
        broken["dives"][0]["avg_depth"] = broken["dives"][0]["max_depth"] + 1
        assert any("avg_depth exceeds max_depth" in issue for issue in conformance_issues(broken))

        broken = json.loads(json.dumps(document))
        broken["dives"][0]["site_uuids"] = ["019f0000-0000-7000-8000-000000000099"]
        assert any("not present in sites" in issue for issue in conformance_issues(broken))

        broken = json.loads(json.dumps(document))
        broken["dives"][1]["profile"]["depth"]["values"].append(1)
        assert any("samples but values has" in issue for issue in conformance_issues(broken))

        broken = json.loads(json.dumps(document))
        broken["exported_at"] = broken["exported_at"].replace("+00:00", "")
        assert any("must carry a UTC offset" in issue for issue in conformance_issues(broken))

        broken = {"version": document["version"], **document}
        del broken["format"]
        assert any('"format" MUST come first' in issue for issue in conformance_issues(broken))

        broken = json.loads(json.dumps(document))
        broken["dives"][0]["mixtures"] = []
        assert any("Additional properties" in issue for issue in conformance_issues(broken))

    @pytest.mark.asyncio
    async def test_the_document_holds_no_explicit_nulls(self, monkeypatch):
        """Absence is the only spelling of "not recorded" (spec §5.4), and the schema
        rejects a null wherever one could appear - but only on a member that is *there*
        to be typed. This says it for the document as a whole, including the extension
        payloads the schema leaves open.
        """
        document = await _render(full_bundle(), monkeypatch, {2: TRIMIX_PROFILE})
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
        document = await _render(full_bundle(), monkeypatch, {2: profile}, duration=60)

        assert document["dives"][1]["profile"]["duration"] == 60
        assert document["dives"][1]["profile"]["events"] == [{"time": 95, "type": "bookmark"}]
        assert conformance_issues(document) == []


class TestTheDeclaredShape:
    @pytest.mark.asyncio
    async def test_the_streamed_bytes_validate_against_export_envelope(self, monkeypatch):
        """The whole reason `ExportEnvelope` is worth declaring - see the module docstring."""
        document = await _render(full_bundle(), monkeypatch, {2: TRIMIX_PROFILE})
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
    async def test_both_account_preferences_travel_under_this_producer_s_key(self, monkeypatch):
        """`/export/archive` promises nothing in the account is reachable only through the
        app, and these two are the whole of what an account can be set to.

        They ride `extensions.opendiving` because they are application preferences, not
        logbook data, and the format gives them no core member (spec §6.1) - a writer may
        not invent one. `units` in particular travels as *account data*: it says which
        system the diver reads in, and every measurement in this document stays metric
        regardless.
        """
        document = await _render(full_bundle(), monkeypatch)
        assert document["diver"]["extensions"] == {"opendiving": {"units": "metric", "gear_service_emails": True}}
        assert document["dives"][0]["max_depth"] == 28.4

    @pytest.mark.asyncio
    async def test_the_per_cylinder_role_ppo2_limit_and_usage_survive(self, monkeypatch):
        """The three fields UDDF has no slot for, which is why this document exists.
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
    async def test_multi_site_visit_order_is_a_list_not_a_primary_site(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch)
        assert document["dives"][0]["site_uuids"] == [str(UUIDS["site-reef"]), str(UUIDS["site-wall"])]

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
        document = await _render(full_bundle(), monkeypatch, {2: TRIMIX_PROFILE})
        assert document["dives"][1]["profile"]["ceiling"] == {"times": [60, 90], "values": [600, 300]}


class TestTheProfileVocabulary:
    """One profile vocabulary across the app: `GET /dive/{uuid}/profile` and this document
    both serve `DiveProfileRead`, whose member names are the format's."""

    @pytest.mark.asyncio
    async def test_the_channels_and_events_use_the_format_s_member_names(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch, {2: TRIMIX_PROFILE})
        profile = document["dives"][1]["profile"]

        assert set(profile) == {"duration", "depth", "ceiling", "temperature", "pressures", "events"}
        assert profile["depth"] == {"times": [0, 30, 60, 90], "values": [0, 1800, 5200, 300]}
        assert profile["pressures"][0] == {"times": [0, 60], "values": [2320, 1400], "gas_number": 1}
        assert profile["events"][0] == {"time": 0, "type": "gas_switch", "gas_number": 1}

    @pytest.mark.asyncio
    async def test_the_samples_keep_the_stored_integer_scales(self, monkeypatch):
        """Depth in centimeters, temperature in tenths of a degree - the scales are part of
        the format (spec §5.1), chosen so a round trip cannot introduce float noise."""
        document = await _render(full_bundle(), monkeypatch, {2: TRIMIX_PROFILE})
        profile = document["dives"][1]["profile"]
        assert profile["depth"]["values"] == [0, 1800, 5200, 300]
        assert profile["temperature"] == {"times": [0, 60], "values": [249, 181]}


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
    async def test_a_trip_carries_its_places_structured_and_in_order(self, monkeypatch):
        """The flat formats join these into a string; this is the document that keeps what
        the geocoder actually said, box included, so a reader can redraw the trip's map."""
        document = await _render(full_bundle(), monkeypatch)
        locations = document["trips"][0]["locations"]
        assert [location["name"] for location in locations] == ["Sharm el-Sheikh", "Ras Mohammed"]
        assert locations[0]["position"] == {"latitude": 27.9158, "longitude": 34.33}
        assert locations[0]["bbox"] == {"south": 27.8, "north": 28.0, "west": 34.2, "east": 34.4}
        # The free-text one: a place the geocoder had no answer for is still a place, and
        # it says so by carrying nothing but its name.
        assert locations[1] == {"name": "Ras Mohammed"}

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
        document = await _render(full_bundle(), monkeypatch, {2: TRIMIX_PROFILE})
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
            "dive_number",
            "started_at",
            "duration",
            "site_uuids",
            "gear_uuids",
            "species_uuids",
            "cylinders",
            "created_at",
        }


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
        assert document["dives"][1]["source_file"]["sha256"] == "a" * 64

    @pytest.mark.asyncio
    async def test_which_parser_read_the_file_rides_this_producer_s_key(self, monkeypatch):
        """Parser registries are application-specific, so the format has no core member for
        one (spec §6.7) - and the archive-restore path reads it back from here."""
        document = await _render(full_bundle(), monkeypatch)
        source_file = document["dives"][1]["source_file"]
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
        assert "archive_path" not in document["dives"][1]["source_file"]
        certification = document["certifications"][0]
        assert all("archive_path" not in certification[side] for side in ("front_file", "back_file"))

    @pytest.mark.asyncio
    async def test_archive_paths_are_filled_in_inside_one(self, monkeypatch):
        bundle = full_bundle()
        document = await _render(bundle, monkeypatch, paths=plan_archive_paths(bundle))
        assert document["dives"][1]["source_file"]["archive_path"] == "files/0002-Suunto-Ocean-2026-06-01.json"
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
