"""Map tiles: which squares of the map there are, and finding or drawing one.

**A tile is a square of the Web Mercator grid, drawn once for the whole instance.** `z/x/y`
in one theme, with nothing of any record in it: the web composes a card's or a page head's
map from the tiles covering its frame and draws the pins itself. So the store carries no
account, and one tile serves every record and every account whose map covers it. What that
leaks is timing - a stored tile answers faster than a drawn one, so an account can learn
that someone here was shown a region in the last `UNSERVED_RETENTION` - and the request
limit slows such a probe without removing it.

**The zoom stops at `MAX_ZOOM`.** No card or page head is fitted deeper, so a deeper tile,
like one outside the grid, is refused before anything reaches the renderer, and the store is
bounded whatever anyone asks.

**Concurrent misses share one draw**, across every API process: the first claims it in
Redis, the rest wait for its row. The claimant draws in a task of its own, so a client that
disconnects mid-draw still leaves the tile stored.
"""

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
from redis.exceptions import RedisError
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db.database import local_session, release_read_transaction
from ..core.utils import cache
from ..core.utils.rate_limit import enforce_rate_limit
from ..models.map_tile import MapTile
from ..schemas.map_tile import MapTheme
from . import blob_store, map_renderer

logger = logging.getLogger(__name__)

BLOB_KIND = "map-tiles"
CONTENT_TYPE = "image/webp"

# The web's `MAX_FIT_ZOOM`, past which the renderer refuses too: a change here is a change to
# both, and to the renderer's contract.
MAX_ZOOM = 9

# A tile no request has found for this long is deleted by the worker, and drawn again by the
# next one that asks.
UNSERVED_RETENTION = timedelta(days=30)
# How stale `last_served_at` may be before a request moves it, so reads stay reads.
_SERVED_REFRESH = timedelta(days=1)

# How often a request waiting on another's draw looks for its row.
_WAIT_POLL_SECONDS = 0.25
# A claim outlives its draw's deadline by this much, for the store after it, so it cannot
# lapse while its claimant is still writing.
_CLAIM_MARGIN_SECONDS = 15.0

PURGE_BATCH_SIZE = 500

# Claimants' draws, held so the event loop does not collect one whose request has gone.
_pending_draws: set[asyncio.Task[Any]] = set()


class MapTilesOff(Exception):
    """This instance names no renderer."""


class MapTileUnavailable(Exception):
    """No tile could be had in time: the renderer failed, refused or ran out of time."""


@dataclass(frozen=True, slots=True)
class Tile:
    theme: MapTheme
    z: int
    x: int
    y: int

    def body(self) -> dict[str, Any]:
        """What the renderer is sent for this tile: the contract's body, and no other field."""
        return {"kind": "tile", "theme": self.theme.value, "z": self.z, "x": self.x, "y": self.y}


def tile_at(theme: str, z: int, x: int, y: int) -> Tile | None:
    """The tile those path segments name, or `None` for a theme there is none of, a zoom past
    `MAX_ZOOM`, or a square outside the grid at that zoom."""
    try:
        parsed = MapTheme(theme)
    except ValueError:
        return None
    if not (0 <= z <= MAX_ZOOM and 0 <= x < 2**z and 0 <= y < 2**z):
        return None
    return Tile(parsed, z, x, y)


@dataclass(frozen=True, slots=True)
class ServedMapTile:
    """What a request is answered with. `data` is `None` when the caller already holds it."""

    sha256: str
    data: bytes | None


@dataclass(frozen=True, slots=True)
class _Stored:
    id: int
    storage_key: str
    sha256: str
    last_served_at: datetime


def etag(sha256: str) -> str:
    return f'"{sha256}"'


async def _find(db: AsyncSession, *, signature: str, tile: Tile) -> _Stored | None:
    row = (
        await db.execute(
            select(MapTile.id, MapTile.storage_key, MapTile.sha256, MapTile.last_served_at).where(
                MapTile.signature == signature,
                MapTile.theme == tile.theme.value,
                MapTile.z == tile.z,
                MapTile.x == tile.x,
                MapTile.y == tile.y,
            )
        )
    ).one_or_none()
    return None if row is None else _Stored(row.id, row.storage_key, row.sha256, row.last_served_at)


