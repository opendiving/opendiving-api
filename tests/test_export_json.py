"""Tests for the `export.json` writer (`services/export/envelope.py`).

The first test is the one the module was written to need. `envelope.py` streams the file
a record at a time rather than serializing an `ExportEnvelope`, so the declared shape is
*not* on the write path and nothing but a test stops the two drifting apart. Validating
the streamed bytes back through the model is what makes `schemas/export.py` a
specification rather than documentation that happens to be Python.

The rest cover the promise the format makes: everything UDDF cannot hold is in here, and
what is here refers to other records by public uuid rather than by an internal id that
means nothing outside this database.
"""

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.app.schemas.export import EXPORT_FORMAT, EXPORT_VERSION, ExportEnvelope
from src.app.services.dive_profiles import LoadedProfile
from src.app.services.export.envelope import write_export_json
from src.app.services.export.paths import plan_archive_paths
from tests.helpers.export import EXPORTED_AT, TRIMIX_PROFILE, UUIDS, build_bundle, full_bundle


async def _render(bundle: Any, monkeypatch: Any, profiles: dict[int, dict] | None = None, paths: Any = None) -> dict:
    payloads = profiles or {}

    async def fake_load_profile(db: Any, *, dive_id: int) -> LoadedProfile | None:
        data = payloads.get(dive_id)
        return None if data is None else LoadedProfile(duration_seconds=90, data=data)

    monkeypatch.setattr("src.app.services.export.envelope.load_profile", fake_load_profile)
    chunks = [chunk async for chunk in write_export_json(AsyncMock(), bundle, exported_at=EXPORTED_AT, paths=paths)]
    document: dict = json.loads(b"".join(chunks))
    return document


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
        assert envelope.user.username == "ada"

    @pytest.mark.asyncio
    async def test_format_and_version_come_first(self, monkeypatch):
        """A reader has to be able to dispatch on them before parsing anything else."""
        document = await _render(full_bundle(), monkeypatch)
        assert list(document)[:2] == ["format", "version"]
        assert (document["format"], document["version"]) == (EXPORT_FORMAT, EXPORT_VERSION)

    @pytest.mark.asyncio
    async def test_every_declared_collection_is_present(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch)
        assert set(document) == set(ExportEnvelope.model_fields)


class TestWhatUddfCannotHold:
    """The reason this file exists beside `dives.uddf`."""

    @pytest.mark.asyncio
    async def test_gear_sets_service_history_and_c_cards_are_all_here(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch)
        assert [s["name"] for s in document["gear_sets"]] == ["Tech"]
        assert len(document["gear_service_schedules"]) == 1
        assert len(document["gear_service_records"]) == 1
        assert [c["name"] for c in document["certifications"]] == ["Open Water Diver"]

    @pytest.mark.asyncio
    async def test_both_account_preferences_travel_with_the_logbook(self, monkeypatch):
        """`/export/archive` promises nothing in the account is reachable only through the
        app, and these two are the whole of what an account can be set to.

        `units` in particular travels as *account data*: it says which system the diver
        reads in, and every measurement in this file stays metric regardless (the module
        docstring's "not re-scaled or re-unitised" promise).
        """
        document = await _render(full_bundle(), monkeypatch)
        assert document["user"]["gear_service_emails"] is True
        assert document["user"]["units"] == "metric"
        assert document["dives"][0]["max_depth"] == 28.4

    @pytest.mark.asyncio
    async def test_the_per_cylinder_role_ppo2_limit_and_usage_survive(self, monkeypatch):
        """The three fields UDDF has no slot for, which is why `export.json` exists.
        `usage` rides in for free on `DiveMixtureBase` - the envelope re-wraps every read
        as that schema - so this is what would catch it silently not doing so.
        """
        document = await _render(full_bundle(), monkeypatch)
        mixtures = document["dives"][1]["mixtures"]
        assert [(m["role"], m["po2_limit"], m["usage"]) for m in mixtures] == [
            ("bottom", 1.4, None),
            ("deco", 1.6, "staged"),
        ]

    @pytest.mark.asyncio
    async def test_multi_site_visit_order_is_a_list_not_a_primary_site(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch)
        assert document["dives"][0]["dive_site_uuids"] == [str(UUIDS["site-reef"]), str(UUIDS["site-wall"])]

    @pytest.mark.asyncio
    async def test_cns_and_otu_are_here_since_uddf_has_no_slot_for_them(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch)
        assert (document["dives"][0]["cns_end"], document["dives"][0]["otu_end"]) == (8.0, 21.0)

    @pytest.mark.asyncio
    async def test_the_entry_and_exit_positions_are_here_for_the_same_reason(self, monkeypatch):
        """UDDF 3.2.2 hangs `<geography>` off a `<site>` and nowhere else - neither
        `informationbeforedive` nor `waypoint` has a coordinate element - so a dive's own
        two positions survive only in this file and in `dives.csv`."""
        document = await _render(full_bundle(), monkeypatch)
        dive = document["dives"][0]
        assert (dive["entry_latitude"], dive["entry_longitude"]) == (27.7278, 34.2564)
        assert (dive["exit_latitude"], dive["exit_longitude"]) == (27.7291, 34.2572)

    @pytest.mark.asyncio
    async def test_a_dive_with_only_an_exit_position_exports_one(self, monkeypatch):
        """Which is the ordinary shape: no wrist computer gets a fix before the descent."""
        document = await _render(full_bundle(), monkeypatch)
        dive = document["dives"][1]
        assert (dive["entry_latitude"], dive["entry_longitude"]) == (None, None)
        assert (dive["exit_latitude"], dive["exit_longitude"]) == (27.7315, 34.259)

    @pytest.mark.asyncio
    async def test_the_water_type_is_here_because_uddf_has_no_per_dive_slot(self, monkeypatch):
        """3.2.2's `density` elements are site-level (`sitedata`) and deco-planner input
        (`baseCalculationType`) - neither is a fact about one dive - so this file and
        `dives.csv` are the only two that carry it. `altitude` is the counter-example: it
        has a real slot, and the UDDF gets it too."""
        document = await _render(full_bundle(), monkeypatch)
        assert document["dives"][0]["water_type"] == "salt"
        assert document["dives"][0]["altitude"] == 0
        assert document["dives"][1]["water_type"] is None
        assert document["dives"][1]["altitude"] is None

    @pytest.mark.asyncio
    async def test_the_ceiling_channel_survives_in_the_embedded_profile(self, monkeypatch):
        """UDDF drops it (`<decostop>` needs a duration we do not have); the JSON must not."""
        document = await _render(full_bundle(), monkeypatch, {2: TRIMIX_PROFILE})
        assert document["dives"][1]["profile"]["ceiling"] == {"t": [60, 90], "v": [600, 300]}


