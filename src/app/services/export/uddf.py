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

**What the format cannot hold, this module does not fake.** Three cases came out of
reading the XSD, and all three are exported in `export.json`/CSV instead:

- **The deco ceiling.** The only per-waypoint slot is `<decostop>`, whose `duration`
  attribute is `use="required"` - and a ceiling sample says how deep the obligation was,
  never how long the stop should last. Emitting one would mean inventing the number that
  matters most.
- **CNS and OTU.** `informationafterdiveType` has no oxygen-exposure element at all (the
  only `<cns>`/`<otu>` in the schema are children of `<waypoint>`, and we store end-of-dive
  scalars rather than a per-sample series).
- **Gas `role` and service schedules.** No slot for either. `po2_limit`, by contrast,
  *does* map - `<mix><maximumpo2>` - which is why mixes dedupe on it below.

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

from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.utils.datetime_offset import combine_start_time
from ...models.dive import Dive
from ...models.gear_item import GearItem
from ...schemas.dive_mixture import DiveMixtureRead
from ...schemas.dive_profile import DEPTH_SCALE, PRESSURE_SCALE, TEMPERATURE_SCALE, ProfileEventType
from ...schemas.gear_item import GearType
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
    GearType.REEL: "variouspieces",
    GearType.KNIFE: "knife",
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

# `_EQUIPMENT_ELEMENT` is looked up unguarded, so a `GearType` added without a home here
# would be a `KeyError` at export time rather than a mis-categorized item - and the one
# place it would surface is a diver's download. Asserted at import, where it is a startup
# failure in CI instead.
assert set(_EQUIPMENT_ELEMENT) == set(GearType), "every GearType needs a UDDF equipment element"
assert set(_EQUIPMENT_ELEMENT.values()) <= set(_EQUIPMENT_ORDER), "equipmentType is a sequence; every tag needs a slot"


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
    """

    oxygen: float
    helium: float
    po2_limit: float | None

    @property
    def sort_key(self) -> tuple[float, float, float]:
        # `-1.0` sorts "no recorded limit" ahead of every real one (the constraint floor
        # is 0.4 bar), so the mix list is stable without special-casing `None`.
        return (self.oxygen, self.helium, -1.0 if self.po2_limit is None else self.po2_limit)


def _mix_key(mixture: DiveMixtureRead) -> _MixKey:
    return _MixKey(
        oxygen=round(mixture.oxygen, _MIX_PRECISION),
        helium=round(mixture.helium, _MIX_PRECISION),
        po2_limit=None if mixture.po2_limit is None else round(mixture.po2_limit, _MIX_PRECISION),
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


def _equipment_element(bundle: ExportBundle) -> ET.Element | None:
    """The owner's whole gear list, grouped into UDDF's typed equipment elements."""
    if not bundle.gear_items:
        return None

    by_element: dict[str, list[tuple[GearType, GearItem]]] = {}
    for item in bundle.gear_items:
        gear_type = GearType(item.type) if item.type else GearType.OTHER
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
    # in it would be a surprise. It is in `export.json`, which is the diver's own copy.
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
        if site.location:
            # `geographyType` makes `<location>` mandatory, so a site with nothing but a
            # name gets no `<geography>` at all rather than an empty one.
            _sub(_sub(element, "geography"), "location", site.location)
        if site.notes:
            _sub(_sub(element, "notes"), "para", site.notes)
    return divesite


def _divetrip_element(bundle: ExportBundle) -> ET.Element | None:
    if not bundle.trips:
        return None
    divetrip = ET.Element("divetrip")
    for trip in bundle.trips:
        element = _sub(divetrip, "trip", id=_uddf_id("trip", trip.uuid))
        _sub(element, "name", trip.name)
        # `tripType` requires at least one `<trippart>`, and a trip here has no parts -
        # so it becomes a single part standing for the whole thing, named after it.
        part = _sub(element, "trippart")
        _sub(part, "name", trip.name)
        # `dateoftrip`'s attributes are `xs:dateTime` while we store plain dates, so each
        # is widened to midnight. A one-day trip with no end date ends the day it began.
        end_date = trip.end_date or trip.start_date
        _sub(part, "dateoftrip", startdate=f"{trip.start_date}T00:00:00", enddate=f"{end_date}T00:00:00")
        if trip.location:
            _sub(_sub(part, "geography"), "location", trip.location)
        if trip.notes:
            _sub(_sub(part, "notes"), "para", trip.notes)
    return divetrip


def _gasdefinitions_element(mix_ids: dict[_MixKey, str]) -> ET.Element | None:
    if not mix_ids:
        return None
    gasdefinitions = ET.Element("gasdefinitions")
    for key, mix_id in mix_ids.items():
        mix = _sub(gasdefinitions, "mix", id=mix_id)
        _sub(mix, "name", gas_name(key.oxygen, key.helium))
        # Fractions, not percentages: UDDF's `<o2>`/`<he>` are 0-1.
        _sub(mix, "o2", _num(key.oxygen / 100.0))
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
    end of the dive - are dropped. They are in `export.json`, on their own unsnapped axis,
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
    unsnapped time axis, is in `export.json`.
    """
    depth = _series_by_second(data.get("depth"))
    if not depth:
        return
    seconds = sorted(depth)

    tolerance = _snap_tolerance(seconds)
    temperature = _snapped(_series_by_second(data.get("temperature")), seconds, tolerance)
    pressure: list[tuple[str, dict[int, int]]] = []
    for cylinder in data.get("pressure") or []:
        mix_id = mix_id_by_gas_number.get(cylinder["gas_number"])
        if mix_id is None:
            # `<tankpressure ref>` is an `xs:IDREF`: without a mix to point at, the
            # reading has nowhere valid to go. It survives in `export.json`, which keeps
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
        # `<switchmix>` at all. It stays in `export.json`, which carries the raw events.
        if gas_number is not None and gas_number in mix_id_by_gas_number:
            switch_at[second] = mix_id_by_gas_number[gas_number]

    samples = _sub(parent, "samples")
    for second in seconds:
        # `waypointType` is an `xs:sequence`, so these have to go in exactly this order.
        waypoint = _sub(samples, "waypoint")
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


