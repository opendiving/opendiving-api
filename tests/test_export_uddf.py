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
3. **What is deliberately absent.** The ceiling, OTU, the deco model, `tts` and
   `surface_gradient_factor` have no honest slot in this format (see the module docstring
   in `uddf.py`), so their absence is asserted rather than left to be quietly reintroduced
   by someone reading the mapping table. The four deco readouts that *do* have a slot are
   asserted the other way round, in `TestDecoReadouts` - an element the format holds and
   this writer skips is the same defect seen from the other side.

The bundle under test is `tests/helpers/export.py::full_bundle`, hand-built precisely
because the dev corpus has no trimix, no gas switches and one profile between five
hundred dives.

`TestCheckedInCorpus` is the exception to all three: it validates a document that was
downloaded rather than rendered here, and its job is to notice that file rotting, not to
say anything about the writer.
"""

import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import xmlschema
from uuid6 import uuid7

from src.app.models.gear_item import GearItem
from src.app.schemas.dive import DiveMode
from src.app.schemas.gear_item import GearType
from src.app.schemas.trip import TripLocationRead, TripPartRead
from src.app.services.dive_profiles import LoadedProfile
from src.app.services.export.uddf import (
    _DIVE_MODE_TYPE,
    _EQUIPMENT_ELEMENT,
    _EQUIPMENT_ORDER,
    UDDF_NAMESPACE,
    _num,
    _person_names,
    collect_mixes,
    write_uddf,
)
from tests.helpers.export import (
    CREATED_AT,
    EXPORTED_AT,
    OFF_GRID_PROFILE,
    PRIMARY_RECORDING_ID,
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

    async def fake_load_profile(db: Any, *, recording_id: int) -> LoadedProfile | None:
        data = payloads.get(recording_id)
        return (
            None
            if data is None
            else LoadedProfile(duration=data.get("duration", 0), data=data, parser_key="suunto_xml")
        )

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


def _gear(name: str, gear_type: GearType) -> GearItem:
    """A bare gear item for bundles built here rather than by `full_bundle`.

    Local to this module on purpose: `full_bundle`'s gear list is asserted *exactly* by
    `test_an_untyped_gear_item_is_not_dropped`, so a category added there to exercise a
    mapping would break an unrelated test.
    """
    return GearItem(user_id=1, name=name, type=gear_type, uuid=uuid7(), created_at=CREATED_AT)


class TestSchemaValidity:
    @pytest.mark.asyncio
    async def test_the_full_logbook_validates(self, schema, monkeypatch):
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
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
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: profile}, monkeypatch)
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
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
        # 24.9 C stored on the dive -> 24.9 + 273.15
        assert _text(_dive(_tree(document), 0), f"{UDDF}informationafterdive/{UDDF}lowesttemperature") == "298.05"
        # 181 tenths of a degree in the profile = 18.1 C -> 291.25 K
        temperatures = [e.text for e in _tree(document).iter(f"{UDDF}temperature")]
        assert temperatures == ["298.05", "291.25"]

    @pytest.mark.asyncio
    async def test_pressures_are_pascal(self, monkeypatch):
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
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
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
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
        """An earlier mapping table said `po2_limit` had no UDDF slot. The XSD disagrees."""
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
    async def test_the_profile_s_deepest_sample_stands_in_before_zero_does(self, monkeypatch):
        """The trimix dive has no `max_depth` of its own here - only samples.

        **Off the samples in the document rather than off a stored summary**, which matters
        now that a dive can have several recordings: each has its own deepest reading, and
        `<greatestdepth>` takes one number. It takes the one belonging to the waypoints
        written beside it, so the two cannot disagree.
        """
        bundle = full_bundle()
        bundle.dives[1].max_depth = None
        document = await _render(bundle, {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
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
    async def test_a_cylinder_with_no_recorded_size_omits_the_tankvolume(self, schema, monkeypatch):
        """The mirror of the case above, and the reason it is a mirror rather than a copy:
        `<tankvolume>` is `minOccurs="0"` where `<tankpressurebegin>` is mandatory, so this
        cylinder *is* a valid `<tankdata>` with one child left out. Validated against the
        XSD, because omitting a child of an `xs:sequence` is exactly the mistake that reads
        fine and does not parse."""
        bundle = build_bundle(
            dives=[make_dive(1, full_bundle().dives[0].uuid)],
            mixtures_by_dive={1: [mixture(volume=None, start_pressure=200.0, end_pressure=80.0)]},
        )
        document = await _render(bundle, monkeypatch=monkeypatch)
        schema.validate(document)

        tanks = _dive(_tree(document), 0).findall(f"{UDDF}tankdata")
        assert len(tanks) == 1
        assert tanks[0].find(f"{UDDF}tankvolume") is None
        assert _text(tanks[0], f"{UDDF}tankpressurebegin") == "20000000"

    @pytest.mark.asyncio
    async def test_a_mix_nobody_recorded_is_named_and_carries_no_fractions(self, schema, monkeypatch):
        """`<name>` is mandatory - `mixType` extends `namedType` - so the mix has to say
        something, and what it says is that nothing was recorded rather than `Air`. `<o2>`
        and `<he>` are both optional, so a fraction the source never had is simply not
        written: a `0` there would claim there is no oxygen in the cylinder."""
        bundle = build_bundle(
            dives=[make_dive(1, full_bundle().dives[0].uuid)],
            mixtures_by_dive={1: [mixture(oxygen=None, helium=None)]},
        )
        document = await _render(bundle, monkeypatch=monkeypatch)
        schema.validate(document)

        mixes = _tree(document).findall(f"{UDDF}gasdefinitions/{UDDF}mix")
        assert len(mixes) == 1
        assert _text(mixes[0], f"{UDDF}name") == "Unrecorded gas"
        assert mixes[0].find(f"{UDDF}o2") is None
        assert mixes[0].find(f"{UDDF}he") is None

    @pytest.mark.asyncio
    async def test_a_half_recorded_mix_is_spelled_out_rather_than_named(self, schema, monkeypatch):
        """`EAN32` would assert the helium this cylinder does not have, and a name is the
        one place a reader cannot see behind - so the half that was recorded is written and
        the half that was not says so, in the register the impossible-gas fallback already
        uses. The recorded `<o2>` still travels; only the unrecorded `<he>` is absent."""
        bundle = build_bundle(
            dives=[make_dive(1, full_bundle().dives[0].uuid)],
            mixtures_by_dive={1: [mixture(oxygen=32.0, helium=None)]},
        )
        document = await _render(bundle, monkeypatch=monkeypatch)
        schema.validate(document)

        mixes = _tree(document).findall(f"{UDDF}gasdefinitions/{UDDF}mix")
        assert _text(mixes[0], f"{UDDF}name") == "O2 32% / He unrecorded"
        assert _text(mixes[0], f"{UDDF}o2") == "0.32"
        assert mixes[0].find(f"{UDDF}he") is None

    def test_an_unrecorded_mix_is_not_the_same_mix_as_air(self):
        """`collect_mixes` keys on the fractions, and "nothing was recorded" is a value of
        its own there: one `<mix>` cannot be both air and an unknown gas. All the unknown
        ones do share a single entry, which is right - the document has one gas it knows
        nothing about, not one per cylinder."""
        bundle = build_bundle(
            dives=[make_dive(1, full_bundle().dives[0].uuid)],
            mixtures_by_dive={
                1: [mixture(), mixture(id=2, oxygen=None, helium=None), mixture(id=3, oxygen=None, helium=None)]
            },
        )
        assert len(collect_mixes(bundle)) == 2

    @pytest.mark.asyncio
    async def test_the_altitude_lands_between_the_datetime_and_the_equipment(self, schema, monkeypatch):
        """`informationbeforediveType` is an `xs:sequence`, so the position is the test:
        emitted anywhere else the document stops validating. The air dive records 0 m -
        the Red Sea really is at sea level - which is also what tells a `is not None`
        guard apart from a truthiness one."""
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        schema.validate(document)
        before = _dive(_tree(document), 0).find(f"{UDDF}informationbeforedive")
        assert _text(before, f"{UDDF}altitude") == "0"
        tags = [child.tag for child in before]
        assert tags.index(f"{UDDF}altitude") == tags.index(f"{UDDF}datetime") + 1
        assert tags.index(f"{UDDF}altitude") < tags.index(f"{UDDF}equipmentused")

    @pytest.mark.asyncio
    async def test_a_dive_that_records_no_altitude_gets_no_element(self, monkeypatch):
        """UDDF has no way to say "not recorded" other than leaving the element out."""
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        assert _text(_dive(_tree(document), 1), f"{UDDF}informationbeforedive/{UDDF}altitude") is None

    @pytest.mark.asyncio
    async def test_a_mountain_lake_altitude_is_written_as_metres(self, schema, monkeypatch):
        """Metres, unconverted - `altitudeType` in the XSD is `xs:float` and the schema's
        own documentation says metres, so this is one of the few places UDDF's SI units
        and ours already agree."""
        bundle = build_bundle(dives=[make_dive(1, UUIDS["dive-air"], altitude=1500, water_type="fresh")])
        document = await _render(bundle, monkeypatch=monkeypatch)
        schema.validate(document)
        assert _text(_dive(_tree(document), 0), f"{UDDF}informationbeforedive/{UDDF}altitude") == "1500"

    @pytest.mark.asyncio
    async def test_the_water_type_has_nowhere_to_go_in_this_format(self, monkeypatch):
        """3.2.2 has no *per-dive* salinity or density child at all: the `density`
        elements it does have belong to `sitedata` and to `baseCalculationType`, a
        deco-planner input. Asserted rather than left implicit, because a reader who greps
        the XSD for `density` finds hits and would otherwise "fix" this into
        `applicationdata`. It is in `logbook.divejson` and `dives.csv` instead."""
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        assert list(_tree(document).iter(f"{UDDF}density")) == []
        assert b"salt" not in document

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
    async def test_the_trip_is_linked_and_each_part_carries_its_own_dates(self, monkeypatch):
        """The mapping is close to an identity: a `<trippart>` is a part, so the two parts
        of the fixture trip emit their own ranges instead of the whole span landing on the
        first one."""
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        trip = _tree(document).find(f"{UDDF}divetrip/{UDDF}trip")
        assert _dive(_tree(document), 0).find(f"{UDDF}informationbeforedive/{UDDF}tripmembership").get(
            "ref"
        ) == trip.get("id")
        dates = [
            (element.get("startdate"), element.get("enddate"))
            for element in trip.findall(f"{UDDF}trippart/{UDDF}dateoftrip")
        ]
        assert dates == [
            ("2026-05-30T00:00:00", "2026-06-02T00:00:00"),
            ("2026-06-02T00:00:00", "2026-06-04T00:00:00"),
            # The end-only part: both attributes are required, so the one date it has
            # fills both. Formatting the absent start would emit `NoneT00:00:00`.
            ("2026-06-06T00:00:00", "2026-06-06T00:00:00"),
        ]

    @pytest.mark.asyncio
    async def test_each_part_gets_its_own_place_and_keeps_its_coordinates(self, schema, monkeypatch):
        """A place per part, where the whole trip used to get one joined line - and the
        coordinates survive with it, which they could not when three places shared one
        `<geography>`. The free-text part has no position and emits none."""
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        schema.validate(document)
        parts = _tree(document).findall(f"{UDDF}divetrip/{UDDF}trip/{UDDF}trippart")
        assert [part.findtext(f"{UDDF}geography/{UDDF}location") for part in parts] == [
            "Sharm el-Sheikh",
            "Ras Mohammed",
            None,
        ]
        assert [part.findtext(f"{UDDF}geography/{UDDF}latitude") for part in parts] == ["27.9158", None, None]

    @pytest.mark.asyncio
    async def test_a_part_with_no_place_gets_an_empty_name_and_no_geography(self, schema, monkeypatch):
        """`<location>` is mandatory inside `<geography>`, so a part with no place emits
        no element at all - and `<name>` is mandatory on the part itself but is an
        `xs:string`, so it gets an empty one rather than borrowing the trip's."""
        bundle = full_bundle()
        bundle.parts_by_trip[1] = [TripPartRead(start_date=date(2026, 5, 30), end_date=date(2026, 6, 6))]
        document = await _render(bundle, monkeypatch=monkeypatch)
        schema.validate(document)
        (part,) = _tree(document).findall(f"{UDDF}divetrip/{UDDF}trip/{UDDF}trippart")
        assert part.find(f"{UDDF}geography") is None
        assert part.findtext(f"{UDDF}name") in (None, "")

    @pytest.mark.asyncio
    async def test_a_part_with_no_dates_gets_no_dateoftrip(self, schema, monkeypatch):
        """`<dateoftrip>` is `minOccurs="0"`, so the absence is expressible here - unlike
        in `logbook.divejson`, where the span is REQUIRED."""
        bundle = full_bundle()
        bundle.parts_by_trip[1] = [TripPartRead(location=TripLocationRead(name="Dahab"))]
        document = await _render(bundle, monkeypatch=monkeypatch)
        schema.validate(document)
        (part,) = _tree(document).findall(f"{UDDF}divetrip/{UDDF}trip/{UDDF}trippart")
        assert part.find(f"{UDDF}dateoftrip") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "part",
        [
            pytest.param(TripPartRead(start_date=date(2026, 5, 30)), id="start-only"),
            pytest.param(TripPartRead(end_date=date(2026, 5, 30)), id="end-only"),
        ],
    )
    async def test_a_part_with_one_date_repeats_it(self, schema, monkeypatch, part):
        """Both attributes are `use="required"`, and the rule is symmetric: a stretch that
        began on a day and has no recorded end ends that day, and one that ended on a day
        with no recorded start began it.

        The end-only direction is the one that bites. Each date is independently optional
        and `validate_date_range` only compares a pair, so every write route accepts it -
        and formatting the absent start would emit `NoneT00:00:00`, which `schema.validate`
        below is what catches.
        """
        bundle = full_bundle()
        bundle.parts_by_trip[1] = [part]
        document = await _render(bundle, monkeypatch=monkeypatch)
        schema.validate(document)
        dates = _tree(document).find(f"{UDDF}divetrip/{UDDF}trip/{UDDF}trippart/{UDDF}dateoftrip")
        assert (dates.get("startdate"), dates.get("enddate")) == ("2026-05-30T00:00:00", "2026-05-30T00:00:00")

    @pytest.mark.asyncio
    async def test_a_trip_with_no_parts_still_gets_one_trippart(self, schema, monkeypatch):
        """`tripType` requires at least one, so the floor is the writer's rather than the
        data's. Without it the document would be silently invalid, and no fixture would
        catch it: every trip in every corpus document has a place."""
        bundle = full_bundle()
        bundle.parts_by_trip[1] = []
        document = await _render(bundle, monkeypatch=monkeypatch)
        schema.validate(document)
        (part,) = _tree(document).findall(f"{UDDF}divetrip/{UDDF}trip/{UDDF}trippart")
        assert part.find(f"{UDDF}geography") is None
        assert part.find(f"{UDDF}dateoftrip") is None

    @pytest.mark.asyncio
    async def test_the_trips_notes_stay_on_the_first_part(self, schema, monkeypatch):
        """The reader joins every part's notes, so writing them on each one returns them N
        times through a round trip."""
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        schema.validate(document)
        parts = _tree(document).findall(f"{UDDF}divetrip/{UDDF}trip/{UDDF}trippart")
        assert [part.findtext(f"{UDDF}notes/{UDDF}para") for part in parts] == ["Liveaboard", None, None]


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
        assert (_text(geography, f"{UDDF}latitude"), _text(geography, f"{UDDF}longitude")) == ("27.7", "34.2")

    @pytest.mark.asyncio
    async def test_a_lone_coordinate_is_not_a_position(self, schema, monkeypatch):
        """The write schemas refuse half a pair, but nothing at the database level does,
        so a restored dump or a hand-run `UPDATE` can hand one to the writer. `<latitude>`
        without `<longitude>` is valid UDDF and a lie, so the pair is dropped - and with
        no location either, that leaves no `<geography>` to emit."""
        site = make_dive_site(2, UUIDS["site-wall"], latitude=27.7)
        document = await _render(build_bundle(dive_sites=[site]), monkeypatch=monkeypatch)
        schema.validate(document)
        assert self._site(_tree(document), 0).find(f"{UDDF}geography") is None

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
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
        waypoints = _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        assert [_text(w, f"{UDDF}divetime") for w in waypoints] == ["0", "30", "60", "90"]
        assert all(_text(w, f"{UDDF}depth") is not None for w in waypoints)
        # The 30 s waypoint has a depth but no temperature - temperature was sampled at
        # 0 s and 60 s only, and nothing off-axis is near enough to claim it.
        assert _text(waypoints[1], f"{UDDF}temperature") is None
        assert _text(waypoints[1], f"{UDDF}depth") == "18"

    @pytest.mark.asyncio
    async def test_readings_between_depth_samples_snap_to_the_nearest(self, schema, monkeypatch):
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: OFF_GRID_PROFILE}, monkeypatch)
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
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: OFF_GRID_PROFILE}, monkeypatch)
        waypoints = _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        assert [_text(w, f"{UDDF}tankpressure") for w in waypoints] == [None, "20000000", None, None]

    @pytest.mark.asyncio
    async def test_events_snap_too_and_still_join_on_arrival(self, monkeypatch):
        """The 7 s and 8 s markers are not simultaneous in the profile; they become so
        here, which is the case `waypointType`'s single `<setmarker>` cannot hold."""
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: OFF_GRID_PROFILE}, monkeypatch)
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
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: profile}, monkeypatch)
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
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: profile}, monkeypatch)
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
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: profile}, monkeypatch)
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
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: profile}, monkeypatch)
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
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: profile}, monkeypatch)
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
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: profile}, monkeypatch)
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
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: profile}, monkeypatch)
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
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: profile}, monkeypatch)
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
        `logbook.divejson` either way.
        """
        profile = {"temperature": {"t": [0, 60], "v": [249, 181]}, "events": [{"t": 30, "type": "safety_stop"}]}
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: profile}, monkeypatch)
        schema.validate(document)
        assert _dive(_tree(document), 1).find(f"{UDDF}samples") is None

    @pytest.mark.asyncio
    async def test_gas_switches_become_switchmix_links(self, monkeypatch):
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
        waypoints = _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        mixes = {m.get("id") for m in _tree(document).findall(f"{UDDF}gasdefinitions/{UDDF}mix")}
        switches = [w.find(f"{UDDF}switchmix") for w in waypoints]
        assert [s is not None for s in switches] == [True, False, False, True]
        assert {s.get("ref") for s in switches if s is not None} <= mixes

    @pytest.mark.asyncio
    async def test_simultaneous_markers_are_joined_rather_than_dropped(self, monkeypatch):
        """`waypointType` allows one `<setmarker>`, and the fixture puts two events at 60s."""
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
        waypoints = _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        assert _text(waypoints[2], f"{UDDF}setmarker") == "safety_stop; Ceiling Broken"

    @pytest.mark.asyncio
    async def test_a_pressure_channel_with_no_matching_cylinder_is_dropped(self, schema, monkeypatch):
        """`<tankpressure ref>` is an `xs:IDREF`; a dangling one would fail validation.

        The fixture's `gas_number: 9` channel has no mixture, which is what a device that
        reports five cylinder slots for a two-cylinder dive produces.
        """
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
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
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
        assert list(_tree(document).iter(f"{UDDF}decostop")) == []

    @pytest.mark.asyncio
    async def test_otu_is_not_emitted_and_the_dive_s_own_totals_are_not_either(self, monkeypatch):
        """The half of the old CNS/OTU refusal that survived the deco channels.

        `<otu>` is a `<waypoint>` child exactly like `<cns>`, and it stays empty for the
        one reason `<cns>` no longer does: there is no `otu` channel to put in it. The
        dive's own `cns_start`/`cns_end`/`otu_start`/`otu_end` are a different quantity
        again - the device's figures for the whole dive - and `informationafterdiveType`
        has no oxygen-exposure element to carry them.
        """
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
        assert list(_tree(document).iter(f"{UDDF}otu")) == []
        # 8.0 and 21.0 are `full_bundle`'s `cns_end` and `otu_end`, asserted from the other
        # direction by `test_export_json.py` - the document that does carry them.
        after = _dive(_tree(document), 1).find(f"{UDDF}informationafterdive")
        assert {child.text for child in after}.isdisjoint({"8", "21"})

    @pytest.mark.asyncio
    async def test_the_deco_model_is_not_emitted(self, monkeypatch):
        """`<decomodel>` is an `xs:all` whose three branches are each `minOccurs` 1 and each
        require a `<tissue>` table, which a recording does not hold.

        `<gradientfactorlow>`/`<gradientfactorhigh>` live inside `<buehlmann>`, so the
        recording's gradient-factor pair goes with it - the per-waypoint `<gradientfactor>`
        is a reading and not the setting.
        """
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
        tree = _tree(document)
        for tag in ("decomodel", "buehlmann", "gradientfactorlow", "gradientfactorhigh"):
            assert list(tree.iter(f"{UDDF}{tag}")) == [], tag
        # `full_bundle`'s recording runs ZHL-16C at 50/85 with a conservatism of -1.
        assert b"ZHL-16C" not in document

    @pytest.mark.asyncio
    async def test_a_waypoint_carries_no_child_this_writer_did_not_choose(self, monkeypatch):
        """The closed set, which is how `tts` and `surface_gradient_factor` get asserted.

        3.2.2 has no time-to-surface element and no surface gradient factor, so unlike the
        ceiling and the deco model there is no tag to look for and prove absent. What can
        be pinned is that the waypoints carry *exactly* the eleven children this writer
        chose - so a later hand that decides `tts` is close enough to `<remainingbottom
        time>`, or that the surface figure may as well ride in `<gradientfactor>` beside
        the leading tissue's, breaks this rather than shipping a number the file did not
        record.

        `TRIMIX_PROFILE` carries every channel the format defines, which is what makes the
        set meaningful: a channel missing here would be a channel with nowhere to go.
        """
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
        waypoints = _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        written = {child.tag.removeprefix(UDDF) for waypoint in waypoints for child in waypoint}
        assert written == {
            "cns",
            "calculatedpo2",
            "depth",
            "divetime",
            "setmarker",
            "switchmix",
            "tankpressure",
            "temperature",
            "divemode",
            "gradientfactor",
            "nodecotime",
        }

    @pytest.mark.asyncio
    async def test_the_owner_s_email_is_not_in_the_file(self, monkeypatch):
        """The schema has a slot. A UDDF file is what a diver hands to a dive shop."""
        document = await _render(full_bundle(), monkeypatch=monkeypatch)
        assert b"ada@example.com" not in document


class TestDecoReadouts:
    """The four channels UDDF has a `<waypoint>` child for, and the units they go out in.

    The refusals in `TestWhatUddfCannotHold` are the same rule pointed the other way: an
    element the format holds and this writer skips is data lost for no reason, which is
    what the module docstring's census exists to keep honest in both directions.
    """

    @staticmethod
    def _waypoints(document: bytes) -> list[ET.Element]:
        return _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")

    @pytest.mark.asyncio
    async def test_the_no_decompression_clock_is_seconds_in_both(self, monkeypatch):
        """The one channel with no factor, and 5940 is why the absence is worth a test:
        a Shearwater's display maximum means *at least this*, not "unrecorded"."""
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
        waypoints = self._waypoints(document)
        assert [_text(w, f"{UDDF}nodecotime") for w in waypoints] == ["5940", "1260", "0", None]

    @pytest.mark.asyncio
    async def test_the_calculated_ppo2_is_bar_where_we_store_hundredths(self, monkeypatch):
        """0.34 bar and 0.96 bar, hand-computed from the stored 34 and 96.

        The second pressure in this writer that is not Pascal, `<mix><maximumpo2>` being
        the first - which is exactly why it gets its own assertion.
        """
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
        waypoints = self._waypoints(document)
        assert [_text(w, f"{UDDF}calculatedpo2") for w in waypoints] == ["0.34", None, "0.96", None]

    @pytest.mark.asyncio
    async def test_the_cns_clock_is_percent_where_we_store_tenths(self, monkeypatch):
        """10 % and 80 %, from the stored 100 and 800."""
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
        waypoints = self._waypoints(document)
        assert [_text(w, f"{UDDF}cns") for w in waypoints] == ["10", None, None, "80"]

    @pytest.mark.asyncio
    async def test_the_gradient_factor_goes_out_as_the_documented_fraction(self, monkeypatch):
        """17 % is `0.17` and not `17`, and 398 % is `3.98` and not `1`.

        The percent spelling is only read back correctly by a consumer that recognizes the
        generator that wrote the file, and nothing recognizes this app's - including this
        app's own logbook import, which is the reader most likely to meet its own export.
        A written `17` would come back as 1700.

        398 is the second half of the same assertion: the channel is uncapped above (a real
        Suunto ascent reaches five figures), so a writer that clamped to the 0-1 the *pair*
        is documented with would flatten a reading the diver actually saw.
        """
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
        waypoints = self._waypoints(document)
        assert [_text(w, f"{UDDF}gradientfactor") for w in waypoints] == [None, None, "0.17", "3.98"]

    @pytest.mark.asyncio
    async def test_the_readouts_are_ordered_by_the_schema_and_not_by_the_channel_list(self, monkeypatch):
        """`waypointType` is an `xs:sequence`, and its order is nothing like ours.

        `<cns>` is third in the type and therefore *first* in a waypoint carrying no alarm
        or battery reading - ahead of the `<depth>` it was computed at - while
        `<nodecotime>` is last of all. The XSD tests catch a violation; this one says what
        the order is, so a reader of the writer does not have to reconstruct it from a
        schema failure.
        """
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
        first = self._waypoints(document)[0]
        assert [child.tag.removeprefix(UDDF) for child in first] == [
            "cns",
            "calculatedpo2",
            "depth",
            "divetime",
            "switchmix",
            "tankpressure",
            "temperature",
            "divemode",
            "nodecotime",
        ]

    @pytest.mark.asyncio
    async def test_a_readout_snaps_to_the_depth_axis_like_every_other_channel(self, schema, monkeypatch):
        """No exemption for being a computed reading rather than a measured one.

        The rule is the depth channel's - a reading that cannot reach a waypoint within
        half its typical interval is dropped rather than relocated - and a no-decompression
        clock moved 800 s onto a waypoint it was not computed anywhere near would be as
        wrong as a temperature moved the same distance. Here 4 s snaps back to 0 s and 27 s
        forward to 30 s, and the reading at 55 s is 25 s past the last depth sample - the
        second of the two cases `_snap_tolerance` names, a reading taken after the diver
        surfaced rather than one inside a dropout, and the depth axis here is uniform.
        """
        profile = {
            "depth": {"t": [0, 10, 20, 30], "v": [0, 1000, 2000, 1500]},
            "ndl": {"t": [4, 27, 55], "v": [900, 0, 600]},
        }
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: profile}, monkeypatch)
        schema.validate(document)
        waypoints = self._waypoints(document)
        assert [_text(w, f"{UDDF}nodecotime") for w in waypoints] == ["900", None, None, "0"]


