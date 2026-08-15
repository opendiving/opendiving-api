"""Forward and reverse geocoding against a Nominatim-compatible provider.

This is the API's first outbound HTTP call, and it exists as a server-side proxy rather
than a browser fetch for two reasons: `GEOCODER_API_KEY` stays off the client, and the web
app's strict-nonce CSP needs no new `connect-src` host.

Modelled on `services.email_service`: a third-party dependency that is *optional*. Set
`GEOCODER_URL` to an empty string, or take the provider away, and every function here logs
and returns nothing rather than raising - a diver typing a site name into the form must not
be blocked because a geocoder is down. That is also why the return types are "no result"
shapes (`None`, `[]`) instead of exceptions.

The one thing here that does *not* need the provider is the offshore fallback: a pin the
provider has no row for is answered from vendored sea polygons (`services.marine_areas`),
because Nominatim does not consult them and open water is where a lot of diving happens.
See `_offshore`.

**Nominatim's usage policy is load-bearing, not paperwork.** It caps callers at one request
a second, requires that results be cached, and bars applications whose primary purpose is
geocoding. A dive log that geocodes when a site is created fits comfortably, but only with
all three of the things this module does: an identifying `User-Agent`
(`GEOCODER_USER_AGENT`), Redis caching of every answer, and throttling of the outbound call
through `enforce_rate_limit`.
"""

import hashlib
import json
import logging
from itertools import islice
from typing import Any, NamedTuple

import anyio
import httpx
from pydantic import ValidationError
from redis.exceptions import RedisError

from ..core.config import settings
from ..core.exceptions.http_exceptions import RateLimitException
from ..core.utils import cache
from ..core.utils.rate_limit import enforce_rate_limit
from ..schemas.geocoding import GeocodeResult
from .marine_areas import water_name

logger = logging.getLogger(__name__)

# Short enough that a slow provider can't hold a request open. There is no retry: a second
# attempt would spend the caller's remaining patience *and* a second slot against the
# provider's rate limit, for an endpoint whose failure mode is already "type it yourself".
_TIMEOUT = httpx.Timeout(5.0)

# The bounds that actually hold, because `_TIMEOUT` is per socket read: a host that answers
# slowly enough, or endlessly enough, is bounded by these two and by nothing else. Both are
# far above any honest Nominatim answer - five geocoding results are a few kilobytes.
#
# Note for anyone sizing a client-side timeout against this: the worst case for the whole
# handler is this plus `_MAX_PROVIDER_WAIT_SECONDS`, which is spent before the deadline
# scope opens. Eleven seconds, not ten.
_DEADLINE_SECONDS = 10.0
_MAX_RESPONSE_BYTES = 512 * 1024

_SEARCH_RESULT_LIMIT = 5

# How long a request will wait for the instance's provider cap to free up before giving up
# and answering "no suggestion". Capped independently of the configured window so that
# raising that window (a self-hoster throttling their own Nominatim more gently) can never
# turn into a request held open for a minute.
_MAX_PROVIDER_WAIT_SECONDS = 1.0

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

# Mirror `schemas.geocoding.GeocodeResult`'s bounds. Applied by truncating here rather than
# by letting an over-long provider string raise a ValidationError inside `_normalize`, which
# would turn one verbose row into a failed lookup. `_LOCATION_MAX_LENGTH` is the width of
# `dive_site.location`, which is where that field is headed.
_LOCATION_MAX_LENGTH = 255
_DISPLAY_NAME_MAX_LENGTH = 512
_NAME_MAX_LENGTH = 255
_ATTRIBUTION_MAX_LENGTH = 255

# Bound on a provider string quoted into a log line - see `_log_safe`.
_LOGGED_VALUE_MAX_LENGTH = 200

# Used when the provider sends no `licence` of its own. The default provider is OSM-backed,
# and attribution is a condition of using the data - never let a result go out without one.
_DEFAULT_ATTRIBUTION = "Data © OpenStreetMap contributors, ODbL 1.0. https://osm.org/copyright"

# The offshore fallback's credit. Natural Earth asks for nothing, but the clients render
# this string verbatim under the suggestion, and "where did this name come from" is a fair
# question when it did not come from the provider named everywhere else.
_MARINE_ATTRIBUTION = "Water body names from Natural Earth, public domain. https://www.naturalearthdata.com"

