"""Dive, trip and dive site map pictures: what names one, and finding or drawing it.

**Drawn on first view, not on save.** A card asks for its record's picture; the route builds
the record's positional subset - the payload - from the database, and this module digests it
with the renderer's signature and serves the row stored under that digest, or has the
renderer draw it there and then. Whatever moves a record's places names a different picture,
so a stale one is never served and no write path knows this module exists.

**The payload and the digest come from one function** - `dive_payload`, `trip_payload` -
because a field the renderer reads and the digest leaves out would be a picture that never
updates. The kind is in both, since it decides how the renderer reads the fields; the theme
keys the row beside the digest instead; names are in neither, the renderer drawing no text.
A dive site has no kind of its own: it is drawn as a one-site dive with no fix.

**Concurrent misses share one draw**, across every API process: the first claims it in
Redis, the rest wait for its row. The claimant draws in a task of its own, so a client that
disconnects mid-draw still leaves the picture stored.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping
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
from ..models.map_picture import MapPicture
from ..schemas.map_picture import MapTheme
from . import blob_store, map_renderer

logger = logging.getLogger(__name__)

BLOB_KIND = "map-pictures"
CONTENT_TYPE = "image/webp"

# A picture no request has found for this long is deleted by the worker, and drawn again by
# the next one that asks.
UNSERVED_RETENTION = timedelta(days=30)
# How stale `last_served_at` may be before a request moves it, so reads stay reads.
_SERVED_REFRESH = timedelta(days=1)

# How often a request waiting on another's draw looks for its row.
_WAIT_POLL_SECONDS = 0.25
# A claim outlives its draw's deadline by this much, for the store after it, so it cannot
# lapse while its claimant is still writing.
_CLAIM_MARGIN_SECONDS = 15.0

PURGE_BATCH_SIZE = 500

FIX_FIELDS = ("entry_latitude", "entry_longitude", "exit_latitude", "exit_longitude")
_SITE_FIELDS = ("latitude", "longitude")
_LOCATION_FIELDS = ("latitude", "longitude", "bbox_south", "bbox_north", "bbox_west", "bbox_east")

# Claimants' draws, held so the event loop does not collect one whose request has gone.
_pending_draws: set[asyncio.Task[Any]] = set()


class MapPicturesOff(Exception):
    """This instance names no renderer."""


class MapPictureUnavailable(Exception):
    """No picture could be had in time: the renderer failed, refused or ran out of time."""


@dataclass(frozen=True, slots=True)
class ServedMapPicture:
    """What a request is answered with. `data` is `None` when the caller already holds it."""

    digest: str
    sha256: str
    data: bytes | None


@dataclass(frozen=True, slots=True)
class _Stored:
    id: int
    storage_key: str
    sha256: str
    last_served_at: datetime


def _coordinate(value: Any) -> float | None:
    return None if value is None else float(value)


def _placed(latitude: float | None, longitude: float | None) -> bool:
    return latitude is not None and longitude is not None


def dive_payload(dive: Mapping[str, Any]) -> dict[str, Any] | None:
    """What the renderer is sent for a dive, from `DiveRead`'s names: every site's position in
    order and the two fixes. `None` when nothing in it has a position, which is when the
    dive's card shows water rather than a map."""
    sites = [{field: _coordinate(site.get(field)) for field in _SITE_FIELDS} for site in dive.get("dive_sites") or []]
    fixes = {field: _coordinate(dive.get(field)) for field in FIX_FIELDS}
    if not (
        any(_placed(site["latitude"], site["longitude"]) for site in sites)
        or _placed(fixes["entry_latitude"], fixes["entry_longitude"])
        or _placed(fixes["exit_latitude"], fixes["exit_longitude"])
    ):
        return None
    return {"kind": "dive", "dive_sites": sites, **fixes}


def dive_site_payload(site: Mapping[str, Any]) -> dict[str, Any] | None:
    """What the renderer is sent for a dive site: a one-site dive's payload with no fix, built
    by `dive_payload` itself, so a site and a dive there recording no fix name one picture and
    share it. `None` for a site with no position."""
    return dive_payload({"dive_sites": [site]})


def _location(location: Mapping[str, Any] | None) -> dict[str, float | None] | None:
    return None if location is None else {field: _coordinate(location.get(field)) for field in _LOCATION_FIELDS}


def trip_payload(trip: Mapping[str, Any]) -> dict[str, Any]:
    """What the renderer is sent for a trip, from `TripRead`'s names: each part's place, its
    position and box, in order. Never `None`: a trip with no place is drawn as the whole
    world, as its card has always shown it."""
    parts = [{"location": _location(part.get("location"))} for part in trip.get("parts") or []]
    return {"kind": "trip", "parts": parts}


