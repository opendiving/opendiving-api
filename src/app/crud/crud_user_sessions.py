import uuid as uuid_pkg
from datetime import UTC, datetime
from typing import cast

from fastcrud import FastCRUD
from sqlalchemy import ColumnElement, CursorResult, and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.user_session import UserSession
from ..schemas.user_session import UserSessionCreateInternal, UserSessionReadInternal

CRUDUserSession = FastCRUD[
    UserSession,
    UserSessionCreateInternal,
    UserSessionCreateInternal,
    UserSessionCreateInternal,
    UserSessionCreateInternal,
    UserSessionReadInternal,
]
crud_user_sessions = CRUDUserSession(UserSession)

# GitLab's number, and the reason to have one at all is the same as theirs: an account
# accumulating sessions without bound is a list nobody can read and a set of credentials
# nobody can reason about. Not a `Settings` field - see `MAX_LIVE_SESSIONS_PER_USER`'s use
# in `evict_stalest_sessions` for what it actually protects.
MAX_LIVE_SESSIONS_PER_USER = 100


def _live(now: datetime) -> ColumnElement[bool]:
    """The predicate for "this row can still authenticate something".

    One definition, used by the list, the cap count, the bulk revoke and `live_session_for`
    - which is itself asked by two callers now, `/auth/refresh` before it rotates and
    `get_current_user` on every authenticated request. Another spelling of it would be
    another chance to forget one of the two clauses.
    """
    return and_(UserSession.revoked_at.is_(None), UserSession.expires_at > now)


async def evict_stalest_sessions(db: AsyncSession, *, user_id: int, keep: int) -> int:
    """Revoke the account's least-recently-used live sessions until at most `keep` remain,
    returning how many were revoked. Commits nothing - the caller's transaction owns it.

    **By `last_used_at`, not by `created_at`, and that is a deliberate departure from
    GitLab's documented oldest-deleted.** A session actively in use must never be evicted
    while a dormant one holds a slot; eviction by creation order would sign the diver's
    oldest - often busiest - browser out ahead of ninety-nine idle ones.

    Best-effort under concurrency: two sign-ins racing can both count `keep` and both
    insert, so an account can momentarily sit one or two rows above the cap. That is why
    the list endpoint reads well past it - a session nobody can see is a session nobody can
    revoke.

    **The subquery orders `last_used_at` DESC and then skips `keep`**, which reads
    backwards and is the only ordering that does the right thing: `OFFSET` keeps what it
    skips, so the rows it must skip are the ones to *retain* - the freshest. Sorted the
    other way the statement is still a valid eviction of exactly the right number of rows,
    and it evicts precisely the wrong ones - every session in daily use, sparing the idle
    ones. Written the wrong way round first, and caught by
    `TestTheCapEvictsTheStalest::test_the_least_recently_used_row_goes_not_the_oldest`,
    which is why that case asserts *which* row survived rather than only how many did.
    """
    now = datetime.now(UTC)
    stalest = (
        select(UserSession.id)
        .where(UserSession.user_id == user_id, _live(now))
        .order_by(UserSession.last_used_at.desc(), UserSession.id.desc())
        .offset(keep)
    )
    result = cast(
        CursorResult,
        await db.execute(
            update(UserSession)
            .where(UserSession.id.in_(stalest.scalar_subquery()))
            .values(revoked_at=now)
            # `synchronize_session=False` on both bulk revokes here: the default `"auto"`
            # falls back to a `RETURNING`-based fetch for criteria it cannot evaluate in
            # Python, which an `IN (SELECT ...)` is - and that fallback is what would make
            # `rowcount` stop being the count of rows the statement changed.
            .execution_options(synchronize_session=False)
        ),
    )
    # Read before any commit: the count belongs to the statement, not the transaction.
    return result.rowcount


