from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class InviteRequest(Base):
    """A stranger asking to be let into a closed instance, and nothing more.

    Written by `POST /invite-requests`, which is anonymous. A row exists only while the
    request is *pending*, so there is no state column: inviting the address deletes the row
    in the same transaction that creates the invitation, and from then on the invitation
    carries the address. The operator removing it and the 90-day sweep are its only other
    exits, plus the by-address arm of an account purge.

    **Three columns, and the omissions are the design.**

    No `uuid`: the operator's routes key on the address, which is the only thing anyone
    knows about a request, and a public identifier for a row a stranger cannot see would be
    unreachable by anybody. That absence also keeps this model out of the hard-delete
    registry, which enumerates by `uuid` (`tests/helpers/model_metadata.py`).

    No IP and no User-Agent, unlike `user_session`. Those belong on the `INVITE_REQUESTED`
    audit row, where they expire on the seven-day anonymous tier - the same split
    `AUTH_REQUEST_CREATED` already makes against `authentication_request`. Keeping them
    here would give an anonymous endpoint's exhaust a 90-day life.

    `email` is `unique`, which is what makes the endpoint's insert an on-conflict-do-nothing
    and its answer identical for a first request and a repeat. Stored lowercased, so
    `Diver@Example.com` and `diver@example.com` are one row and one queue entry.
    """

    __tablename__ = "invite_request"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, primary_key=True, init=False)

    email: Mapped[str] = mapped_column(String(50), unique=True, index=True)

    # Indexed for the hourly sweep's `WHERE created_at < :cutoff`
    # (`core.worker.functions.purge_expired_invite_requests`) - the argument
    # `b24933e17c19_index_authentication_request_expires_at` makes for its own column, and
    # this table is written by unauthenticated callers, so it is the one most able to grow.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC), index=True
    )
