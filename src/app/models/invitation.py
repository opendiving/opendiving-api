from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class Invitation(Base, PublicUUIDMixin, TimestampMixin):
    """One member's invitation of one email address into a closed instance.

    **An allow-list entry keyed on the address, not a bearer token.** Signing in already
    proves ownership of an address - a magic link delivered to it, the code printed in the
    same email, or Google's `email_verified` claim - so a token carried in the invitation
    email would prove nothing the sign-in does not, and would add a redemption surface
    (generation, hashing, single use, carriage through `/auth/verify` into `/onboarding`)
    that an allow-list does not need. The invitee simply signs in with the address that was
    invited, which is the binding Plausible uses and, in effect, Ghost's. What it costs is
    forwardability: an invitation cannot be handed to somebody else, which is the point.
    There is correspondingly **no secret on this row** - nothing here to hash.

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
    invitation emails one account has caused to be sent.

    No `expires_at`, and the absence is a decision: a beta invitee slow to act should not
    have to be re-invited within the week. What bounds the address instead is the retention
    sweep, `core.worker.functions.purge_expired_invitations`, which deletes an unaccepted
    row 90 days after `created_at`. An accepted row is never swept - it belongs to two
    accounts, and goes when either of them does.
    """

    __tablename__ = "invitation"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, primary_key=True, init=False)

    # The invited address, lowercased on every write (`api.v1.invitations`, and the admin
    # batch route). Indexed because the gate reads by it on every account creation, and the
    # account purge deletes by it.
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
