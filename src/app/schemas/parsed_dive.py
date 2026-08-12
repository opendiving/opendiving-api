from pydantic import BaseModel


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
    helium: float | None
    name: str | None
    oxygen: float | None
    start_pressure: float | None
    volume: float | None


class ParsedDiveSchema(BaseModel):
    avg_depth: float | None
    bottom_temperature: float | None
    dive_number: int | None
    duration: int | None
    max_depth: float | None
    start_time: str | None
    mixtures: list[DiveMixtureSchema]


class ParsedDiveResponse(ParsedDiveSchema):
    """What `POST /dive/parse` returns: the parsed dive, plus a token the client hands
    back to `PUT /dive/{uuid}/file` to attach the file it came from.

    A subclass rather than a wrapper object (`{dive: ..., file_token: ...}`) so the
    response stays flat and the frontend's existing form-filling code is unaffected.
    Parsers keep returning a bare `ParsedDiveSchema` - the token is minted by the route,
    which is the only layer that knows who is asking.
    """

    file_token: str
