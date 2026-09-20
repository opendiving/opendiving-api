"""Writes a diver's logbook as a UDDF 3.2.2 document.

UDDF is the interchange format the export exists for: it is what Subsurface, divelogs.de
and MacDive read, and it is the half of "your data, in open formats" that another program
has to be able to open. `tests/fixtures/uddf/uddf_3.2.2.xsd` is vendored alongside the
tests, and every document this module produces is validated against it - the schema is
the referee for everything below, not habit or memory.

**UDDF is SI throughout, and our storage is not.** Depths are meters (ours already are),
times seconds (ditto), but temperatures are **Kelvin** where we hold tenths of a degree
Celsius, pressures **Pascal** where we hold tenths of a bar and plain bar, and tank
volumes **cubic meters** where a diver says "twelve litres". Every one of those
conversions is a one-liner with a unit test carrying a hand-computed expectation, because
a silently wrong factor of 100 000 produces a file that validates perfectly and is
nonsense.

**The device's own decompression arithmetic goes out too, wherever UDDF has an element for
it.** `<nodecotime>`, `<calculatedpo2>`, `<cns>` and `<gradientfactor>` are `<waypoint>`
children, so `ndl`, `ppo2`, `cns` and `gradient_factor` travel a sample at a time beside
the depth they were computed at; the primary recording's `mode` becomes a `<divemode type>`
on the first waypoint. `<gradientfactor>` carries the **documented fraction** - our whole
percent divided by 100 - rather than the percent itself, because the percent spelling is
only read back by a consumer that recognizes the generator that wrote it, and nothing
recognizes ours. A reader taking `67` at the documented scale gets 6700 %; our own logbook
import is one of those readers, so the fraction is what makes a round trip through this
app's own front door land on the number it started from.

**What the format cannot hold, this module does not fake.** Every case here came out of
reading the XSD, and each is exported in `logbook.divejson`/CSV instead:

- **The deco ceiling.** The only per-waypoint slot is `<decostop>`, whose `duration`
  attribute is `use="required"` - and a ceiling sample says how deep the obligation was,
  never how long the stop should last. Emitting one would mean inventing the number that
  matters most.
- **OTU, and the dive's own CNS and OTU totals.** `informationafterdiveType` has no
  oxygen-exposure element at all, so `cns_start`/`cns_end`/`otu_start`/`otu_end` - the
  device's figures for the whole dive - have nowhere to go. `<otu>` is a `<waypoint>` child
  like `<cns>` and stays empty for the other half of the same sentence: we store no `otu`
  channel to put in it. The per-sample `cns` channel *is* written, which is what makes
  these two different answers now rather than one.
- **The deco model.** `<decomodel>` is an `xs:all` of `<buehlmann>`, `<rgbm>` and `<vpm>`
  with none of the three optional, and each of those requires at least one `<tissue>`
  carrying a half-time and its coefficients. A recording holds a family, the device's own
  name for the model, a gradient-factor pair and a conservatism setting - no tissue table -
  so there is no way to write one and stay valid against the XSD every document here is
  held to. `<gradientfactorlow>`/`<gradientfactorhigh>` live *inside* `<buehlmann>` and go
  with it, which is why `deco_gf_low`/`deco_gf_high` reach the file nowhere at all.
- **`tts` and `surface_gradient_factor`.** 3.2.2 has no time-to-surface element and no
  surface gradient factor - not a mandatory attribute we cannot fill, simply no slot. The
  per-waypoint `<gradientfactor>` above is the leading tissue's now, which is
  `gradient_factor` and not the surface figure beside it.
- **Gas `role`, tank `usage`, service schedules and training courses.** No slot for any
  of them. A course is the one that looks close to having one - `<divetrip>` carries a
  name and a date range - but a training course is not a trip, and folding it in would
  make an importer read "PADI Open Water" as a holiday and collide with the real trips
  already emitted there. UDDF has no
  manifold or sidemount representation at all: every cylinder is its own `<tankdata>`
  linking a shared `<mix>`, and the closest the spec comes is an aside on `<tankpressure>`
  that a linked double-tank measured at one pressure may omit the tank reference. So there
  is nothing to write `usage` into and nothing to read it back from. `po2_limit`, by
  contrast, *does* map - `<mix><maximumpo2>` - which is why mixes dedupe on it below.
  `usage` deliberately stays out of that dedup key: it is a property of the cylinder, not
  of the gas, and folding it in would split one `<mix>` into two for no reason UDDF knows.

Output is deterministic: fixed element order, fixed id derivation, and mixes sorted by
their fractions rather than by encounter. Golden-file tests depend on that, and so does
anyone diffing two exports of the same log.

The document is emitted **incrementally**. A thousand-dive log at 1 Hz is a few million
waypoints, and one `ElementTree` holding all of them would be hundreds of megabytes; so
the envelope is written by hand and each `<dive>` is built, serialized and dropped one at
a time. That is also why the profile payloads are fetched per dive here rather than
batched by `loader.py`.
"""

import bisect
import uuid as uuid_pkg
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.utils.datetime_offset import combine_start_time
from ...models.dive import Dive
from ...models.gear_item import GearItem
from ...schemas.dive import DiveMode
from ...schemas.dive_mixture import DiveMixtureRead
from ...schemas.dive_profile import (
    CNS_SCALE,
    DEPTH_SCALE,
    GRADIENT_FACTOR_SCALE,
    NDL_SCALE,
    PPO2_SCALE,
    PRESSURE_SCALE,
    TEMPERATURE_SCALE,
    ProfileEventType,
)
from ...schemas.gear_item import GearType
from ...schemas.trip import TripPartRead
from ..dive_profiles import load_profile
from .loader import ExportBundle
from .naming import gas_name

UDDF_NAMESPACE = "http://www.streit.cc/uddf/3.2/"
UDDF_VERSION = "3.2.2"

