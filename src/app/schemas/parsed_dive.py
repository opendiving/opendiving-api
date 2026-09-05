import math
from typing import Self

from pydantic import BaseModel, field_validator, model_validator

from .dive import WaterType
from .dive_mixture import GasRole

# The bounds `ck_dive_entry_latitude_range` and its three siblings enforce, and the only
# bounds a coordinate has. Named here rather than written out at each guard because three
# places mirror them - these validators, `services/dive_parsers/positions.py` (which drops
# a junk fix before it can displace a real one), and the `CHECK`s themselves.
LATITUDE_LIMIT = 90.0
LONGITUDE_LIMIT = 180.0


class _ParserOutput(BaseModel):
    """Base for the shapes a parser returns, holding the one rule that applies to all of
    them: **no non-finite float leaves a parser.**

    Stated once, on every field, rather than per-field alongside the bounds below, because
    the bounds are the wrong place to catch this and the first attempt proved it. Each of
    those guards was written as a comparison - `value < 0`, `value <= 0` - and a `NaN`
    compares `False` against all of them, so it passed straight through every one-sided
    check while the two-sided ones happened to reject it as a side effect of how `and`
    falls out. A rule that holds only where someone remembered to bound a field is not the
    rule; `avg_depth`, `max_depth` and `bottom_temperature` have no bound at all and were
    the proof.

    The constraints behind those columns are not the backstop they look like either:
    `'NaN'::float8 >= 0` and `'NaN'::float8 > 0` are both **true** in Postgres, which sorts
    `NaN` above every number. Nor is anything upstream - `<CnsStart>NaN</CnsStart>` is a
    float literal to `float()`, and `json.loads` accepts a bare `NaN` token, so both Suunto
    formats can express one.

    What it costs is the whole list, not the row. `DiveTechScalars` rides on `DiveRead`
    (see its docstring for why), and `JSONResponse` serializes with `allow_nan=False` - so
    one stored `NaN` turns `GET /dives` into a 500 that only hand-written SQL clears. The
    attach path is reached only behind a `POST /dive/parse` that would fail to serialize
    first, but `backfill_tech_fields` re-parses stored files and writes through a Core
    `UPDATE` with no serialization in between, so a route-level guard would not have
    covered it.

    `isfinite` rather than an `isnan` check: `inf` passes `>= 0` honestly and is no more a
    reading than `NaN` is. Nulled rather than rejected, on the `_drop_unpressurized`
    principle - a file with one unusable number is still a file worth storing.
    """

    @field_validator("*")
    @classmethod
    def _drop_non_finite(cls, value: object) -> object:
        # Typed as `object` because this runs for every field, including the `str`, `int`,
        # enum and `list` ones it deliberately passes through untouched.
        return None if isinstance(value, float) and not math.isfinite(value) else value


