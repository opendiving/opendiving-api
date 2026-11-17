"""What every outbound call to a third-party provider shares: the capped fetch, the provider
slot, and the fail-open answer cache.

The mechanism lives here and the numbers do not. Each caller passes its own timeout, deadline,
byte cap, accepted statuses and slot policy, because they are measured against one provider
each and are wrong for the others - see *"The timeouts are measured, and the geocoder's must
not be copied"* in `DECISIONS.md`.

Every fetch, every refused slot and every cache hit is counted under the service and kind its
caller names (`external_requests`): how the event ended, and for a sent request how long it
took. That is all it records about one. Nothing here logs a fetch's failure. The callers do,
each in its own logger and at its own level, naming the provider and never the URL: a built URL
carries a diver's typed search or an API key, and logs get collected and kept. The cache's own
warnings follow the same rule for its keys, which carry the same search (`_loggable`).
"""

import hashlib
import json
import logging
from collections.abc import Callable, Container, Sequence
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import Any

import anyio
import httpx
from anyio import sleep  # A name of this module's own, so a test can stub the slot wait and nobody else's.
from redis.exceptions import RedisError

from ..config import PROJECT_USER_AGENT
from . import cache, external_requests
from .external_requests import ExternalRequest, Outcome
from .rate_limit import claim_rate_limit_slot

logger = logging.getLogger(__name__)


class FetchFailure(StrEnum):
    STATUS = "status"
    TOO_LARGE = "too_large"
    DEADLINE = "deadline"
    TRANSPORT = "transport"


@dataclass(frozen=True, slots=True)
class Fetched:
    status: int
    headers: httpx.Headers
    body: bytes


@dataclass(frozen=True, slots=True)
class Failed:
    """Why a fetch produced no body. `status` is set for `STATUS`, `error` for `TRANSPORT`."""

    reason: FetchFailure
    status: int | None = None
    error: str | None = None

    def __str__(self) -> str:
        match self.reason:
            case FetchFailure.STATUS:
                return f"answered {self.status}"
            case FetchFailure.TOO_LARGE:
                return "answered over the byte cap"
            case FetchFailure.DEADLINE:
                return "missed its deadline"
            case FetchFailure.TRANSPORT:
                return f"failed ({self.error})"


async def fetch_capped(
    url: str,
    *,
    counted_as: ExternalRequest,
    timeout: float,
    deadline: float,
    max_bytes: int,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    user_agent: str = PROJECT_USER_AGENT,
    accept: Container[int] = (200,),
    follow_redirects: bool = False,
) -> Fetched | Failed:
    """GET `url`, returning the whole body or the one reason there is none.

    `timeout` bounds each socket read, which is not the same as bounding the call: a host that
    dribbles one byte every few seconds never trips it. `deadline` is the bound that holds, and
    `max_bytes` is the matching bound on how much such a host can make this process buffer. A
    status outside `accept` fails before the body is read.

    The `User-Agent` is always sent. Nominatim and Wikimedia block empty and generic ones, and a
    server never gets the browser's for free.

    `httpx.InvalidURL` is caught beside `HTTPError` because it descends from `Exception` instead,
    so a merely malformed URL setting - a typo'd port - would otherwise escape as a 500.

    A client per call, deliberately, and knowingly against httpx's advice: every caller is held
    to a few requests a second by its provider cap or runs once per sign-up, so a pooled
    connection would sit idle far longer than any keep-alive, and a module-level client would
    need lifespan wiring to be closed. An integration with real throughput should not use this.

    It is counted once, when it ends, under `counted_as`: a cancellation from outside as
    `cancelled`, re-raised unchanged once counted.
    """
    started = monotonic()
    try:
        fetched, outcome = await _fetch(
            url,
            timeout=timeout,
            deadline=deadline,
            max_bytes=max_bytes,
            params=params,
            headers={"User-Agent": user_agent, **(headers or {})},
            accept=accept,
            follow_redirects=follow_redirects,
        )
    except anyio.get_cancelled_exc_class():
        await external_requests.count([counted_as], Outcome.CANCELLED, _elapsed_ms(started))
        raise
    await external_requests.count([counted_as], outcome, _elapsed_ms(started))
    return fetched


def _elapsed_ms(started: float) -> int:
    return round((monotonic() - started) * 1000)


