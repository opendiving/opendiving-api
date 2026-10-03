"""Place search against Photon, and reverse geocoding against a Nominatim-compatible provider.

This is the API's first outbound HTTP call, and it exists as a server-side proxy rather
than a browser fetch for two reasons: `GEOCODER_API_KEY` stays off the client, and the web
app's strict-nonce CSP needs no new `connect-src` host.

**Two providers, one per question.** Naming the spot behind a pin asks Nominatim's
`/reverse` at `GEOCODER_URL`; a typed search asks Photon's `/api` at `GEOCODER_SEARCH_URL`.
Nominatim matches whole words only - "phi phi" never reaches Ko Phi Phi Don - and its usage
policy forbids search-as-you-type, which is what Photon is built for, over the same
OpenStreetMap data. The two wire formats share nothing, so each has its own request and
normalizer (`_request` and `_normalize`, `_search_photon` and `_normalize_photon`); what they
share is what is provider-neutral - the deadline, the byte cap, the User-Agent (`_fetch`),
the cache and the per-user limit.

Modelled on `services.email_service`: a third-party dependency that is *optional*. Set
`GEOCODER_URL` to an empty string, or take a provider away, and every function here logs and
returns nothing rather than raising - a diver typing a site name into the form must not be
blocked because a geocoder is down. That is also why the return types are "no result" shapes
(`None`, `[]`) instead of exceptions. `GEOCODER_URL=""` switches search off too - disabled
means disabled, and an upgrade must not switch a disabled feature back on - and
`GEOCODER_SEARCH_URL=""` switches off search alone. Pointing `GEOCODER_URL` at a Nominatim of
one's own is not a switch: searches still go to `GEOCODER_SEARCH_URL`, public Photon by
default.

The one thing here that does *not* need a provider is the offshore fallback: a pin Nominatim
has no row for is answered from vendored sea polygons (`services.marine_areas`), because
Nominatim does not consult them and open water is where a lot of diving happens. See
`_offshore`.

**Nominatim's usage policy is load-bearing, not paperwork.** It caps callers at one request
a second, requires that results be cached, and bars applications whose primary purpose is
geocoding. Photon's public instance asks for use "within a reasonable limit" and throttles or
bans beyond it. Both are met by the same three things: an identifying `User-Agent`
(`GEOCODER_USER_AGENT`), Redis caching of every answer, and throttling of each provider's
outbound calls through `enforce_rate_limit`.
"""

import hashlib
import json
import logging
import re
from collections.abc import Iterator
from itertools import islice
from typing import Any, NamedTuple

import anyio
import httpx
from anyio import sleep  # A name of this module's own, so a test can stub its wait and nobody else's.
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
# far above any honest answer from either provider - a search's rows are a few kilobytes.
#
# Note for anyone sizing a client-side timeout against this: the worst case for the whole
# handler is this plus `_MAX_PROVIDER_WAIT_SECONDS`, which is spent before the deadline
# scope opens. Eleven seconds, not ten.
_DEADLINE_SECONDS = 10.0
_MAX_RESPONSE_BYTES = 512 * 1024

_SEARCH_RESULT_LIMIT = 5

# How many rows a search asks Photon for. More than it returns, because Photon sends one OSM
# object more than once in one answer, and the rows `_is_place` refuses are not replaced -
# asking for exactly `_SEARCH_RESULT_LIMIT` would hand back fewer places than Photon had.
# Photon's own default, well under the 50 it clamps to.
_SEARCH_ROWS_REQUESTED = 15

# Every Photon layer but `house` and `street`, sent as repeated `layer` parameters. A shop, a
# station or a restaurant sits in `house`, and asking Photon not to send them keeps them from
# taking the rows above - `dahab` otherwise answers the town and fourteen shops, cafés and
# restaurants. `_is_place` still judges every row, so a host that ignores the filter costs
# rows, not correctness.
_SEARCH_LAYERS = ("city", "county", "country", "district", "locality", "other", "state")