class DiveMixtureSchema(_ParserOutput):
    """One cylinder as a dive-computer export describes it.

    **Every field is nullable, and `None` means "the file did not record this"** - a
    parser reports what it read and never substitutes a plausible value for a missing
    one. This schema describes a *file*; `DiveMixtureCreate` (`schemas/dive_mixture.py`)
    describes a dive being saved, and that is still the difference between them even
    though the two shapes have converged: `oxygen`, `helium` and `volume` used to be
    required over there, so a parsed absence had nowhere to go and the form filled it in.
    Their columns are nullable now and mean the same thing on both sides, which is what
    lets an absence travel all the way to the row instead of being defaulted at the door
    (see *A cylinder may record a mix without a vessel* in `DECISIONS.md`). What has not
    changed is who may guess: a parser still never does, and a *form* still may.

    The distinction is load-bearing rather than pedantic. These formats routinely omit
    gas data - a FIT file has nowhere to record cylinder size at all, and the 2026 Suunto
    Ocean JSON export records no gas fraction anywhere - and the parsers used to fill the
    gap with `0.0`, which is indistinguishable from a reading. A 0 % oxygen mix is a
    hypoxic gas nobody dives, and a 0 L cylinder violates a DB constraint, so both were
    obviously-wrong values presented as data; worse, the parsed `volume: 0.0` overwrote
    the dive form's own sensible 11.1 L default. Nulling them instead lets the form apply
    `DEFAULT_MIXTURE` exactly as it does for a manually added cylinder, and leaves the
    guess visible to any caller that wants to say "this wasn't in your file".
    """

    end_pressure: float | None
    gas_number: int | None
    helium: float | None
    oxygen: float | None
    po2_limit: float | None
    role: GasRole | None
    start_pressure: float | None
    volume: float | None

    @field_validator("start_pressure", "end_pressure")
    @classmethod
    def _drop_unpressurized(cls, value: float | None) -> float | None:
        """Outside `(0, 350]` bar this is not a cylinder pressure, whichever parser
        produced it.

        This is **not** the "treat zero as missing" rule DECISIONS.md rejects, and the
        two are worth holding apart. A gas fraction of 0 is inside the range the
        quantity takes on a real dive - a nitrox mix genuinely contains 0 % helium, and
        `TestParsersInventNothing` pins that it survives. A start pressure of 0 is not: a
        cylinder at 0 bar delivers no gas, so it is not a fill anyone dived.

        DM5's XML doesn't mean it as one either. 255 of the 353 mixtures in the 384-file
        XML corpus record exactly `<StartPressure>0</StartPressure><EndPressure>0</...>`,
        and all 255 are precisely the ones whose `<TransmitterId>` is `xsi:nil` - 353 of
        353 agree, and not one mixture in the corpus pairs a zero with a real pressure.
        The same dive exported as JSON settles what that means: `d5-last-header.json` and
        `Dive_2025-06-03-1215.xml` are one dive (same timestamp, same transmitter serial,
        same 22 L/11 L and 21 %/49 % cylinders), and where the XML writes `0` for the
        untransmitted 49 % bottle, the JSON simply omits `StartPressure`/`EndPressure` -
        while keeping `Helium: 0` in the same object. One format's absent-marker is the
        other's absent key, and a parser that read the zero literally made the two
        exports of one dive disagree.

        Enforced here rather than in `SuuntoXmlParser`, though that is where the evidence
        is, because the fact is about the field and not the format: no export can express
        a cylinder that was breathed from 0 bar, so no parser should claim one. It is the
        same judgement `_mixtures_from_cylinders` already makes for a `null` Ocean
        reading, and putting it on the schema means a fourth parser inherits it.

        The lower clause excludes everything `<= 0` rather than just `== 0` - a negative
        gauge reading is no more a fill than a zero - though only the zero is attested.
        `NaN` is not this validator's to catch, and deliberately so:
        `_ParserOutput._drop_non_finite` has already run it out.

        **The upper clause is the parse-side half of a bounded column**, on the same terms
        as `_drop_implausible_po2_limit` below and `_drop_implausible_surface_pressure`:
        no parsed value should reach a bounded column without having passed the bound the
        column applies, and `ck_dive_mixture_start_pressure_range` /
        `ck_dive_mixture_end_pressure_range` now band both fields at 350 bar. That bound
        is attested from this exact direction - the DM5 XML parser read millibar as bar
        and stored `start_pressure = 205203` (see DECISIONS.md) - so without this clause a
        recurrence would hand `/dive/parse` a 205203, prefill the form with it, and 422 on
        Save: a field the diver never chose, which is the failure the other two are
        written up for. 350 rather than a rounder number because it clears a 300 bar DIN
        fill, the highest real one, and rejects everything above it.

        The band is deliberately the same on both fields even though the request layer's
        floors differ (`gt=0` for start, `ge=0` for end): a parser has no diver asserting
        anything, and a 0 from a file is an absent-marker in either column.
        """
        return None if value is not None and not (0 < value <= 350) else value

    @field_validator("gas_number")
    @classmethod
    def _drop_negative_gas_number(cls, value: int | None) -> int | None:
        """A label, but not a negative one - `ck_dive_mixture_gas_number_non_negative`.

        `< 0`, not `<= 0`: **0 is a real label**, which is the whole point of that
        constraint being `>= 0` rather than the 1-based check it started as - a Suunto
        Ocean numbers its cylinders from 0 and the stored profiles label their pressure
        channels to match.

        Only one parser can produce a number a file chose: `_mixtures_from_cylinders`
        reads `int(cylinder["GasNumber"])` out of the Ocean's sample data. The other three
        paths synthesize it with `enumerate`, so they cannot go negative by construction.
        That one path is enough - a negative label reaches `/dive/parse`, pre-fills the
        form, and `DiveMixtureCreate`'s `ge=0` then 422s a field the diver never chose and
        cannot see, which is exactly the failure `_drop_implausible_po2_limit` below is
        written up for.
        """
        return None if value is not None and value < 0 else value

    @field_validator("po2_limit")
    @classmethod
    def _drop_implausible_po2_limit(cls, value: float | None) -> float | None:
        """Outside 0.4-2.0 bar this is not a ppO₂ anyone planned a gas to.

        The band `ck_dive_mixture_po2_limit_range` enforces, mirrored here on the same
        terms as `ParsedDiveSchema._drop_implausible_surface_pressure`: no parsed value
        should reach a bounded column without having passed the bound the column applies.
        `backfill_tech_fields` writes this one through a Core `UPDATE` that bypasses
        Pydantic entirely, so the schema is the only place the guard can sit and still
        cover both paths.

        Unattested, and the *format* trap `_drop_unpressurized` documents does not apply
        here: DM5 says "no ppO₂ recorded" with `<PO2 i:nil="true"/>` (363 of 716 mixtures)
        rather than with a zero, and across the whole corpus the three parsers produce 371
        `po2_limit` values of which every one is 1.4 or 1.6. This is the unattested half
        of the same rule - a limit of 0 bar is not a limit, the way 0 bar is not a fill.
        """
        return None if value is not None and not (0.4 <= value <= 2.0) else value