# Absolute zero, for the Celsius -> Kelvin conversions below. Spelled out once so the
# four call sites cannot drift.
KELVIN_OFFSET = 273.15
PASCAL_PER_BAR = 100_000.0
# UDDF tank volumes are cubic meters; divers, cylinder stampings and our `volume` column
# are all litres.
LITRES_PER_CUBIC_METRE = 1000.0
# `<gradientfactor>` is documented as a fraction - its one example is `0.8` glossed as 80 %
# - where `gradient_factor` is stored in whole percent. See the module docstring for why
# the fraction is the only spelling this writer can use.
PERCENT_PER_FRACTION = 100.0
# The furthest a reading is ever moved to reach a waypoint - see `_snap_tolerance`.
MAX_SNAP_SECONDS = 30

_INDENT = "  "

# Where each of our gear categories lands in `equipmentType`. That type is an
# `xs:sequence`, so the elements have to be emitted in *its* order, not ours - hence
# `_EQUIPMENT_ORDER` below rather than a plain iteration over the mapping.
#
# `camera` is the one deliberate demotion: UDDF's `cameraType` extends `ID_TYPE`, not
# `namedType`, so it has no `<name>` element and could only carry a nameless body/lens
# breakdown we don't record. A camera keeps its name as a `<variouspieces>` instead.
#
# `knife` is the one deliberate collapse: `equipmentType` has no cutting-tool slot other
# than that one, so `line_cutter` and `shears` join `knife` there rather than scattering
# a diver's cutting tools into `<variouspieces>` beside the SMB and the camera. Three of
# our categories therefore render as one element - see DECISIONS.md for why that is
# cheap here.
_EQUIPMENT_ELEMENT: dict[GearType, str] = {
    GearType.MASK: "mask",
    GearType.SNORKEL: "variouspieces",
    GearType.FINS: "fins",
    GearType.WETSUIT: "suit",
    GearType.DRYSUIT: "suit",
    GearType.VEST: "variouspieces",
    GearType.HOOD: "variouspieces",
    GearType.GLOVES: "gloves",
    GearType.BOOTS: "boots",
    GearType.BCD: "buoyancycontroldevice",
    GearType.REGULATOR: "regulator",
    GearType.COMPUTER: "divecomputer",
    GearType.CYLINDER: "tank",
    GearType.LIGHT: "light",
    GearType.SMB: "variouspieces",
    GearType.MIRROR: "variouspieces",
    GearType.WHISTLE: "variouspieces",
    GearType.REEL: "variouspieces",
    GearType.KNIFE: "knife",
    GearType.LINE_CUTTER: "knife",
    GearType.SHEARS: "knife",
    GearType.COMPASS: "compass",
    GearType.CAMERA: "variouspieces",
    GearType.OTHER: "variouspieces",
}

# `equipmentType`'s own declaration order.
_EQUIPMENT_ORDER = (
    "boots",
    "buoyancycontroldevice",
    "compass",
    "divecomputer",
    "fins",
    "gloves",
    "knife",
    "light",
    "mask",
    "regulator",
    "suit",
    "tank",
    "variouspieces",
)

_SUIT_TYPE = {GearType.WETSUIT: "wet-suit", GearType.DRYSUIT: "dry-suit"}

# What `<divemode type>` spells each of our modes. `divemodeType` enumerates five values and
# `DiveMode` five, and they are not the same five.
#
# `freedive` is written `apnoe` rather than `apnea`: the schema carries both, `apnea` having
# been added beside the original in 2017, and the older word is the one every 3.2.x reader
# knows.
#
# **`gauge` maps to nothing, and that is a fact about UDDF.** `divemodeType` has no value
# for a computer run as a bottom timer, so a gauge recording gets no `<divemode>` at all -
# writing the nearest would tell an importer the diver was on a circuit they were not.
# Spelled as an explicit `None` rather than left out, so the assert below covers every mode
# and a sixth added later cannot arrive here as a silent hole.
_DIVE_MODE_TYPE: dict[DiveMode, str | None] = {
    DiveMode.OPEN_CIRCUIT: "opencircuit",
    DiveMode.CLOSED_CIRCUIT: "closedcircuit",
    DiveMode.SEMI_CLOSED: "semiclosedcircuit",
    DiveMode.GAUGE: None,
    DiveMode.FREEDIVE: "apnoe",
}

# `_EQUIPMENT_ELEMENT` is looked up unguarded, so a `GearType` added without a home here
# would be a `KeyError` at export time rather than a mis-categorized item - and the one
# place it would surface is a diver's download. Asserted at import, where it is a startup
# failure in CI instead.
assert set(_EQUIPMENT_ELEMENT) == set(GearType), "every GearType needs a UDDF equipment element"
assert set(_EQUIPMENT_ELEMENT.values()) <= set(_EQUIPMENT_ORDER), "equipmentType is a sequence; every tag needs a slot"
# The same guard for the same reason, and the `None` above is why it can be an equality: a
# mode with no UDDF value is answered here rather than absent from here.
assert set(_DIVE_MODE_TYPE) == set(DiveMode), "every DiveMode needs an answer, including 'no UDDF value'"


def _num(value: float) -> str:
    """Format a float for an `xs:float` element.

    Fixed notation with the trailing zeros trimmed, rather than `%g`: a tank pressure in
    Pascal is eight digits, and `%g` would render 20 520 000 as `2.052e+07`. Legal, but
    the first thing a human checking an export looks at is whether the numbers look like
    numbers.
    """
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-", "-0") else text