# What Nominatim says when a position resolves to nothing - the *only* `error` payload that
# means "this is the answer" rather than "we are not answering you". Matched on the message
# because the shape is shared with bandwidth and abuse complaints.
_NO_RESULT_ERROR = "unable to geocode"

# Most specific populated place first: Nominatim fills whichever of these the point falls
# in, and a diver names the town, not the administrative district it belongs to.
_PLACE_KEYS = ("city", "town", "village", "hamlet", "municipality", "suburb", "city_district")
_REGION_KEYS = ("state", "province", "region", "county")


def _cache_key(kind: str, discriminator: str) -> str:
    """Geocoding cache keys are deliberately **not** user-scoped, unlike every other key in
    this app.

    The configured language *and provider* are part of the key, for the same reason: both
    change the answer, so entries written under one must not be served after an operator
    changes it. The provider matters most for `attribution`, which is read from whatever
    answered and is a licence condition of that data - without this, a month of cached rows
    would keep crediting OpenStreetMap for results now coming from somewhere else. Swapping
    `GEOCODER_URL` is a `.env` edit, so it cannot rely on `_CACHE_VERSION`, which is a code
    change.

    "What is at 28.572, 34.537" has the same answer for everybody, and the whole reason the
    provider's terms tolerate this feature is that one lookup serves every user who ever
    pins that spot. Keying per user would multiply outbound calls by the number of divers.

    The `geocode:` prefix keeps them clear of the `user_{id}_*` namespace that
    `services.cache_invalidation` sweeps by pattern, so nothing here is ever collateral
    damage of a mutation elsewhere - and nothing here needs invalidating, only expiring.
    """
    provider = hashlib.sha256(settings.GEOCODER_URL.encode()).hexdigest()[:8]
    return f"geocode:{_CACHE_VERSION}:{provider}:{settings.GEOCODER_LANGUAGE}:{kind}:{discriminator}"


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


def _log_safe(value: Any) -> str:
    """Make a provider-supplied string safe to hand to `logger`.

    It is the only such string that reaches a log rather than going through `_normalize`'s
    truncation, and it arrives from a body bounded at half a megabyte. Newlines are
    collapsed first: a log line is one line, and a value that can contain `\\n` can forge
    entries around itself in anything that parses the file afterwards.
    """
    return " ".join(str(value).split())[:_LOGGED_VALUE_MAX_LENGTH]


async def _claim_provider_slot() -> bool:
    """Take one slot against the instance-wide provider cap, or report that there is none.

    A boolean rather than the exception `enforce_rate_limit` raises, because here being over
    the cap is an ordinary branch to wait on - not an error to propagate.
    """
    try:
        await enforce_rate_limit(
            "geocode:provider",
            settings.GEOCODER_PROVIDER_RATE_LIMIT_REQUESTS,
            settings.GEOCODER_PROVIDER_RATE_LIMIT_WINDOW_SECONDS,
        )
    except RateLimitException:
        return False
    return True


