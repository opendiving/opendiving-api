from pydantic import BaseModel


class DiveMixtureSchema(BaseModel):
    end_pressure: float | None
    helium: float
    name: str | None
    oxygen: float
    start_pressure: float | None
    volume: float


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
