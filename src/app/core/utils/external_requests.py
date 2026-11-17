"""What this instance asks of other services through `outbound`, counted per UTC day: the
services and kinds of request there are, how an event ended, and the Redis buffer the worker
moves into `daily_external_request`.

Only the shared outbound helper counts. Google sign-in's token exchange and the MCP server's
fetch of a client's metadata document each use a client of their own and are not counted: the
first has no limit to watch, the second goes to whatever host a connecting client names.

A count is buffered rather than written at the call because a request is out with no
database transaction open, so writing it there would reopen one, or take a second pooled
connection, for every request and every cache hit. Each row of the buffer is one key holding
its count and both durations, so an eviction drops the three together, and a move takes a key
by deleting it, so a move that dies before its commit loses what it took and never counts it
twice - see `DECISIONS.md`.

Nothing but the day, the service, the kind and the outcome is stored with a count: no URL, no
query, no account.
"""

import contextlib
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal, get_args

import anyio
from redis.exceptions import RedisError

from ..config import settings
from . import cache

logger = logging.getLogger(__name__)

# The closed vocabulary. A service is the slug its module already calls it; a kind is what was
# asked of it, meaningful only beside its service. Two call paths share a kind when they ask the
# same question of the same service.
OpenMeteoKind = Literal["forecast", "sea", "temperatures", "weather"]
GeocoderRequest = tuple[Literal["nominatim"], Literal["reverse"]] | tuple[Literal["photon"], Literal["search"]]
WormsKind = Literal["by-name", "by-vernacular", "name-part", "record", "synonyms", "vernaculars"]
WikidataKind = Literal["by-aphia-id", "entities", "search"]
CommonsKind = Literal["category-files", "file-categories", "image-info", "photo", "photo-preview"]
SpeciesRequest = (
    tuple[Literal["worms"], WormsKind]
    | tuple[Literal["wikidata"], WikidataKind]
    | tuple[Literal["commons"], CommonsKind]
)
ExternalRequest = (
    tuple[Literal["open-meteo"], OpenMeteoKind]
    | GeocoderRequest
    | SpeciesRequest
    | tuple[Literal["google"], Literal["avatar"]]
)


def _declared() -> dict[str, tuple[str, ...]]:
    """Every service and its kinds, read off `ExternalRequest` so the two cannot disagree."""
    kinds: dict[str, set[str]] = {}
    for member in get_args(ExternalRequest):
        provider_type, kind_type = get_args(member)
        (provider,) = get_args(provider_type)
        kinds.setdefault(provider, set()).update(get_args(kind_type))
    return {provider: tuple(sorted(kinds[provider])) for provider in sorted(kinds)}


DECLARED = _declared()


class Outcome(StrEnum):
    OK = "ok"
    RATE_LIMITED = "rate_limited"
    STATUS = "status"
    TOO_LARGE = "too_large"
    DEADLINE = "deadline"
    TRANSPORT = "transport"
    CANCELLED = "cancelled"
    # The two that sent nothing.
    SKIPPED = "skipped"
    CACHE_HIT = "cache_hit"


# The instance's own cap on each service, as its slot claims pass it to the limiter. Read at
# call time, so an operator's setting is what both the claim and the admin's figures see.
# Indexed rather than `.get`-ed by the claim: a service gaining a claim without a pair here is a
# `KeyError` at its first call, where a default would be a third party nobody throttles.
_LIMITS: dict[str, Callable[[], tuple[int, int]]] = {
    "open-meteo": lambda: (
        settings.DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_REQUESTS,
        settings.DIVE_CONDITIONS_OPEN_METEO_RATE_LIMIT_WINDOW_SECONDS,
    ),
    # Both geocoders share the one configured limit, each on its own counter.
    "nominatim": lambda: (
        settings.GEOCODER_PROVIDER_RATE_LIMIT_REQUESTS,
        settings.GEOCODER_PROVIDER_RATE_LIMIT_WINDOW_SECONDS,
    ),
    "photon": lambda: (
        settings.GEOCODER_PROVIDER_RATE_LIMIT_REQUESTS,
        settings.GEOCODER_PROVIDER_RATE_LIMIT_WINDOW_SECONDS,
    ),
    "worms": lambda: (settings.SPECIES_WORMS_RATE_LIMIT_REQUESTS, settings.SPECIES_WORMS_RATE_LIMIT_WINDOW_SECONDS),
    "wikidata": lambda: (
        settings.SPECIES_WIKIDATA_RATE_LIMIT_REQUESTS,
        settings.SPECIES_WIKIDATA_RATE_LIMIT_WINDOW_SECONDS,
    ),
    "commons": lambda: (
        settings.SPECIES_COMMONS_RATE_LIMIT_REQUESTS,
        settings.SPECIES_COMMONS_RATE_LIMIT_WINDOW_SECONDS,
    ),
}