class TestDiveMode:
    """`<divemode type>`, which is the recording's setting rather than a reading.

    It rides on the first waypoint because that is the only place UDDF has for it, and it
    comes from the recording whose samples are in the document - the primary one - for the
    same reason the samples do.
    """

    @staticmethod
    def _with_mode(mode: str | None) -> Any:
        """`full_bundle` with its one recording's mode replaced.

        `full_bundle`'s own value is `open_circuit`, which the conformance tests depend on,
        so these vary it here rather than in the shared helper.
        """
        bundle = full_bundle()
        bundle.recordings_by_dive[2] = [replace(bundle.recordings_by_dive[2][0], mode=mode)]
        return bundle

    @pytest.mark.asyncio
    async def test_the_mode_lands_on_the_first_waypoint_and_no_other(self, schema, monkeypatch):
        """One setting for the whole recording, so once per profile.

        Repeating it would be the same fact written several thousand times, and a reader
        takes the mode from the first waypoint that states one either way.
        """
        document = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
        schema.validate(document)
        waypoints = _dive(_tree(document), 1).findall(f"{UDDF}samples/{UDDF}waypoint")
        modes = [w.find(f"{UDDF}divemode") for w in waypoints]
        assert [None if m is None else m.get("type") for m in modes] == ["opencircuit", None, None, None]

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ("open_circuit", "opencircuit"),
            ("closed_circuit", "closedcircuit"),
            ("semi_closed", "semiclosedcircuit"),
            # `apnoe` rather than the `apnea` added beside it in 2017: both are current in
            # 3.2.x and the older word is the one every reader knows.
            ("freedive", "apnoe"),
        ],
    )
    @pytest.mark.asyncio
    async def test_each_mode_uddf_can_name_is_written_in_uddf_s_spelling(self, stored, expected, schema, monkeypatch):
        document = await _render(self._with_mode(stored), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
        schema.validate(document)
        first = _dive(_tree(document), 1).find(f"{UDDF}samples/{UDDF}waypoint")
        assert first.find(f"{UDDF}divemode").get("type") == expected

    @pytest.mark.parametrize(
        "stored",
        [
            # `divemodeType`'s five values have no bottom-timer among them, so the nearest
            # would tell an importer the diver was on a circuit they were not.
            pytest.param("gauge", id="gauge-has-no-uddf-value"),
            # The column is a stored vocabulary with no DB `CHECK`, so a row outside
            # `DiveMode` is a real row and not a defensive hypothetical.
            pytest.param("rebreather", id="outside-the-vocabulary"),
            pytest.param(None, id="the-file-recorded-no-mode"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_mode_uddf_cannot_name_gets_no_element_rather_than_the_nearest(self, stored, schema, monkeypatch):
        """Three different facts, one answer, and the answer is silence.

        UDDF reads an absent `<divemode>` as open circuit, which is the format's claim
        about its own default rather than ours about the dive - and it is the only spelling
        available, `divemodeType` having no way to say "not one of these".
        """
        document = await _render(self._with_mode(stored), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
        schema.validate(document)
        assert list(_tree(document).iter(f"{UDDF}divemode")) == []

    def test_every_dive_mode_has_an_answer_here(self):
        """`_DIVE_MODE_TYPE` is looked up unguarded once `DiveMode(stored)` has succeeded,
        so a mode added to the enum without a row here would be a `KeyError` on a diver's
        download. The `None` for `gauge` is what lets this be an equality rather than a
        subset - "no UDDF value" is answered in the table instead of being absent from it.
        """
        assert set(_DIVE_MODE_TYPE) == set(DiveMode)

    @pytest.mark.asyncio
    async def test_a_recording_with_no_samples_has_nowhere_to_put_its_mode(self, monkeypatch):
        """`informationbeforedive` has no slot of its own, so a profile with no depth
        channel - which emits no `<samples>` at all - loses the mode with it. It survives
        in `logbook.divejson`, which carries the recording rather than the waypoints."""
        document = await _render(
            full_bundle(), {PRIMARY_RECORDING_ID: {"temperature": {"t": [0], "v": [250]}}}, monkeypatch
        )
        assert list(_tree(document).iter(f"{UDDF}samples")) == []
        assert list(_tree(document).iter(f"{UDDF}divemode")) == []


class TestDeterminism:
    @pytest.mark.asyncio
    async def test_two_exports_of_an_unchanged_logbook_are_byte_identical(self, monkeypatch):
        first = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
        second = await _render(full_bundle(), {PRIMARY_RECORDING_ID: TRIMIX_PROFILE}, monkeypatch)
        assert first == second


class TestEquipmentMapping:
    def test_every_gear_type_has_a_uddf_element(self):
        """`_EQUIPMENT_ELEMENT` is looked up unguarded, and `full_bundle` only exercises
        three of the twenty-four categories - so an enum member added without a home here
        would first surface as a 500 on a diver's download."""
        assert set(_EQUIPMENT_ELEMENT) == set(GearType)

    def test_every_mapped_element_has_a_slot_in_the_sequence(self):
        """`equipmentType` is an `xs:sequence`, so an element `_EQUIPMENT_ORDER` does not
        list would simply never be emitted."""
        assert set(_EQUIPMENT_ELEMENT.values()) <= set(_EQUIPMENT_ORDER)

    @pytest.mark.asyncio
    async def test_the_cutting_tools_all_render_as_knife(self, schema, monkeypatch):
        """`equipmentType` has exactly one cutting-tool element, so `line_cutter` and
        `shears` join `knife` in it rather than falling into `<variouspieces>` beside the
        SMB and the camera. Three of our categories collapse onto one element and the
        distinction is not recoverable from the file - which costs nothing, because no
        reader imports a UDDF kit list back (see DECISIONS.md)."""
        bundle = build_bundle(
            gear_items=[
                _gear("Z-Knife", GearType.KNIFE),
                _gear("Trilobite", GearType.SHEARS),
                _gear("Piranha", GearType.LINE_CUTTER),
            ]
        )
        document = await _render(bundle, monkeypatch=monkeypatch)
        schema.validate(document)
        knives = _tree(document).findall(f".//{UDDF}knife/{UDDF}name")
        assert [e.text for e in knives] == ["Z-Knife", "Trilobite", "Piranha"]
        assert _tree(document).find(f".//{UDDF}variouspieces") is None

    @pytest.mark.asyncio
    async def test_a_mirror_and_a_whistle_land_in_variouspieces(self, schema, monkeypatch):
        """Neither has any other home in `equipmentType` - the catch-all is the whole
        answer here rather than a choice between slots."""
        bundle = build_bundle(gear_items=[_gear("Signal mirror", GearType.MIRROR), _gear("Fox 40", GearType.WHISTLE)])
        document = await _render(bundle, monkeypatch=monkeypatch)
        schema.validate(document)
        various = _tree(document).findall(f".//{UDDF}variouspieces/{UDDF}name")
        assert [e.text for e in various] == ["Signal mirror", "Fox 40"]


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