# Everything XML 1.0 forbids outright, even escaped: the C0 controls other than tab, LF
# and CR. `ElementTree` escapes `&`, `<` and `>` and passes these straight through, so a
# single one makes the whole document unparseable - the "validates perfectly and is
# nonsense" failure this module is otherwise careful about, in its loudest form.
#
# They do reach here. Notes and names are plain Pydantic strings with no character
# filter, and `<setmarker>` carries a device's own wording off an uploaded file
# (`dive_parsers/fit.py` builds it with `str(data)`). Scrubbing at the single point every
# string passes through is the only version of this that cannot be forgotten at a call
# site. Dropped rather than replaced: they carry no meaning a diver put there.
_FORBIDDEN_IN_XML = str.maketrans(dict.fromkeys(range(0x20), None) | {0x09: "\t", 0x0A: "\n", 0x0D: "\r", 0x7F: None})


def _xml_safe(value: str) -> str:
    return value.translate(_FORBIDDEN_IN_XML)


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    element = ET.SubElement(parent, tag, {key: _xml_safe(value) for key, value in attrs.items()})
    if text is not None:
        element.text = _xml_safe(text)
    return element


def _optional(parent: ET.Element, tag: str, value: float | None) -> None:
    """Emit `<tag>` only when there is a reading. `None` means the file never recorded
    it, and UDDF has no way to say that other than by leaving the element out."""
    if value is not None:
        _sub(parent, tag, _num(value))


def _serialize(element: ET.Element, *, level: int) -> bytes:
    ET.indent(element, space=_INDENT, level=level)
    return (_INDENT * level + ET.tostring(element, encoding="unicode") + "\n").encode("utf-8")


def _person_names(full_name: str, username: str) -> tuple[str, str]:
    """Split a single free-text name into UDDF's required `firstname`/`lastname` pair.

    `personalType` makes both mandatory, and we store one string. The first whitespace-
    separated token becomes the given name and the remainder the family name, which is
    right for the overwhelmingly common "Ada Lovelace" and harmless for the rest.

    A one-token name leaves `<lastname>` **empty** rather than repeating the given name
    or substituting the username: an empty `xs:string` is valid, and it says "we don't
    hold this" instead of asserting a surname the diver never gave.
    """
    tokens = full_name.split()
    if not tokens:
        return username, ""
    return tokens[0], " ".join(tokens[1:])


# How finely two gases have to differ to be different `<mix>` entries. Three decimal
# places of a percentage is a thousandth of a percent - a hundred times finer than any
# analyzer reads, and coarse enough to absorb the float noise the corpus actually
# carries: the dev database holds `28.000000000000004` and `28` as separate `oxygen`
# values, and `28.999999999999996` beside `29`. Without this the export emitted two
# `<mix id>` entries whose `<o2>` printed the same number, because `_num` already rounds
# on the way out - the dedup key and the rendered value have to agree, and this is what
# makes them.
_MIX_PRECISION = 3


@dataclass(frozen=True, slots=True)
class _MixKey:
    """What makes two cylinders the same `<mix>`.

    `po2_limit` is part of the key, not just of the payload. It maps onto
    `<mix><maximumpo2>`, and a diver who carries the same EAN32 planned to 1.4 on the
    bottom and 1.6 on the ascent has genuinely defined two mixes as far as UDDF is
    concerned - collapsing them would mean picking one limit and silently dropping the
    other.

    An unrecorded `oxygen` or `helium` is a key value of its own rather than a zero, on
    exactly that reasoning: "no mix was recorded" and "air" are different gases, and one
    `<mix>` cannot be both. All such cylinders do share a single `<mix>`, which is
    correct - the document has one gas it knows nothing about, not one per cylinder.
    """

    oxygen: float | None
    helium: float | None
    po2_limit: float | None

    @property
    def sort_key(self) -> tuple[float, float, float]:
        # `-1.0` sorts an unrecorded value ahead of every real one - the fractions floor
        # at 0 and the ppO2 constraint at 0.4 bar - so the mix list is stable without
        # special-casing `None`.
        return (
            -1.0 if self.oxygen is None else self.oxygen,
            -1.0 if self.helium is None else self.helium,
            -1.0 if self.po2_limit is None else self.po2_limit,
        )


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, _MIX_PRECISION)


def _mix_key(mixture: DiveMixtureRead) -> _MixKey:
    return _MixKey(
        oxygen=_rounded(mixture.oxygen),
        helium=_rounded(mixture.helium),
        po2_limit=_rounded(mixture.po2_limit),
    )


def collect_mixes(bundle: ExportBundle) -> dict[_MixKey, str]:
    """Every distinct gas in the log, mapped to the `xs:ID` its `<mix>` will carry.

    Sorted by the fractions rather than by first encounter, so an id is a function of the
    *set* of gases and of nothing else - not of how many dives used each, nor of the order
    they were logged in. Two exports of the same logbook therefore agree, and logging
    another dive on a gas already in the file changes no ids.

    It does **not** follow that an id survives a gas disappearing: dropping a middle gas
    shifts every one after it, and only persisting the numbers would prevent that. They
    are document-local labels, which is all `xs:ID` has to be.
    """
    keys = {_mix_key(mixture) for mixtures in bundle.mixtures_by_dive.values() for mixture in mixtures}
    return {key: f"mix-{index}" for index, key in enumerate(sorted(keys, key=lambda k: k.sort_key), start=1)}


def _uddf_id(prefix: str, uuid: uuid_pkg.UUID) -> str:
    """An `xs:ID` from a public uuid.

    Prefixed because `xs:ID` is an `NCName` and cannot start with a digit, which a hex
    uuid regularly does. The prefix doubles as a readable hint at what a `ref` points to.
    """
    return f"{prefix}-{uuid}"


def _gear_type(stored: str | None) -> GearType:
    """The `GearType` a stored `gear_item.type` maps to, for choosing a UDDF element.

    Unlike DiveJSON, UDDF has no vocabulary of ours to keep here: the value only selects
    which typed element the piece is written into, and `<variouspieces>` is already the
    catch-all `OTHER` lands in. So a stored value outside the enum takes the same route as
    no value at all - the diver's gear still appears in the document, named and branded,
    under the element that means "something else".
    """
    try:
        return GearType(stored) if stored else GearType.OTHER
    except ValueError:
        return GearType.OTHER