async def _fetch(
    url: str,
    *,
    timeout: float,
    deadline: float,
    max_bytes: int,
    params: dict[str, Any] | None,
    headers: dict[str, str],
    accept: Container[int],
    follow_redirects: bool,
) -> tuple[Fetched | Failed, Outcome]:
    try:
        with anyio.fail_after(deadline):
            async with (
                httpx.AsyncClient(follow_redirects=follow_redirects, timeout=timeout) as client,
                client.stream("GET", url, params=params, headers=headers) as response,
            ):
                if response.status_code not in accept:
                    outcome = Outcome.RATE_LIMITED if response.status_code == 429 else Outcome.STATUS
                    return Failed(FetchFailure.STATUS, status=response.status_code), outcome

                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        return Failed(FetchFailure.TOO_LARGE), Outcome.TOO_LARGE
                return Fetched(response.status_code, response.headers, bytes(body)), Outcome.OK
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # Each caller reads an httpx timeout its own way; counted, every one is a timeout.
        outcome = Outcome.DEADLINE if isinstance(exc, httpx.TimeoutException) else Outcome.TRANSPORT
        return Failed(FetchFailure.TRANSPORT, error=type(exc).__name__), outcome
    except TimeoutError:
        return Failed(FetchFailure.DEADLINE), Outcome.DEADLINE


async def claim_provider_slot(key: str, *, counted_as: ExternalRequest, wait_seconds: float = 0) -> bool:
    """Take one slot against the instance-wide cap at `key`, or report that there is none.

    The cap is the service's own (`external_requests.claimed_limit`), so the figure the admin
    reads is the one the limiter applies. A boolean rather than the exception
    `enforce_rate_limit` raises: the counter belongs to the instance rather than to any caller,
    so being over it is never a caller's fault and never a 429 - only an ordinary branch to
    degrade on. With `wait_seconds`, a full cap is waited on once before it is given up on, and
    only giving up is counted, as `skipped`. The limiter fails open on a Redis outage, so an
    instance with no Redis still calls out.
    """
    max_requests, window_seconds = external_requests.claimed_limit(counted_as[0])
    if await claim_rate_limit_slot(key, max_requests, window_seconds):
        return True
    if wait_seconds > 0:
        await sleep(wait_seconds)
        if await claim_rate_limit_slot(key, max_requests, window_seconds):
            return True
    await external_requests.count([counted_as], Outcome.SKIPPED)
    return False


def _loggable(key: str) -> str:
    """A cache key as a log line may carry it: its namespace and cache version, and a digest
    standing in for the rest, which holds a diver's typed search or the pin they dropped."""
    head = ":".join(key.split(":", 2)[:2])
    return f"{head}:#{hashlib.sha256(key.encode()).hexdigest()[:12]}"


async def read_cached[T](key: str, parse: Callable[[Any], T], *, counted_as: Sequence[ExternalRequest]) -> T | None:
    """The cached answer at `key` through `parse`, or `None` for "nothing usable cached".

    Redis absent or unreachable is a miss, not an error: the caller then asks the provider. An
    entry that will not decode or that `parse` refuses is a miss too, so a shape change behind a
    forgotten key version costs one provider call rather than a 500.

    A hit is counted under each of `counted_as`: every request a miss on `key` would have sent.
    """
    if cache.client is None:
        return None

    try:
        raw = await cache.client.get(key)
    except RedisError as exc:
        logger.warning("Provider cache read failed (%s); asking the provider instead.", type(exc).__name__)
        return None

    if raw is None:
        return None

    try:
        parsed = parse(json.loads(raw.decode()))
    except ValueError, TypeError:
        # `ValueError` covers pydantic's `ValidationError`, which subclasses it.
        logger.warning("Discarding an unreadable provider cache entry at %s.", _loggable(key))
        return None
    await external_requests.count(counted_as, Outcome.CACHE_HIT)
    return parsed


async def write_cached(key: str, payload: str, *, ttl_seconds: int) -> None:
    """Store the JSON `payload` at `key`; a Redis that is absent or failing leaves it unstored."""
    if cache.client is None:
        return

    try:
        await cache.client.set(key, payload, ex=ttl_seconds)
    except RedisError as exc:
        logger.warning("Provider cache write failed (%s).", type(exc).__name__)
