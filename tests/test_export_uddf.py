"""Tests for the UDDF writer (`services/export/uddf.py`).

Three kinds of assertion, and they are not interchangeable:

1. **Schema validity.** Every document produced here is validated against the vendored
   UDDF 3.2.2 XSD (`tests/fixtures/uddf/`). That is the only check that catches an
   element in the wrong order or a mandatory child left out, and it is why the schema is
   in the repo at all.
2. **Unit conversions, against hand-computed values.** UDDF is SI and our storage is
   not, so every factor is spelled out in the expectation rather than recomputed from the
   constant it is testing - `24.9 C` is asserted to be `298.05 K`, not
   `24.9 + KELVIN_OFFSET`. A test that reuses the implementation's arithmetic proves
   nothing about the arithmetic.
3. **What is deliberately absent.** The ceiling, CNS and OTU have no honest slot in this
   format (see the module docstring in `uddf.py`), so their absence is asserted rather
   than left to be quietly reintroduced by someone reading the mapping table.

The bundle under test is `tests/helpers/export.py::full_bundle`, hand-built precisely
because the dev corpus has no trimix, no gas switches and one profile between five
hundred dives.

`TestCheckedInCorpus` is the exception to all three: it validates a document that was
downloaded rather than rendered here, and its job is to notice that file rotting, not to
say anything about the writer.
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import xmlschema

from src.app.schemas.gear_item import GearType
from src.app.services.dive_profiles import LoadedProfile
from src.app.services.export.uddf import (
    _EQUIPMENT_ELEMENT,
    _EQUIPMENT_ORDER,
    UDDF_NAMESPACE,
    _num,
    _person_names,
    collect_mixes,
    write_uddf,
)
from tests.helpers.export import (
    EXPORTED_AT,
    OFF_GRID_PROFILE,
    TRIMIX_PROFILE,
    UUIDS,
    build_bundle,
    full_bundle,
    make_dive,
    make_dive_site,
    mixture,
)

UDDF = f"{{{UDDF_NAMESPACE}}}"
SCHEMA_PATH = "tests/fixtures/uddf/uddf_3.2.2.xsd"
CORPUS_PATH = Path(__file__).parent / "fixtures" / "uddf" / "demo-account.uddf"


@pytest.fixture(scope="module")
def schema() -> xmlschema.XMLSchema:
    """Compiled once - `xmlschema` takes appreciably longer to build this than to run
    any single validation against it."""
    return xmlschema.XMLSchema(SCHEMA_PATH)


async def _render(bundle: Any, profiles: dict[int, dict[str, Any]] | None = None, monkeypatch: Any = None) -> bytes:
    payloads = profiles or {}

    async def fake_load_profile(db: Any, *, dive_id: int) -> LoadedProfile | None:
        data = payloads.get(dive_id)
        return None if data is None else LoadedProfile(duration_seconds=data.get("duration", 0), data=data)

    monkeypatch.setattr("src.app.services.export.uddf.load_profile", fake_load_profile)
    chunks = [chunk async for chunk in write_uddf(AsyncMock(), bundle, exported_at=EXPORTED_AT)]
    return b"".join(chunks)


def _tree(document: bytes) -> ET.Element:
    return ET.fromstring(document)


def _dive(tree: ET.Element, index: int) -> ET.Element:
    return tree.findall(f".//{UDDF}dive")[index]


def _text(element: ET.Element, path: str) -> str | None:
    found = element.find(path)
    return None if found is None else found.text


class TestSchemaValidity:
    @pytest.mark.asyncio
    async def test_the_full_logbook_validates(self, schema, monkeypatch):
        document = await _render(full_bundle(), {2: TRIMIX_PROFILE}, monkeypatch)
        schema.validate(document)

    @pytest.mark.asyncio
    async def test_an_empty_logbook_validates(self, schema, monkeypatch):
        """A diver who has logged nothing still gets a well-formed, valid file.

        Not a curiosity: `profiledata` needs at least one `<repetitiongroup>`, a group at
        least one `<dive>`, `divetrip` at least one `<trip>` and `gasdefinitions` at least
        one `<mix>`, so every one of those sections has to be omitted rather than emitted
        empty. Four chances to produce an invalid document out of an account with no data.
        """
        document = await _render(build_bundle(), monkeypatch=monkeypatch)
        schema.validate(document)
        tree = _tree(document)
        assert tree.find(f"{UDDF}profiledata") is None
        assert tree.find(f"{UDDF}divetrip") is None
        assert tree.find(f"{UDDF}gasdefinitions") is None

    @pytest.mark.asyncio
    async def test_a_dive_with_nothing_but_the_mandatory_fields_validates(self, schema, monkeypatch):
        bundle = build_bundle(dives=[make_dive(1, full_bundle().dives[2].uuid, notes="")])
        schema.validate(await _render(bundle, monkeypatch=monkeypatch))

    @pytest.mark.asyncio
    async def test_control_characters_do_not_break_the_document(self, schema, monkeypatch):
        """The failure mode metacharacter escaping does *not* cover.

        `ElementTree` escapes `&`, `<` and `>` and passes the C0 controls straight
        through, but XML 1.0 forbids them outright - so one `\x00` in a diver's notes
        makes the entire download unparseable rather than one element wrong. Nothing
        upstream filters them: notes are plain Pydantic strings, and `<setmarker>` carries
        a device's own wording off an uploaded file.
        """
        bundle = build_bundle(dives=[make_dive(1, full_bundle().dives[0].uuid, notes="a\x00b\x0bc\x1fd\ne")])
        document = await _render(bundle, monkeypatch=monkeypatch)
        schema.validate(document)
        # Tab, newline and carriage return are legal and kept; the rest are dropped
        # rather than replaced, since they carry nothing a diver put there.
        assert _text(_dive(_tree(document), 0), f"{UDDF}informationafterdive/{UDDF}notes/{UDDF}para") == "abcd\ne"

    @pytest.mark.asyncio
    async def test_a_control_character_in_an_attribute_is_scrubbed_too(self, schema, monkeypatch):
        """Attributes go through the same scrub - a `<setmarker>` is element text, but a
        device label could as easily land in one."""
        profile = {**TRIMIX_PROFILE, "events": [{"t": 60, "type": "other", "label": "Ceiling\x00Broken"}]}
        document = await _render(full_bundle(), {2: profile}, monkeypatch)
        schema.validate(document)
        assert b"\x00" not in document

    @pytest.mark.asyncio
    async def test_notes_with_xml_metacharacters_survive(self, schema, monkeypatch):
        """A diver's notes are the one place arbitrary text reaches the document."""
        nasty = "Ampersand & <tag> \"quote\" 'apostrophe' ]]> ünïcode"
        bundle = build_bundle(dives=[make_dive(1, full_bundle().dives[0].uuid, notes=nasty)])
        document = await _render(bundle, monkeypatch=monkeypatch)
        schema.validate(document)
        assert _text(_dive(_tree(document), 0), f"{UDDF}informationafterdive/{UDDF}notes/{UDDF}para") == nasty


