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
