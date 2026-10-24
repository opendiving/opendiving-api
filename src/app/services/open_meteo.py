"""Open-Meteo's Historical Weather and Marine APIs, as `services.dive_conditions` asks them.

Two requests, both with `timeformat=unixtime` so every sample is an instant rather than a
label: `timezone=auto` labels every hour of every date with the zone's offset *at request
time*, so a January day comes back wearing the summer offset. The zone's *name* is the one
thing read from that answer, and only for a dive whose own offset is unknown.

What the caller gets back is one of three things, and keeping them apart is this module's
job. `Answered` is the provider's body - cached, since another dive at the same place and
day asks the same question. `Refused` is a request the provider would not answer, which is
about the dive and is charged to it. `CouldNotAsk` is everything that says nothing about
the dive - over budget, a rate limit, a key refused, a timeout, a 5xx, a transport failure -
and is never cached and never charged to a dive.

The coordinate is rounded by the caller (`services.dive_conditions`) before it gets here, and
is the cache key's as well as the request's: every dive inside one ~1 km square shares an
answer, and the provider learns no finer position than that.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from ..core.config import settings
from ..core.utils.outbound import Failed, FetchFailure, claim_provider_slot, fetch_capped, read_cached, write_cached

logger = logging.getLogger(__name__)

PROVIDER = "open-meteo"
# What answers both requests: the APIs' default, which blends IFS and ERA5 for weather and
# picks MeteoFrance's models for the sea. Recorded so a later bundled tide model can say it
# answered instead.
MODEL = "best_match"

ATTRIBUTION = (
    "[Weather data by Open-Meteo.com](https://open-meteo.com/) "
    "([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)), "
    "with weather, waves and tide times derived from it"
)

_FREE_HOSTS = {"weather": "https://archive-api.open-meteo.com", "sea": "https://marine-api.open-meteo.com"}
_CUSTOMER_HOSTS = {
    "weather": "https://customer-archive-api.open-meteo.com",
    "sea": "https://customer-marine-api.open-meteo.com",
}
_PATHS = {"weather": "/v1/archive", "sea": "/v1/marine"}

# Nine and five: under the ten variables a call may carry before the provider charges it as
# several, as the request's two and three days are under its two weeks.
WEATHER_VARIABLES = (
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "cloud_cover",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "pressure_msl",
    "weather_code",
)
SEA_VARIABLES = (
    "sea_level_height_msl",
    "sea_surface_temperature",
    "wave_height",
    "wave_period",
    "swell_wave_height",
)

# Measured against Open-Meteo itself, not copied: a warm archive or marine call answers in
# 0.3 to 1 s, and a cold archive call for a place and decade nobody had asked about took 31 s
# to its first byte. The read timeout admits that cold call; the deadline is the bound that
# holds. See *"The conditions client's timeouts"* in `DECISIONS.md`.
TIMEOUT_SECONDS = 35.0
DEADLINE_SECONDS = 40.0
# The dive form's preview (`GET /dive-conditions/lookup`), which a diver is waiting on: five
# times the slowest warm answer, and two calls inside about ten seconds. A cold archive call is
# not waited for - the preview is a 503 the form ignores, and the worker's fetch after the
# save writes the same values.
PREVIEW_TIMEOUT_SECONDS = 4.0
PREVIEW_DEADLINE_SECONDS = 5.0
# An answer is a few kilobytes: nine variables over two days, five over three.
_MAX_RESPONSE_BYTES = 256 * 1024

# An hour whatever the day's age. The cache lets an import's burst and a trip's dives share one
# answer, which an hour covers; longer would spend memory a small shared Redis does not have.
_ANSWER_TTL_SECONDS = 60 * 60
_CACHE_VERSION = "v1"

_BUDGET_KEY = f"conditions:provider:{PROVIDER}"


class CouldNotAsk(StrEnum):
    """Why nothing was asked or nothing came back, none of it the dive's doing."""

    BUDGET = "budget"
    RATE_LIMITED = "rate_limited"
    KEY_REFUSED = "key_refused"
    TIMEOUT = "timeout"
    SERVER = "server"
    TRANSPORT = "transport"


@dataclass(frozen=True, slots=True)
class Answered:
    """The parts of a body the derivations read: the zone the provider resolved for the
    coordinate, and the hourly series keyed by variable, `time` among them."""

    timezone: str | None
    hourly: dict[str, list[Any]]


@dataclass(frozen=True, slots=True)
class Refused:
    """The provider would not answer this request: a 4xx naming it, which is about the dive."""

    status: int


type Outcome = Answered | Refused | CouldNotAsk


def _cache_key(kind: str, latitude: float, longitude: float, start: date, end: date, timezone: str | None) -> str:
    variables = WEATHER_VARIABLES if kind == "weather" else SEA_VARIABLES
    digest = hashlib.sha256(",".join(variables).encode()).hexdigest()[:8]
    return (
        f"conditions:{_CACHE_VERSION}:{kind}:{latitude:.2f}:{longitude:.2f}:"
        f"{start.isoformat()}:{end.isoformat()}:{timezone or 'GMT'}:{digest}"
    )