class TestCheckedInCorpus:
    """`tests/fixtures/uddf/demo-account.uddf` is a real download, not a rendering.

    It is checked in for the future UDDF *import* work and for the manual round-trips
    through Subsurface and divelogs.de, which need a file this app produced. Validating it
    here costs one schema run and catches a truncated file and a regeneration whose diff
    nobody read - it does *not* catch a line-ending rewrite, since XML normalizes CRLF to
    LF before the parser sees it, which is what `.gitattributes` is for. It deliberately
    asserts nothing about the writer: the tests above own that, against bundles the demo
    account cannot express.
    """

    def test_the_demo_account_export_validates(self, schema):
        document = CORPUS_PATH.read_bytes()
        schema.validate(document)
        # Facts about the capture rather than about the writer. The owner is the one that
        # actually identifies it: a regeneration against another login would validate
        # happily, and a dive count alone would wave through any account that happens to
        # have eight. Both are quoted in the fixture's README, so both rot together.
        tree = _tree(document)
        owner = tree.find(f"{UDDF}diver/{UDDF}owner/{UDDF}personal")
        assert (_text(owner, f"{UDDF}firstname"), _text(owner, f"{UDDF}lastname")) == ("Sam", "Reef")
        assert len(tree.findall(f".//{UDDF}dive")) == 8