class ParsedDiveSchema(_ParserOutput):
    avg_depth: float | None
    bottom_temperature: float | None
    dive_number: int | None
    duration: int | None
    max_depth: float | None
    start_time: str | None
    mixtures: list[DiveMixtureSchema]

    # A form-prefill field like everything above it, not one of the server-side ones
    # below: a FIT file's `dive_settings.water_type` is a *starting point* the diver can
    # correct, and it reaches the dive through the ordinary `DiveCreateRequest` the
    # prefilled form submits, never through a server-side write.
    #
    # **The `= None` default is deliberate, unlike its undefaulted neighbours above.**
    # Both Suunto parsers construct this schema with explicit keyword arguments and
    # neither format carries salinity, so matching the neighbouring style here would make
    # every Suunto parse a `ValidationError`. The default is what makes "nothing to do
    # for Suunto" true. No validator: the enum type is the guard, and the column has no
    # `CHECK` to mirror.
    water_type: WaterType | None = None

    # Oxygen exposure and surface pressure, on the same all-nullable terms as everything
    # above. These have no place on the dive *form* - they are the device's own
    # accounting and a diver has no way to know them - so unlike the fields above they
    # are written server-side at file attach rather than pre-filling anything. They ride
    # on this schema anyway because `/dive/parse` returns it, which makes them visible in
    # the import preview for free.
    cns_start: float | None = None
    cns_end: float | None = None
    otu_start: float | None = None
    otu_end: float | None = None
    surface_pressure_bar: float | None = None

    # Where the diver got in and where they got out, in decimal degrees, on the same
    # server-side-only terms as the exposure readings above: a file records these, a form
    # does not offer them. See `services/dive_parsers/positions.py` for which fix becomes
    # which - and for why an entry position is routinely absent while an exit one is not.
    entry_latitude: float | None = None
    entry_longitude: float | None = None
    exit_latitude: float | None = None
    exit_longitude: float | None = None

    @field_validator("avg_depth", "max_depth")
    @classmethod
    def _drop_non_positive_depth(cls, value: float | None) -> float | None:
        """A dive to 0 m is not a dive - `ck_dive_max_depth_positive` and its `avg` twin.

        `<= 0`, not `< 0`, and the contrast with `_drop_negative_exposure` right below is
        the whole point: those four constraints are `>= 0` because a dive that began with
        no oxygen loading records a real 0, while these two are `> 0` because no depth
        reading of 0 describes a dive that happened. The model's own comments say so, and
        this mirrors each one on its own terms rather than picking one rule for "depth-ish
        numbers".

        These are the last two bounded columns a parsed value could reach unguarded, and
        they are older than the phase that guarded the rest - which is why they were
        missed. They land like `po2_limit` and `gas_number` rather than like the exposure
        readings: nothing writes them server-side, so the failure is on the form. A 0 from
        a file pre-fills the dive form via `POST /dive/parse`, and the save then dies on
        the `CHECK` - `DiveCreate` carries no bound of its own to catch it earlier - over
        a field the diver never chose and, for `avg_depth`, cannot see.

        Unattested: all 384 XML exports and 531 JSON readings in the corpus record positive
        depths, and a FIT `max_depth` is a `uint32` of millimetres. Here because of where
        the value lands, on the same terms as `_drop_implausible_surface_pressure`.
        """
        return None if value is not None and value <= 0 else value

    @field_validator("cns_start", "cns_end", "otu_start", "otu_end")
    @classmethod
    def _drop_negative_exposure(cls, value: float | None) -> float | None:
        """Oxygen loading does not run backwards, and `ck_dive_*_non_negative` says so.

        `< 0`, not `<= 0`: a dive that began with **no** oxygen loading records a real 0,
        and telling that apart from "didn't record it" is exactly why those four
        constraints are `>= 0` rather than `> 0`. `test_zero_cns_and_otu_are_allowed`
        pins the boundary from the database's side.

        All three parsers pass these through raw - XML `_float(root, "CnsStart")`, JSON
        `_fraction_to_percent(start_tissue.get("CNS"))`, FIT `float(value)` off the
        summary - so a negative in any export reached the `UPDATE` unmodified. `NaN` is
        `_ParserOutput._drop_non_finite`'s job, and reading that docstring is the point:
        this guard was written as `< 0` alone and did not catch one.
        """
        return None if value is not None and value < 0 else value

    @field_validator("surface_pressure_bar")
    @classmethod
    def _drop_implausible_surface_pressure(cls, value: float | None) -> float | None:
        """Outside 0.4-1.2 bar this is a unit error or an absent-marker, not a reading.

        The same band `ck_dive_surface_pressure_range` enforces, and deliberately the same
        numbers rather than a looser sanity check: the point is that no value can reach
        that column without having passed the bound the column applies. Nulled rather than
        rejected, on the `_drop_unpressurized` principle above - a file whose barometer
        reading is unusable is still a file worth storing, and the alternative is failing
        the attach of an otherwise perfectly importable export.

        The floor moved from 0.5 to 0.4 with the constraint, for the reason recorded there:
        ambient pressure at `ck_dive_altitude_range`'s own 6500 m ceiling is about 0.44 bar,
        so the old floor refused a reading the altitude bound admits.

        Unattested in the corpus, unlike `_drop_unpressurized`: the 384 XML exports span
        1.031-1.067 bar and the 531 JSON readings 0.997-1.067, so not one of the 915 comes
        near either bound. It is here because of where the value lands, rather than because
        a file was caught writing a bad one - `store_tech_scalars` runs
        inside `store_dive_file`'s transaction, so a `CHECK` violation from a parsed number
        surfaces to the diver as `IntegrityError` -> "the file changed while this upload was
        in flight", advice that would be both wrong and unactionable: the retry it asks for
        fails identically every time.
        """
        return None if value is not None and not (0.4 <= value <= 1.2) else value

    @field_validator("entry_latitude", "exit_latitude")
    @classmethod
    def _drop_impossible_latitude(cls, value: float | None) -> float | None:
        """Past the poles this is not a latitude - `ck_dive_entry_latitude_range` and its
        exit twin, mirrored on the same terms as every bounded column above."""
        return None if value is not None and abs(value) > LATITUDE_LIMIT else value

    @field_validator("entry_longitude", "exit_longitude")
    @classmethod
    def _drop_impossible_longitude(cls, value: float | None) -> float | None:
        """Past the antimeridian this is not a longitude - `ck_dive_entry_longitude_range`
        and its exit twin.

        Worth stating even though `positions.py` has already filtered the fixes it built
        these from: this schema is also what `backfill_tech_fields` writes through, and
        that path reaches the columns via a Core `UPDATE` with no Pydantic after it.
        """
        return None if value is not None and abs(value) > LONGITUDE_LIMIT else value

    @model_validator(mode="after")
    def _drop_half_positions(self) -> Self:
        """Half a position is not a position, and neither is Null Island.

        The same rule `WholeCoordinatePair` applies to a dive site's coordinates, in the
        one place it can be applied here: these fields are never named by a caller, so
        there is no request body to check and nothing to reject - a parser hands over what
        it read, and the wrong halves are dropped rather than 422'd.

        Both conditions are reachable *only* through this schema's own field validators,
        which is why this runs after them rather than instead of them. `positions.py`
        emits a pair or nothing; `_drop_non_finite` nulling a `NaN` latitude, or
        `_drop_impossible_longitude` nulling a longitude of 400, is what leaves a lone
        ordinate behind - and a lone ordinate written to the column is a dive pinned to
        the equator or the prime meridian, which is a claim the file never made. It also
        violates `ck_dive_entry_position_pair`, so the alternative to dropping it is an
        `IntegrityError` on an otherwise importable file.

        Exactly `0.0, 0.0` goes the same way. See `geo_fix`, and *"What divelogs.de does
        with our UDDF"* in DECISIONS.md, where Null Island was first written up.
        """
        for latitude, longitude in (("entry_latitude", "entry_longitude"), ("exit_latitude", "exit_longitude")):
            values = (getattr(self, latitude), getattr(self, longitude))
            if (values[0] is None) != (values[1] is None) or values == (0.0, 0.0):
                setattr(self, latitude, None)
                setattr(self, longitude, None)
        return self


class ParsedDiveResponse(ParsedDiveSchema):
    """What `POST /dive/parse` returns: the parsed dive, plus a token the client hands
    back to `PUT /dive/{uuid}/file` to attach the file it came from.

    A subclass rather than a wrapper object (`{dive: ..., file_token: ...}`) so the
    response stays flat and the frontend's existing form-filling code is unaffected.
    Parsers keep returning a bare `ParsedDiveSchema` - the token is minted by the route,
    which is the only layer that knows who is asking.
    """

    file_token: str
