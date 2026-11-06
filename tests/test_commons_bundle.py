"""The file the image ships, and what the service reads out of it: the record count, the notice,
the credits, and the two directions of one mapping."""

import json
from pathlib import Path
from typing import Any

import pytest

from src.app.schemas.dive_site import canonical_other_names
from src.app.services import commons_records
from src.app.services.commons_records import (
    COMMONS_REGISTRY,
    CommonsBundle,
    attribution_for,
    columns_of,
    feature_of,
    load_bundle,
)
from src.app.services.geocoding_service import _DEFAULT_ATTRIBUTION

# The tripwire. A refresh that moves this fails here, so it becomes a decision somebody takes
# rather than a diff somebody accepts: a shrinking pool is usually a broken reader, not the world
# losing dive sites.
COMMONS_RECORDS = 3850


@pytest.fixture
def bundle() -> CommonsBundle:
    loaded = load_bundle()
    assert loaded is not None
    return loaded


@pytest.fixture
def document() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(commons_records._DATA_PATH.read_text(encoding="utf-8"))
    return loaded


@pytest.fixture
def unloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commons_records, "_loaded", None)


class TestTheFile:
    def test_it_holds_exactly_the_pinned_records(self, bundle: CommonsBundle, document: dict[str, Any]) -> None:
        assert len(bundle.records) == COMMONS_RECORDS
        assert document["count"] == COMMONS_RECORDS

    def test_it_names_the_commit_it_was_built_from(self, bundle: CommonsBundle) -> None:
        assert len(bundle.commit) == 40

    def test_it_opens_with_the_notice_the_odbl_asks_for(self, bundle: CommonsBundle) -> None:
        assert bundle.licence == "ODbL 1.0"
        assert "Derivative Database of OpenStreetMap" in bundle.notice
        assert "© OpenStreetMap contributors" in bundle.notice

    def test_every_uuid_is_its_own(self, bundle: CommonsBundle) -> None:
        assert len({record["uuid"] for record in bundle.records}) == COMMONS_RECORDS

    def test_every_record_has_a_pin_a_name_and_a_source_the_notice_credits(self, bundle: CommonsBundle) -> None:
        for record in bundle.records:
            assert record["name"] and len(record["name"]) <= 255
            assert -90 <= record["latitude"] <= 90 and -180 <= record["longitude"] <= 180
            assert record["commons_source"] in bundle.attributions

    def test_no_other_name_says_what_a_name_already_says(self, bundle: CommonsBundle) -> None:
        for record in bundle.records:
            assert canonical_other_names(record["name"], record["other_names"]) == record["other_names"]

    def test_a_seed_records_the_entry_it_was_seeded_from_among_its_entries(self, bundle: CommonsBundle) -> None:
        seeds = [record for record in bundle.records if record["commons_seed_registry"] is not None]

        assert seeds
        for record in seeds:
            entry = {"registry": record["commons_seed_registry"], "identifier": record["commons_seed_identifier"]}
            assert entry in record["external_ids"]
            assert record["commons_retrieved"] is not None

    def test_the_english_names_moved_and_nothing_is_left_behind(self, document: dict[str, Any]) -> None:
        provenance = [feature["properties"]["extensions"][COMMONS_REGISTRY] for feature in document["features"]]

        assert not [member for member in provenance if "name_en" in member]
        assert sum(1 for feature in document["features"] if feature["properties"].get("other_names")) == 125

    def test_a_named_seed_keeps_its_uuid_and_its_locality(self, document: dict[str, Any]) -> None:
        thistlegorm = next(
            feature["properties"]
            for feature in document["features"]
            if any(entry["identifier"] == "node/255316037" for entry in feature["properties"].get("external_ids", []))
        )

        assert thistlegorm["uuid"] == "74c091ba-1ec3-5a6b-b70e-fe31c5438605"
        assert thistlegorm["location"] == {"name": "South Sinai, Egypt"}


class TestTheCredit:
    def test_a_seed_from_openstreetmap_shares_the_geocoder_s_credit_byte_for_byte(self) -> None:
        """The place search collapses repeated credits by exact string."""
        assert attribution_for("openstreetmap") == _DEFAULT_ATTRIBUTION

    def test_each_source_says_its_own_terms(self) -> None:
        assert "CC0" in (attribution_for("wikidata") or "")
        assert "CC0" in (attribution_for("contribution") or "")

    def test_a_source_the_notice_does_not_name_has_none(self) -> None:
        assert attribution_for("a-scraped-map") is None


class TestReadingIt:
    def _write(self, path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
        path.write_text(text, encoding="utf-8")
        monkeypatch.setattr(commons_records, "_DATA_PATH", path)

    def test_a_record_whose_source_the_notice_does_not_credit_is_not_loaded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unloaded: None, document: dict[str, Any]
    ) -> None:
        document["sources"] = [source for source in document["sources"] if source.get("source") != "wikidata"]
        self._write(tmp_path / "bundle.geojson", monkeypatch, json.dumps(document))

        bundle = load_bundle()

        assert bundle is not None
        assert 0 < len(bundle.records) < COMMONS_RECORDS
        assert "wikidata" not in {record["commons_source"] for record in bundle.records}

    @pytest.mark.parametrize("text", ["{not json", "{}", '{"features": [{}], "sources": []}'])
    def test_a_broken_file_is_no_bundle_rather_than_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unloaded: None, text: str
    ) -> None:
        self._write(tmp_path / "bundle.geojson", monkeypatch, text)

        assert load_bundle() is None
        assert attribution_for("openstreetmap") is None

    def test_a_failed_read_is_not_remembered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unloaded: None
    ) -> None:
        real = commons_records._DATA_PATH
        monkeypatch.setattr(commons_records, "_DATA_PATH", tmp_path / "not-here.geojson")
        assert load_bundle() is None

        monkeypatch.setattr(commons_records, "_DATA_PATH", real)
        assert load_bundle() is not None


class TestTheMapping:
    @pytest.mark.parametrize(
        "properties",
        [
            {},
            {"other_names": ["Thistlegorm"], "location": {"name": "South Sinai, Egypt"}},
            {"depth_from": 0, "depth_to": 30.5, "water_type": "salt", "altitude": 372, "entry_types": ["boat"]},
        ],
        ids=["bare", "named", "every-member"],
    )
    def test_a_seed_survives_both_directions(self, properties: dict[str, Any]) -> None:
        entry = {"registry": "openstreetmap", "identifier": "node/255316037"}
        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [33.920048, 27.814092]},
            "properties": {
                "uuid": "74c091ba-1ec3-5a6b-b70e-fe31c5438605",
                "name": "SS Thistlegorm",
                "external_ids": [entry],
                "extensions": {
                    COMMONS_REGISTRY: {
                        "source": "openstreetmap",
                        "license": "ODbL-1.0",
                        "retrieved": "2026-10-17",
                        "seeded_from": entry,
                    }
                },
                **properties,
            },
        }

        assert feature_of(columns_of(feature)) == feature

    def test_a_contribution_survives_both_directions(self) -> None:
        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [33.5, 28.5]},
            "properties": {
                "uuid": "019a2f00-0000-7000-8000-000000000001",
                "name": "Walk Reef",
                "extensions": {COMMONS_REGISTRY: {"source": "contribution", "license": "CC0-1.0"}},
            },
        }

        assert feature_of(columns_of(feature)) == feature

    def test_every_shipped_record_does(self, document: dict[str, Any]) -> None:
        for feature in document["features"]:
            assert feature_of(columns_of(feature)) == feature