class TestUnitConversions:
    """Hand-computed expectations. See the module docstring for why they are literals."""

    @pytest.mark.asyncio
    async def test_temperatures_are_kelvin(self, monkeypatch):
        document = await _render(full_bundle(), {2: TRIMIX_PROFILE}, monkeypatch)
        # 24.9 C stored on the dive -> 24.9 + 273.15
        assert _text(_dive(_tree(document), 0), f"{UDDF}informationafterdive/{UDDF}lowesttemperature") == "298.05"
        # 181 tenths of a degree in the profile = 18.1 C -> 291.25 K
        temperatures = [e.text for e in _tree(document).iter(f"{UDDF}temperature")]
        assert temperatures == ["298.05", "291.25"]

    @pytest.mark.asyncio
    async def test_pressures_are_pascal(self, monkeypatch):
        document = await _render(full_bundle(), {2: TRIMIX_PROFILE}, monkeypatch)
        trimix = _dive(_tree(document), 1)
        tanks = trimix.findall(f"{UDDF}tankdata")
        # 232 bar -> 23 200 000 Pa; 90 bar -> 9 000 000 Pa.
        assert _text(tanks[0], f"{UDDF}tankpressurebegin") == "23200000"
        assert _text(tanks[0], f"{UDDF}tankpressureend") == "9000000"
        # 2320 tenths of a bar in the profile = 232 bar -> the same 23 200 000 Pa.
        assert [e.text for e in trimix.iter(f"{UDDF}tankpressure")][0] == "23200000"

    @pytest.mark.asyncio
    async def test_surface_pressure_is_pascal(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        # 1.013 bar -> 101 300 Pa.
        assert _text(_dive(_tree(document), 0), f"{UDDF}informationbeforedive/{UDDF}surfacepressure") == "101300"

    @pytest.mark.asyncio
    async def test_tank_volumes_are_cubic_metres(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        tanks = _dive(_tree(document), 1).findall(f"{UDDF}tankdata")
        # 24 L -> 0.024 m3, 11.1 L -> 0.0111 m3.
        assert [_text(tank, f"{UDDF}tankvolume") for tank in tanks] == ["0.024", "0.0111"]

    @pytest.mark.asyncio
    async def test_depths_are_metres(self, monkeypatch):
        document = await _render(full_bundle(), {2: TRIMIX_PROFILE}, monkeypatch)
        # 5200 cm in the profile -> 52 m; the dive's own scalars are already metres.
        assert [e.text for e in _dive(_tree(document), 1).iter(f"{UDDF}depth")] == ["0", "18", "52", "3"]
        assert _text(_dive(_tree(document), 0), f"{UDDF}informationafterdive/{UDDF}greatestdepth") == "28.4"

    @pytest.mark.asyncio
    async def test_gas_fractions_are_zero_to_one(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        mixes = _tree(document).findall(f"{UDDF}gasdefinitions/{UDDF}mix")
        assert [(_text(m, f"{UDDF}o2"), _text(m, f"{UDDF}he")) for m in mixes] == [
            ("0.21", "0.35"),
            ("0.32", "0"),
            ("0.5", "0"),
        ]

    def test_scientific_notation_is_never_emitted(self):
        """`%g` would render a tank pressure as `2.32e+07`. Legal XML, unreadable file."""
        assert _num(23_200_000.0) == "23200000"
        assert _num(0.0111) == "0.0111"
        assert _num(0.0) == "0"


class TestMixDefinitions:
    def test_the_same_gas_at_two_ppo2_limits_is_two_mixes(self):
        """`<maximumpo2>` is per-mix, so collapsing them would drop one of the limits."""
        bundle = build_bundle(
            dives=[make_dive(1, full_bundle().dives[0].uuid)],
            mixtures_by_dive={
                1: [mixture(oxygen=32.0, po2_limit=1.4), mixture(id=2, oxygen=32.0, po2_limit=1.6)],
            },
        )
        assert len(collect_mixes(bundle)) == 2

    def test_float_noise_does_not_split_one_gas_in_two(self):
        """Straight from the dev corpus, which holds `28.000000000000004` beside `28`.

        Before the key was rounded these produced two `<mix>` entries whose `<o2>`
        printed the same number, because the writer already rounds on the way out.
        """
        bundle = build_bundle(
            dives=[make_dive(1, full_bundle().dives[0].uuid)],
            mixtures_by_dive={1: [mixture(oxygen=28.0), mixture(id=2, oxygen=28.000000000000004)]},
        )
        assert len(collect_mixes(bundle)) == 1

    @pytest.mark.asyncio
    async def test_mixes_are_named_the_way_a_diver_would(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        names = [e.text for e in _tree(document).iter(f"{UDDF}name") if e.text in ("21/35", "EAN32", "EAN50")]
        assert names == ["21/35", "EAN32", "EAN50"]

    @pytest.mark.asyncio
    async def test_the_planned_ppo2_lands_in_maximumpo2(self, monkeypatch):
        """The plan's mapping table said `po2_limit` had no UDDF slot. The XSD disagrees."""
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        mixes = _tree(document).findall(f"{UDDF}gasdefinitions/{UDDF}mix")
        assert [_text(m, f"{UDDF}maximumpo2") for m in mixes] == ["1.4", None, "1.6"]

    def test_the_mix_list_is_sorted_by_fraction_not_by_encounter(self):
        """Reordering the dives must not reorder the mixes.

        Compares the **mappings**, not their values: `collect_mixes` numbers whatever it
        produces `mix-1..n`, so comparing `.values()` alone is `["mix-1", "mix-2"] ==
        ["mix-1", "mix-2"]` for a sorted, an encounter-ordered or a shuffled
        implementation alike. It has to be each gas's own id that is asserted stable.
        """
        bundle = full_bundle()
        reversed_bundle = build_bundle(
            dives=list(reversed(bundle.dives)),
            mixtures_by_dive=bundle.mixtures_by_dive,
        )
        assert collect_mixes(bundle) == collect_mixes(reversed_bundle)

    def test_logging_another_dive_on_a_gas_already_used_changes_no_ids(self):
        """What sorting by fraction actually buys, stated precisely.

        The ids are a function of the *set* of gases and of nothing else - not of how many
        dives used each, nor of the order they were logged in. So the common edit (another
        dive on gas you already own) leaves `<gasdefinitions>` untouched.

        It is deliberately **not** claimed that ids survive a gas disappearing: they
        cannot, since removing a middle gas shifts everything after it, and only
        persisting the numbers would fix that. An earlier version of this docstring said
        otherwise and the test written from it failed, which is how the overclaim was
        found.
        """
        bundle = full_bundle()
        before = collect_mixes(bundle)

        extra = make_dive(4, UUIDS["dive-bare"], dive_number=4)
        with_more = build_bundle(
            dives=[*bundle.dives, extra],
            mixtures_by_dive={**bundle.mixtures_by_dive, 4: [mixture(id=9, oxygen=32.0)]},
        )
        assert collect_mixes(with_more) == before


class TestDiveContent:
    @pytest.mark.asyncio
    async def test_the_datetime_carries_the_dive_s_own_offset(self, monkeypatch):
        """The API's rule everywhere: one combined offset-aware string, never the stored
        UTC instant. 06:15 UTC at +02:00 is 08:15 local."""
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        assert (
            _text(_dive(_tree(document), 0), f"{UDDF}informationbeforedive/{UDDF}datetime")
            == "2026-06-01T08:15:00+02:00"
        )

    @pytest.mark.asyncio
    async def test_every_site_is_linked_in_visit_order(self, monkeypatch):
        """`informationbeforedive/link` is `maxOccurs="unbounded"`, so a multi-site dive
        keeps its whole itinerary - and an importer that reads only the first still gets
        the primary site."""
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        links = _dive(_tree(document), 0).findall(f"{UDDF}informationbeforedive/{UDDF}link")
        sites = _tree(document).findall(f"{UDDF}divesite/{UDDF}site")
        assert [link.get("ref") for link in links] == [site.get("id") for site in sites]

    @pytest.mark.asyncio
    async def test_a_dive_with_no_recorded_depth_still_gets_the_mandatory_element(self, monkeypatch):
        """`<greatestdepth>` is `minOccurs="1"` and `Dive.max_depth` is nullable."""
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        assert _text(_dive(_tree(document), 2), f"{UDDF}informationafterdive/{UDDF}greatestdepth") == "0"

    @pytest.mark.asyncio
    async def test_the_profile_s_max_depth_stands_in_before_zero_does(self, monkeypatch):
        """The trimix dive has no `max_depth` of its own here - only a profile summary."""
        bundle = full_bundle()
        bundle.dives[1].max_depth = None
        document = await _render(bundle, monkeypatch=monkeypatch)
        assert _text(_dive(_tree(document), 1), f"{UDDF}informationafterdive/{UDDF}greatestdepth") == "52"

    @pytest.mark.asyncio
    async def test_a_cylinder_with_no_starting_pressure_is_not_a_tankdata(self, monkeypatch):
        """`<tankpressurebegin>` is mandatory, so there is no valid `<tankdata>` to emit -
        the gas still reaches `<gasdefinitions>` and the cylinder still reaches the JSON."""
        bundle = build_bundle(
            dives=[make_dive(1, full_bundle().dives[0].uuid)],
            mixtures_by_dive={1: [mixture(oxygen=32.0, start_pressure=None)]},
        )
        document = await _render(bundle, monkeypatch=monkeypatch)
        assert _dive(_tree(document), 0).findall(f"{UDDF}tankdata") == []
        assert len(_tree(document).findall(f"{UDDF}gasdefinitions/{UDDF}mix")) == 1

    @pytest.mark.asyncio
    async def test_gear_and_lead_ride_in_equipmentused(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        used = _dive(_tree(document), 0).find(f"{UDDF}informationbeforedive/{UDDF}equipmentused")
        assert _text(used, f"{UDDF}leadquantity") == "6.5"
        assert len(used.findall(f"{UDDF}link")) == 3

    @pytest.mark.asyncio
    async def test_two_items_of_one_brand_get_distinct_manufacturer_ids(self, schema, monkeypatch):
        """`<manufacturer>` is an inline child of each piece and its `id` is `xs:ID`, which
        must be unique across the whole document - so the id has to be per *occurrence*,
        not per brand. Keying it on the brand emitted `mfr-1` twice for a diver who owned
        two Apeks items, which is the ordinary case rather than a corner one, and made the
        whole file fail validation."""
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        schema.validate(document)
        manufacturers = list(_tree(document).iter(f"{UDDF}manufacturer"))
        names = [_text(m, f"{UDDF}name") for m in manufacturers]
        ids = [m.get("id") for m in manufacturers]
        assert names.count("Apeks") == 2
        assert len(ids) == len(set(ids))

    @pytest.mark.asyncio
    async def test_an_untyped_gear_item_is_not_dropped(self, monkeypatch):
        """`GearItem.type` is nullable, and every item has to land somewhere in
        `equipmentType` - `<variouspieces>` is the catch-all."""
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        various = _tree(document).findall(f".//{UDDF}variouspieces/{UDDF}name")
        assert [e.text for e in various] == ["Slate"]

    @pytest.mark.asyncio
    async def test_the_trip_is_linked_and_dated(self, monkeypatch):
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        trip = _tree(document).find(f"{UDDF}divetrip/{UDDF}trip")
        assert _dive(_tree(document), 0).find(f"{UDDF}informationbeforedive/{UDDF}tripmembership").get(
            "ref"
        ) == trip.get("id")
        dates = trip.find(f"{UDDF}trippart/{UDDF}dateoftrip")
        assert (dates.get("startdate"), dates.get("enddate")) == ("2026-05-30T00:00:00", "2026-06-06T00:00:00")


class TestDiveSiteGeography:
    """`geographyType` is where a site's position goes, and its `<location>` is
    `minOccurs="1"` - so what a site does *not* have decides whether the element can be
    emitted at all. Three cases, and the schema is the referee for each.
    """

    @staticmethod
    def _site(tree: ET.Element, index: int) -> ET.Element:
        return tree.findall(f"{UDDF}divesite/{UDDF}site")[index]

    @pytest.mark.asyncio
    async def test_a_position_reaches_geography(self, schema, monkeypatch):
        """Decimal degrees in both formats - the one pair of numbers on this element that
        needs no conversion, which is exactly why a test says so."""
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        schema.validate(document)
        geography = self._site(_tree(document), 0).find(f"{UDDF}geography")
        assert _text(geography, f"{UDDF}location") == "Ras Mohammed"
        assert (_text(geography, f"{UDDF}latitude"), _text(geography, f"{UDDF}longitude")) == ("27.7278", "34.2564")

    @pytest.mark.asyncio
    async def test_a_site_with_only_a_position_borrows_its_name_as_the_location(self, schema, monkeypatch):
        """`<location>` is mandatory inside `<geography>`, so a site with coordinates and
        no free-text location would otherwise have to lose the coordinates to stay
        valid."""
        site = make_dive_site(2, UUIDS["site-wall"], latitude=27.7, longitude=34.2)
        document = await _render(build_bundle(dive_sites=[site]), monkeypatch=monkeypatch)
        schema.validate(document)
        geography = self._site(_tree(document), 0).find(f"{UDDF}geography")
        assert _text(geography, f"{UDDF}location") == "Yolanda"
        assert _text(geography, f"{UDDF}latitude") == "27.7"

    @pytest.mark.asyncio
    async def test_a_site_with_neither_gets_no_geography_at_all(self, monkeypatch):
        """The empty `<geography>` that would be invalid. `full_bundle`'s second site is
        a bare name."""
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        assert self._site(_tree(document), 1).find(f"{UDDF}geography") is None


class TestWaypoints:
    """The depth channel alone sets the time axis, and every waypoint carries a depth.

    Not a stylistic choice - the round-trips recorded in
    `DECISIONS.md` show both importers mangling depth-less waypoints, one by discarding
    them and one by reading the absent depth as zero. These tests pin the rule that
    replaced it.
    """

    @pytest.mark.asyncio
    async def test_the_depth_channel_sets_the_time_axis(self, monkeypatch):
        document = await _render(full_bundle(), {2: TRIMIX_PROFILE}, monkeypatch)
        waypoints = _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        assert [_text(w, f"{UDDF}divetime") for w in waypoints] == ["0", "30", "60", "90"]
        assert all(_text(w, f"{UDDF}depth") is not None for w in waypoints)
        # The 30 s waypoint has a depth but no temperature - temperature was sampled at
        # 0 s and 60 s only, and nothing off-axis is near enough to claim it.
        assert _text(waypoints[1], f"{UDDF}temperature") is None
        assert _text(waypoints[1], f"{UDDF}depth") == "18"

    @pytest.mark.asyncio
    async def test_readings_between_depth_samples_snap_to_the_nearest(self, schema, monkeypatch):
        document = await _render(full_bundle(), {2: OFF_GRID_PROFILE}, monkeypatch)
        schema.validate(document)
        waypoints = _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        assert [_text(w, f"{UDDF}divetime") for w in waypoints] == ["0", "10", "20", "30"]
        assert [_text(w, f"{UDDF}depth") for w in waypoints] == ["0", "10", "20", "15"]
        # 4 s -> 0 s and 27 s -> 30 s; 12 s beats 13 s for the 10 s waypoint by one
        # second, so 99.9 C never appears; nothing is near enough to the 20 s waypoint.
        assert [_text(w, f"{UDDF}temperature") for w in waypoints] == ["298.15", "297.15", None, "295.15"]

    @pytest.mark.asyncio
    async def test_a_reading_equidistant_from_two_samples_takes_the_earlier(self, monkeypatch):
        """The 15 s pressure reading sits exactly between the 10 s and 20 s waypoints.

        Either would be defensible; what matters is that it is decided rather than left to
        dict ordering, because two exports of one dive have to be byte-identical.
        """
        document = await _render(full_bundle(), {2: OFF_GRID_PROFILE}, monkeypatch)
        waypoints = _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        assert [_text(w, f"{UDDF}tankpressure") for w in waypoints] == [None, "20000000", None, None]

    @pytest.mark.asyncio
    async def test_events_snap_too_and_still_join_on_arrival(self, monkeypatch):
        """The 7 s and 8 s markers are not simultaneous in the profile; they become so
        here, which is the case `waypointType`'s single `<setmarker>` cannot hold."""
        document = await _render(full_bundle(), {2: OFF_GRID_PROFILE}, monkeypatch)
        waypoints = _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        assert [_text(w, f"{UDDF}setmarker") for w in waypoints] == [None, "safety_stop; Deco", None, None]
        # The 24 s switch lands on 30, not on the nearer 20: a state change is never shown
        # before it happened. See `test_a_gas_switch_is_never_shown_before_it_happened`.
        switches = [w.find(f"{UDDF}switchmix") for w in waypoints]
        assert [s is not None for s in switches] == [False, False, False, True]

    @pytest.mark.asyncio
    async def test_the_later_of_two_switches_on_one_waypoint_wins(self, schema, monkeypatch):
        """`<switchmix>` is `maxOccurs="1"`, so one of them has to lose.

        Snapping is what makes this reachable: two switches inside a single sampling
        interval were previously two separate waypoints. Keeping the earlier one would
        leave every importer computing the rest of the dive on a gas the diver had
        already left, which is the one wrong answer available here.
        """
        profile = {
            **OFF_GRID_PROFILE,
            "events": [
                {"t": 21, "type": "gas_switch", "gas_number": 1},
                {"t": 23, "type": "gas_switch", "gas_number": 2},
            ],
        }
        document = await _render(full_bundle(), {2: profile}, monkeypatch)
        schema.validate(document)
        tree = _tree(document)
        waypoints = _dive(tree, 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        switches = [w.find(f"{UDDF}switchmix") for w in waypoints]
        assert [s is not None for s in switches] == [False, False, False, True]
        # Resolved through the mix rather than the id, so the assertion says which *gas*
        # won: cylinder 2 is the 50% deco mix, cylinder 1 the 21/35 bottom gas.
        mixes = {m.get("id"): _text(m, f"{UDDF}o2") for m in tree.findall(f"{UDDF}gasdefinitions/{UDDF}mix")}
        assert mixes[switches[3].get("ref")] == "0.5"

    @pytest.mark.asyncio
    async def test_an_unrepresentable_later_switch_does_not_restore_the_earlier_one(self, schema, monkeypatch):
        """The counter-intuitive half of last-wins: the winner is chosen before asking
        whether it can be written.

        Cylinder 9 has no mixture on this dive, so its switch has no `xs:IDREF` to point
        at. Resolving before choosing would quietly hand the waypoint back to cylinder 1 -
        the gas the diver had just left - which is the failure last-wins exists to
        prevent, reached from the other side. No `<switchmix>` at all is the honest answer.
        """
        profile = {
            **OFF_GRID_PROFILE,
            "events": [
                {"t": 21, "type": "gas_switch", "gas_number": 1},
                {"t": 23, "type": "gas_switch", "gas_number": 9},
            ],
        }
        document = await _render(full_bundle(), {2: profile}, monkeypatch)
        schema.validate(document)
        waypoints = _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        assert [w.find(f"{UDDF}switchmix") for w in waypoints] == [None, None, None, None]

    @pytest.mark.asyncio
    async def test_a_gas_switch_is_never_shown_before_it_happened(self, schema, monkeypatch):
        """A switch is a state change, so the tolerance rule that governs readings does
        not govern it.

        Dropping one for being too far from a waypoint would not leave a hole - it would
        tell every importer the diver stayed on the previous gas for the rest of the dive.
        So it lands on the first waypoint at or after it, however far that is, and the
        interval in between is attributed to the old gas rather than to the new one.
        """
        profile = {
            "depth": {"t": [0, 10, 20, 1820, 1830], "v": [0, 1000, 2000, 800, 0]},
            "events": [{"t": 900, "type": "gas_switch", "gas_number": 2}],
        }
        document = await _render(full_bundle(), {2: profile}, monkeypatch)
        schema.validate(document)
        waypoints = _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        switches = [w.find(f"{UDDF}switchmix") for w in waypoints]
        # 900 s is 880 s from the nearest waypoint - a temperature there would be dropped
        # (`test_a_reading_inside_a_dropout_is_dropped_too`), and this survives instead.
        assert [s is not None for s in switches] == [False, False, False, True, False]

    @pytest.mark.asyncio
    async def test_a_switch_after_the_last_sample_has_nowhere_to_go(self, schema, monkeypatch):
        """The one case where dropping a switch is right: nothing follows it in the
        profile, so no importer can compute anything on the wrong gas."""
        profile = {**OFF_GRID_PROFILE, "events": [{"t": 40, "type": "gas_switch", "gas_number": 2}]}
        document = await _render(full_bundle(), {2: profile}, monkeypatch)
        schema.validate(document)
        waypoints = _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        assert [w.find(f"{UDDF}switchmix") for w in waypoints] == [None, None, None, None]

    @pytest.mark.asyncio
    async def test_the_closest_of_two_readings_wins_the_waypoint_not_the_earliest(self, schema, monkeypatch):
        """`OFF_GRID_PROFILE`'s colliding pair has the earlier reading also the closer
        one, so first-wins and closest-wins agree there and the documented rule goes
        unpinned. Here 9 s is one second from the waypoint and 6 s is four, so only
        closest-wins produces 22.0 C."""
        profile = {
            "depth": {"t": [0, 10, 20], "v": [0, 1000, 2000]},
            "temperature": {"t": [6, 9], "v": [999, 220]},
        }
        document = await _render(full_bundle(), {2: profile}, monkeypatch)
        schema.validate(document)
        waypoints = _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        assert [_text(w, f"{UDDF}temperature") for w in waypoints] == [None, "295.15", None]

    @pytest.mark.asyncio
    async def test_a_sparse_depth_channel_does_not_widen_the_tolerance(self, schema, monkeypatch):
        """The median gap is only robust while dropouts are the minority.

        Two usable depth samples half an hour apart - what `suunto_xml` produces from a
        file whose `<Depth>` is nil for most of the dive - would otherwise licence a 900 s
        move, which is the failure the tolerance exists to prevent rather than an
        application of it.
        """
        profile = {"depth": {"t": [0, 1800], "v": [0, 3000]}, "temperature": {"t": [890], "v": [220]}}
        document = await _render(full_bundle(), {2: profile}, monkeypatch)
        schema.validate(document)
        waypoints = _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        assert [_text(w, f"{UDDF}temperature") for w in waypoints] == [None, None]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("second", [-60, 230])
    async def test_a_reading_too_far_from_any_sample_is_dropped_not_clamped(self, second, schema, monkeypatch):
        """`_nearest` alone would put a surface-interval reading on the last in-water
        waypoint, as if it had been taken there - the one way snapping could invent data
        rather than merely move it. Both ends clamp, so both ends are checked."""
        profile = {**OFF_GRID_PROFILE, "temperature": {"t": [second], "v": [300]}}
        document = await _render(full_bundle(), {2: profile}, monkeypatch)
        schema.validate(document)
        waypoints = _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        assert [_text(w, f"{UDDF}temperature") for w in waypoints] == [None, None, None, None]

    @pytest.mark.asyncio
    async def test_a_reading_inside_a_dropout_is_dropped_too(self, schema, monkeypatch):
        """The interior version of the same failure, and the reason the tolerance is a
        property of the channel rather than of the two samples bracketing the reading.

        `suunto_xml` appends a depth sample only where `<Depth>` is non-nil, so a
        mid-dive dropout leaves a hole that temperature samples straight through. Judged
        against its bracketing pair, a reading in the middle of a 1800 s hole has moved
        "less than half an interval" and would be emitted as the temperature at a
        waypoint a quarter of an hour away.
        """
        profile = {
            "depth": {"t": [0, 10, 20, 1820, 1830], "v": [0, 1000, 2000, 800, 0]},
            "temperature": {"t": [12, 900, 1825], "v": [240, 999, 220]},
        }
        document = await _render(full_bundle(), {2: profile}, monkeypatch)
        schema.validate(document)
        waypoints = _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        # The 900 s reading has no waypoint within tolerance and is gone; the two either
        # side of the hole are unaffected by it. 1825 s is equidistant from 1820 and
        # 1830, so the tie-break puts it on the earlier one.
        assert [_text(w, f"{UDDF}temperature") for w in waypoints] == [None, "297.15", None, "295.15", None]

    @pytest.mark.asyncio
    async def test_a_profile_with_no_depth_channel_emits_no_samples(self, schema, monkeypatch):
        """The one case the old union rule produced depth-less waypoints for on its own.

        Emitting a `<samples>` block of temperatures with no depths would hand divelogs.de
        a dive that plunges to the surface and back on every sample; the readings are in
        `export.json` either way.
        """
        profile = {"temperature": {"t": [0, 60], "v": [249, 181]}, "events": [{"t": 30, "type": "safety_stop"}]}
        document = await _render(full_bundle(), {2: profile}, monkeypatch)
        schema.validate(document)
        assert _dive(_tree(document), 1).find(f"{UDDF}samples") is None

    @pytest.mark.asyncio
    async def test_gas_switches_become_switchmix_links(self, monkeypatch):
        document = await _render(full_bundle(), {2: TRIMIX_PROFILE}, monkeypatch)
        waypoints = _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        mixes = {m.get("id") for m in _tree(document).findall(f"{UDDF}gasdefinitions/{UDDF}mix")}
        switches = [w.find(f"{UDDF}switchmix") for w in waypoints]
        assert [s is not None for s in switches] == [True, False, False, True]
        assert {s.get("ref") for s in switches if s is not None} <= mixes

    @pytest.mark.asyncio
    async def test_simultaneous_markers_are_joined_rather_than_dropped(self, monkeypatch):
        """`waypointType` allows one `<setmarker>`, and the fixture puts two events at 60s."""
        document = await _render(full_bundle(), {2: TRIMIX_PROFILE}, monkeypatch)
        waypoints = _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        assert _text(waypoints[2], f"{UDDF}setmarker") == "safety_stop; Ceiling Broken"

    @pytest.mark.asyncio
    async def test_a_pressure_channel_with_no_matching_cylinder_is_dropped(self, schema, monkeypatch):
        """`<tankpressure ref>` is an `xs:IDREF`; a dangling one would fail validation.

        The fixture's `gas_number: 9` channel has no mixture, which is what a device that
        reports five cylinder slots for a two-cylinder dive produces.
        """
        document = await _render(full_bundle(), {2: TRIMIX_PROFILE}, monkeypatch)
        schema.validate(document)
        assert [e.text for e in _dive(_tree(document), 1).iter(f"{UDDF}tankpressure")] == [
            "23200000",
            "14000000",
            "20000000",
        ]


class TestWhatUddfCannotHold:
    """Absences that are decisions, not omissions - see `uddf.py`'s module docstring."""

    @pytest.mark.asyncio
    async def test_the_deco_ceiling_is_not_emitted(self, monkeypatch):
        """`<decostop>` requires a `duration` attribute, and a ceiling sample says how
        deep the obligation was, never how long the stop should last."""
        document = await _render(full_bundle(), {2: TRIMIX_PROFILE}, monkeypatch)
        assert list(_tree(document).iter(f"{UDDF}decostop")) == []

    @pytest.mark.asyncio
    async def test_cns_and_otu_are_not_emitted(self, monkeypatch):
        """`informationafterdiveType` has no oxygen-exposure element; the only `<cns>`/
        `<otu>` in the schema are per-waypoint, and we store end-of-dive scalars."""
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        assert list(_tree(document).iter(f"{UDDF}cns")) == []
        assert list(_tree(document).iter(f"{UDDF}otu")) == []

    @pytest.mark.asyncio
    async def test_the_owner_s_email_is_not_in_the_file(self, monkeypatch):
        """The schema has a slot. A UDDF file is what a diver hands to a dive shop."""
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        assert b"ada@example.com" not in document


class TestDeterminism:
    @pytest.mark.asyncio
    async def test_two_exports_of_an_unchanged_logbook_are_byte_identical(self, monkeypatch):
        first = await _render(full_bundle(), {2: TRIMIX_PROFILE}, monkeypatch)
        second = await _render(full_bundle(), {2: TRIMIX_PROFILE}, monkeypatch)
        assert first == second


class TestEquipmentMapping:
    def test_every_gear_type_has_a_uddf_element(self):
        """`_EQUIPMENT_ELEMENT` is looked up unguarded, and `full_bundle` only exercises
        three of the twenty categories - so an enum member added without a home here
        would first surface as a 500 on a diver's download."""
        assert set(_EQUIPMENT_ELEMENT) == set(GearType)

    def test_every_mapped_element_has_a_slot_in_the_sequence(self):
        """`equipmentType` is an `xs:sequence`, so an element `_EQUIPMENT_ORDER` does not
        list would simply never be emitted."""
        assert set(_EQUIPMENT_ELEMENT.values()) <= set(_EQUIPMENT_ORDER)


class TestPersonNames:
    @pytest.mark.parametrize(
        ("full_name", "expected"),
        [
            ("Ada Lovelace", ("Ada", "Lovelace")),
            ("Jean Luc Picard", ("Jean", "Luc Picard")),
            # `<lastname>` is mandatory but an empty `xs:string` is valid, which says
            # "we don't hold this" rather than asserting a surname nobody gave.
            ("Cher", ("Cher", "")),
            ("   ", ("ada", "")),
        ],
    )
    def test_a_single_stored_name_splits_into_the_two_uddf_wants(self, full_name, expected):
        assert _person_names(full_name, "ada") == expected
