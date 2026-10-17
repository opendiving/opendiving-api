"""What every outbound call to a third-party provider shares: the capped fetch, the provider
slot, and the fail-open answer cache.

The mechanism lives here and the numbers do not. Each caller passes its own timeout, deadline,
byte cap, accepted statuses and slot policy, because they are measured against one provider
each and are wrong for the others - see *"The timeouts are measured, and the geocoder's must
not be copied"* in `DECISIONS.md`.

Nothing here logs a fetch's failure. The callers do, each in its own logger and at its own
level, naming the provider and never the URL: a built URL carries a diver's typed search or an
API key, and logs get collected and kept. The cache's own warnings follow the same rule for its
keys, which carry the same search (`_loggable`).
"""

import hashlib
import json
import logging
from collections.abc import Callable, Container
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import anyio
import httpx
from anyio import sleep  # A name of this module's own, so a test can stub the slot wait and nobody else's.
from redis.exceptions import RedisError

from ..config import PROJECT_USER_AGENT
from . import cache
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
    """
    request_headers = {"User-Agent": user_agent, **(headers or {})}
    try:
        with anyio.fail_after(deadline):
            async with (
                httpx.AsyncClient(follow_redirects=follow_redirects, timeout=timeout) as client,
                client.stream("GET", url, params=params, headers=request_headers) as response,
            ):
                if response.status_code not in accept:
                    return Failed(FetchFailure.STATUS, status=response.status_code)

                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        return Failed(FetchFailure.TOO_LARGE)
                return Fetched(response.status_code, response.headers, bytes(body))
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return Failed(FetchFailure.TRANSPORT, error=type(exc).__name__)
    except TimeoutError:
        return Failed(FetchFailure.DEADLINE)


async def claim_provider_slot(key: str, max_requests: int, window_seconds: int, *, wait_seconds: float = 0) -> bool:
    """Take one slot against the instance-wide cap at `key`, or report that there is none.

    A boolean rather than the exception `enforce_rate_limit` raises: the counter belongs to the
    instance rather than to any caller, so being over it is never a caller's fault and never a
    429 - only an ordinary branch to degrade on. With `wait_seconds`, a full cap is waited on
    once before it is given up on. The limiter fails open on a Redis outage, so an instance with
    no Redis still calls out.
    """
    if await claim_rate_limit_slot(key, max_requests, window_seconds):
        return True
    if wait_seconds <= 0:
        return False
    await sleep(wait_seconds)
    return await claim_rate_limit_slot(key, max_requests, window_seconds)


def _loggable(key: str) -> str:
    """A cache key as a log line may carry it: its namespace and cache version, and a digest
    standing in for the rest, which holds a diver's typed search or the pin they dropped."""
    head = ":".join(key.split(":", 2)[:2])
    return f"{head}:#{hashlib.sha256(key.encode()).hexdigest()[:12]}"


async def read_cached[T](key: str, parse: Callable[[Any], T]) -> T | None:
    """The cached answer at `key` through `parse`, or `None` for "nothing usable cached".

    Redis absent or unreachable is a miss, not an error: the caller then asks the provider. An
    entry that will not decode or that `parse` refuses is a miss too, so a shape change behind a
    forgotten key version costs one provider call rather than a 500.
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
        return parse(json.loads(raw.decode()))
    except ValueError, TypeError:
        # `ValueError` covers pydantic's `ValidationError`, which subclasses it.
        logger.warning("Discarding an unreadable provider cache entry at %s.", _loggable(key))
        return None


async def write_cached(key: str, payload: str, *, ttl_seconds: int) -> None:
    """Store the JSON `payload` at `key`; a Redis that is absent or failing leaves it unstored."""
    if cache.client is None:
        return

    try:
        await cache.client.set(key, payload, ex=ttl_seconds)
    except RedisError as exc:
        logger.warning("Provider cache write failed (%s).", type(exc).__name__)
