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


class ParsedDiveResponse(ParsedDiveSchema):
    """What `POST /dive/parse` returns: the parsed dive, plus a token the client hands
    back to `PUT /dive/{uuid}/file` to attach the file it came from.

    A subclass rather than a wrapper object (`{dive: ..., file_token: ...}`) so the
    response stays flat and the frontend's existing form-filling code is unaffected.
    Parsers keep returning a bare `ParsedDiveSchema` - the token is minted by the route,
    which is the only layer that knows who is asking.
    """

    file_token: str