class TestReferences:
    @pytest.mark.asyncio
    async def test_records_reference_each_other_by_public_uuid(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch)
        assert document["dives"][0]["trip_uuid"] == str(UUIDS["trip"])
        assert document["gear_sets"][0]["gear_item_uuids"] == [
            str(UUIDS["gear-regulator"]),
            str(UUIDS["gear-suit"]),
        ]
        assert document["gear_service_records"][0]["gear_service_schedule_uuid"] == str(UUIDS["schedule"])

    @pytest.mark.asyncio
    async def test_a_dive_site_carries_its_coordinates(self, monkeypatch):
        """`ExportEnvelope` defaults both to `None`, so dropping the mapping in
        `envelope.py` would leave every other assertion here green."""
        document = await _render(full_bundle(), monkeypatch)
        site = document["dive_sites"][0]
        assert (site["latitude"], site["longitude"]) == (27.7278, 34.2564)

    @pytest.mark.asyncio
    async def test_a_trip_carries_its_places_structured_and_in_order(self, monkeypatch):
        """The flat formats join these into a string; this is the file that keeps what the
        geocoder actually said, box included, so a reader can redraw the trip's map."""
        document = await _render(full_bundle(), monkeypatch)
        locations = document["trips"][0]["locations"]
        assert [location["name"] for location in locations] == ["Sharm el-Sheikh", "Ras Mohammed"]
        assert (locations[0]["latitude"], locations[0]["bbox_north"]) == (27.9158, 28.0)
        # The free-text one: a place the geocoder had no answer for is still a place.
        assert (locations[1]["display_name"], locations[1]["latitude"]) == (None, None)

    @pytest.mark.asyncio
    async def test_the_species_a_dive_saw_are_a_list_of_uuids_the_file_defines(self, monkeypatch):
        """The catalog is global, so `species` is the one collection here that is not the
        diver's own rows - it is the slice their dives reference. The point of exporting it
        at all is that the file stays self-contained: every uuid a dive names is defined in
        the document, so a reader never has to go looking.
        """
        document = await _render(full_bundle(), monkeypatch)

        assert document["dives"][0]["species_uuids"] == [
            str(UUIDS["species-clownfish"]),
            str(UUIDS["species-manta"]),
        ]
        # The dive with no sightings says so as an empty list rather than by omitting it.
        assert document["dives"][2]["species_uuids"] == []
        defined = {species["uuid"] for species in document["species"]}
        for dive in document["dives"]:
            assert set(dive["species_uuids"]) <= defined

    @pytest.mark.asyncio
    async def test_a_species_carries_the_identifier_that_means_something_elsewhere(self, monkeypatch):
        """The uuids are this instance's; `aphia_id` is the World Register of Marine
        Species' own, and it is what lets a reader re-link a sighting to a real taxon
        rather than guess from a name."""
        document = await _render(full_bundle(), monkeypatch)
        clownfish, morays = document["species"]

        assert (clownfish["aphia_id"], clownfish["common_name"]) == (278400, "ocellaris clownfish")
        assert clownfish["wikidata_qid"] == "Q1126155"
        # A family-rank sighting with no common name: both facts survive, so a reader does
        # not render "Muraenidae" as though the diver identified a species.
        assert (morays["rank"], morays["common_name"]) == ("Family", None)

    @pytest.mark.asyncio
    async def test_no_internal_integer_id_leaks(self, monkeypatch):
        """They are an implementation detail of this database and actively misleading in
        a file meant to outlive it."""
        document = await _render(full_bundle(), monkeypatch, {2: TRIMIX_PROFILE})
        assert "id" not in document["dives"][0]
        assert "user_id" not in document["user"]
        assert "gear_item_id" not in document["gear_service_records"][0]
        # Cylinders too: `DiveMixtureRead` carries the row `id` and the API serves it,
        # but nothing in this file references a cylinder, so it would be the one integer
        # key in the document.
        assert "id" not in document["dives"][1]["mixtures"][0]

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
        assert document["gear_service_records"][0]["gear_service_schedule_uuid"] is None

    @pytest.mark.asyncio
    async def test_the_start_time_is_the_combined_offset_aware_string(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch)
        assert document["dives"][0]["start_time"] == "2026-06-01T08:15:00+02:00"


