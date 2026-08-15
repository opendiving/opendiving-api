"""Forward and reverse geocoding against a Nominatim-compatible provider.

This is the API's first outbound HTTP call, and it exists as a server-side proxy rather
than a browser fetch for two reasons: `GEOCODER_API_KEY` stays off the client, and the web
app's strict-nonce CSP needs no new `connect-src` host.

Modelled on `services.email_service`: a third-party dependency that is *optional*. Set
`GEOCODER_URL` to an empty string, or take the provider away, and every function here logs
and returns nothing rather than raising - a diver typing a site name into the form must not
be blocked because a geocoder is down. That is also why the return types are "no result"
shapes (`None`, `[]`) instead of exceptions.

**Nominatim's usage policy is load-bearing, not paperwork.** It caps callers at one request
a second, requires that results be cached, and bars applications whose primary purpose is
geocoding. A dive log that geocodes when a site is created fits comfortably, but only with
all three of the things this module does: an identifying `User-Agent`
(`GEOCODER_USER_AGENT`), Redis caching of every answer, and throttling of the outbound call
through `enforce_rate_limit`.
"""

import json
import logging
from typing import Any

import httpx
from pydantic import ValidationError
from redis.exceptions import RedisError

from ..core.config import settings
from ..core.utils import cache
from ..core.utils.rate_limit import enforce_rate_limit
from ..schemas.geocoding import GeocodeResult

logger = logging.getLogger(__name__)

# Short enough that a slow provider can't hold a request open. There is no retry: a second
# attempt would spend the caller's remaining patience *and* a second slot against the
# provider's rate limit, for an endpoint whose failure mode is already "type it yourself".
_TIMEOUT = httpx.Timeout(5.0)

_SEARCH_RESULT_LIMIT = 5

# ~110 m. Reverse lookups are rounded to this before both the cache key and the outbound
# query, so every pin inside one cell shares one answer - which is the point, since the
# answer is a locality name and two dive-boat GPS fixes 40 m apart are the same place.
_COORDINATE_PRECISION = 3

# A place name does not change, so a hit is held for a month. A *miss* is held for an hour
# instead: an empty answer is far more likely to be provider weirdness than a permanent
# fact about the world, and pinning it for a month would make one bad afternoon look like
# a broken feature until the key expired.
_HIT_TTL_SECONDS = 30 * 24 * 60 * 60
_MISS_TTL_SECONDS = 60 * 60

# Bumped whenever the normalized shape or the way it is composed changes. Cached entries
# hold `GeocodeResult`s, not raw provider payloads - re-normalizing on every hit is wasted
# work - so a change to the normalizer has to invalidate them, and a new key prefix does
# that without a flush.
_CACHE_VERSION = "v1"

# Width of `dive_site.location`, which is where `location` is headed.
_LOCATION_MAX_LENGTH = 255

# Used when the provider sends no `licence` of its own. The default provider is OSM-backed,
# and attribution is a condition of using the data - never let a result go out without one.
_DEFAULT_ATTRIBUTION = "Data © OpenStreetMap contributors, ODbL 1.0. https://osm.org/copyright"

# Most specific populated place first: Nominatim fills whichever of these the point falls
# in, and a diver names the town, not the administrative district it belongs to.
_PLACE_KEYS = ("city", "town", "village", "hamlet", "municipality", "suburb", "city_district")
_REGION_KEYS = ("state", "province", "region", "county")


def _cache_key(kind: str, discriminator: str) -> str:
    """Geocoding cache keys are deliberately **not** user-scoped, unlike every other key in
    this app.

    The configured language is part of the key: it changes the answer, so entries written
    under one must not be served after an operator changes it.

    "What is at 28.572, 34.537" has the same answer for everybody, and the whole reason the
    provider's terms tolerate this feature is that one lookup serves every user who ever
    pins that spot. Keying per user would multiply outbound calls by the number of divers.

    The `geocode:` prefix keeps them clear of the `user_{id}_*` namespace that
    `services.cache_invalidation` sweeps by pattern, so nothing here is ever collateral
    damage of a mutation elsewhere - and nothing here needs invalidating, only expiring.
    """
    return f"geocode:{_CACHE_VERSION}:{settings.GEOCODER_LANGUAGE}:{kind}:{discriminator}"


async def _cached(key: str) -> list[GeocodeResult] | None:
    """The cached results for `key`, or `None` for "nothing usable cached".

    An empty *list* is a real cached answer ("the provider had nothing"), and is distinct
    from `None` - that distinction is what stops a negative result being re-asked on every
    keystroke. Redis being absent or unreachable is a cache miss, not an error: the caller
    then simply asks the provider.
    """
    if cache.client is None:
        return None

    try:
        raw = await cache.client.get(key)
    except RedisError as exc:
        logger.warning("Geocoding cache read failed (%s); asking the provider instead.", type(exc).__name__)
        return None

    if raw is None:
        return None

    try:
        rows = json.loads(raw.decode())
        return [GeocodeResult(**row) for row in rows]
    except ValueError, TypeError, ValidationError:
        logger.warning("Discarding an unreadable geocoding cache entry at %s.", key)
        return None


async def _store(key: str, results: list[GeocodeResult]) -> None:
    if cache.client is None:
        return

    ttl = _HIT_TTL_SECONDS if results else _MISS_TTL_SECONDS
    try:
        await cache.client.set(key, json.dumps([result.model_dump() for result in results]), ex=ttl)
    except RedisError as exc:
        logger.warning("Geocoding cache write failed (%s).", type(exc).__name__)


