"""Check-in links: a diver's check-in page, opened by whoever holds the token.

The token is the credential, so every anonymous read starts at `resolve_checkin_link`, and a
link that is unknown, expired, revoked or whose diver has asked for deletion resolves to
nothing at all - the routes answer each of those, and everything else they refuse, with one
404.
"""

import uuid as uuid_pkg
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import ColumnElement, and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import generate_secure_token, hash_token
from ..crud.crud_certifications import get_every_certification
from ..crud.crud_contacts import get_contact_names_by_ids
from ..crud.crud_users import read_account
from ..models.certification import Certification
from ..models.certification_file import CertificationFile
from ..models.checkin_link import CheckinLink
from ..models.user import User
from ..schemas.certification import CertificationSide
from ..schemas.checkin_link import (
    CheckinCertification,
    CheckinDiver,
    CheckinFigures,
    CheckinLinkCreate,
    CheckinSummary,
)
from .certification_files import LoadedCardFile, get_file_infos_for_certifications, load_certification_file

# A desk check-in is a same-day event, and a leaked link should not outlive it.
CHECKIN_LINK_TTL = timedelta(hours=24)


def _live(now: datetime) -> ColumnElement[bool]:
    """Unrevoked and unexpired. The anonymous reads add the diver's own liveness."""
    return and_(CheckinLink.revoked_at.is_(None), CheckinLink.expires_at > now)


def swept_checkin_link_predicate(now: datetime) -> ColumnElement[bool]:
    """What the hourly sweep deletes: the complement of `_live`, kept beside it so the two
    cannot drift into deleting a live link or keeping a dead one forever."""
    return or_(CheckinLink.revoked_at.is_not(None), CheckinLink.expires_at <= now)


@dataclass(frozen=True, slots=True)
class LiveCheckinLink:
    user_id: int
    user_uuid: uuid_pkg.UUID
    expires_at: datetime
    figures: CheckinFigures


@dataclass(frozen=True, slots=True)
class CardFront:
    """A card's front, without its bytes, so a conditional request needs no read of them."""

    certification_id: int
    sha256: str
    content_type: str


def is_servable_front(content_type: str) -> bool:
    """Only an image front is served. A PDF served `inline` from this origin is what the owner
    route's `attachment` exists to prevent, and the page draws a PDF front as a label."""
    return content_type.startswith("image/")


async def mint_checkin_link(db: AsyncSession, *, user_id: int, figures: CheckinLinkCreate) -> tuple[str, datetime]:
    """Revoke the diver's links and mint one in their place, answering its token and expiry.

    The diver's row is locked first, so two mints racing each other cannot both find nothing
    to revoke and leave two links live.
    """
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    now = datetime.now(UTC)
    await revoke_checkin_links(db, user_id=user_id, now=now)

    token = generate_secure_token()
    expires_at = now + CHECKIN_LINK_TTL
    db.add(
        CheckinLink(
            user_id=user_id,
            token_hash=hash_token(token),
            expires_at=expires_at,
            total_dives=figures.total_dives,
            max_depth=figures.max_depth,
            last_dive_on=figures.last_dive_on,
            created_at=now,
        )
    )
    await db.commit()
    return token, expires_at


async def revoke_checkin_links(db: AsyncSession, *, user_id: int, now: datetime | None = None) -> None:
    """Stamp every unrevoked link of the diver's. Commits nothing - the caller's transaction
    owns it."""
    await db.execute(
        update(CheckinLink)
        .where(CheckinLink.user_id == user_id, CheckinLink.revoked_at.is_(None))
        .values(revoked_at=now or datetime.now(UTC))
    )


async def live_checkin_link_expiry(db: AsyncSession, *, user_id: int) -> datetime | None:
    """When the diver's live link expires, or `None` without one."""
    return cast(
        datetime | None,
        await db.scalar(
            select(CheckinLink.expires_at)
            .where(CheckinLink.user_id == user_id, _live(datetime.now(UTC)))
            .order_by(CheckinLink.expires_at.desc())
            .limit(1)
        ),
    )


async def resolve_checkin_link(db: AsyncSession, *, token: str) -> LiveCheckinLink | None:
    """The live link `token` names, or `None`.

    A soft-deleted diver's link is dead from the deletion request on rather than at the purge
    days later.
    """
    row = (
        await db.execute(
            select(
                CheckinLink.user_id,
                User.uuid,
                CheckinLink.expires_at,
                CheckinLink.total_dives,
                CheckinLink.max_depth,
                CheckinLink.last_dive_on,
            )
            .join(User, User.id == CheckinLink.user_id)
            .where(
                CheckinLink.token_hash == hash_token(token),
                _live(datetime.now(UTC)),
                User.is_deleted.is_(False),
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return LiveCheckinLink(
        user_id=row.user_id,
        user_uuid=row.uuid,
        expires_at=row.expires_at,
        figures=CheckinFigures(
            total_dives=row.total_dives,
            max_depth=row.max_depth,
            last_dive_on=row.last_dive_on,
        ),
    )


async def checkin_summary(db: AsyncSession, link: LiveCheckinLink) -> CheckinSummary | None:
    """What the check-in page prints, read the way the signed-in page reads it: the account
    through `get_current_user`'s own query and the cards in the list endpoint's order. The
    figures are the link's. `None` if the account went between resolving the link and now.
    """
    account = await read_account(db, uuid=link.user_uuid)
    if account is None:
        return None

    cards = await get_every_certification(db, user_id=link.user_id)
    files = await get_file_infos_for_certifications(db, certification_ids=[card["id"] for card in cards])
    contact_names = await get_contact_names_by_ids(
        db, contact_ids=[card["contact_id"] for card in cards], user_id=link.user_id
    )

    certifications = []
    for card in cards:
        front = next((info for info in files[card["id"]] if info.side == CertificationSide.FRONT), None)
        certifications.append(
            CheckinCertification.model_validate(
                card
                | {
                    "contact_name": contact_names.get(card["contact_id"]) if card["contact_id"] is not None else None,
                    "front_content_type": front.content_type if front is not None else None,
                }
            )
        )

    return CheckinSummary(
        expires_at=link.expires_at,
        diver=CheckinDiver.model_validate(account),
        diving=link.figures,
        certifications=certifications,
    )


async def find_card_front(
    db: AsyncSession, link: LiveCheckinLink, *, certification_uuid: uuid_pkg.UUID
) -> CardFront | None:
    """The front of one of the link's diver's cards, or `None` - for a card that is someone
    else's, deleted, or has no front alike."""
    row = (
        await db.execute(
            select(CertificationFile.certification_id, CertificationFile.sha256, CertificationFile.content_type)
            .join(Certification, Certification.id == CertificationFile.certification_id)
            .where(
                Certification.uuid == certification_uuid,
                Certification.user_id == link.user_id,
                Certification.is_deleted.is_(False),
                CertificationFile.side == CertificationSide.FRONT.value,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return CardFront(certification_id=row.certification_id, sha256=row.sha256, content_type=row.content_type)


async def load_card_front(db: AsyncSession, front: CardFront) -> LoadedCardFile | None:
    """The front's bytes, or `None` if it is gone or has been replaced by a PDF since
    `find_card_front` looked."""
    loaded = await load_certification_file(db, certification_id=front.certification_id, side=CertificationSide.FRONT)
    if loaded is None or not is_servable_front(loaded.content_type):
        return None
    return loaded
