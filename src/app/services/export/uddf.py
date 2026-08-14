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


def _num(value: float) -> str:
    """Format a float for an `xs:float` element.

    Fixed notation with the trailing zeros trimmed, rather than `%g`: a tank pressure in
    Pascal is eight digits, and `%g` would render 20 520 000 as `2.052e+07`. Legal, but
    the first thing a human checking an export looks at is whether the numbers look like
    numbers.
    """
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-", "-0") else text


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    element = ET.SubElement(parent, tag, attrs)
    if text is not None:
        element.text = text
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

    Sorted by the fractions rather than by first encounter, so re-exporting a log after
    deleting its oldest dive doesn't renumber every mix in the file.
    """
    keys = {_mix_key(mixture) for mixtures in bundle.mixtures_by_dive.values() for mixture in mixtures}
    return {key: f"mix-{index}" for index, key in enumerate(sorted(keys, key=lambda k: k.sort_key), start=1)}


def _uddf_id(prefix: str, uuid: uuid_pkg.UUID) -> str:
    """An `xs:ID` from a public uuid.

    Prefixed because `xs:ID` is an `NCName` and cannot start with a digit, which a hex
    uuid regularly does. The prefix doubles as a readable hint at what a `ref` points to.
    """
    return f"{prefix}-{uuid}"


def _equipment_element(bundle: ExportBundle, manufacturer_ids: dict[str, str]) -> ET.Element | None:
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
                manufacturer = _sub(piece, "manufacturer", id=manufacturer_ids[item.brand])
                _sub(manufacturer, "name", item.brand)
            if item.notes:
                _sub(_sub(piece, "notes"), "para", item.notes)
            if tag == "suit" and gear_type in _SUIT_TYPE:
                _sub(piece, "suittype", _SUIT_TYPE[gear_type])
    return equipment


def _manufacturer_ids(bundle: ExportBundle) -> dict[str, str]:
    """One `xs:ID` per distinct brand. `manufacturerType` extends `namedType`, so every
    `<manufacturer>` needs an id whether anything references it or not."""
    brands = sorted({item.brand for item in bundle.gear_items if item.brand})
    return {brand: f"mfr-{index}" for index, brand in enumerate(brands, start=1)}


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
    equipment = _equipment_element(bundle, _manufacturer_ids(bundle))
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


def _waypoints(
    parent: ET.Element,
    data: dict,
    *,
    mix_id_by_gas_number: dict[int, str],
) -> None:
    """Turn the stored per-channel series into UDDF's one-waypoint-per-instant shape.

    Our channels are sampled independently - a device logs depth every second and
    temperature every twenty - so there is no shared time axis to walk. The waypoints are
    the **union** of every channel's timestamps, each carrying only the readings actually
    taken at that instant. Nothing is interpolated onto a neighbouring waypoint: a
    waypoint with a temperature and no depth is the honest rendering of a temperature
    sample taken between two depth samples.
    """
    depth = _series_by_second(data.get("depth"))
    temperature = _series_by_second(data.get("temperature"))
    pressure: list[tuple[str, dict[int, int]]] = []
    for cylinder in data.get("pressure") or []:
        mix_id = mix_id_by_gas_number.get(cylinder["gas_number"])
        if mix_id is None:
            # `<tankpressure ref>` is an `xs:IDREF`: without a mix to point at, the
            # reading has nowhere valid to go. It survives in `export.json`, which keeps
            # the gas number itself.
            continue
        pressure.append((mix_id, dict(zip(cylinder["t"], cylinder["v"], strict=True))))

    switch_at: dict[int, str] = {}
    markers_at: dict[int, list[str]] = {}
    for event in data.get("events") or []:
        second = event["t"]
        if event["type"] == ProfileEventType.GAS_SWITCH:
            gas_number = event.get("gas_number")
            # A switch the file recorded without saying what to, or to a cylinder this
            # dive has no mixture for, has no `xs:IDREF` to point at. It stays in
            # `export.json`, which carries the raw event list.
            if gas_number is not None and gas_number in mix_id_by_gas_number:
                switch_at.setdefault(second, mix_id_by_gas_number[gas_number])
            continue
        markers_at.setdefault(second, []).append(event.get("label") or event["type"])

    seconds = sorted(
        set(depth) | set(temperature) | set(switch_at) | set(markers_at) | {t for _, series in pressure for t in series}
    )
    if not seconds:
        return

    samples = _sub(parent, "samples")
    for second in seconds:
        # `waypointType` is an `xs:sequence`, so these have to go in exactly this order.
        waypoint = _sub(samples, "waypoint")
        if second in depth:
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
