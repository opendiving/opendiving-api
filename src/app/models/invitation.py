from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class Invitation(Base, PublicUUIDMixin, TimestampMixin):
    """One member's invitation of one email address into a closed instance.

    **A typed invitation is an allow-list entry keyed on the address, not a bearer token.**
    Signing in already proves ownership of an address - a magic link delivered to it, the
    code printed in the same email, or Google's `email_verified` claim - so a token carried
    in the invitation email would prove nothing the sign-in does not, and would add a
    redemption surface (generation, hashing, single use, carriage through `/auth/verify`
    into `/onboarding`) that an allow-list does not need. The invitee simply signs in with
    the address that was invited, which is the binding Plausible uses and, in effect,
    Ghost's. What it costs is forwardability: a typed invitation cannot be handed to
    somebody else, which is the point. There is correspondingly **no secret on this row** -
    nothing here to hash.

    The forwardable exception is the member's invite link (`models.invite_link`), whose
    token is the secret and lives on that table. A sign-up through it is recorded here as an
    already-accepted row with `from_invite_link` set, so the member's list shows it.

    Deliberately not a third `purpose` on `AuthenticationRequest`. That table's rows are
    swept seven days after their own expiry and are deleted by address on an account purge
    regardless of purpose; an invitation needs an inviter, a listing of its own and a life
    measured in months.

    `email` is **not unique**: two members may each invite the same friend, and each sees
    their own row. Always stored lowercased - `services.registration_gate` compares against
    it and a Google claim arrives with whatever capitals the sender typed.

    A **live** invitation is one whose `revoked_at` is `NULL`; that is the whole predicate
    the gate asks. Revoking stamps the column rather than deleting the row, so the quota -
    counted over rows created in the trailing window - stays an honest bound on how many
    invitations one account has made, by address or through its invite link.

    No `expires_at`, and the absence is a decision: a beta invitee slow to act should not
    have to be re-invited within the week. What bounds the address instead is the retention
    sweep, `core.worker.functions.purge_expired_invitations`, which deletes an unaccepted
    row 90 days after `created_at`. An accepted row is never swept - it belongs to two
    accounts, and goes when either of them does.
    """

    __tablename__ = "invitation"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, primary_key=True, init=False)

    # The invited address, lowercased on every write (`api.v1.invitations`, the admin batch
    # route, and an invite-link sign-up's row in `POST /auth/complete`). Indexed because the
    # gate reads by it on every account creation, and the account purge deletes by it.
    email: Mapped[str] = mapped_column(String(50), index=True)

    # The inviter. `CASCADE` for the reason every other FK into `user.id` carries it: the
    # account purge is a `DELETE FROM "user"`, and this row naming an account that no
    # longer exists would be an allow-list entry nobody can see or revoke. An invitee who
    # had not signed up yet loses the invitation with their inviter - acceptable, and said
    # so on the privacy page.
    #
    # Named `user_id` rather than `inviter_id` so that `fetch_owned_or_raise`'s `OwnedRow`
    # protocol resolves against the read schema, which is what `DELETE /user/invitation/
    # {uuid}` goes through; the cascade suite fails any other FK name into `user.id`.
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)

    # Stamped inside `POST /auth/complete`'s own transaction, on every live invitation for
    # the address, alongside the `User` insert - so "account created" and "invitation
    # accepted" are one commit and cannot disagree.
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Stamped by `DELETE /user/invitation/{uuid}`. Revoking an already-accepted invitation
    # is refused (409): the account exists, and the stamp would be a lie.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Whether the operator sent this from the waiting-list queue - set by the admin batch
    # route exactly when it deleted a pending request for the address, never by a member.
    # The gate reads it to count the account under `waitlist`, and `accept_invitations`
    # clears it in the same statement that stamps `accepted_at`: an accepted row is never
    # swept, so leaving it set would keep "this address asked to be let in" for the life of
    # the account. It is a fact about a pending invitation only.
    from_invite_request: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Whether this row records a sign-up through the inviter's invite link rather than an
    # address they typed. Written true, already accepted, by `POST /auth/complete` and by
    # nothing else. The inviter never typed the address, so `GET /user/invitations` shows
    # them the new member's name instead of it. Unlike the flag above it is a fact about an
    # accepted row, and so is never cleared.
    from_invite_link: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