async def _request(path: str, params: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Ask the provider, returning its rows - or `None` when we could not ask at all.

    That distinction is the one thing this function exists to preserve. "The provider
    answered, and had nothing" is a fact worth caching; "the provider timed out" is not,
    and caching it would turn a thirty-second outage into a month of empty answers.

    Being over the *provider's* cap is one of the ways we "could not ask", not a 429. That
    counter is global - it has to be, since the cap belongs to the instance rather than to
    any caller - so raising would mean one diver's search rejecting another diver's, which
    is both baffling from the outside and a contract this endpoint doesn't make.

    But skipping straight to "no result" is its own trap, because `[]` is byte-identical to
    "nothing matched": the diver is told a place doesn't exist when it does, and retrying
    looks like confirmation. So the cap is *waited on* once before it is given up on. At the
    default of one request per second that turns the common collision - two type-ahead
    queries from one diver landing in the same window - into a slightly slow right answer
    instead of a confidently wrong one. Only once, and bounded, so a genuinely saturated
    instance sheds load rather than queueing behind itself.

    The limiter fails open on a Redis outage, which is the right trade here too: a
    stripped-down instance with no Redis should still geocode.
    """
    if not settings.GEOCODER_URL:
        logger.warning("GEOCODER_URL is not configured; geocoding is unavailable.")
        return None

    if not await _claim_provider_slot():
        await anyio.sleep(min(settings.GEOCODER_PROVIDER_RATE_LIMIT_WINDOW_SECONDS, _MAX_PROVIDER_WAIT_SECONDS))
        if not await _claim_provider_slot():
            logger.warning("Skipping a geocoder call to %s: this instance is over its provider rate limit.", path)
            return None

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
        # `_TIMEOUT` bounds each socket read, which is not the same as bounding the call: a
        # host that dribbles one byte every few seconds never trips it and holds the request
        # open forever. `fail_after` is the actual deadline; the byte cap below is the
        # matching bound on how much such a host can make this process buffer.
        with anyio.fail_after(_DEADLINE_SECONDS):
            # A client per call, deliberately: the provider cap above holds this to roughly
            # one request a second, so a pooled connection would sit idle far longer than
            # any keep-alive, and a module-level client would need lifespan wiring to be
            # closed. An integration with real throughput should not copy this.
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                headers = {"User-Agent": settings.GEOCODER_USER_AGENT}
                async with client.stream("GET", url, params=query, headers=headers) as response:
                    response.raise_for_status()

                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_RESPONSE_BYTES:
                            logger.warning("Geocoder response for %s exceeded %d bytes.", path, _MAX_RESPONSE_BYTES)
                            return None

        payload = json.loads(body)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # `InvalidURL` is listed separately because it descends from `Exception` rather than
        # `HTTPError`, so a merely malformed `GEOCODER_URL` - a typo'd port, say - would
        # otherwise escape as a 500 and break this module's one promise.
        #
        # `path`, never the built URL: that one carries GEOCODER_API_KEY as a query
        # parameter, and logs get collected, shipped and kept. httpx would log the whole URL
        # itself at INFO, which is why `core.setup` pins its logger to WARNING.
        logger.warning("Geocoder request to %s failed (%s).", path, type(exc).__name__)
        return None
    except TimeoutError:
        logger.warning("Geocoder request to %s exceeded its %ss deadline.", path, _DEADLINE_SECONDS)
        return None
    except ValueError:
        logger.warning("Geocoder response for %s was not JSON.", path)
        return None

    if isinstance(payload, dict):
        # `/reverse` answers with a bare object - and with `{"error": ...}` for two very
        # different things. "Unable to geocode" is a real answer about a real position and
        # belongs in the cache; a bandwidth or abuse complaint wears the same shape and
        # must not be, or one bad minute pins a genuine place as "no result" for an hour.
        error = payload.get("error")
        if error is not None:
            if _NO_RESULT_ERROR in str(error).casefold():
                return []
            logger.warning("Geocoder refused %s: %s", path, _log_safe(error))
            return None
        return [payload]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]

    # Valid JSON that is neither an object nor an array is not this provider answering -
    # it's a proxy, a CDN error page rendered as JSON, or a host that isn't Nominatim at
    # all. Same treatment as a body that didn't parse: a failure, not an empty answer, so
    # it is never cached as "no such place".
    logger.warning("Geocoder response for %s was not an object or an array.", path)
    return None


def _text(value: Any) -> str | None:
    """A trimmed string, or `None` for anything blank or not a string. The provider's JSON
    is untyped, so every field it hands over is a maybe-string."""
    return value.strip() or None if isinstance(value, str) else None


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
    in genuinely open ocean gets no row at all, and is answered by `_offshore` instead.
    """
    address = row.get("address")
    parts: list[str] = []
    if isinstance(address, dict):
        place = _first_present(address, _PLACE_KEYS) or _first_present(address, _REGION_KEYS)
        parts = [part for part in (place, _first_present(address, ("country",))) if part]

    return (", ".join(parts) or _text(row.get("display_name")) or "")[:_LOCATION_MAX_LENGTH]


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

    name = _text(row.get("name"))

    # The one provider string that is *replaced* rather than truncated when it is too long.
    # The others are labels, and a clipped label is still a usable label; this one is a
    # licence notice, and one cut mid-sentence is not attribution at all - which is the
    # thing this field exists to guarantee. Nominatim's own is about seventy characters, so
    # in practice this only fires for a provider doing something strange.
    licence = _text(row.get("licence"))
    if licence is not None and len(licence) > _ATTRIBUTION_MAX_LENGTH:
        logger.warning("Geocoder sent a %d-character licence; falling back to the default credit.", len(licence))
        licence = None

    return GeocodeResult(
        latitude=latitude,
        longitude=longitude,
        location=location,
        display_name=(_text(row.get("display_name")) or location)[:_DISPLAY_NAME_MAX_LENGTH],
        name=name[:_NAME_MAX_LENGTH] if name else None,
        attribution=licence or _DEFAULT_ATTRIBUTION,
    )


def _offshore(lat: float, lon: float) -> GeocodeResult | None:
    """The sea at a position, for a point the provider had no row for - or `None`.

    Coastal water is *not* this: territorial waters fall inside an admin boundary, so a pin
    off Bali already reverse-geocodes to "Bali, Indonesia", which beats "Bali Sea".

    This runs where the provider answered and left us with nothing usable. Nearly always that
    means "unable to geocode", which is genuinely open water; it also covers the rarer case of
    a row `_normalize` had to drop - one with no coordinates or nothing displayable - which is
    strictly a provider answer and could in principle be excluded. It isn't, because the cache
    cannot tell the two apart: both are stored as `[]`, so gating the fresh call on
    `rows == []` would have the first request answer `None` and the second "Red Sea". A
    coherent answer beats a marginally more principled one on a branch a `/reverse` row has
    to be malformed to reach.

    That trade was struck when the cost of getting it wrong was "Red Sea" instead of `None`,
    and it is worth restating now that it is dearer: a dropped row leaves the caller with an
    `asked` outcome and no result, which the route answers `204` - "this position has no
    name" - and the web app acts on by clearing a location the diver may have typed. A mirror
    that omitted `lat`/`lon` from `/reverse` would do that for every pin in the cell for the
    hour the `[]` is cached. Still not worth splitting, for the reason above, but a fix here
    has to keep the cached and fresh branches agreeing or it trades one intermittent for a
    worse one.

    `latitude`/`longitude` echo the position that was asked about rather than the polygon's
    centroid - the caller is about to drop a pin at what comes back, and the centre of the
    Red Sea is not where they were looking.

    A disabled geocoder means disabled: with `GEOCODER_URL` empty the endpoint answers
    nothing at all, rather than half a feature that only works over water. That check is
    belt and braces - `_request` already refuses to ask - but it is also the line that says
    which behaviour was chosen, since the opposite one is perfectly defensible.
    """
    if not settings.GEOCODER_URL:
        return None

    name = water_name(lat, lon)
    if name is None:
        return None

    # Bounded like every provider string, for the same reason and despite the data being
    # ours: the longest name in the file is a few dozen characters, so this only ever fires
    # for a refresh that pulled in something strange - and a `ValidationError` here would be
    # a 500 from the one branch that exists to avoid answering nothing.
    return GeocodeResult(
        latitude=lat,
        longitude=lon,
        location=name[:_LOCATION_MAX_LENGTH],
        display_name=name[:_DISPLAY_NAME_MAX_LENGTH],
        name=name[:_NAME_MAX_LENGTH],
        attribution=_MARINE_ATTRIBUTION,
    )


class ReverseGeocode(NamedTuple):
    """A reverse lookup's outcome, kept as two values because "no name here" and "we never
    got to ask" are different facts and the caller has to act differently on each.

    Flattening them into one `GeocodeResult | None` is what this type exists to stop. The
    web app writes a named answer straight into a dive site's `location` field, and a
    no-name answer has to *clear* it - otherwise the previous pin's name silently follows the
    pin to a new position. Doing that on "we could not ask" would wipe a location the diver
    typed, a round trip after they moved a pin twice inside the provider's one-per-second
    window.

    `result` is `None` on both sorts of empty outcome; `asked` is the one that separates them.
    """

    result: GeocodeResult | None
    # True only when the provider returned a usable verdict about this position - including
    # the verdict "nothing here". Everything else is False: geocoding off, over the provider
    # cap, unreachable, and equally a refusal, an error status or a body that did not parse,
    # since none of those told us anything about the position either. Read it as "was
    # anything learned", not as "did a packet leave"; a reader who flips one of those
    # branches turns a provider outage into a stream of cleared location fields.
    asked: bool


async def reverse_geocode(latitude: float, longitude: float) -> ReverseGeocode:
    """The place at a position, and whether the question was actually put to the provider.

    The coordinates are rounded before both the cache key and the outbound query so the two
    always agree: everyone who pins the same ~110 m cell gets the identical cached answer
    rather than the first caller's exact position.

    A cache hit is an `asked` outcome, including a cached `[]` - that entry is the provider
    having answered "nothing here", written under a TTL and re-asked when it expires. Reading
    it as "could not ask" would make the outcome depend on whether Redis happens to be warm,
    which is the worst kind of intermittent.

    The offshore fallback runs **outside the cache**, on both branches below, and changes
    nothing about what is stored or for how long. What is stored stays an honest record of
    what the provider said, so refreshing the polygons takes effect immediately instead of
    waiting out a cached `[]`; the lookup is local and costs a fraction of a millisecond, so
    there is nothing to save by caching it. Keeping a corroborated `[]` for a month rather
    than an hour was tried and rejected - see `DECISIONS.md`.

    It is deliberately not reached when `_request` returns `None` - "we could not ask" is not
    the provider telling us the position is open water, and answering "Bali Sea" during an
    outage where the answer is "Bali, Indonesia" would write the worse string into a dive site
    for good.
    """
    lat = round(latitude, _COORDINATE_PRECISION)
    lon = round(longitude, _COORDINATE_PRECISION)

    key = _cache_key("reverse", f"{lat}:{lon}")
    cached = await _cached(key)
    if cached is not None:
        return ReverseGeocode(cached[0] if cached else _offshore(lat, lon), asked=True)

    rows = await _request("/reverse", {"lat": lat, "lon": lon})
    if rows is None:
        return ReverseGeocode(None, asked=False)

    results = [result for result in (_normalize(row) for row in rows) if result is not None][:1]
    await _store(key, results)
    return ReverseGeocode(results[0] if results else _offshore(lat, lon), asked=True)


async def search_places(query: str) -> list[GeocodeResult]:
    """Forward search - "blue hole dahab" - returning at most `_SEARCH_RESULT_LIMIT` places.

    The query is whitespace-collapsed and lower-cased for the cache key *and* for the
    outbound call, so trivially different spellings of the same search share one cached
    answer instead of each costing a provider slot. Nominatim matches case-insensitively,
    so nothing is lost by asking in lower case.

    The key holds a digest of that text rather than the text itself. Unlike every other key
    in this app the discriminator here is free-form input - up to 200 characters of
    whatever a diver typed, in any script - and a fixed-width digest keeps key length off
    the caller entirely. It costs the ability to read the query out of `redis-cli --scan`,
    which is a fair trade for the same reason the coordinate keys are rounded: these are a
    cache, not a log of what people searched for.
    """
    normalized = " ".join(query.split()).lower()
    if not normalized:
        return []

    key = _cache_key("search", hashlib.sha256(normalized.encode()).hexdigest()[:32])
    cached = await _cached(key)
    if cached is not None:
        return cached

    rows = await _request("/search", {"q": normalized, "limit": _SEARCH_RESULT_LIMIT})
    if rows is None:
        return []

    # Bounded here, not only asked for via `limit`: a mirror that caps differently, or
    # ignores the parameter, would otherwise have every row it sent normalized, cached for a
    # month and returned. The bound on the response belongs to this app.
    #
    # Lazily, and counting only the rows that *survive* normalization. Slicing the raw rows
    # first is cheaper to read but silently shrinks the answer - five unusable leading rows
    # would empty a search that had fifteen good ones behind them - while normalizing all of
    # them first makes 50,000 junk rows cost 50,000 normalizations. `islice` over a generator
    # is both: it stops at five successes and never touches the rest.
    results = list(islice((result for result in map(_normalize, rows) if result is not None), _SEARCH_RESULT_LIMIT))
    await _store(key, results)
    return results
