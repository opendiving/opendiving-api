"""`GET /commons/dive-sites/points`: every Commons record with a pin, one document for every
account on the instance.

Kept here in Redis rather than through the response cache, whose one-megabyte cap the document
outgrows near 12,000 records and whose keys are per account where this has nothing per account.
A body is built once per version - the Commons rows' count and their latest creation, change and
review - and kept under a key carrying the build and the version, so a write moves the version
and the next request builds the new body, and a build that reshapes the rows reads none of the
previous build's.
"""

import hashlib
import json
import logging

from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils import cache
from ..models.dive_site import DiveSite
from .commons_records import attribution_for

logger = logging.getLogger(__name__)

# Long, because a key is never stale: a write moves the version and with it the key. This only
# bounds how long a superseded version's body lingers.
_TTL_SECONDS = 24 * 60 * 60

# Five decimals is a metre, finer than any pin a diver drops.
_COORDINATE_DECIMALS = 5


async def commons_points_version(db: AsyncSession) -> str:
    """A digest of what every write to a Commons row moves: an insert the count and the latest
    creation, a sync's update the latest change, a review the latest review."""
    stamps = (
        await db.execute(
            select(
                func.count(),
                func.max(DiveSite.created_at),
                func.max(DiveSite.updated_at),
                func.max(DiveSite.commons_reviewed_at),
            ).where(DiveSite.user_id.is_(None))
        )
    ).one()
    return hashlib.sha256(repr(tuple(stamps)).encode()).hexdigest()[:16]


def commons_points_etag(version: str) -> str:
    return f'"{cache.current_build()}.{version}"'


def _key(version: str) -> str:
    return f"commons-points:{cache.current_build()}:{version}"


async def _build(db: AsyncSession, version: str) -> bytes:
    rows = (
        await db.execute(
            select(DiveSite.longitude, DiveSite.latitude, DiveSite.uuid, DiveSite.name, DiveSite.commons_source)
            .where(DiveSite.user_id.is_(None), DiveSite.latitude.is_not(None), DiveSite.longitude.is_not(None))
            .order_by(DiveSite.uuid)
        )
    ).all()
    sources: dict[str, str] = {}
    for source in sorted({row.commons_source for row in rows}):
        if (credit := attribution_for(source)) is not None:
            sources[source] = credit
    document = {
        "version": version,
        "sources": sources,
        "points": [
            [
                round(row.longitude, _COORDINATE_DECIMALS),
                round(row.latitude, _COORDINATE_DECIMALS),
                str(row.uuid),
                row.name,
                row.commons_source,
            ]
            for row in rows
        ],
    }
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()


async def commons_points_body(db: AsyncSession, version: str) -> bytes:
    """The document at `version`, from Redis where a request has built it, else built and kept.
    A Redis that is absent or failing costs the build, never the answer."""
    key = _key(version)
    if cache.client is not None:
        try:
            held = await cache.client.get(key)
        except RedisError as exc:
            logger.warning("Commons points read failed (%s); building them.", type(exc).__name__)
            held = None
        if held is not None:
            return bytes(held)

    body = await _build(db, version)
    if cache.client is not None:
        try:
            await cache.client.set(key, body, ex=_TTL_SECONDS)
        except RedisError as exc:
            logger.warning("Commons points write failed (%s).", type(exc).__name__)
    return body
