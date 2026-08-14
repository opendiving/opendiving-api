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
    async def test_the_per_cylinder_role_and_ppo2_limit_survive(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch)
        mixtures = document["dives"][1]["mixtures"]
        assert [(m["role"], m["po2_limit"]) for m in mixtures] == [("bottom", 1.4), ("deco", 1.6)]

    @pytest.mark.asyncio
    async def test_multi_site_visit_order_is_a_list_not_a_primary_site(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch)
        assert document["dives"][0]["dive_site_uuids"] == [str(UUIDS["site-reef"]), str(UUIDS["site-wall"])]

    @pytest.mark.asyncio
    async def test_cns_and_otu_are_here_since_uddf_has_no_slot_for_them(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch)
        assert (document["dives"][0]["cns_end"], document["dives"][0]["otu_end"]) == (8.0, 21.0)

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
    async def test_no_internal_integer_id_leaks(self, monkeypatch):
        """They are an implementation detail of this database and actively misleading in
        a file meant to outlive it."""
        document = await _render(full_bundle(), monkeypatch, {2: TRIMIX_PROFILE})
        assert "id" not in document["dives"][0]
        assert "user_id" not in document["user"]
        assert "gear_item_id" not in document["gear_service_records"][0]

    @pytest.mark.asyncio
    async def test_a_record_whose_schedule_was_deleted_keeps_its_history(self, monkeypatch):
        """`gear_service_record` outlives the rule it was logged against by design, and a
        *soft*-deleted schedule leaves the id in place while dropping out of the export."""
        bundle = full_bundle()
        bundle.schedules.clear()
        bundle.schedule_by_id.clear()
        document = await _render(bundle, monkeypatch)
        assert document["gear_service_records"][0]["gear_service_schedule_uuid"] is None

    @pytest.mark.asyncio
    async def test_the_start_time_is_the_combined_offset_aware_string(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch)
        assert document["dives"][0]["start_time"] == "2026-06-01T08:15:00+02:00"


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
