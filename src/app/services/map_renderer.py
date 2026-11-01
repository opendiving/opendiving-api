"""The API's side of the map renderer: a client of the three routes it answers, and the
signature it last named.

The renderer is a command of the web image, reachable only inside the stack at
`MAP_RENDERER_URL`. `GET /signature` names how this instance draws - its drawing code and its
basemap - and the credit that basemap owes, and `POST /render` draws one map tile, a 1024x1024
WebP, saying in `X-Map-Signature` which signature drew it. Every stored tile is keyed by the
signature as well as its address (`services/map_tiles.py`), so a renderer that would draw
differently names every tile afresh.

**A stored tile waits on nothing.** The signature is held in memory per process: fetched when
the process starts, refreshed on a timer, kept through an outage, and adopted from any draw
that names another. A tile request reads it and calls out only on a miss, or while the
process has not learned it yet.

**The credit is the renderer's to say, never this API's.** The renderer resolves its basemap from
the same settings as the web app, by the web's own code, so the credit it reports is the one the
web draws beside the same tiles. A copy configured here could name the default basemap's credit
over an operator's own, which is a false credit; so there is none, and until the renderer has
said one there is no credit to give - `GET /config` answers `null`.
"""

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass
from typing import Any

import anyio
import httpx

from ..core.config import settings

logger = logging.getLogger(__name__)

_SIGNATURE = re.compile(r"[0-9a-f]{64}")

# A known signature changes only when the renderer is redeployed, so a minute is soon enough;
# an unknown one costs every tile request a round trip to learn it, so it is asked for again
# sooner.
_REFRESH_SECONDS = 60.0
_RETRY_SECONDS = 5.0
_SIGNATURE_TIMEOUT_SECONDS = 5.0

_signature: str | None = None
# Learned with the signature and replaced by each answer, an answer without one included: it is
# what the renderer last said, kept through an outage like the signature.
_attribution: str | None = None
# Whether the last refresh failed, so an outage warns once on the way in rather than per minute.
_unreachable = False
_refresher: asyncio.Task[None] | None = None


class RendererUnavailable(Exception):
    """The renderer handed back no tile or no signature: unreachable, refusing, past its
    deadline, or answering outside its contract."""


@dataclass(frozen=True, slots=True)
class Drawn:
    image: bytes
    signature: str


def current_signature() -> str | None:
    return _signature


def current_attribution() -> str | None:
    return _attribution


def adopt_signature(signature: str) -> None:
    global _signature
    if signature != _signature:
        logger.info("The map renderer's signature is now %s", signature)
        _signature = signature


def _client(*, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=settings.MAP_RENDERER_URL, timeout=timeout)


def _valid_signature(value: object) -> str | None:
    return value if isinstance(value, str) and _SIGNATURE.fullmatch(value) else None


def _valid_attribution(value: object) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


async def fetch_signature() -> str:
    """Ask the renderer for its signature and its basemap's credit, and adopt the answer.

    A credit missing or malformed fails nothing: the signature alone is what tiles need, and a
    renderer older than the credit sends none.
    """
    global _attribution
    try:
        with anyio.fail_after(_SIGNATURE_TIMEOUT_SECONDS):
            async with _client(timeout=_SIGNATURE_TIMEOUT_SECONDS) as client:
                response = await client.get("/signature")
        body = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, httpx.InvalidURL, TimeoutError, ValueError) as exc:
        raise RendererUnavailable(f"GET /signature failed: {type(exc).__name__}") from exc
    if not isinstance(body, dict):
        body = {}
    signature = _valid_signature(body.get("signature"))
    if signature is None:
        raise RendererUnavailable(f"GET /signature answered {response.status_code} with no signature")
    adopt_signature(signature)
    _attribution = _valid_attribution(body.get("attribution"))
    return signature


async def refresh_signature() -> None:
    """`fetch_signature`, keeping whatever was known when the renderer cannot be asked."""
    global _unreachable
    try:
        await fetch_signature()
    except RendererUnavailable as exc:
        if not _unreachable:
            _unreachable = True
            logger.warning("The map renderer could not be asked for its signature, keeping %s: %s", _signature, exc)
        return
    if _unreachable:
        _unreachable = False
        logger.info("The map renderer answers for its signature again")


async def _keep_signature_fresh() -> None:
    while True:
        try:
            await refresh_signature()
        except Exception:
            # Logged and survived: a refresher that died here would leave the signature to age
            # silently for the life of the process.
            logger.exception("Refreshing the map renderer's signature failed")
        await anyio.sleep(_REFRESH_SECONDS if _signature is not None else _RETRY_SECONDS)


def start_signature_refresh() -> None:
    """Begin learning the signature, in the background so a renderer that is down never holds
    up startup. Nothing to learn while map tiles are off."""
    global _refresher
    if settings.map_tiles and _refresher is None:
        _refresher = asyncio.create_task(_keep_signature_fresh())


async def stop_signature_refresh() -> None:
    global _refresher
    if _refresher is None:
        return
    _refresher.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _refresher
    _refresher = None


def _is_webp(data: bytes) -> bool:
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


async def render(body: dict[str, Any], *, timeout: float) -> Drawn:
    """Have the renderer draw `body`, within `timeout` seconds all told.

    Anything but a whole WebP naming its signature is a failure, never a partial tile.
    """
    try:
        with anyio.fail_after(timeout):
            async with _client(timeout=timeout) as client:
                response = await client.post("/render", json=body)
    # `InvalidURL` is not an `HTTPError`: it is how httpx refuses a `MAP_RENDERER_URL` the
    # settings validator let through, and that is a renderer this process cannot reach.
    except (httpx.HTTPError, httpx.InvalidURL, TimeoutError) as exc:
        raise RendererUnavailable(f"POST /render failed: {type(exc).__name__}") from exc
    if response.status_code == 400:
        # The body is this API's own, so a refusal is a contract mismatch, not a bad request.
        logger.error("The map renderer refused a body this API built: %s", response.text[:200])
    if response.status_code != 200:
        raise RendererUnavailable(f"POST /render answered {response.status_code}")
    signature = _valid_signature(response.headers.get("x-map-signature"))
    content_type = response.headers.get("content-type", "").partition(";")[0].strip()
    if signature is None or content_type != "image/webp" or not _is_webp(response.content):
        raise RendererUnavailable("POST /render answered 200 with something other than a signed WebP")
    return Drawn(image=response.content, signature=signature)
