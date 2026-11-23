from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin
from ..core.schemas import EMAIL_MAX_LENGTH


class Invitation(Base, PublicUUIDMixin, TimestampMixin):
    """One invitation of one email address into a closed instance: the operator's, sent from
    the waiting list or typed, or the record of a sign-up through a member's connect link.

    **An invitation by address is an allow-list entry keyed on the address, not a bearer token.**
    Signing in already proves ownership of an address - a magic link delivered to it, the
    code printed in the same email, or Google's `email_verified` claim - so a token carried
    in the invitation email would prove nothing the sign-in does not, and would add a
    redemption surface (generation, hashing, single use, carriage through `/auth/verify`
    into `/onboarding`) that an allow-list does not need. The invitee simply signs in with
    the address that was invited, which is the binding Plausible uses and, in effect,
    Ghost's. What it costs is forwardability: such an invitation cannot be handed to
    somebody else, which is the point. There is correspondingly **no secret on this row** -
    nothing here to hash.

    The forwardable exception is the member's connect link (`models.connect_link`), whose
    token is the secret and lives on that table. A sign-up through it is recorded here as an
    already-accepted row, which is what charges it to the member's quota.

    Deliberately not a third `purpose` on `AuthenticationRequest`. That table's rows are
    swept seven days after their own expiry and are deleted by address on an account purge
    regardless of purpose; an invitation needs an inviter and a life measured in months.

    `email` is **not unique**: two inviters may each have invited the same friend. Always
    stored lowercased - `services.registration_gate` compares against it and a Google claim
    arrives with whatever capitals the sender typed.

    A **live** invitation is one whose `revoked_at` is `NULL`; that is the whole predicate
    the gate asks. Nothing revokes one; a row that carries the stamp stays counted in the
    quota, which counts rows created in the trailing window.

    No `expires_at`, and the absence is a decision: a beta invitee slow to act should not
    have to be re-invited within the week. What bounds the address instead is the retention
    sweep, `core.worker.functions.purge_expired_invitations`, which deletes an unaccepted
    row 90 days after `created_at`. An accepted row is never swept - it belongs to two
    accounts, and goes when either of them does.
    """

    __tablename__ = "invitation"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, primary_key=True, init=False)

    # The invited address, lowercased on every write (the admin batch route, and a
    # connect-link sign-up's row in `POST /auth/complete`). Indexed because the
    # gate reads by it on every account creation, and the account purge deletes by it.
    email: Mapped[str] = mapped_column(String(EMAIL_MAX_LENGTH), index=True)

    # The inviter. `CASCADE` for the reason all but two FKs into `user.id` carry it: the
    # account purge is a `DELETE FROM "user"`, and this row naming an account that no
    # longer exists would be an allow-list entry nobody can see. An invitee who
    # had not signed up yet loses the invitation with their inviter - acceptable, and said
    # so on the privacy page.
    #
    # Named `user_id` rather than `inviter_id`: the cascade suite fails any other FK name
    # into `user.id`.
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)

    # Stamped inside `POST /auth/complete`'s own transaction, on every live invitation for
    # the address, alongside the `User` insert - so "account created" and "invitation
    # accepted" are one commit and cannot disagree.
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Written by nothing; a stamp a row carries keeps it out of the gate's predicate until
    # the sweep takes it.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Whether the operator sent this from the waiting-list queue - set by the admin batch
    # route exactly when it deleted a pending request for the address, never by a member.
    # The gate reads it to count the account under `waitlist`, and `accept_invitations`
    # clears it in the same statement that stamps `accepted_at`: an accepted row is never
    # swept, so leaving it set would keep "this address asked to be let in" for the life of
    # the account. It is a fact about a pending invitation only.
    from_invite_request: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