def _divemode_type(stored: str | None) -> str | None:
    """The `<divemode type>` a stored `dive_recording.mode` maps to, or `None` for no element.

    Three separate cases arrive as the same answer, which is the point: no mode was recorded,
    the mode was `gauge` and UDDF has no word for it, or the column holds a value outside
    `DiveMode` altogether. `mode` is a stored vocabulary with no DB `CHECK` behind it (see
    *"A stored vocabulary is read back as a string"* in DECISIONS.md), so the third is a real
    row and not a defensive hypothetical - the same trap `_gear_type` above exists for, where
    a bare `DiveMode(stored)` was a `ValueError` on one row and a 500 on the whole export.

    `None` here means the waypoint gets no `<divemode>`, which UDDF reads as its own default
    of open circuit. That is the format's claim about its default rather than ours about the
    dive, and it is the only thing this writer can do: an absence is the one spelling
    available for a mode 3.2.2 cannot name.
    """
    if not stored:
        return None
    try:
        mode = DiveMode(stored)
    except ValueError:
        return None
    return _DIVE_MODE_TYPE[mode]


def _equipment_element(bundle: ExportBundle) -> ET.Element | None:
    """The owner's whole gear list, grouped into UDDF's typed equipment elements."""
    if not bundle.gear_items:
        return None

    by_element: dict[str, list[tuple[GearType, GearItem]]] = {}
    for item in bundle.gear_items:
        # `_gear_type`, not `GearType(item.type)`: the column is deliberately
        # unconstrained (see *"A stored vocabulary is read back as a string"* in
        # DECISIONS.md), and the bare conversion raised `ValueError` on a row outside the
        # enum - a 500 on `GET /export/uddf` and on the whole archive with it.
        gear_type = _gear_type(item.type)
        by_element.setdefault(_EQUIPMENT_ELEMENT[gear_type], []).append((gear_type, item))

    equipment = ET.Element("equipment")
    for tag in _EQUIPMENT_ORDER:
        for gear_type, item in by_element.get(tag, []):
            piece = _sub(equipment, tag, id=_uddf_id("gear", item.uuid))
            _sub(piece, "name", item.name)
            if item.brand:
                # `manufacturerType` extends `namedType` -> `ID_TYPE`, so the id is
                # mandatory, and `<manufacturer>` is an inline child of each piece rather
                # than a shared definition anything links to. So the id has to be unique
                # **per occurrence**, not per brand: an earlier version keyed it on the
                # brand and emitted `mfr-1` twice the moment a diver owned two Apeks
                # items, which duplicates an `xs:ID` and makes the whole document invalid.
                # Derived from the owning item's uuid because nothing references it, so
                # the only requirement is uniqueness.
                manufacturer = _sub(piece, "manufacturer", id=_uddf_id("mfr", item.uuid))
                _sub(manufacturer, "name", item.brand)
            if item.notes:
                _sub(_sub(piece, "notes"), "para", item.notes)
            if tag == "suit" and gear_type in _SUIT_TYPE:
                _sub(piece, "suittype", _SUIT_TYPE[gear_type])
    return equipment


def _diver_element(bundle: ExportBundle) -> ET.Element:
    diver = ET.Element("diver")
    owner = _sub(diver, "owner", id="owner")
    personal = _sub(owner, "personal")
    first, last = _person_names(bundle.user.name, bundle.user.username)
    _sub(personal, "firstname", first)
    _sub(personal, "lastname", last)

    # No `<contact><email>`, although the schema has the slot: a UDDF file is the thing a
    # diver hands to a dive shop or uploads to divelogs.de, and their address riding along
    # in it would be a surprise. It is in `logbook.divejson`, which is the diver's own copy.
    equipment = _equipment_element(bundle)
    if equipment is not None:
        owner.append(equipment)
    return diver


def _divesite_element(bundle: ExportBundle) -> ET.Element | None:
    if not bundle.dive_sites:
        return None
    divesite = ET.Element("divesite")
    for site in bundle.dive_sites:
        element = _sub(divesite, "site", id=_uddf_id("site", site.uuid))
        _sub(element, "name", site.name)
        # A lone coordinate is not a position and the write side won't store one, so a
        # pair is all or nothing here too.
        position = (site.latitude, site.longitude) if site.latitude is not None and site.longitude is not None else None
        if site.location or position is not None:
            # `geographyType` makes `<location>` mandatory, so a site with nothing to put
            # in a `<geography>` gets none at all rather than an empty one - and a site
            # that has only coordinates repeats its name there, since dropping the
            # position to stay silent about the location would lose the more useful half.
            geography = _sub(element, "geography")
            _sub(geography, "location", site.location or site.name)
            if position is not None:
                _sub(geography, "latitude", _num(position[0]))
                _sub(geography, "longitude", _num(position[1]))
        if site.notes:
            _sub(_sub(element, "notes"), "para", site.notes)
    return divesite