# What a search result may be: a place or a natural feature - towns, villages, islands, reefs,
# bays, peaks, regions, parks - judged by the row's main OSM tag. An allow-list, because the
# layers `_SEARCH_LAYERS` keeps still carry land use and the rest of OSM, and there is no end
# to listing what is *not* a place. A park is a `boundary` or a nature reserve: marine parks
# are mapped as either, often both.
_PLACE_OSM_KEYS = frozenset({"place", "natural", "boundary"})
_PLACE_OSM_TAGS = frozenset({("leisure", "nature_reserve")})

# The languages Photon's public instance answers in; anything else is a 400. `en` stands in
# for the rest rather than `default`, which is the local script - the thing
# `GEOCODER_LANGUAGE` exists to avoid - and ranks shops above towns besides.
_SEARCH_LANGUAGES = frozenset({"de", "en", "fr"})
_FALLBACK_SEARCH_LANGUAGE = "en"

# A Photon row's address parts, finest first; the first present names a row that has no name
# of its own. Street and house number are left out: a place has neither, and a postcode is
# not a part of where a place is.
_PHOTON_ADDRESS_KEYS = ("locality", "district", "city", "county", "state", "country")

# Photon's `osm_type`, spelled the way the dive-site catalog spells an OSM identity
# (`scripts/build_dive_site_catalog.py`), so a client compares the two by string.
_OSM_TYPES = {"N": "node", "W": "way", "R": "relation"}

# How long a request will wait for a provider's cap to free up before giving up and answering
# "no suggestion". Capped independently of the configured window so that raising that window
# (a self-hoster throttling their own Nominatim more gently) can never turn into a request
# held open for a minute.
_MAX_PROVIDER_WAIT_SECONDS = 1.0

# One counter each, so a pin's reverse lookup and a search keystroke never contend for one
# slot. Named for the wire format, which is what fixes the provider behind each setting.
_PROVIDER_NOMINATIM = "nominatim"
_PROVIDER_PHOTON = "photon"

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
_CACHE_VERSION = "v5"

# Mirror `schemas.geocoding.GeocodeResult`'s bounds. Applied by truncating here rather than
# by letting an over-long provider string raise a ValidationError inside a normalizer, which
# would turn one verbose row into a failed lookup. `_LOCATION_MAX_LENGTH` is the width of a
# place's `name` column (`schemas.location.LOCATION_NAME_MAX`), which is where that field is
# headed.
_LOCATION_MAX_LENGTH = 255
_NAME_MAX_LENGTH = 255
_ATTRIBUTION_MAX_LENGTH = 255
_COUNTRY_MAX_LENGTH = 255
_REGION_MAX_LENGTH = 255

# Bounds an OSM id so its `source_id` always fits `GeocodeResult.source_id`'s 64 characters:
# JSON integers are unbounded, and OSM's are 64-bit.
_OSM_ID_LIMIT = 2**63

# Bound on a provider string quoted into a log line - see `_log_safe`.
_LOGGED_VALUE_MAX_LENGTH = 200

# Used when the provider sends no `licence` of its own, which Photon never does. Both
# providers serve OpenStreetMap data, and attribution is a condition of using it - never let
# a result go out without one.
#
# Written already folded, in the shape `_linked_attribution` produces, so the credit looks
# the same whether it came from the provider or from here - which of the two answered is not
# something a reader should be able to see. `test_the_built_in_credits_are_already_folded`
# holds the two shapes together.
_DEFAULT_ATTRIBUTION = "[Data © OpenStreetMap contributors, ODbL 1.0.](https://osm.org/copyright)"

# The offshore fallback's credit. Natural Earth asks for nothing, but the clients render
# this string verbatim under the suggestion, and "where did this name come from" is a fair
# question when it did not come from the provider named everywhere else.
_MARINE_ATTRIBUTION = "[Water body names from Natural Earth, public domain.](https://www.naturalearthdata.com)"