class TestUnresolvableReferences:
    """Unreachable through the API - a join row cannot outlive the row it points at, now
    that the five referenced tables are hard-deleted - but the export's stated policy is
    that a row nobody can see must never cost a diver their download, and only a test keeps
    that true."""

    @pytest.mark.asyncio
    async def test_a_schedule_and_record_whose_gear_item_is_missing_are_skipped(self, monkeypatch):
        bundle = full_bundle()
        bundle.gear_items.clear()
        bundle.gear_item_by_id.clear()
        document = await _render(bundle, monkeypatch)
        assert document["gear_items"] == []
        assert document["gear_service_schedules"] == []
        assert document["gear_service_records"] == []

    @pytest.mark.asyncio
    async def test_a_gear_set_drops_members_it_cannot_resolve(self, monkeypatch):
        bundle = full_bundle()
        bundle.gear_items.clear()
        bundle.gear_item_by_id.clear()
        document = await _render(bundle, monkeypatch)
        assert document["gear_sets"][0]["gear_item_uuids"] == []


class TestStoredFiles:
    @pytest.mark.asyncio
    async def test_the_stored_digest_travels_with_the_metadata(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch)
        assert document["dives"][1]["source_file"]["sha256"] == "a" * 64

    @pytest.mark.asyncio
    async def test_archive_paths_are_null_outside_an_archive(self, monkeypatch):
        """There is no zip for them to point into when `export.json` is served alone."""
        document = await _render(full_bundle(), monkeypatch)
        assert document["dives"][1]["source_file"]["archive_path"] is None
        assert all(f["archive_path"] is None for f in document["certifications"][0]["files"])

    @pytest.mark.asyncio
    async def test_archive_paths_are_filled_in_inside_one(self, monkeypatch):
        bundle = full_bundle()
        document = await _render(bundle, monkeypatch, paths=plan_archive_paths(bundle))
        assert document["dives"][1]["source_file"]["archive_path"] == "files/0002-Suunto-Ocean-2026-06-01.json"
        assert [f["archive_path"] for f in document["certifications"][0]["files"]] == [
            "certifications/open-water-diver-front.jpg",
            "certifications/open-water-diver-back.png",
        ]


class TestEncoding:
    @pytest.mark.asyncio
    async def test_non_ascii_notes_are_written_as_themselves(self, monkeypatch):
        """`ensure_ascii=False`: the file is declared UTF-8, and a diver's notes read
        better as themselves than as `\\u00e4`-escapes."""
        bundle = full_bundle()
        bundle.dives[0].notes = "Zackenbarsch, Blaupunktrochen — ünïcode"
        chunks = []

        async def fake_load_profile(db: Any, *, dive_id: int) -> None:
            return None

        monkeypatch.setattr("src.app.services.export.envelope.load_profile", fake_load_profile)
        async for chunk in write_export_json(AsyncMock(), bundle, exported_at=EXPORTED_AT):
            chunks.append(chunk)
        assert "Blaupunktrochen — ünïcode".encode() in b"".join(chunks)