def _trippart_element(trip_element: ET.Element, part: TripPartRead | None) -> ET.Element:
    """One `<trippart>`, which is what a part is - the mapping is close to an identity.

    `<name>` is mandatory (`trippartType` extends `simpleNamedType`) but is an
    `xs:string`, so a part with no place gets an empty one rather than borrowing the
    trip's name, which would invent a place the diver never picked. `part` is `None` for
    the floor below.

    `<dateoftrip>` is `minOccurs="0"`, so a part with no dates simply has none - the
    absence is expressible here, unlike in `logbook.divejson`. Both of its attributes are
    required when it is present, so a part with one date repeats it: a stretch that began
    on a day and has no recorded end ends that day, which is what the pair already said
    for a trip.
    """
    element = _sub(trip_element, "trippart")
    location = None if part is None else part.location
    _sub(element, "name", "" if location is None else location.name)
    if part is not None and (part.start_date is not None or part.end_date is not None):
        # The attributes are `xs:dateTime` while we store plain dates, so each is widened
        # to midnight.
        start_date = part.start_date or part.end_date
        end_date = part.end_date or part.start_date
        _sub(element, "dateoftrip", startdate=f"{start_date}T00:00:00", enddate=f"{end_date}T00:00:00")
    if location is not None:
        # A place per part, where the whole trip used to get one joined line - and the
        # coordinates survive with it, `geographyType` allowing the single lat/lon pair a
        # part has where a trip of three places had no one position to put there.
        geography = _sub(element, "geography")
        _sub(geography, "location", location.name)
        if location.latitude is not None and location.longitude is not None:
            _sub(geography, "latitude", _num(location.latitude))
            _sub(geography, "longitude", _num(location.longitude))
    return element


def _divetrip_element(bundle: ExportBundle) -> ET.Element | None:
    if not bundle.trips:
        return None
    divetrip = ET.Element("divetrip")
    for trip in bundle.trips:
        element = _sub(divetrip, "trip", id=_uddf_id("trip", trip.uuid))
        _sub(element, "name", trip.name)
        parts = bundle.parts_by_trip[trip.id]
        # `tripType` requires at least one `<trippart>`, so a trip with no parts still
        # gets one - nameless and dateless, standing for the trip itself. Without the
        # floor the document would be silently invalid, and no fixture would catch it:
        # every trip in every corpus document has a place.
        elements = [_trippart_element(element, part) for part in parts] or [_trippart_element(element, None)]
        if trip.notes:
            # On the first part only. The reader joins every part's notes, so writing them
            # on each one returns them N times through a round trip.
            _sub(_sub(elements[0], "notes"), "para", trip.notes)
    return divetrip


def _gasdefinitions_element(mix_ids: dict[_MixKey, str]) -> ET.Element | None:
    if not mix_ids:
        return None
    gasdefinitions = ET.Element("gasdefinitions")
    for key, mix_id in mix_ids.items():
        mix = _sub(gasdefinitions, "mix", id=mix_id)
        # `<name>` is mandatory - `mixType` extends `namedType` - so `gas_name` always
        # returns a string, and says "unrecorded" rather than naming a gas it doesn't have.
        _sub(mix, "name", gas_name(key.oxygen, key.helium))
        # Fractions, not percentages: UDDF's `<o2>`/`<he>` are 0-1. Both are
        # `minOccurs="0"`, so a fraction the source never recorded is simply not written -
        # the format can say "no mix here" and a zero would say "no oxygen here".
        if key.oxygen is not None:
            _sub(mix, "o2", _num(key.oxygen / 100.0))
        if key.helium is not None:
            _sub(mix, "he", _num(key.helium / 100.0))
        if key.po2_limit is not None:
            # Bar in both formats - the one pressure UDDF does *not* express in Pascal,
            # which is exactly why it gets its own line and its own test.
            _sub(mix, "maximumpo2", _num(key.po2_limit))
    return gasdefinitions


def _generator_element(exported_at: datetime) -> ET.Element:
    generator = ET.Element("generator")
    _sub(generator, "name", settings.APP_NAME)
    _sub(generator, "type", "logbook")
    if settings.APP_VERSION:
        _sub(generator, "version", settings.APP_VERSION)
    _sub(generator, "datetime", exported_at.isoformat())
    return generator


def _series_by_second(series: dict | None) -> dict[int, int]:
    """One stored channel as a `{second: reading}` lookup. Absent channel -> empty."""
    if not series:
        return {}
    return dict(zip(series["t"], series["v"], strict=True))


def _nearest(seconds: list[int], second: int) -> int:
    """The depth sample closest in time to `second`; the earlier one wins a tie."""
    index = bisect.bisect_left(seconds, second)
    if index == 0:
        return seconds[0]
    if index == len(seconds):
        return seconds[-1]
    before, after = seconds[index - 1], seconds[index]
    return before if second - before <= after - second else after