async def live_session_for(
    db: AsyncSession, *, session_uuid: uuid_pkg.UUID, user_id: int
) -> UserSessionReadInternal | None:
    """The session a token's `sid` names, if it can still authenticate and belongs to the
    token's subject.

    All three conditions in one statement rather than a fetch plus checks: "revoked",
    "expired" and "somebody else's" are one answer to the caller - the same uniform 401 -
    so there is nothing for either caller to tell apart.

    Two of them now. `POST /auth/refresh` asks before it will rotate, and
    `api.dependencies.get_current_user` asks on every authenticated request, which is what
    makes a revoke end the device's access token rather than only its refresh. One lookup
    for both, so the two can never answer differently about the same row.
    """
    row = await db.execute(
        select(UserSession).where(
            UserSession.uuid == session_uuid,
            UserSession.user_id == user_id,
            _live(datetime.now(UTC)),
        )
    )
    session = row.scalar_one_or_none()
    return UserSessionReadInternal.model_validate(session, from_attributes=True) if session is not None else None


async def touch_session(db: AsyncSession, *, session_uuid: uuid_pkg.UUID, expires_at: datetime) -> None:
    """Record a refresh against a session: `last_used_at` now, `expires_at` slid out to
    match the replacement cookie's own lifetime.

    Sliding the expiry is what makes the row an *inactivity* window rather than a hard
    session length, which is what the cookie has always been (it is single-use, and each
    rotation restarts the clock) and what the privacy copy already describes.
    """
    await db.execute(
        update(UserSession)
        .where(UserSession.uuid == session_uuid)
        .values(last_used_at=datetime.now(UTC), expires_at=expires_at)
    )


async def revoke_session(db: AsyncSession, *, session_uuid: uuid_pkg.UUID) -> None:
    """Stamp `revoked_at` on one session, leaving an already-revoked row as it is.

    Idempotent on purpose, unlike the hard-deleting resources whose second `DELETE` is a
    404: the row is not the thing being removed, and re-revoking a session the caller owns
    discloses nothing and changes nothing. The cron sweep is what removes it.
    """
    await db.execute(
        update(UserSession)
        .where(UserSession.uuid == session_uuid, UserSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def revoke_other_sessions(db: AsyncSession, *, user_id: int, except_uuid: uuid_pkg.UUID | None) -> int:
    """Revoke every live session on the account except the caller's own, returning the
    count. Commits nothing.

    `except_uuid=None` revokes every session including the caller's own - the honest answer
    when nothing identifies the current one, since a session that cannot be named cannot be
    spared. No route arrives that way any more: `get_current_user` refuses a token carrying
    no `sid`, so `DELETE /user/sessions` always has one to pass. The branch stays because
    this is a store function and "spare nothing" is a coherent thing to ask it for.
    """
    now = datetime.now(UTC)
    conditions = [UserSession.user_id == user_id, _live(now)]
    if except_uuid is not None:
        conditions.append(UserSession.uuid != except_uuid)

    result = cast(
        CursorResult,
        await db.execute(
            update(UserSession).where(*conditions).values(revoked_at=now).execution_options(synchronize_session=False)
        ),
    )
    # Read before any commit, like the eviction above.
    return result.rowcount


async def live_sessions_for_user(db: AsyncSession, *, user_id: int, limit: int) -> list[UserSessionReadInternal]:
    """The account's live sessions, most recently used first."""
    rows = await db.execute(
        select(UserSession)
        .where(UserSession.user_id == user_id, _live(datetime.now(UTC)))
        .order_by(UserSession.last_used_at.desc(), UserSession.id.desc())
        .limit(limit)
    )
    return [UserSessionReadInternal.model_validate(row, from_attributes=True) for row in rows.scalars()]


def swept_session_predicate(now: datetime) -> ColumnElement[bool]:
    """What the hourly sweep deletes: a row that can no longer authenticate anything.

    Expressed here rather than in the worker so it sits beside `_live`, whose exact
    complement it is - the two drifting apart would either strand rows forever or delete
    live ones.
    """
    return or_(UserSession.revoked_at.is_not(None), UserSession.expires_at < now)
