"""A member's invite link: one standing token per member, shown on their Invitations card.

Two readers ask about a token, and they are deliberately asked different questions.
`POST /auth/email/request`, which anybody can reach, asks only `invite_link_exists` - one
indexed lookup on this table, naming no account. The gates, which run after the address is
proven, ask `live_invite_link`, which also reads whether the owner is deleted, whether they
are a superuser and their name.
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..crud.crud_invitations import hold_invitation_quota, invitations_created_since
from ..models.invite_link import InviteLink
from ..models.user import User


def new_invite_link_token() -> str:
    """32 lowercase hexadecimal characters, 128 bits.

    Hex because the token rides `via`, where a join-channel slug rides, and has to pass that
    field's pattern on both request bodies, fit its `String(32)` column on the sign-in request
    row and survive the web's own copy of the pattern on the Google attempt record.
    """
    return secrets.token_hex(16)


@dataclass(frozen=True, slots=True)
class LiveInviteLink:
    owner_id: int
    owner_name: str
    owner_is_superuser: bool


async def read_invite_link(db: AsyncSession, *, user_id: int) -> InviteLink | None:
    return await db.scalar(select(InviteLink).where(InviteLink.user_id == user_id))


async def replace_invite_link(db: AsyncSession, *, user_id: int) -> tuple[str, datetime]:
    """Give the member a fresh token, creating their row or overwriting the one they have.

    One upsert on `user_id`, so two first creates racing each other - a double click, two
    tabs - both succeed and the later token stands. `created_at` moves with the token, so it
    always says when the current one was made. Commits nothing: the caller's audit row
    belongs to the same commit.
    """
    token = new_invite_link_token()
    now = datetime.now(UTC)
    statement = insert(InviteLink).values(user_id=user_id, token=token, created_at=now)
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[InviteLink.user_id],
            set_={"token": statement.excluded.token, "created_at": statement.excluded.created_at},
        )
    )
    return token, now


async def remove_invite_link(db: AsyncSession, *, user_id: int) -> bool:
    """Delete the member's link, answering whether there was one. Commits nothing."""
    removed = await db.scalar(delete(InviteLink).where(InviteLink.user_id == user_id).returning(InviteLink.id))
    return removed is not None


async def invite_link_exists(db: AsyncSession, *, token: str) -> bool:
    """Whether `token` is a member's current link. Reads this table alone: the anonymous
    sign-in request asks this, and it must learn nothing about any account."""
    return bool(await db.scalar(select(func.count()).select_from(InviteLink).where(InviteLink.token == token)))


async def live_invite_link(db: AsyncSession, *, token: str) -> LiveInviteLink | None:
    """The link `token` names, if its owner has not asked for their account to be deleted -
    the check-in link's predicate, so a deletion request kills both at once."""
    row = (
        await db.execute(
            select(User.id, User.name, User.is_superuser)
            .join(InviteLink, InviteLink.user_id == User.id)
            .where(InviteLink.token == token, User.is_deleted.is_(False))
        )
    ).one_or_none()
    if row is None:
        return None
    return LiveInviteLink(owner_id=row.id, owner_name=row.name, owner_is_superuser=row.is_superuser)


async def can_still_invite(db: AsyncSession, link: LiveInviteLink) -> bool:
    """Whether the link's owner is under the invitation quota, which is the only bound on a
    standing link. Counted as `POST /user/invitations` counts it, so a sign-up through the
    link and an invitation typed by address draw on one allowance.

    Under the owner's `hold_invitation_quota`, so the sign-up `admit_or_refuse` admits is
    counted against a typed invitation made at the same moment. The advisory check before
    the profile form takes it too and holds it only until its own request commits."""
    if link.owner_is_superuser:
        return True
    await hold_invitation_quota(db, user_id=link.owner_id)
    made = await invitations_created_since(db, user_id=link.owner_id, window_days=settings.INVITATIONS_WINDOW_DAYS)
    return made < settings.INVITATIONS_PER_USER