def _snap_tolerance(seconds: list[int]) -> int:
    """How far a reading may be moved to reach a waypoint: half the depth channel's
    **typical** interval, taken as the median of its gaps.

    Not half of whichever two samples happen to bracket the reading, which sounds like the
    same rule and is not: the depth channel has interior holes. `suunto_xml` appends a
    sample only where `<Depth>` is non-nil, and mid-dive dropouts are a documented feature
    of the corpus, while temperature and pressure keep sampling straight through them. A
    bracket-relative bound would call a 1800 s hole "one interval" and cheerfully emit a
    temperature taken at 2000 s as the temperature at the 1200 s waypoint - 800 s away,
    presented as measured there. Which is the thing snapping exists not to do: it moves a
    reading in time, it does not invent a measurement.

    So the tolerance is a property of the channel rather than of the neighbourhood, and
    readings that cannot reach a waypoint within it - across a dropout, or beyond either
    end of the dive - are dropped. They are in `logbook.divejson`, on their own unsnapped axis,
    like everything else this format cannot carry honestly.

    A 1 Hz depth channel yields a tolerance of zero, which is exact rather than strict:
    `dive_profiles` stores whole-second timestamps, so a reading either coincides with a
    depth sample or sits in a genuine dropout. Sub-second storage would turn that into
    silent data loss, and would be the thing to revisit here.
    """
    if len(seconds) < 2:
        return 0
    gaps = sorted(later - earlier for earlier, later in zip(seconds, seconds[1:], strict=False))
    # Capped, because the median is only robust while dropouts are the minority. A depth
    # channel of two usable samples half an hour apart - which `suunto_xml` will produce
    # from a file whose `<Depth>` is nil for most of the dive while temperature keeps
    # sampling - has a median gap of 1800 s and would otherwise permit a 900 s move, the
    # exact failure this bound exists to prevent. No dive computer in the corpus samples
    # depth slower than every 20 s, so half a minute is generous as an outer limit.
    return min(gaps[len(gaps) // 2] // 2, MAX_SNAP_SECONDS)


def _at_or_after(seconds: list[int], second: int) -> int | None:
    """The first depth sample not earlier than `second`; `None` past the end."""
    index = bisect.bisect_left(seconds, second)
    return None if index == len(seconds) else seconds[index]


def _snapped(readings: dict[int, int], seconds: list[int], tolerance: int) -> dict[int, int]:
    """Move each reading onto the nearest depth sample, the closest reading winning."""
    snapped: dict[int, int] = {}
    distance: dict[int, int] = {}
    for second, reading in sorted(readings.items()):
        target = _nearest(seconds, second)
        gap = abs(second - target)
        if gap > tolerance:
            continue
        if target not in snapped or gap < distance[target]:
            snapped[target] = reading
            distance[target] = gap
    return snapped


def _waypoints(
    parent: ET.Element,
    data: dict,
    *,
    mix_id_by_gas_number: dict[int, str],
    mode: str | None,
) -> None:
    """Turn the stored per-channel series into UDDF's one-waypoint-per-instant shape.

    **Every waypoint carries a `<depth>`, and the depth channel alone sets the time
    axis.** The schema permits a waypoint without one, and the honest rendering of our
    independently-sampled channels would be the union of all their timestamps - a
    temperature taken between two depth samples becoming its own depth-less waypoint. Both
    importers that matter get that wrong, in opposite and equally fatal ways: Subsurface
    silently discards every depth-less waypoint (a 706-sample temperature curve arrives as
    29), and divelogs.de reads the missing depth as **zero**, producing a stored profile
    that saws between the real depth and the surface on every other sample. A file that
    validates and that neither consumer can read is not an exit door. See `DECISIONS.md`,
    *"Every UDDF waypoint carries a depth, because the alternative broke both importers"*.

    So readings on other channels snap to the nearest depth sample - the closest reading
    wins where several land on one waypoint, and the earlier sample wins a tie. The
    reading itself is never altered and no depth is ever invented; only the timestamp
    moves, and never by more than `_snap_tolerance`, which is half the channel's typical
    interval. Anything that cannot reach a waypoint within that - a reading inside a
    dropout in the depth channel, or one taken after the diver surfaced - is dropped
    rather than relocated onto a waypoint it was not measured anywhere near.

    Markers snap the same way, joined rather than dropped where several land together,
    since `waypointType` has room for one `<setmarker>`. **Gas switches do not**: a switch
    is a state change, so it is exempt from the tolerance and lands on the first waypoint
    at or after it however far that is - dropping one would not leave a hole, it would
    tell an importer the diver never switched. The body below says why in full.

    A profile with no depth channel therefore emits **no `<samples>` at all** rather than
    the depth-less waypoints that started this. Everything at full resolution, on its own
    unsnapped time axis, is in `logbook.divejson`.

    **`mode` is the recording's and rides on the first waypoint**, which is where UDDF puts
    a `<divemode>` and why it is a parameter here rather than something `_dive_element`
    could write beside `<greatestdepth>`. A recording whose depth channel is missing
    therefore loses its mode along with its samples - there is no waypoint to carry it, and
    `informationbeforedive` has no slot of its own.
    """
    depth = _series_by_second(data.get("depth"))
    if not depth:
        return
    seconds = sorted(depth)

    tolerance = _snap_tolerance(seconds)
    temperature = _snapped(_series_by_second(data.get("temperature")), seconds, tolerance)
    # The four deco readouts UDDF has a `<waypoint>` child for, snapped onto the depth axis
    # exactly like temperature: they are readings the device computed at an instant, and the
    # rule for reaching a waypoint honestly is the channel's, not the quantity's.
    ndl = _snapped(_series_by_second(data.get("ndl")), seconds, tolerance)
    ppo2 = _snapped(_series_by_second(data.get("ppo2")), seconds, tolerance)
    cns = _snapped(_series_by_second(data.get("cns")), seconds, tolerance)
    gradient_factor = _snapped(_series_by_second(data.get("gradient_factor")), seconds, tolerance)
    divemode = _divemode_type(mode)
    pressure: list[tuple[str, dict[int, int]]] = []
    for cylinder in data.get("pressure") or []:
        mix_id = mix_id_by_gas_number.get(cylinder["gas_number"])
        if mix_id is None:
            # `<tankpressure ref>` is an `xs:IDREF`: without a mix to point at, the
            # reading has nowhere valid to go. It survives in `logbook.divejson`, which keeps
            # the gas number itself.
            continue
        readings = dict(zip(cylinder["t"], cylinder["v"], strict=True))
        pressure.append((mix_id, _snapped(readings, seconds, tolerance)))

    # A gas switch is a state change, not a reading, and that changes both rules it obeys.
    #
    # It is never *dropped* for being out of tolerance. A missing temperature leaves a
    # hole; a missing switch tells every importer the diver stayed on the previous gas for
    # the rest of the dive - wrong data rather than absent data, and the same failure the
    # last-wins rule below exists to prevent. So it lands on the first waypoint at or
    # after it happened however far that is, which also means it is never shown *earlier*
    # than it happened: the interval in between is attributed to the old gas, which is the
    # conservative direction and the one an importer recomputing deco can live with.
    # `<divetime>` still tells a careful reader where the switch really fell. Past the end
    # of the profile there is no such waypoint and nothing left to be wrong about.
    #
    # Where two land on one waypoint the last wins, because `<switchmix>` is
    # `maxOccurs="1"` and the diver is breathing the later gas for everything that
    # follows. The winner is picked before representability is considered - resolving
    # first would let an unrepresentable later switch hand the waypoint back to the gas
    # just left behind, which is the same failure by a quieter route.
    switch_event_at: dict[int, dict] = {}
    markers_at: dict[int, list[str]] = {}
    for event in sorted(data.get("events") or [], key=lambda event: event["t"]):
        if event["type"] == ProfileEventType.GAS_SWITCH:
            after = _at_or_after(seconds, event["t"])
            if after is not None:
                switch_event_at[after] = event
            continue
        # Markers are annotations: one that cannot reach a waypoint honestly is dropped
        # like any other reading, since nothing downstream computes on its absence.
        second = _nearest(seconds, event["t"])
        if abs(event["t"] - second) > tolerance:
            continue
        markers_at.setdefault(second, []).append(event.get("label") or event["type"])

    switch_at: dict[int, str] = {}
    for second, event in switch_event_at.items():
        gas_number = event.get("gas_number")
        # A switch the file recorded without saying what to, or to a cylinder this dive
        # has no mixture for, has no `xs:IDREF` to point at, so the waypoint gets no
        # `<switchmix>` at all. It stays in `logbook.divejson`, which carries the raw events.
        if gas_number is not None and gas_number in mix_id_by_gas_number:
            switch_at[second] = mix_id_by_gas_number[gas_number]

    samples = _sub(parent, "samples")
    for second in seconds:
        # `waypointType` is an `xs:sequence`, so these have to go in exactly this order -
        # which is the type's own order and not a preference. `<cns>` comes third in it and
        # therefore first in a waypoint carrying no alarm or battery reading, and
        # `<nodecotime>` is last of all, several elements after the `<depth>` it was
        # computed at. The XSD validation test is what holds this.
        waypoint = _sub(samples, "waypoint")
        if second in cns:
            # Percent, from our tenths of a percent.
            _sub(waypoint, "cns", _num(cns[second] / CNS_SCALE))
        if second in ppo2:
            # Bar, from our hundredths of a bar - the second pressure in this file that is
            # not Pascal, and for the same reason `<mix><maximumpo2>` is not.
            _sub(waypoint, "calculatedpo2", _num(ppo2[second] / PPO2_SCALE))
        _sub(waypoint, "depth", _num(depth[second] / DEPTH_SCALE))
        _sub(waypoint, "divetime", _num(second))
        if second in markers_at:
            # One `<setmarker>` per waypoint is all the schema allows, so simultaneous
            # markers are joined rather than dropped.
            _sub(waypoint, "setmarker", "; ".join(markers_at[second]))
        if second in switch_at:
            _sub(waypoint, "switchmix", ref=switch_at[second])
        for mix_id, series in pressure:
            if second in series:
                _sub(waypoint, "tankpressure", _num(series[second] / PRESSURE_SCALE * PASCAL_PER_BAR), ref=mix_id)
        if second in temperature:
            _sub(waypoint, "temperature", _num(temperature[second] / TEMPERATURE_SCALE + KELVIN_OFFSET))
        if divemode is not None and second == seconds[0]:
            # Once per profile, on the first waypoint: the mode is one setting for the whole
            # recording, and UDDF's reader convention is that the first waypoint stating one
            # gives the dive its mode. Repeating it on every waypoint would be the same fact
            # written several thousand times.
            _sub(waypoint, "divemode", type=divemode)
        if second in gradient_factor:
            # The documented fraction, from our whole percent - see the module docstring.
            # `@tissue` is left off: the channel is the *leading* tissue's, and the schema
            # makes the attribute optional precisely because a file need not say which.
            _sub(
                waypoint, "gradientfactor", _num(gradient_factor[second] / GRADIENT_FACTOR_SCALE / PERCENT_PER_FRACTION)
            )
        if second in ndl:
            # Seconds in both, which is why this one has no factor and still names its scale.
            _sub(waypoint, "nodecotime", _num(ndl[second] / NDL_SCALE))


def _deepest(profile_data: dict[str, Any] | None) -> float | None:
    """The deepest sampled reading in a stored payload, in metres, or `None`.

    Reads the stored depth channel directly rather than a summary column, because the two
    would otherwise have to be fetched from different rows for the same recording - and the
    one thing `<greatestdepth>` must not do is disagree with the waypoints beside it.
    """
    depth = (profile_data or {}).get("depth")
    values = (depth or {}).get("v") or []
    return max(values) / DEPTH_SCALE if values else None


def _dive_element(
    bundle: ExportBundle,
    dive: Dive,
    *,
    mix_ids: dict[_MixKey, str],
    profile_data: dict | None,
    mode: str | None,
) -> ET.Element:
    element = ET.Element("dive", {"id": _uddf_id("dive", dive.uuid)})

    before = _sub(element, "informationbeforedive")
    # Every site, in visit order, not just the primary one: `informationbeforedive/link`
    # is `maxOccurs="unbounded"`, so a drift dive's full itinerary fits, and an importer
    # that only reads one still reads the primary site because it is first.
    for site in bundle.sites_for(dive):
        _sub(before, "link", ref=_uddf_id("site", site.uuid))
    if dive.dive_number > 0:
        # `xs:positiveInteger`. Nothing in the schema stops a dive being numbered 0, and
        # a 0 would make the whole document invalid rather than one element wrong.
        _sub(before, "divenumber", str(dive.dive_number))
    _sub(before, "datetime", combine_start_time(dive.start_time, dive.utc_offset_minutes).isoformat())
    # Between `<datetime>` and `<equipmentused>`, because `informationbeforediveType` is an
    # `xs:sequence` and that is where `altitude` sits in it. Water type has no counterpart
    # here at all - 3.2.2's `density` elements are site-level and deco-planner input, never
    # a per-dive fact - so it stays in `logbook.divejson` and `dives.csv`. See DECISIONS.md.
    _optional(before, "altitude", dive.altitude)

    gear = bundle.gear_for(dive)
    if dive.weight is not None or gear:
        used = _sub(before, "equipmentused")
        _optional(used, "leadquantity", dive.weight)
        for item in gear:
            _sub(used, "link", ref=_uddf_id("gear", item.uuid))

    trip = bundle.trip_for(dive)
    if trip is not None:
        _sub(before, "tripmembership", ref=_uddf_id("trip", trip.uuid))
    if dive.surface_pressure_bar is not None:
        _sub(before, "surfacepressure", _num(dive.surface_pressure_bar * PASCAL_PER_BAR))

    mixtures = bundle.mixtures_by_dive[dive.id]
    mix_id_by_gas_number: dict[int, str] = {}
    for mixture in mixtures:
        if mixture.gas_number is not None:
            mix_id_by_gas_number.setdefault(mixture.gas_number, mix_ids[_mix_key(mixture)])

    for mixture in mixtures:
        if mixture.start_pressure is None:
            # `tankdataType` makes `<tankpressurebegin>` mandatory, so a cylinder with no
            # recorded starting pressure cannot be a `<tankdata>` at all. Its gas is still
            # in `<gasdefinitions>`, and the cylinder itself is in `logbook.divejson`.
            continue
        tank = _sub(element, "tankdata")
        _sub(tank, "link", ref=mix_ids[_mix_key(mixture)])
        # `<tankvolume>` is `minOccurs="0"` where `<tankpressurebegin>` above is not, so a
        # cylinder whose size nobody recorded is omitted rather than skipped - the same
        # answer the mandatory element gets, in the form this optional one allows. This is
        # the shape the app now stores and the one foreign UDDF routinely emits, so it is
        # also what a round trip through this writer has to preserve.
        if mixture.volume is not None:
            _sub(tank, "tankvolume", _num(mixture.volume / LITRES_PER_CUBIC_METRE))
        _sub(tank, "tankpressurebegin", _num(mixture.start_pressure * PASCAL_PER_BAR))
        if mixture.end_pressure is not None:
            _sub(tank, "tankpressureend", _num(mixture.end_pressure * PASCAL_PER_BAR))

    if profile_data:
        _waypoints(element, profile_data, mix_id_by_gas_number=mix_id_by_gas_number, mode=mode)

    after = _sub(element, "informationafterdive")
    lowest = None if dive.bottom_temperature is None else dive.bottom_temperature + KELVIN_OFFSET
    _optional(after, "lowesttemperature", lowest)
    # `<greatestdepth>` is mandatory and our column is not, so a dive with no recorded
    # depth falls back to the profile's deepest sample and then to 0. Zero here means
    # "the log never recorded one" - the format has no way to say that.
    # From the samples this writer is about to emit - the primary recording's - rather than
    # from a stored summary. A dive with several recordings has several deepest readings and
    # `<greatestdepth>` takes one number, so it takes the one belonging to the profile in the
    # document beside it.
    sampled = _deepest(profile_data)
    greatest = dive.max_depth if dive.max_depth is not None else sampled
    _sub(after, "greatestdepth", _num(greatest if greatest is not None else 0.0))
    _optional(after, "visibility", dive.visibility)
    if dive.notes:
        _sub(_sub(after, "notes"), "para", dive.notes)
    _sub(after, "diveduration", _num(dive.duration))
    _optional(after, "averagedepth", dive.avg_depth)
    return element


async def write_uddf(db: AsyncSession, bundle: ExportBundle, *, exported_at: datetime) -> AsyncIterator[bytes]:
    """Stream the whole logbook as one UDDF document.

    Chunked per dive so that neither this generator nor its caller ever holds more than
    one dive's waypoints - see the module docstring.
    """
    mix_ids = collect_mixes(bundle)

    yield f'<?xml version="1.0" encoding="utf-8"?>\n<uddf xmlns="{UDDF_NAMESPACE}" version="{UDDF_VERSION}">\n'.encode()

    header: Iterable[ET.Element | None] = (
        _generator_element(exported_at),
        _diver_element(bundle),
        _divesite_element(bundle),
        _divetrip_element(bundle),
        _gasdefinitions_element(mix_ids),
    )
    for element in header:
        if element is not None:
            yield _serialize(element, level=1)

    if bundle.dives:
        # `profiledata` requires at least one `<repetitiongroup>` and a group at least one
        # `<dive>`, so an empty logbook omits the section entirely rather than emitting a
        # hollow one that would not validate.
        yield f'{_INDENT}<profiledata>\n{_INDENT * 2}<repetitiongroup id="rg-1">\n'.encode()
        for dive in bundle.dives:
            # **The primary recording's profile, and nothing of the others.** UDDF 3.2.2 has
            # one waypoint stream per `<dive>`, so a dive recorded by two computers has to
            # choose - and the choice is the same one the format's own reference writer
            # makes and the same one ordinal 0 means everywhere else in this app: the record
            # a reader shows by default. DiveJSON is this app's interchange of record and
            # carries all of them; UDDF is its courtesy to other programs and carries one.
            primary = next(iter(bundle.recordings_by_dive.get(dive.id, [])), None)
            profile = (
                await load_profile(db, recording_id=primary.id) if primary is not None and primary.has_profile else None
            )
            # The mode comes from the same recording as the samples, for the same reason the
            # samples come from the primary one: `<divemode>` is a waypoint child, so it can
            # only ever describe the record the document actually carries. A backup computer
            # run in gauge mode beside this one keeps its answer in `logbook.divejson`.
            element = _dive_element(
                bundle,
                dive,
                mix_ids=mix_ids,
                profile_data=profile.data if profile else None,
                mode=primary.mode if primary is not None else None,
            )
            yield _serialize(element, level=3)
        yield f"{_INDENT * 2}</repetitiongroup>\n{_INDENT}</profiledata>\n".encode()

    yield b"</uddf>\n"
