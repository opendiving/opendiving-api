from pydantic import BaseModel, field_validator

from .dive_mixture import GasRole


class DiveMixtureSchema(BaseModel):
    """One cylinder as a dive-computer export describes it.

    **Every field is nullable, and `None` means "the file did not record this"** - a
    parser reports what it read and never substitutes a plausible value for a missing
    one. That is not the same shape as `DiveMixtureCreate` (`schemas/dive_mixture.py`),
    where `oxygen`/`helium`/`volume` are required and the DB additionally enforces
    `volume > 0`: this schema describes a *file*, that one describes a dive being saved.

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
    name: str | None
    oxygen: float | None
    po2_limit: float | None
    role: GasRole | None
    start_pressure: float | None
    volume: float | None

    @field_validator("start_pressure", "end_pressure")
    @classmethod
    def _drop_unpressurized(cls, value: float | None) -> float | None:
        """A cylinder pressure of 0 bar is not a reading, whichever parser produced it.

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

        `<= 0` rather than `== 0` - a negative gauge reading is no more a fill than a zero
        - though only the zero is attested.
        """
        return None if value is not None and value <= 0 else value

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


class ParsedDiveSchema(BaseModel):
    avg_depth: float | None
    bottom_temperature: float | None
    dive_number: int | None
    duration: int | None
    max_depth: float | None
    start_time: str | None
    mixtures: list[DiveMixtureSchema]

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
        summary - so a negative in any export reached the `UPDATE` unmodified.
        """
        return None if value is not None and value < 0 else value

    @field_validator("surface_pressure_bar")
    @classmethod
    def _drop_implausible_surface_pressure(cls, value: float | None) -> float | None:
        """Outside 0.5-1.2 bar this is a unit error or an absent-marker, not a reading.

        The same band `ck_dive_surface_pressure_range` enforces, and deliberately the same
        numbers rather than a looser sanity check: the point is that no value can reach
        that column without having passed the bound the column applies. Nulled rather than
        rejected, on the `_drop_unpressurized` principle above - a file whose barometer
        reading is unusable is still a file worth storing, and the alternative is failing
        the attach of an otherwise perfectly importable export.

        Unattested in the corpus, unlike `_drop_unpressurized`: the 384 XML exports span
        1.031-1.067 bar and the 531 JSON readings 0.997-1.067, so not one of the 915 comes
        near either bound. It is here because of where the value lands, rather than because
        a file was caught writing a bad one - `store_tech_scalars` runs
        inside `store_dive_file`'s transaction, so a `CHECK` violation from a parsed number
        surfaces to the diver as `IntegrityError` -> "the file changed while this upload was
        in flight", advice that would be both wrong and unactionable: the retry it asks for
        fails identically every time.
        """
        return None if value is not None and not (0.5 <= value <= 1.2) else value


class ParsedDiveResponse(ParsedDiveSchema):
    """What `POST /dive/parse` returns: the parsed dive, plus a token the client hands
    back to `PUT /dive/{uuid}/file` to attach the file it came from.

    A subclass rather than a wrapper object (`{dive: ..., file_token: ...}`) so the
    response stays flat and the frontend's existing form-filling code is unaffected.
    Parsers keep returning a bare `ParsedDiveSchema` - the token is minted by the route,
    which is the only layer that knows who is asking.
    """

    file_token: str