# A credit that ends in a bare URL, as `<text> <url>` with optional punctuation trailing the
# URL. Matching on *shape* rather than on anything about OpenStreetMap is the whole point:
# `GEOCODER_URL` is an operator setting, so the string is whatever answered.
#
# The text before the URL is required, not optional, and that is what makes this safe for the
# other providers in reach: LocationIQ's `licence` is the bare URL
# `https://locationiq.com/attribution` and nothing else, which under a `\s*` anchor would
# fold to `[](https://locationiq.com/attribution)` - an empty label crediting nobody.
#
# The trailing-punctuation group exists so a provider ending "... see https://example.com/x."
# does not get the sentence's full stop swallowed into the href. Nominatim's URL is the final
# token with nothing after it, so in practice this group is empty.
_TRAILING_CREDIT_URL = re.compile(r"^(?P<text>\S.*?)\s+(?P<url>https?://\S+?)(?P<trailing>[.,;:)]*)$")

# What Nominatim says when a position resolves to nothing - the *only* `error` payload that
# means "this is the answer" rather than "we are not answering you". Matched on the message
# because the shape is shared with bandwidth and abuse complaints.
_NO_RESULT_ERROR = "unable to geocode"

# Most specific populated place first: Nominatim fills whichever of these the point falls
# in, and a diver names the town, not the administrative district it belongs to.
_PLACE_KEYS = ("city", "town", "village", "hamlet", "municipality", "suburb", "city_district")
# A pin's region is the first of these present. `province` is not on Nominatim's list of
# address labels, and it is the one a pin in Thailand is sent.
_REGION_KEYS = ("state", "province", "region", "county")