def _dive_element(
    bundle: ExportBundle,
    dive: Dive,
    *,
    mix_ids: dict[_MixKey, str],
    profile_data: dict | None,
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
            # in `<gasdefinitions>`, and the cylinder itself is in `export.json`.
            continue
        tank = _sub(element, "tankdata")
        _sub(tank, "link", ref=mix_ids[_mix_key(mixture)])
        _sub(tank, "tankvolume", _num(mixture.volume / LITRES_PER_CUBIC_METRE))
        _sub(tank, "tankpressurebegin", _num(mixture.start_pressure * PASCAL_PER_BAR))
        if mixture.end_pressure is not None:
            _sub(tank, "tankpressureend", _num(mixture.end_pressure * PASCAL_PER_BAR))

    if profile_data:
        _waypoints(element, profile_data, mix_id_by_gas_number=mix_id_by_gas_number)

    after = _sub(element, "informationafterdive")
    lowest = None if dive.bottom_temperature is None else dive.bottom_temperature + KELVIN_OFFSET
    _optional(after, "lowesttemperature", lowest)
    # `<greatestdepth>` is mandatory and our column is not, so a dive with no recorded
    # depth falls back to the profile's deepest sample and then to 0. Zero here means
    # "the log never recorded one" - the format has no way to say that.
    profile_info = bundle.profile_by_dive[dive.id]
    greatest = dive.max_depth if dive.max_depth is not None else (profile_info.max_depth if profile_info else None)
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
            profile = await load_profile(db, dive_id=dive.id)
            element = _dive_element(bundle, dive, mix_ids=mix_ids, profile_data=profile.data if profile else None)
            yield _serialize(element, level=3)
        yield f"{_INDENT * 2}</repetitiongroup>\n{_INDENT}</profiledata>\n".encode()

    yield b"</uddf>\n"
