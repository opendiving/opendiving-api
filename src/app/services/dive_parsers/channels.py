"""The per-channel series contract every parser emits against.

Three formats now produce `ParsedSeries` values, and these helpers were copied between
them - `_series` verbatim, docstring included, and `_scaled_int` with enough drift that
one copy took `float` and another `float | None`. They live here so the reasoning below
is written once and so the next parser inherits it rather than re-deriving it.

Scales themselves are declared in `schemas/dive_profile.py`; the constants here are the
conversions onto those scales that more than one format needs.
"""

from decimal import ROUND_HALF_UP, Decimal

from ...schemas.dive_profile import ParsedSeries

# Depth in meters -> centimeters, and temperature in Celsius -> tenths of a degree. Every
# format so far reports both in these units, so these two are shared; a conversion only
# one format needs (Pascal or millibar to tenths of a bar) belongs in that parser.
CENTIMETERS_PER_METER = Decimal("100")
TENTHS_PER_UNIT = Decimal("10")

# For a channel whose source already reports it in the scale the format stores it in - a
# no-decompression time in seconds, a gradient factor in whole percent. Written out rather
# than passed as a bare `1` at each call site, so a reading that needs no conversion still
# goes through the same guard as one that does.
UNSCALED = Decimal("1")


def scaled_int(value: float, factor: Decimal) -> int:
    """Scale a reading into the integer units a profile is stored in.

    Via `Decimal(str(value))` rather than `round(value * factor)`. The readings arrive as
    decimal literals - from `json.loads`, from XML element text, or from `fitdecode`
    dividing a raw integer by the profile's scale factor - and multiplying those as binary
    floats puts values on the wrong side of a rounding boundary: `round(25.85 * 10)` is
    258, because the product is really 258.49999999999997. Several thousand times per
    dive. `str()` recovers the shortest decimal that round-trips, which is the number the
    device meant.

    `ROUND_HALF_UP` rather than Python's banker's rounding, so a half is always a half.
    """
    return int((Decimal(str(value)) * factor).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def scaled_int_or_none(value: float | None, factor: Decimal) -> int | None:
    """`scaled_int` for the formats whose readings are optional at the point of scaling.

    Both spellings exist on purpose. The Suunto parsers scale a value straight out of a
    lookup that may find nothing, so threading `None` through is what keeps a missing
    reading distinct from a real one. The FIT parser has already dropped the samples that
    carried no reading by this point, and calls `scaled_int` directly: an optional
    parameter there would need an `or 0` at each call site, quietly turning "no reading"
    into a reading of zero - which for depth is a real, distinct value, since a Suunto
    Ocean records 0.0 m at the surface.
    """
    return None if value is None else scaled_int(value, factor)


def ceiling_cm(value: float | None) -> int | None:
    """A deco ceiling in centimeters, or `None` where the diver owed no stop.

    **A ceiling of zero is not a ceiling**, and this is the one place that judgement is
    made so the three formats cannot disagree about the same dive. The ceiling is the depth
    a diver may not ascend above; zero means "you may surface", which is the absence of an
    obligation rather than an obligation at 0 m. Drawing it would put a flat line along the
    surface across every no-deco dive in the log.

    The corpus is what settles that this is a reading of nothing rather than a reading:
    the two Suunto exports write the *same fact* two different ways. DM5 XML writes
    `<Ceiling i:nil="true"/>` and never once writes a zero - across all 384 exports the
    1 760 non-nil readings run 3.0 m to 15.44 m - while the JSON export of the same dives
    writes `"Ceiling": 0` on every no-deco sample. Treating the JSON zero as a reading
    would give one dive a ceiling channel and its twin none, depending only on which file
    the diver happened to import.

    Distinct from `scaled_int_or_none`, which is deliberately faithful to a zero
    (`suunto_json` records a real 0.0 m depth at the surface), and from the zero-pressure
    rule on `DiveMixtureSchema`, which drops a zero because the device wrote one where it
    had measured nothing. Here the device measured, and zero is what "no ceiling" looks
    like. Negatives, which no export in the corpus produces, go the same way.
    """
    if value is None:
        return None
    scaled = scaled_int(value, CENTIMETERS_PER_METER)
    return scaled if scaled > 0 else None


def unsigned_int_or_none(value: float | None, factor: Decimal) -> int | None:
    """A reading on one of the unsigned channels, or `None` where the device meant nothing.

    **A negative is an absent-marker on every channel this covers**, and this is the one
    place that judgement is made so the formats cannot disagree about it. A no-decompression
    time, a time to surface, a partial pressure, a CNS clock and a gradient factor are none
    of them quantities that run below zero, and the devices in hand use a negative to say
    they have no figure: a Suunto Ocean writes `NoDecTime: -1` where it is showing a stop
    depth instead of a clock, and `gf99: -100` where no compartment leads. The format floors
    all six at zero for the same reason.

    `ceiling_cm`'s shape rather than a new rule, and deliberately *not* its rule: a zero
    ceiling is dropped because for that quantity zero is the absence of the thing being
    measured, while a zero here is a reading - an NDL of zero is the moment a dive stopped
    being a no-decompression dive, which is the one reading a decompression dive most needs.
    Where a zero means something else in one format, that format's own parser decides it, the
    way the Suunto JSON export's `TimeToSurface: 0` is decided against the file.

    **Nothing is clamped above.** A gradient factor runs into four figures on a real Suunto
    decompression ascent and a no-decompression time sits at the device's display maximum on
    most recreational dives; both are what the diver was shown, and a cap would be a guess
    wearing a plausible number.
    """
    if value is None:
        return None
    scaled = scaled_int(value, factor)
    return scaled if scaled >= 0 else None


def series(points: list[tuple[float, int]]) -> ParsedSeries | None:
    """Turn `(seconds, value)` pairs into a time-sorted series, or `None` if there are none.

    A stable sort keyed on the timestamp alone, so two readings that landed on the same
    instant keep the order the file listed them in.

    Sorting is the parser's job rather than the normalizer's because only a parser knows
    which timestamps belong to which sensor stream - the *union* of a Suunto Ocean
    export's sample timestamps is not monotonic, since separate streams are appended out
    of order. Grouping by channel before emitting satisfies `ParsedSeries` for free.
    """
    if not points:
        return None
    ordered = sorted(points, key=lambda point: point[0])
    return ParsedSeries(t=[t for t, _ in ordered], v=[v for _, v in ordered])