def _cache_key(kind: str, provider_url: str, language: str, discriminator: str) -> str:
    """Geocoding cache keys are deliberately **not** user-scoped, unlike every other key in
    this app.

    The language asked for *and the provider* are part of the key, for the same reason: both
    change the answer, so entries written under one must not be served after an operator
    changes it. For a pin the provider matters most for `attribution`, which is read from
    whatever answered and is a licence condition of that data - without this, a month of
    cached rows would keep crediting OpenStreetMap for results now coming from somewhere
    else. Swapping `GEOCODER_URL` or `GEOCODER_SEARCH_URL` is a `.env` edit, so it cannot rely
    on `_CACHE_VERSION`, which is a code change. Each lookup is keyed by its own provider only:
    a search key that also hashed `GEOCODER_URL` would re-ask Photon for everything whenever
    an operator repointed their Nominatim.

    "What is at 28.572, 34.537" has the same answer for everybody, and the whole reason the
    provider's terms tolerate this feature is that one lookup serves every user who ever
    pins that spot. Keying per user would multiply outbound calls by the number of divers.

    The `geocode:` prefix keeps them clear of the `user_{id}_*` namespace that
    `services.cache_invalidation` sweeps by pattern, so nothing here is ever collateral
    damage of a mutation elsewhere - and nothing here needs invalidating, only expiring.
    """
    provider = hashlib.sha256(provider_url.encode()).hexdigest()[:8]
    return f"geocode:{_CACHE_VERSION}:{provider}:{language}:{kind}:{discriminator}"


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

    It is the only such string that reaches a log rather than going through a normalizer's
    truncation, and it arrives from a body bounded at half a megabyte. Newlines are
    collapsed first: a log line is one line, and a value that can contain `\\n` can forge
    entries around itself in anything that parses the file afterwards.
    """
    return " ".join(str(value).split())[:_LOGGED_VALUE_MAX_LENGTH]


async def _claim_provider_slot(provider: str) -> bool:
    """Take one slot against the instance-wide cap for `provider`, or report that there is none.

    A boolean rather than the exception `enforce_rate_limit` raises, because here being over
    the cap is an ordinary branch to wait on - not an error to propagate. Both providers share
    the one configured limit, each on its own counter.
    """
    try:
        await enforce_rate_limit(
            f"geocode:provider:{provider}",
            settings.GEOCODER_PROVIDER_RATE_LIMIT_REQUESTS,
            settings.GEOCODER_PROVIDER_RATE_LIMIT_WINDOW_SECONDS,
        )
    except RateLimitException:
        return False
    return True


async def _fetch(provider: str, base_url: str, path: str, params: dict[str, Any]) -> Any | None:
    """Ask a provider, returning its parsed JSON body - or `None` when we could not ask at all.

    That distinction is the one thing this function exists to preserve. "The provider
    answered, and had nothing" is a fact worth caching; "the provider timed out" is not,
    and caching it would turn a thirty-second outage into a month of empty answers. So
    anything but a 200 is "could not ask" - including Photon's public instance, which signals
    a block with an HTML 404 or a 504 rather than a 429.

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

    Logs name the provider and the path, never the built URL: that carries the diver's typed
    search, and for Nominatim `GEOCODER_API_KEY`, and logs get collected, shipped and kept.
    httpx would log the whole URL itself at INFO, which is why `core.setup` pins its logger to
    WARNING.
    """
    if not await _claim_provider_slot(provider):
        await sleep(min(settings.GEOCODER_PROVIDER_RATE_LIMIT_WINDOW_SECONDS, _MAX_PROVIDER_WAIT_SECONDS))
        if not await _claim_provider_slot(provider):
            logger.warning("Skipping a %s call to %s: this instance is over its provider rate limit.", provider, path)
            return None

    url = f"{base_url.rstrip('/')}{path}"
    try:
        # `_TIMEOUT` bounds each socket read, which is not the same as bounding the call: a
        # host that dribbles one byte every few seconds never trips it and holds the request
        # open forever. `fail_after` is the actual deadline; the byte cap below is the
        # matching bound on how much such a host can make this process buffer.
        with anyio.fail_after(_DEADLINE_SECONDS):
            # A client per call, deliberately: the provider caps above hold this to roughly
            # one request a second per provider, so a pooled connection would sit idle far
            # longer than any keep-alive, and a module-level client would need lifespan
            # wiring to be closed. An integration with real throughput should not copy this.
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                headers = {"User-Agent": settings.GEOCODER_USER_AGENT}
                async with client.stream("GET", url, params=params, headers=headers) as response:
                    if response.status_code != 200:
                        logger.warning("The %s geocoder answered %s with %d.", provider, path, response.status_code)
                        return None

                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_RESPONSE_BYTES:
                            logger.warning(
                                "The %s geocoder's answer to %s exceeded %d bytes.", provider, path, _MAX_RESPONSE_BYTES
                            )
                            return None

        return json.loads(body)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # `InvalidURL` is listed separately because it descends from `Exception` rather than
        # `HTTPError`, so a merely malformed URL setting - a typo'd port, say - would
        # otherwise escape as a 500 and break this module's one promise.
        logger.warning("A %s geocoder request to %s failed (%s).", provider, path, type(exc).__name__)
        return None
    except TimeoutError:
        logger.warning("A %s geocoder request to %s exceeded its %ss deadline.", provider, path, _DEADLINE_SECONDS)
        return None
    except ValueError:
        logger.warning("The %s geocoder's answer to %s was not JSON.", provider, path)
        return None


async def _request(path: str, params: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Ask Nominatim, returning its rows - or `None` when we could not ask at all (`_fetch`)."""
    if not settings.GEOCODER_URL:
        logger.warning("GEOCODER_URL is not configured; geocoding is unavailable.")
        return None

    # `accept-language` is not optional politeness: without it Nominatim answers in the
    # local script, and "دهب, جنوب سيناء, مصر" is not what a diver wants written into their
    # logbook.
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

    payload = await _fetch(_PROVIDER_NOMINATIM, settings.GEOCODER_URL, path, query)
    if payload is None:
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