def _parse(raw: Any) -> Answered:
    """An answer as cached or as received, refusing anything without the hourly series."""
    if not isinstance(raw, dict):
        raise ValueError("not an object")
    hourly = raw.get("hourly")
    zone = raw.get("timezone")
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        raise ValueError("no hourly series")
    if zone is not None and not isinstance(zone, str):
        raise ValueError("timezone is not a name")
    return Answered(timezone=zone, hourly=hourly)


def _classify(failed: Failed) -> Refused | CouldNotAsk:
    if failed.reason is FetchFailure.STATUS and failed.status is not None:
        if failed.status == 429:
            return CouldNotAsk.RATE_LIMITED
        if failed.status in (401, 403):
            return CouldNotAsk.KEY_REFUSED
        # A 400 is how Open-Meteo refuses a request it reads as malformed - a date out of
        # range, a coordinate it will not take - which is the dive's inputs, not the instance.
        if failed.status in (400, 422):
            return Refused(failed.status)
        return CouldNotAsk.SERVER
    if failed.reason is FetchFailure.DEADLINE:
        return CouldNotAsk.TIMEOUT
    if failed.reason is FetchFailure.TRANSPORT:
        return CouldNotAsk.TIMEOUT if failed.error and "Timeout" in failed.error else CouldNotAsk.TRANSPORT
    return CouldNotAsk.SERVER


async def _ask(
    kind: str,
    latitude: float,
    longitude: float,
    start: date,
    end: date,
    *,
    extra: dict[str, Any],
    timezone: str | None,
    timeout: float,
    deadline: float,
) -> Outcome:
    key = _cache_key(kind, latitude, longitude, start, end, timezone)
    cached = await read_cached(key, _parse)
    if cached is not None:
        return cached

    if not await claim_provider_slot(
        _BUDGET_KEY,
        settings.DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_REQUESTS,
        settings.DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_WINDOW_SECONDS,
    ):
        return CouldNotAsk.BUDGET

    params: dict[str, Any] = {
        "latitude": f"{latitude:.2f}",
        "longitude": f"{longitude:.2f}",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": ",".join(WEATHER_VARIABLES if kind == "weather" else SEA_VARIABLES),
        "timeformat": "unixtime",
        **extra,
    }
    if timezone is not None:
        params["timezone"] = timezone
    api_key = settings.DIVE_CONDITIONS_OPEN_METEO_API_KEY
    hosts = _FREE_HOSTS
    if api_key is not None and api_key.get_secret_value():
        params["apikey"] = api_key.get_secret_value()
        hosts = _CUSTOMER_HOSTS

    fetched = await fetch_capped(
        f"{hosts[kind]}{_PATHS[kind]}",
        params=params,
        timeout=timeout,
        deadline=deadline,
        max_bytes=_MAX_RESPONSE_BYTES,
    )
    if isinstance(fetched, Failed):
        # The provider and the failure only: the URL carries the key and the coordinate.
        logger.warning("An %s %s request %s.", PROVIDER, kind, fetched)
        return _classify(fetched)

    try:
        answered = _parse(json.loads(fetched.body))
    except ValueError:
        logger.warning("An %s %s answer was not the shape it documents.", PROVIDER, kind)
        return CouldNotAsk.SERVER

    await write_cached(
        key,
        json.dumps({"timezone": answered.timezone, "hourly": answered.hourly}),
        ttl_seconds=_ANSWER_TTL_SECONDS,
    )
    return answered


async def fetch_weather(
    latitude: float,
    longitude: float,
    start: date,
    end: date,
    *,
    auto_zone: bool,
    timeout: float = TIMEOUT_SECONDS,
    deadline: float = DEADLINE_SECONDS,
) -> Outcome:
    """The archive's hourly weather from `start` to `end`, inclusive, with wind in m/s like every
    other speed the app stores. `auto_zone` asks the provider to resolve the coordinate's zone -
    days are then read in it, and its name comes back - for a dive that knows only its wall
    clock; otherwise days are UTC."""
    return await _ask(
        "weather",
        latitude,
        longitude,
        start,
        end,
        extra={"wind_speed_unit": "ms"},
        timezone="auto" if auto_zone else None,
        timeout=timeout,
        deadline=deadline,
    )


async def fetch_sea(
    latitude: float,
    longitude: float,
    start: date,
    end: date,
    *,
    timeout: float = TIMEOUT_SECONDS,
    deadline: float = DEADLINE_SECONDS,
) -> Outcome:
    """The marine model's hourly sea level, sea temperature and waves from `start` to `end`,
    inclusive, in UTC days."""
    return await _ask(
        "sea", latitude, longitude, start, end, extra={}, timezone=None, timeout=timeout, deadline=deadline
    )