async def _request(path: str, params: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Ask the provider, returning its rows - or `None` when we could not ask at all.

    That distinction is the one thing this function exists to preserve. "The provider
    answered, and had nothing" is a fact worth caching; "the provider timed out" is not,
    and caching it would turn a thirty-second outage into a month of empty answers.

    `enforce_rate_limit` is called before the request and deliberately outside the `try`:
    it is the *provider's* one-request-per-second cap, so exceeding it must surface to the
    caller as a 429 rather than be swallowed as a geocoding failure. It fails open on a
    Redis outage, which is the right trade here too - a self-hosted instance with no Redis
    should still geocode.
    """
    if not settings.GEOCODER_URL:
        logger.warning("GEOCODER_URL is not configured; geocoding is unavailable.")
        return None

    await enforce_rate_limit(
        "geocode:provider",
        settings.GEOCODER_PROVIDER_RATE_LIMIT_REQUESTS,
        settings.GEOCODER_PROVIDER_RATE_LIMIT_WINDOW_SECONDS,
    )

    # `accept-language` is not optional politeness: without it Nominatim answers in the
    # local script, and "دهب, مصر" is not what a diver wants written into their logbook.
    query: dict[str, Any] = {
        **params,
        "format": "jsonv2",
        "addressdetails": 1,
        "accept-language": settings.GEOCODER_LANGUAGE,
    }
    if settings.GEOCODER_API_KEY:
        # The parameter Nominatim-compatible hosted mirrors expect. A provider that names
        # it something else (Geoapify uses `apiKey`) is a one-word change here.
        query["key"] = settings.GEOCODER_API_KEY

    url = f"{settings.GEOCODER_URL.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(url, params=query, headers={"User-Agent": settings.GEOCODER_USER_AGENT})
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        # `path`, never the built URL: that one carries GEOCODER_API_KEY as a query
        # parameter, and `core.logger` writes to a file on disk.
        logger.warning("Geocoder request to %s failed (%s).", path, type(exc).__name__)
        return None
    except ValueError:
        logger.warning("Geocoder response for %s was not JSON.", path)
        return None

    if isinstance(payload, dict):
        # `/reverse` answers with a bare object, and with `{"error": ...}` for a point that
        # resolves to nothing - which is an answer, not a failure.
        return [] if "error" in payload else [payload]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _first_present(address: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = address.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _short_location(row: dict[str, Any]) -> str:
    """Compose the value a diver would have typed themselves.

    Built from the provider's structured `address` rather than by trimming
    `display_name`, so the result barely moves if the provider changes how verbose that
    label is. Place plus country ("Dahab, Egypt") is what dive logs actually contain; the
    region only stands in when the point is too remote to fall inside a named settlement.

    Falls back to the full `display_name` for a row carrying no structured address - a
    named bay or reef, where the feature's own name is the best answer available. A point
    in genuinely open ocean gets no row at all and reverse-geocodes to `None`.
    """
    address = row.get("address")
    parts: list[str] = []
    if isinstance(address, dict):
        place = _first_present(address, _PLACE_KEYS) or _first_present(address, _REGION_KEYS)
        parts = [part for part in (place, _first_present(address, ("country",))) if part]

    display_name = str(row.get("display_name") or "").strip()
    return (", ".join(parts) or display_name)[:_LOCATION_MAX_LENGTH]


def _normalize(row: dict[str, Any]) -> GeocodeResult | None:
    """`None` for a row this app can do nothing with - no coordinates, or nothing to show a
    human. Dropping it beats surfacing a blank entry in a picker."""
    try:
        latitude = float(row["lat"])
        longitude = float(row["lon"])
    except KeyError, TypeError, ValueError:
        return None

    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None

    location = _short_location(row)
    if not location:
        return None

    name = row.get("name")
    licence = row.get("licence")
    return GeocodeResult(
        latitude=latitude,
        longitude=longitude,
        location=location,
        display_name=str(row.get("display_name") or "").strip() or location,
        name=name.strip() or None if isinstance(name, str) else None,
        attribution=licence.strip() if isinstance(licence, str) and licence.strip() else _DEFAULT_ATTRIBUTION,
    )


async def reverse_geocode(latitude: float, longitude: float) -> GeocodeResult | None:
    """The place at a position, or `None` when there isn't one to be had.

    The coordinates are rounded before both the cache key and the outbound query so the two
    always agree: everyone who pins the same ~110 m cell gets the identical cached answer
    rather than the first caller's exact position.
    """
    lat = round(latitude, _COORDINATE_PRECISION)
    lon = round(longitude, _COORDINATE_PRECISION)

    key = _cache_key("reverse", f"{lat}:{lon}")
    cached = await _cached(key)
    if cached is not None:
        return cached[0] if cached else None

    rows = await _request("/reverse", {"lat": lat, "lon": lon})
    if rows is None:
        return None

    results = [result for result in (_normalize(row) for row in rows) if result is not None][:1]
    await _store(key, results)
    return results[0] if results else None


async def search_places(query: str) -> list[GeocodeResult]:
    """Forward search - "blue hole dahab" - returning at most `_SEARCH_RESULT_LIMIT` places.

    The query is whitespace-collapsed and lower-cased for the cache key *and* for the
    outbound call, so trivially different spellings of the same search share one cached
    answer instead of each costing a provider slot. Nominatim matches case-insensitively,
    so nothing is lost by asking in lower case.
    """
    normalized = " ".join(query.split()).lower()
    if not normalized:
        return []

    key = _cache_key("search", normalized)
    cached = await _cached(key)
    if cached is not None:
        return cached

    rows = await _request("/search", {"q": normalized, "limit": _SEARCH_RESULT_LIMIT})
    if rows is None:
        return []

    results = [result for result in (_normalize(row) for row in rows) if result is not None]
    await _store(key, results)
    return results