def _search_language() -> str:
    """The `lang` a search asks Photon in: `GEOCODER_LANGUAGE` where Photon speaks it, else `en`.

    Judged on the primary subtag, so an operator's `de-AT` still searches in German. Sent on
    every search rather than left to `Accept-Language`, and it is this value, not the setting,
    that goes into the cache key - two settings that ask in the same language share answers.
    """
    primary = re.split(r"[-_,;]", settings.GEOCODER_LANGUAGE.strip().lower(), maxsplit=1)[0]
    return primary if primary in _SEARCH_LANGUAGES else _FALLBACK_SEARCH_LANGUAGE


async def _search_photon(query: str, language: str) -> list[dict[str, Any]] | None:
    """Ask Photon, returning its features - or `None` when we could not ask at all (`_fetch`).

    Only parameters Photon's `/api` knows go out: it answers anything else with a 400, which
    is why none of `_request`'s Nominatim parameters, and never `GEOCODER_API_KEY`, are here.
    """
    params: dict[str, Any] = {
        "q": query,
        "lang": language,
        "limit": _SEARCH_ROWS_REQUESTED,
        "layer": list(_SEARCH_LAYERS),
    }
    payload = await _fetch(_PROVIDER_PHOTON, settings.GEOCODER_SEARCH_URL, "/api", params)
    if payload is None:
        return None

    # Only a GeoJSON FeatureCollection is Photon answering. Anything else - a proxy's JSON
    # error, a host that is not Photon at all - is a failure rather than "no such place", so
    # it is never cached as one.
    if isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
        features = payload.get("features")
        if isinstance(features, list):
            return [feature for feature in features if isinstance(feature, dict)]
    logger.warning("The photon geocoder's answer to /api was not a GeoJSON FeatureCollection.")
    return None


def _text(value: Any) -> str | None:
    """A trimmed string, or `None` for anything blank or not a string. The provider's JSON
    is untyped, so every field it hands over is a maybe-string."""
    return value.strip() or None if isinstance(value, str) else None


def _linked_attribution(credit: str) -> str:
    """A credit ending in a bare URL, rewritten as the one `[text](url)` shape the clients
    can parse.

    The clients render this string as fine print, and a printed URL is not a link. The OSM
    Foundation's attribution guidelines ask for a way to *reach* the licence - "for example
    by making the text a clickable link" - and a bare URL only names it. It is also the
    widest part of the line: at the 10px the web app draws it, the URL alone is a third of
    the string, which is what pushed the credit onto a second line in a phone-width dialog.

    Folding rather than substituting a credit of our own, because `GEOCODER_URL` is an
    operator setting and the string belongs to whoever answered. Nominatim is the only
    provider in reach that sends text-then-URL; everything else in that shape's neighbourhood
    is left alone by construction rather than by exception - LocationIQ sends the URL with no
    text (see `_TRAILING_CREDIT_URL`), Mapbox puts one mid-sentence, MapTiler already sends
    anchors, and Photon and HERE send no credit at all. None of those has a *trailing* bare
    URL, so none of them matches.

    Idempotent, which the month-long cache TTL makes a requirement rather than a nicety: the
    folded form ends in `)` and its URL is preceded by `(` instead of whitespace, so a value
    re-read and re-normalized has nothing left to fold.
    """
    match = _TRAILING_CREDIT_URL.match(credit)
    if match is None:
        return credit

    # The one part of the provider's string that is rewritten rather than moved. We are
    # minting an href a browser will follow, and Nominatim still sends `http://osm.org` -
    # which redirects to TLS anyway, so honouring the scheme literally costs a plaintext hop
    # and buys nothing. The visible text is never touched.
    url = re.sub(r"^http://", "https://", match["url"])
    return f"[{match['text']}]({url}){match['trailing']}"


