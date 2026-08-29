from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base
from ..core.utils.request_context import MAX_IP_LENGTH, MAX_USER_AGENT_LENGTH

# Every member of `AuthEventType` is comfortably inside this; the longest today is
# `account_deletion_requested` at 26 characters. A plain `VARCHAR` with no DB-level `CHECK`
# and no Postgres enum, which is the closed-vocabulary pattern `DECISIONS.md` §"`GearItem.
# type` is a closed vocabulary, but has no DB `CHECK` constraint" records: the vocabulary is
# enforced by the enum every writer goes through, and a copy in the schema would need a
# `DROP`/`ADD CONSTRAINT` every time an event is added.
EVENT_TYPE_MAX_LENGTH = 40

# Matches `authentication_request.email`, which is the other table holding an address
# somebody typed rather than one an account owns.
EMAIL_MAX_LENGTH = 50

# `email` / `google` / `passkey`, same vocabulary as `authentication_provider.provider`.
PROVIDER_MAX_LENGTH = 20


class AuthAuditEvent(Base):
    """One thing that happened to an identity: a sign-in, an account created, a passkey
    added, a session revoked.

    Nothing logged a successful authentication before this. The durable trace of an email
    sign-in was `authentication_request.used_at`, which the hourly sweep deletes a week
    after the row expires, and no other auth event left one at all.

    **Persist-only in this version.** The rows are written here and read through the admin
    panel; there is no diver-facing view and no API route that returns one, which is what
    keeps the enumeration-oracle discipline untouched - that discipline constrains
    *responses*, and no response ever exposes these.

    **No public `uuid`, deliberately.** No route will ever key on one, and staying uuid-less
    keeps this out of the hard-delete registry's scope, which filters on models carrying a
    public `uuid` (`tests/helpers/model_metadata.py`).

    **What a row must never contain: tokens, token hashes, codes, or code hashes** - the
    fact of the artifact, never the artifact. That is OWASP's never-log list, and it is the
    one property here that a future event type could quietly break.
    """

    __tablename__ = "auth_audit_event"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, primary_key=True, init=False)

    # An `AuthEventType` value. The column is `str` rather than the enum so that reading a
    # row written by a newer version cannot raise on an unknown member.
    event_type: Mapped[str] = mapped_column(String(EVENT_TYPE_MAX_LENGTH), index=True)

    # Bounded by `RequestContext`, which is where both widths come from - see its module
    # docstring for why an over-length value must never reach a column.
    ip: Mapped[str] = mapped_column(String(MAX_IP_LENGTH))
    user_agent: Mapped[str] = mapped_column(String(MAX_USER_AGENT_LENGTH))

    # **Nullable on purpose, and set only where the request has already established the
    # account.** `request_email_link` "never even queries `crud_users`" - the enumeration
    # protection there is structural rather than a response-shaping trick, and an
    # audit-time lookup would reverse that guarantee (`DECISIONS.md` §"Unified auth flow").
    # So the auth-request-created event is always written user-less, as are the other two
    # genuinely pre-account events (sign-in code failed, onboarding started). Everything
    # downstream of a resolved identity carries the id it already had.
    #
    # `CASCADE`, so a purged account takes its own auth history with it. The cascade cannot
    # reach the user-less rows - `purge_deleted_accounts` deletes those by email, the same
    # second arm `authentication_request` already needs.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True, default=None)

    # Only on user-less rows, where it is the only thing naming a data subject at all. It is
    # what the purge's by-email arm matches, and what the 7-day tier of the retention sweep
    # bounds: an address typed by somebody who never signed up must not survive longer in
    # here than it does in `authentication_request`.
    email: Mapped[str | None] = mapped_column(String(EMAIL_MAX_LENGTH), index=True, default=None)

    provider: Mapped[str | None] = mapped_column(String(PROVIDER_MAX_LENGTH), default=None)

    # Indexed because it is the retention sweep's predicate, on both tiers.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, default_factory=lambda: datetime.now(UTC)
    )
