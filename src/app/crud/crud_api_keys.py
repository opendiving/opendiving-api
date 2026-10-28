import uuid as uuid_pkg
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastcrud import FastCRUD
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import hash_token
from ..models.api_key import ApiKey
from ..models.user import User
from ..schemas.api_key import ApiKeyCreateInternal, ApiKeyReadInternal

CRUDApiKey = FastCRUD[
    ApiKey,
    ApiKeyCreateInternal,
    ApiKeyCreateInternal,
    ApiKeyCreateInternal,
    ApiKeyCreateInternal,
    ApiKeyReadInternal,
]
crud_api_keys = CRUDApiKey(ApiKey)

# How stale `last_used_at` may get before a use stamps it again. A tool polling every second
# then costs one write per window rather than one per request, and the card shows a day.
API_KEY_LAST_USED_GRANULARITY = timedelta(minutes=5)


@dataclass(frozen=True)
class LiveApiKey:
    """What `get_current_user` needs of a key it has just matched: whose it is and what it may
    do. Never the hash or the secret."""

    id: int
    scope: str
    owner_uuid: uuid_pkg.UUID
    last_used_at: datetime | None


async def live_api_key(db: AsyncSession, *, secret: str, now: datetime) -> LiveApiKey | None:
    """The key `secret` is, if it can still authenticate: its row exists, it has not expired,
    and its owner has not asked for deletion. `None` for every other case alike, which the
    caller answers with one 401.

    The owner-join is what keeps a key dark through the deletion grace period and brings it
    back with a restore, as a session is; the purge's `DELETE FROM "user"` cascades it away.
    """
    row = (
        await db.execute(
            select(ApiKey.id, ApiKey.scope, ApiKey.last_used_at, User.uuid)
            .join(User, User.id == ApiKey.user_id)
            .where(
                ApiKey.token_hash == hash_token(secret),
                or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > now),
                User.is_deleted.is_(False),
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return LiveApiKey(id=row.id, scope=row.scope, owner_uuid=row.uuid, last_used_at=row.last_used_at)


async def stamp_api_key_used(db: AsyncSession, *, key: LiveApiKey, now: datetime) -> None:
    """Record that `key` was used, at most once per `API_KEY_LAST_USED_GRANULARITY`.

    **Commits on its own**, before the route runs. `async_get_db` never commits, and a route
    that only reads has no commit of its own to ride - so an uncommitted stamp would be lost
    on exactly the requests a read key makes. The stale-or-null condition is repeated in the
    `WHERE` so two requests racing past the read above write once between them.
    """
    cutoff = now - API_KEY_LAST_USED_GRANULARITY
    if key.last_used_at is not None and key.last_used_at > cutoff:
        return
    await db.execute(
        update(ApiKey)
        .where(ApiKey.id == key.id, or_(ApiKey.last_used_at.is_(None), ApiKey.last_used_at <= cutoff))
        .values(last_used_at=now)
    )
    await db.commit()


async def count_api_keys(db: AsyncSession, *, user_id: int) -> int:
    """Every key the account holds, expired ones included - which is what the cap counts."""
    return int(
        (await db.execute(select(func.count()).select_from(ApiKey).where(ApiKey.user_id == user_id))).scalar_one()
    )