async def _serve_stored(db: AsyncSession, stored: _Stored, *, if_none_match: str | None) -> ServedMapTile | None:
    """A found row's tile, or `None` when its file is gone and the row with it.

    A row whose file is gone is a miss, not the 500 a missing upload is: the worker deletes
    the row and then the file, so a request can read a row the worker is about to unlink.
    """
    now = datetime.now(UTC)
    if stored.last_served_at < now - _SERVED_REFRESH:
        await db.execute(
            update(MapTile)
            .where(MapTile.id == stored.id, MapTile.last_served_at < now - _SERVED_REFRESH)
            .values(last_served_at=now)
        )
        await db.commit()
    await release_read_transaction(db)

    if if_none_match == etag(stored.sha256):
        return ServedMapTile(sha256=stored.sha256, data=None)
    try:
        data = await blob_store.get(stored.storage_key)
    except blob_store.BlobMissingError:
        await db.execute(delete(MapTile).where(MapTile.id == stored.id, MapTile.storage_key == stored.storage_key))
        await db.commit()
        return None
    return ServedMapTile(sha256=stored.sha256, data=data)


def _claim_key(signature: str, tile: Tile) -> str:
    return f"map-tile:claim:{signature}:{tile.theme.value}:{tile.z}:{tile.x}:{tile.y}"


async def _claim(key: str, *, seconds: float) -> bool:
    """Whether this request draws the tile. Fails open, as the rate limiter does: without
    Redis every miss draws for itself, which costs renders and never a tile."""
    if cache.client is None:
        return True
    try:
        return bool(await cache.client.set(key, "1", nx=True, px=int((seconds + _CLAIM_MARGIN_SECONDS) * 1000)))
    except RedisError:
        return True


async def _release(key: str) -> None:
    """Let a claim go. Shielded, since the usual reason to is a request being cancelled - and
    a claim left standing holds every other request for that tile until it lapses."""
    if cache.client is None:
        return
    with anyio.CancelScope(shield=True):
        try:
            await cache.client.delete(key)
        except RedisError:
            logger.warning("A map tile's draw claim could not be released: %s", key)


