"""A dive's fetched surface conditions as the API returns them and the export writes them.

Present on a dive only where a provider answered (`services.dive_conditions`). Every instant
inside is spelled at `utc_offset_minutes`, the offset the record's instant was resolved at -
which a dive whose own offset is unknown gives a client no other way to learn.
"""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field


class ConditionsPosition(BaseModel):
    """Where the provider was asked about, rounded to two decimals as the request is."""

    latitude: float
    longitude: float


class ConditionsWeather(BaseModel):
    model: str
    air_temperature: Annotated[float | None, Field(default=None, description="In degrees Celsius, 2 m above ground")]
    relative_humidity: Annotated[float | None, Field(default=None, description="In percent")]
    wind_speed: Annotated[float | None, Field(default=None, description="In metres per second, 10 m above ground")]
    wind_direction: Annotated[float | None, Field(default=None, description="Where the wind blows from, in degrees")]
    wind_gusts: Annotated[float | None, Field(default=None, description="In metres per second")]
    cloud_cover: Annotated[float | None, Field(default=None, description="In percent")]
    precipitation: Annotated[float | None, Field(default=None, description="In millimetres over the preceding hour")]
    pressure: Annotated[float | None, Field(default=None, description="Reduced to mean sea level, in hectopascals")]
    weather_code: Annotated[int | None, Field(default=None, description="The WMO 4677 code")]


class ConditionsExtreme(BaseModel):
    kind: Annotated[str, Field(description="`high` or `low`")]
    at: datetime
    height: Annotated[float, Field(description="In metres above the model's mean sea level")]


class ConditionsSea(BaseModel):
    model: str
    sea_level_height: Annotated[
        float | None,
        Field(default=None, description="In metres above the model's mean sea level, not a chart datum"),
    ]
    tide_state: Annotated[str | None, Field(default=None, description="`high`, `low`, `rising` or `falling`")]
    previous_extreme: ConditionsExtreme | None = None
    next_extreme: ConditionsExtreme | None = None
    sea_surface_temperature: Annotated[float | None, Field(default=None, description="In degrees Celsius")]
    wave_height: Annotated[float | None, Field(default=None, description="Significant wave height, in metres")]
    wave_period: Annotated[float | None, Field(default=None, description="In seconds")]
    swell_wave_height: Annotated[float | None, Field(default=None, description="In metres")]


class ConditionsFilled(BaseModel):
    """The dive's own fields the fetch wrote, each with the value it wrote, null where it wrote
    nothing. A field named here is marked as fetched until the diver replaces its value. The two
    vocabulary values are the dive's own `weather` and `waves` members."""

    air_temperature: float | None = None
    weather: str | None = None
    waves: str | None = None


class DiveConditionsRead(BaseModel):
    provider: str
    fetched_at: datetime
    at: Annotated[datetime, Field(description="The instant described, spelled at `utc_offset_minutes`")]
    utc_offset_minutes: int
    position: ConditionsPosition
    attribution: Annotated[str, Field(description="The source line the data's licence asks for, as markdown links")]
    weather: ConditionsWeather | None = None
    sea: ConditionsSea | None = None
    filled: ConditionsFilled