def provider_limit(provider: str) -> tuple[int, int] | None:
    """`(requests, window_seconds)` for a service whose requests claim a slot, else `None`."""
    limit = _LIMITS.get(provider)
    return limit() if limit is not None else None


def claimed_limit(provider: str) -> tuple[int, int]:
    return _LIMITS[provider]()


_KEY_PREFIX = "external-requests"
# Long enough to outlive a worker that is down for a day; finite, so a buffer nothing moves
# does not grow for good.
_BUFFER_TTL_SECONDS = 7 * 24 * 3600
# The most counting may hold a caller, or delay a cancellation, when Redis is slow or hung.
_COUNT_TIMEOUT_SECONDS = 0.25

# One round trip for every key a call counts. A row is "count total slowest", one string, so
# the three are evicted together and a take is one `GETDEL`.
_COUNT_SCRIPT = """-- external-requests:count
local duration = tonumber(ARGV[1])
for _, key in ipairs(KEYS) do
  local count, total, slowest = string.match(redis.call('GET', key) or '', '^(%d+) (%d+) (%d+)$')
  if count == nil then
    count, total, slowest = 0, 0, 0
  end
  slowest = math.max(tonumber(slowest), duration)
  redis.call('SET', key, string.format('%d %d %d', tonumber(count) + 1, tonumber(total) + duration, slowest),
    'EX', ARGV[2])
end
return #KEYS
"""


def _key(day: date, provider: str, kind: str, outcome: Outcome) -> str:
    return f"{_KEY_PREFIX}:{day.isoformat()}:{provider}:{kind}:{outcome.value}"


async def count(requests: Iterable[ExternalRequest], outcome: Outcome, duration_ms: int = 0) -> None:
    """Add one event under each of `requests`, on today's UTC day.

    Never raises and logs nothing: counting must not change what a request answers, and an
    outage would otherwise log once per request. Shielded and bounded, so a cancelled request
    is still counted and its cancellation is held for at most `_COUNT_TIMEOUT_SECONDS`.
    """
    client = cache.client
    if client is None:
        return
    today = datetime.now(UTC).date()
    keys = [_key(today, provider, kind, outcome) for provider, kind in requests]
    if not keys:
        return
    with anyio.CancelScope(shield=True), anyio.move_on_after(_COUNT_TIMEOUT_SECONDS):
        # Anything, not only `RedisError`: a client bound to another event loop raises its own.
        with contextlib.suppress(Exception):
            await client.eval(_COUNT_SCRIPT, len(keys), *keys, max(0, duration_ms), _BUFFER_TTL_SECONDS)  # type: ignore[misc]


@dataclass(frozen=True, slots=True)
class Buffered:
    day: date
    provider: str
    kind: str
    outcome: str
    count: int
    duration_ms_total: int
    duration_ms_max: int


def _parse(key: str, value: bytes | None) -> Buffered | None:
    """A taken row, or `None` for a key or value this build cannot read or one counting nothing."""
    if value is None:
        return None
    parts = key.split(":")
    figures = value.decode(errors="replace").split(" ")
    if len(parts) != 5 or len(figures) != 3:
        return None
    try:
        day = date.fromisoformat(parts[1])
        count_, total, slowest = (int(figure) for figure in figures)
    except ValueError:
        return None
    if count_ <= 0:
        return None
    return Buffered(day, parts[2], parts[3], parts[4], count_, total, slowest)


async def take_buffered() -> list[Buffered]:
    """Remove every buffered row from Redis and return it.

    Each key is taken by `GETDEL`, so an event counted during the take lands in a fresh key
    for the next move, and a key `SCAN` returns twice is taken once. A Redis that fails part
    way returns what was already taken, which is gone from the buffer and must be written.
    """
    client = cache.client
    if client is None:
        return []
    taken: list[Buffered] = []
    try:
        # A dict, as an ordered set: `SCAN` may return a key twice, and a second `GETDEL` could
        # take a count made since the first, a second row of one identity for the upsert.
        keys: dict[str, None] = {}
        cursor = 0
        while True:
            cursor, batch = await client.scan(cursor, match=f"{_KEY_PREFIX}:*", count=100)
            keys.update(dict.fromkeys(key.decode() if isinstance(key, bytes) else key for key in batch))
            if not cursor:
                break
        for key in keys:
            row = _parse(key, await client.getdel(key))
            if row is not None:
                taken.append(row)
    except RedisError as exc:
        logger.warning("Taking the external request counts stopped part way (%s).", type(exc).__name__)
    return taken