async def _store(*, signature: str, tile: Tile, image: bytes, sha256: str) -> None:
    """The file first, then the row, in a session of the draw's own: the request's may be gone."""
    key = blob_store.new_key(BLOB_KIND, sha256=sha256)
    await blob_store.put(key, image)
    now = datetime.now(UTC)
    async with local_session() as db:
        inserted = (
            await db.execute(
                pg_insert(MapTile)
                .values(
                    signature=signature,
                    theme=tile.theme.value,
                    z=tile.z,
                    x=tile.x,
                    y=tile.y,
                    storage_key=key,
                    sha256=sha256,
                    created_at=now,
                    last_served_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[MapTile.signature, MapTile.theme, MapTile.z, MapTile.x, MapTile.y]
                )
                .returning(MapTile.id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            # Another writer stored the same tile first - a lapsed claim, or no Redis.
            blob_store.delete_after_commit(db, key)
        await db.commit()


async def _draw_and_store(*, claim: str, tile: Tile, signature: str, timeout: float) -> ServedMapTile:
    try:
        try:
            drawn = await map_renderer.render(tile.body(), timeout=timeout)
        except map_renderer.RendererUnavailable as exc:
            logger.warning("A map tile was not drawn: %s", exc)
            raise MapTileUnavailable(str(exc)) from exc
        # A renderer redeployed mid-request drew with a signature this one did not ask under:
        # the tile is stored under the signature it was drawn with, which becomes current.
        if drawn.signature != signature:
            map_renderer.adopt_signature(drawn.signature)
        sha256 = hashlib.sha256(drawn.image).hexdigest()
        await _store(signature=drawn.signature, tile=tile, image=drawn.image, sha256=sha256)
        return ServedMapTile(sha256=sha256, data=drawn.image)
    finally:
        await _release(claim)


def _retrieve(task: asyncio.Task[Any]) -> None:
    """Collect an abandoned draw's outcome, so a failure logs nothing beyond its warning."""
    if not task.cancelled():
        task.exception()


async def _draw_as_claimant(
    db: AsyncSession, *, claim: str, user_id: int, tile: Tile, signature: str, timeout: float
) -> ServedMapTile | None:
    """Draw the tile this request has claimed, or let the claim go and answer `None` when
    another request stored it between this one's miss and its claim.

    Every way out before the draw's own task holds the claim lets it go here.
    """
    try:
        stored = await _find(db, signature=signature, tile=tile)
        await release_read_transaction(db)
        if stored is None:
            await enforce_rate_limit(
                f"map-tile:draws:user:{user_id}",
                settings.MAP_RENDERER_DRAW_LIMIT_PER_USER,
                settings.MAP_RENDERER_LIMIT_WINDOW_SECONDS,
            )
    except BaseException:
        await _release(claim)
        raise
    if stored is not None:
        await _release(claim)
        return None
    task = asyncio.create_task(_draw_and_store(claim=claim, tile=tile, signature=signature, timeout=timeout))
    _pending_draws.add(task)
    task.add_done_callback(_pending_draws.discard)
    task.add_done_callback(_retrieve)
    # Shielded: a client that disconnects cancels this request, never the draw.
    return await asyncio.shield(task)


async def _await_pending_draws() -> None:
    """Wait for draws whose requests have gone. **The suite is the only caller.**"""
    while outstanding := [task for task in _pending_draws if not task.done()]:
        await asyncio.gather(*outstanding, return_exceptions=True)


async def find_or_draw(db: AsyncSession, *, user_id: int, tile: Tile, if_none_match: str | None) -> ServedMapTile:
    """`tile` under the renderer's current signature: stored, or drawn now within
    `MAP_RENDERER_TIMEOUT`, on behalf of the account `user_id`, whose limits it counts against.

    Every request counts against the request limit before anything is read, a stored tile and
    a wait on another's draw included; only a draw this request starts counts against the draw
    limit. The transaction is released before anything outbound and before any wait, and the
    draw writes through a session of its own.
    """
    if not settings.map_tiles:
        raise MapTilesOff
    await enforce_rate_limit(
        f"map-tile:requests:user:{user_id}",
        settings.MAP_RENDERER_REQUEST_LIMIT_PER_USER,
        settings.MAP_RENDERER_LIMIT_WINDOW_SECONDS,
    )
    signature = map_renderer.current_signature()
    if signature is None:
        await release_read_transaction(db)
        try:
            signature = await map_renderer.fetch_signature()
        except map_renderer.RendererUnavailable as exc:
            raise MapTileUnavailable(str(exc)) from exc
    claim = _claim_key(signature, tile)
    deadline = anyio.current_time() + settings.MAP_RENDERER_TIMEOUT

    while True:
        stored = await _find(db, signature=signature, tile=tile)
        if stored is not None:
            served = await _serve_stored(db, stored, if_none_match=if_none_match)
            if served is not None:
                return served
        await release_read_transaction(db)

        remaining = deadline - anyio.current_time()
        if remaining <= 0:
            raise MapTileUnavailable("no tile was stored before the deadline")
        if await _claim(claim, seconds=remaining):
            drawn = await _draw_as_claimant(
                db, claim=claim, user_id=user_id, tile=tile, signature=signature, timeout=remaining
            )
            if drawn is not None:
                return drawn
            continue
        await anyio.sleep(min(_WAIT_POLL_SECONDS, remaining))


async def purge_unserved(db: AsyncSession, *, cutoff: datetime, limit: int) -> int:
    """Delete up to `limit` tiles last served before `cutoff`, their files after the commit,
    and return how many went.

    The `DELETE` repeats the selection's predicate, so a tile a request found between the two
    is kept rather than deleted under it.
    """
    batch = select(MapTile.id).where(MapTile.last_served_at < cutoff).limit(limit)
    keys = list(
        (
            await db.execute(
                delete(MapTile)
                .where(MapTile.id.in_(batch), MapTile.last_served_at < cutoff)
                .returning(MapTile.storage_key)
            )
        )
        .scalars()
        .all()
    )
    blob_store.delete_after_commit(db, keys)
    await db.commit()
    return len(keys)