def digest(payload: Mapping[str, Any], signature: str) -> str:
    canonical = json.dumps({"payload": payload, "signature": signature}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def current_digest(payload: Mapping[str, Any] | None) -> str | None:
    """The name a record's picture has right now, or `None` while this instance draws none or
    has not yet learned its renderer's signature."""
    signature = map_renderer.current_signature()
    if payload is None or not settings.map_pictures or signature is None:
        return None
    return digest(payload, signature)


def dive_map_picture(dive: Mapping[str, Any]) -> str | None:
    return current_digest(dive_payload(dive))


def trip_map_picture(trip: Mapping[str, Any]) -> str | None:
    return current_digest(trip_payload(trip))


def dive_site_map_picture(site: Mapping[str, Any]) -> str | None:
    return current_digest(dive_site_payload(site))


def etag(sha256: str) -> str:
    return f'"{sha256}"'


async def _find(db: AsyncSession, *, user_id: int, digest: str, theme: MapTheme) -> _Stored | None:
    row = (
        await db.execute(
            select(MapPicture.id, MapPicture.storage_key, MapPicture.sha256, MapPicture.last_served_at).where(
                MapPicture.user_id == user_id, MapPicture.digest == digest, MapPicture.theme == theme.value
            )
        )
    ).one_or_none()
    return None if row is None else _Stored(row.id, row.storage_key, row.sha256, row.last_served_at)


async def _serve_stored(
    db: AsyncSession, stored: _Stored, *, digest: str, if_none_match: str | None
) -> ServedMapPicture | None:
    """A found row's picture, or `None` when its file is gone and the row with it.

    A row whose file is gone is a miss, not the 500 a missing upload is: the worker deletes
    the row and then the file, so a request can read a row the worker is about to unlink.
    """
    now = datetime.now(UTC)
    if stored.last_served_at < now - _SERVED_REFRESH:
        await db.execute(
            update(MapPicture)
            .where(MapPicture.id == stored.id, MapPicture.last_served_at < now - _SERVED_REFRESH)
            .values(last_served_at=now)
        )
        await db.commit()
    await release_read_transaction(db)

    if if_none_match == etag(stored.sha256):
        return ServedMapPicture(digest=digest, sha256=stored.sha256, data=None)
    try:
        data = await blob_store.get(stored.storage_key)
    except blob_store.BlobMissingError:
        await db.execute(
            delete(MapPicture).where(MapPicture.id == stored.id, MapPicture.storage_key == stored.storage_key)
        )
        await db.commit()
        return None
    return ServedMapPicture(digest=digest, sha256=stored.sha256, data=data)


def _claim_key(user_id: int, digest: str, theme: MapTheme) -> str:
    return f"map-picture:draw:{user_id}:{digest}:{theme.value}"


async def _claim(key: str, *, seconds: float) -> bool:
    """Whether this request draws the picture. Fails open, as the rate limiter does: without
    Redis every miss draws for itself, which costs renders and never a picture."""
    if cache.client is None:
        return True
    try:
        return bool(await cache.client.set(key, "1", nx=True, px=int((seconds + _CLAIM_MARGIN_SECONDS) * 1000)))
    except RedisError:
        return True


async def _release(key: str) -> None:
    """Let a claim go. Shielded, since the usual reason to is a request being cancelled - and
    a claim left standing holds every other request for that picture until it lapses."""
    if cache.client is None:
        return
    with anyio.CancelScope(shield=True):
        try:
            await cache.client.delete(key)
        except RedisError:
            logger.warning("A map picture's draw claim could not be released: %s", key)


async def _store(*, user_id: int, digest: str, theme: MapTheme, image: bytes, sha256: str) -> None:
    """The file first, then the row, in a session of the draw's own: the request's may be gone."""
    key = blob_store.new_key(BLOB_KIND, sha256=sha256)
    await blob_store.put(key, image)
    now = datetime.now(UTC)
    async with local_session() as db:
        inserted = (
            await db.execute(
                pg_insert(MapPicture)
                .values(
                    user_id=user_id,
                    digest=digest,
                    theme=theme.value,
                    storage_key=key,
                    sha256=sha256,
                    created_at=now,
                    last_served_at=now,
                )
                .on_conflict_do_nothing(index_elements=[MapPicture.user_id, MapPicture.digest, MapPicture.theme])
                .returning(MapPicture.id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            # Another writer stored the same picture first - a lapsed claim, or no Redis.
            blob_store.delete_after_commit(db, key)
        await db.commit()


async def _draw_and_store(
    *, claim: str, user_id: int, payload: dict[str, Any], theme: MapTheme, signature: str, timeout: float
) -> ServedMapPicture:
    try:
        try:
            drawn = await map_renderer.render({**payload, "theme": theme.value}, timeout=timeout)
        except map_renderer.RendererUnavailable as exc:
            logger.warning("A map picture was not drawn: %s", exc)
            raise MapPictureUnavailable(str(exc)) from exc
        # A renderer redeployed mid-request drew with a signature this one did not digest: the
        # picture is stored under the name it was drawn as, and that signature becomes current.
        if drawn.signature != signature:
            map_renderer.adopt_signature(drawn.signature)
        drawn_digest = digest(payload, drawn.signature)
        sha256 = hashlib.sha256(drawn.image).hexdigest()
        await _store(user_id=user_id, digest=drawn_digest, theme=theme, image=drawn.image, sha256=sha256)
        return ServedMapPicture(digest=drawn_digest, sha256=sha256, data=drawn.image)
    finally:
        await _release(claim)


def _retrieve(task: asyncio.Task[Any]) -> None:
    """Collect an abandoned draw's outcome, so a failure logs nothing beyond its warning."""
    if not task.cancelled():
        task.exception()


async def _draw_as_claimant(
    db: AsyncSession,
    *,
    claim: str,
    user_id: int,
    digest: str,
    payload: dict[str, Any],
    theme: MapTheme,
    signature: str,
    timeout: float,
) -> ServedMapPicture | None:
    """Draw the picture this request has claimed, or let the claim go and answer `None` when
    another request stored it between this one's miss and its claim.

    Every way out before the draw's own task holds the claim lets it go here.
    """
    try:
        stored = await _find(db, user_id=user_id, digest=digest, theme=theme)
        await release_read_transaction(db)
        if stored is None:
            await enforce_rate_limit(
                f"map-picture:user:{user_id}",
                settings.MAP_PICTURE_RATE_LIMIT_PER_USER,
                settings.MAP_PICTURE_RATE_LIMIT_WINDOW_SECONDS,
            )
    except BaseException:
        await _release(claim)
        raise
    if stored is not None:
        await _release(claim)
        return None
    task = asyncio.create_task(
        _draw_and_store(
            claim=claim, user_id=user_id, payload=payload, theme=theme, signature=signature, timeout=timeout
        )
    )
    _pending_draws.add(task)
    task.add_done_callback(_pending_draws.discard)
    task.add_done_callback(_retrieve)
    # Shielded: a client that disconnects cancels this request, never the draw.
    return await asyncio.shield(task)


async def _await_pending_draws() -> None:
    """Wait for draws whose requests have gone. **The suite is the only caller.**"""
    while outstanding := [task for task in _pending_draws if not task.done()]:
        await asyncio.gather(*outstanding, return_exceptions=True)


async def find_or_draw(
    db: AsyncSession, *, user_id: int, payload: dict[str, Any], theme: MapTheme, if_none_match: str | None
) -> ServedMapPicture:
    """The account's picture of `payload` in `theme`, under its current digest: stored, or
    drawn now within `MAP_RENDERER_TIMEOUT`.

    `db` holds only the reads the caller made, all detached: the transaction is released
    before anything outbound and before any wait, and the draw writes through a session of
    its own.
    """
    if not settings.map_pictures:
        raise MapPicturesOff
    signature = map_renderer.current_signature()
    if signature is None:
        await release_read_transaction(db)
        try:
            signature = await map_renderer.fetch_signature()
        except map_renderer.RendererUnavailable as exc:
            raise MapPictureUnavailable(str(exc)) from exc
    current = digest(payload, signature)
    claim = _claim_key(user_id, current, theme)
    deadline = anyio.current_time() + settings.MAP_RENDERER_TIMEOUT

    while True:
        stored = await _find(db, user_id=user_id, digest=current, theme=theme)
        if stored is not None:
            served = await _serve_stored(db, stored, digest=current, if_none_match=if_none_match)
            if served is not None:
                return served
        await release_read_transaction(db)

        remaining = deadline - anyio.current_time()
        if remaining <= 0:
            raise MapPictureUnavailable("no picture was stored before the deadline")
        if await _claim(claim, seconds=remaining):
            drawn = await _draw_as_claimant(
                db,
                claim=claim,
                user_id=user_id,
                digest=current,
                payload=payload,
                theme=theme,
                signature=signature,
                timeout=remaining,
            )
            if drawn is not None:
                return drawn
            continue
        await anyio.sleep(min(_WAIT_POLL_SECONDS, remaining))


async def purge_unserved(db: AsyncSession, *, cutoff: datetime, limit: int) -> int:
    """Delete up to `limit` pictures last served before `cutoff`, their files after the
    commit, and return how many went.

    The `DELETE` repeats the selection's predicate, so a picture a request found between the
    two is kept rather than deleted under it.
    """
    batch = select(MapPicture.id).where(MapPicture.last_served_at < cutoff).limit(limit)
    keys = list(
        (
            await db.execute(
                delete(MapPicture)
                .where(MapPicture.id.in_(batch), MapPicture.last_served_at < cutoff)
                .returning(MapPicture.storage_key)
            )
        )
        .scalars()
        .all()
    )
    blob_store.delete_after_commit(db, keys)
    await db.commit()
    return len(keys)
