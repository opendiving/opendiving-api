"""A dive's fetched surface conditions as the API returns them and the export writes them.

Present on a dive only where a provider answered (`services.dive_conditions`). Every instant
inside is spelled at `utc_offset_minutes`, the offset the record's instant was resolved at -
which a dive whose own offset is unknown gives a client no other way to learn.

The bounds are the columns' and the vocabularies', because the import reads this same shape back
out of a document (`services.logbook_import.planner`): an entry that would not fit a column, or
an offset no `timezone` can hold, is refused there and the dive left for the worker to answer.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

# Strictly inside a day, which is what `datetime.timezone` accepts.
_OFFSET_BOUND = 24 * 60 - 1
_Name = Annotated[str, Field(min_length=1, max_length=32)]
ExtremeKindName = Literal["high", "low"]
TideStateName = Literal["high", "low", "rising", "falling"]


class ConditionsPosition(BaseModel):
    """Where the provider was asked about, rounded to two decimals as the request is."""

    latitude: Annotated[float, Field(ge=-90, le=90)]
    longitude: Annotated[float, Field(ge=-180, le=180)]


class ConditionsWeather(BaseModel):
    model: _Name
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
    kind: ExtremeKindName
    at: datetime
    height: Annotated[float, Field(description="In metres above the model's mean sea level")]


class ConditionsSea(BaseModel):
    model: _Name
    sea_level_height: Annotated[
        float | None,
        Field(default=None, description="In metres above the model's mean sea level, not a chart datum"),
    ]
    tide_state: TideStateName | None = None
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
    weather: Annotated[str | None, Field(default=None, max_length=32)]
    waves: Annotated[str | None, Field(default=None, max_length=32)]


class DiveConditionsRead(BaseModel):
    provider: _Name
    fetched_at: datetime
    at: Annotated[datetime, Field(description="The instant described, spelled at `utc_offset_minutes`")]
    utc_offset_minutes: Annotated[int, Field(ge=-_OFFSET_BOUND, le=_OFFSET_BOUND)]
    position: ConditionsPosition
    attribution: Annotated[str, Field(description="The source line the data's licence asks for, as markdown links")]
    weather: ConditionsWeather | None = None
    sea: ConditionsSea | None = None
    filled: ConditionsFilled