def _first_present(address: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = address.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _place_name(place: str | None, region: str | None, country: str | None) -> str:
    """A geocoder result's `location`, for either provider: the place, its region and its
    country, joined with ", ".

    A part equal to any earlier one, compared whole and case-insensitively, is left out, and
    not only where the two are neighbours. So a pin in no settlement, whose region stands in
    for the place, reads "Bali, Indonesia" rather than "Bali, Bali, Indonesia", and a
    country's own row names it once.
    """
    seen: set[str] = set()
    parts: list[str] = []
    for part in (place, region, country):
        if part is not None and part.casefold() not in seen:
            seen.add(part.casefold())
            parts.append(part)
    return ", ".join(parts)


def _short_location(row: dict[str, Any], region: str | None, country: str | None) -> str:
    """Compose the value a diver would have typed themselves, for a pin: the settlement it
    falls in, its region and its country, "Dahab, South Sinai, Egypt".

    The region is there because a name cut to the place and its country is often ambiguous,
    and people write it that way for being quicker to type rather than for being better -
    prefilled, the region costs the diver nothing. Where the point falls in no named
    settlement the region stands in for the place.

    Built from the provider's structured `address` rather than by trimming its
    `display_name`, so the result barely moves if the provider changes how verbose that
    label is - and that label can carry a postcode, which is no part of a place's name.

    Falls back to the provider's `display_name` for a row carrying no structured address - a
    named bay or reef, where the feature's own name is the best answer available. A point
    in genuinely open ocean gets no row at all, and is answered by `_offshore` instead.
    """
    address = row.get("address")
    place = _first_present(address, _PLACE_KEYS) if isinstance(address, dict) else None
    location = _place_name(place or region, region, country)

    return (location or _text(row.get("display_name")) or "")[:_LOCATION_MAX_LENGTH]


def _normalize(row: dict[str, Any]) -> GeocodeResult | None:
    """A Nominatim row as a result, or `None` for a row this app can do nothing with - no
    coordinates, or nothing to show a human. Dropping it beats surfacing a blank entry.

    Only `/reverse` rows come through here, so the result carries no box: it answers "what is
    this position called" for a caller already holding the position. It does carry the
    region and the country its name was composed from, as a search result does, so a client
    reads one shape from both routes.
    """
    try:
        latitude = float(row["lat"])
        longitude = float(row["lon"])
    except KeyError, TypeError, ValueError:
        return None

    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None

    address = row.get("address")
    region = country = None
    if isinstance(address, dict):
        region = _first_present(address, _REGION_KEYS)
        country = _first_present(address, ("country",))

    location = _short_location(row, region, country)
    if not location:
        return None

    name = _text(row.get("name"))

    # The one provider string that is *replaced* rather than truncated when it is too long.
    # The others are labels, and a clipped label is still a usable label; this one is a
    # licence notice, and one cut mid-sentence is not attribution at all - which is the
    # thing this field exists to guarantee. Nominatim's own is about seventy characters, so
    # in practice this only fires for a provider doing something strange.
    #
    # Measured *after* folding, not before. Folding grows the string by up to four: four
    # brackets in, one space out, and one more when an `http:` href is upgraded - which is
    # Nominatim's case, so +4 is the figure that matters rather than the +3 an already-`https`
    # provider would see. Checking the provider's own length would therefore let a
    # 252-character licence through the guard and straight into a `ValidationError` on
    # `GeocodeResult.attribution`. The length logged is still the provider's, since that is
    # the number an operator would go looking for.
    licence = _text(row.get("licence"))
    attribution = None
    if licence is not None:
        attribution = _linked_attribution(licence)
        if len(attribution) > _ATTRIBUTION_MAX_LENGTH:
            logger.warning("Geocoder sent a %d-character licence; falling back to the default credit.", len(licence))
            attribution = None

    # Every optional field is spelled out, here and below, because mypy cannot see the
    # schema's defaults through `Annotated[..., Field(default=None)]` (CONTRIBUTING.md).
    return GeocodeResult(
        latitude=latitude,
        longitude=longitude,
        location=location,
        name=name[:_NAME_MAX_LENGTH] if name else None,
        attribution=attribution or _DEFAULT_ATTRIBUTION,
        country=country[:_COUNTRY_MAX_LENGTH] if country else None,
        region=region[:_REGION_MAX_LENGTH] if region else None,
        source=None,
        source_id=None,
        bbox_south=None,
        bbox_north=None,
        bbox_west=None,
        bbox_east=None,
    )


def _is_place(properties: dict[str, Any]) -> bool:
    key = _text(properties.get("osm_key"))
    return key in _PLACE_OSM_KEYS or (key, _text(properties.get("osm_value"))) in _PLACE_OSM_TAGS


def _osm_identity(properties: dict[str, Any]) -> str | None:
    """`node/6215139685` - the row's OSM object, or `None` when the row does not say."""
    osm_type = _OSM_TYPES.get(_text(properties.get("osm_type")) or "")
    osm_id = properties.get("osm_id")
    if osm_type is None or isinstance(osm_id, bool) or not isinstance(osm_id, int):
        return None
    if not 0 < osm_id < _OSM_ID_LIMIT:
        return None
    return f"{osm_type}/{osm_id}"


def _photon_extent(extent: Any) -> tuple[float, float, float, float] | None:
    """A Photon `extent` as `(south, north, west, east)`, or `None` for anything unusable.

    Photon orders it west, north, east, south - neither Nominatim's order nor GeoJSON's.
    Everything about the shape is checked rather than assumed, because a box is optional to
    the caller and a host that sends three corners, or strings, or nonsense, must cost the
    result its box and nothing more - a dropped row would lose a place a diver searched for
    over a detail only a map uses.

    West > east passes: that box crosses the antimeridian. South > north does not - it is the
    one ordering that carries no meaning.
    """
    if not isinstance(extent, list) or len(extent) != 4:
        return None

    try:
        west, north, east, south = (float(corner) for corner in extent)
    except TypeError, ValueError:
        return None

    # Chained on purpose: this also rejects a `nan` corner, which compares false against
    # everything and would otherwise sail through as a number.
    if not (-90 <= south <= north <= 90 and -180 <= west <= 180 and -180 <= east <= 180):
        return None

    return south, north, west, east


def _normalize_photon(feature: dict[str, Any]) -> GeocodeResult | None:
    """A Photon feature as a result, named by the place itself - or `None` for one with no
    usable position or nothing to show a human.

    `location` is what a pick saves as the place's name: the row's own name, its region and
    its country, "Ko Tao, Surat Thani Province, Thailand" - never the settlement OSM files an
    island or a peak under, which is what Nominatim's address gives. The region is `state`,
    else `county`. A row with no name of its own is named by its finest address part.
    """
    properties = feature.get("properties")
    geometry = feature.get("geometry")
    if not isinstance(properties, dict) or not isinstance(geometry, dict):
        return None

    # GeoJSON order, `[lon, lat]`, and a third element for altitude is legal.
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return None
    try:
        longitude = float(coordinates[0])
        latitude = float(coordinates[1])
    except TypeError, ValueError:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None

    name = _text(properties.get("name"))
    address = [part for part in (_text(properties.get(key)) for key in _PHOTON_ADDRESS_KEYS) if part]
    label = name or (address[0] if address else None)
    if label is None:
        return None

    country = _text(properties.get("country"))
    region = _text(properties.get("state")) or _text(properties.get("county"))
    location = _place_name(label, region, country)
    source_id = _osm_identity(properties)

    box = _photon_extent(properties.get("extent"))
    south, north, west, east = box if box is not None else (None, None, None, None)

    return GeocodeResult(
        latitude=latitude,
        longitude=longitude,
        location=location[:_LOCATION_MAX_LENGTH],
        name=name[:_NAME_MAX_LENGTH] if name else None,
        # Photon sends no licence, and what it serves is OpenStreetMap's. Byte-identical to
        # the dive-site catalog's OSM credit on purpose: the site form shows both sources
        # under one credit line that collapses repeats by string.
        attribution=_DEFAULT_ATTRIBUTION,
        country=country[:_COUNTRY_MAX_LENGTH] if country else None,
        region=region[:_REGION_MAX_LENGTH] if region else None,
        source="osm" if source_id else None,
        source_id=source_id,
        bbox_south=south,
        bbox_north=north,
        bbox_west=west,
        bbox_east=east,
    )


def _places(features: list[dict[str, Any]]) -> Iterator[GeocodeResult]:
    """The features that are places, each OSM object once, in Photon's order.

    Photon returns one object more than once in a single answer - Ko Tao twice, differing
    only in an `extra` tag; a marine park once as a `boundary` and once as a
    `leisure=nature_reserve` - and its `dedupe` parameter only touches roads. So the place
    filter judges every copy first and the dedupe runs on what survives it: a place whose
    first copy carries a refused tag still arrives through another. An object is marked seen
    only once one of its copies has produced a result, for the same reason.
    """
    seen: set[str] = set()
    for feature in features:
        properties = feature.get("properties")
        if not isinstance(properties, dict) or not _is_place(properties):
            continue
        identity = _osm_identity(properties)
        if identity is not None and identity in seen:
            continue
        result = _normalize_photon(feature)
        if result is None:
            continue
        if identity is not None:
            seen.add(identity)
        yield result


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
        name=name[:_NAME_MAX_LENGTH],
        attribution=_MARINE_ATTRIBUTION,
        country=None,
        region=None,
        source=None,
        source_id=None,
        # A sea's polygon has an extent, but this answer is about the pin rather than the
        # sea: it echoes the position asked about, and framing a map on the whole Red Sea
        # is not what the caller is looking at.
        bbox_south=None,
        bbox_north=None,
        bbox_west=None,
        bbox_east=None,
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

    key = _cache_key("reverse", settings.GEOCODER_URL, settings.GEOCODER_LANGUAGE, f"{lat}:{lon}")
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
    """Forward search - "ko tao" - returning at most `_SEARCH_RESULT_LIMIT` places.

    The query is whitespace-collapsed and lower-cased for the cache key *and* for the
    outbound call, so trivially different spellings of the same search share one cached
    answer instead of each costing a provider slot. Photon matches case-insensitively, so
    nothing is lost by asking in lower case.

    The key holds a digest of that text rather than the text itself. Unlike every other key
    in this app the discriminator here is free-form input - up to 200 characters of
    whatever a diver typed, in any script - and a fixed-width digest keeps key length off
    the caller entirely. It costs the ability to read the query out of `redis-cli --scan`,
    which is a fair trade for the same reason the coordinate keys are rounded: these are a
    cache, not a log of what people searched for.

    Either switch is read before the cache, not only on the way to the provider: the key
    carries `GEOCODER_SEARCH_URL`'s hash but not `GEOCODER_URL`'s, so an answer cached before
    an operator emptied `GEOCODER_URL` would otherwise still be served.
    """
    normalized = " ".join(query.split()).lower()
    if not normalized:
        return []

    if not settings.GEOCODER_URL or not settings.GEOCODER_SEARCH_URL:
        logger.warning("GEOCODER_URL or GEOCODER_SEARCH_URL is not configured; place search is unavailable.")
        return []

    language = _search_language()
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:32]
    key = _cache_key("search", settings.GEOCODER_SEARCH_URL, language, digest)
    cached = await _cached(key)
    if cached is not None:
        return cached

    features = await _search_photon(normalized, language)
    if features is None:
        return []

    # Bounded here, not only by what was asked for: a host that caps differently, or ignores
    # `limit`, would otherwise have every row it sent normalized, cached for a month and
    # returned. The bound on the response belongs to this app.
    #
    # Lazily, and counting only the rows that *survive*. Slicing the raw rows first is cheaper
    # to read but silently shrinks the answer - five unusable leading rows would empty a
    # search that had ten good ones behind them - while normalizing all of them first makes
    # 50,000 junk rows cost 50,000 normalizations. `islice` over a generator is both: it stops
    # at five successes and never touches the rest.
    results = list(islice(_places(features), _SEARCH_RESULT_LIMIT))
    await _store(key, results)
    return results
