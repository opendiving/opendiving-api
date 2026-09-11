from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin
from ..core.utils.request_context import MAX_IP_LENGTH, MAX_USER_AGENT_LENGTH


class UserSession(Base, PublicUUIDMixin):
    """One signed-in device, and the thing a refresh token's `sid` claim names.

    Refresh tokens used to be stateless: `create_refresh_token` signed `{sub, exp, jti,
    token_type}` and nothing was written anywhere, so "list my sessions" had no data
    source, "sign out my other devices" was inexpressible, and "which session am I?" was
    unanswerable. This table is that missing state.

    **In `app/models/` rather than beside `TokenBlacklist` under `core/db/`**, unlike the
    other token-shaped table here. A blacklist entry is a fact about a string; this is a
    diver-owned resource with a public uuid, three routes of its own and an owner - so it
    belongs where the registration inventory (`DECISIONS.md`, *"The test registrations that
    fail on nothing"*) will see it.

    **`jti` and `sid` do different jobs and both stay.** The `jti` identifies one
    *issuance*, which is what makes revoking a token by value a per-issuance revocation
    (see `core.security._new_jti`); `sid` identifies the *device*, and is deliberately
    carried unchanged across every rotation. That identifier is the prerequisite
    `DECISIONS.md` §"A reused refresh token is a `WARNING`" recorded as missing for its
    *Tier 3 - family revocation*; this table supplies it, and a row here is what that
    revocation now stamps when a spent refresh token is replayed past the threshold.

    Hard-deleted rather than soft-deleted, but `revoked_at` is what a revoke stamps: the
    row has to outlive the revoke long enough for the refresh path to answer 401 on a
    stated reason rather than on an absent row, and the cron sweep is what removes it.
    That is why `UserSession` is in `NOT_A_DIVERS_OWN_RESOURCE` rather than in
    `HARD_DELETED_RESOURCES` - see `tests/helpers/model_metadata.py` for the reason as the
    registry records it.
    """

    __tablename__ = "user_session"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, primary_key=True, init=False)

    # `CASCADE` from day one, like `webauthn_credential`: the account purge is a
    # `DELETE FROM "user"`, and a session row outliving its account would be a refresh
    # cookie pointing at a row whose owner no longer exists.
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)

    # The sliding *inactivity* window, not a session length: every refresh pushes it out by
    # `REFRESH_TOKEN_EXPIRE_DAYS` so the row and the cookie it backs expire together.
    # Indexed because it is the sweep's predicate, the same argument
    # `b24933e17c19_index_authentication_request_expires_at` makes for its column.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    # `client_ip()`'s answer, which may be the literal `"unknown"` and - behind a
    # configured `TRUSTED_PROXY_IPS` - may be an unvalidated `X-Forwarded-For` element.
    # Bounded by `RequestContext`, which is also where the width comes from.
    ip: Mapped[str] = mapped_column(String(MAX_IP_LENGTH))

    # The raw header, empty string when the client sent none. Not parsed into a device
    # label: the web client owns that (`passkeyNameForUserAgent`), so one browser cannot be
    # named two different things on one settings page. Storing the string keeps the label
    # re-derivable as those tables improve.
    user_agent: Mapped[str] = mapped_column(String(MAX_USER_AGENT_LENGTH))

    # All three timestamps are `DateTime(timezone=True)` and written UTC-aware: asyncpg
    # refuses an aware->naive bind, and a naive one would be filed in the host's zone -
    # see `DECISIONS.md` §"Blacklist expiries are UTC-aware, and so is the purge that reads
    # them", which is the same mistake one table over.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))

    # Stamped on every successful refresh. Also the eviction key when an account reaches
    # the live-session cap: the *stalest* row goes, never the oldest, so a browser in daily
    # use is never signed out to make room for one that has sat idle since March.
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))

    # Set by a revoke (either session route, logout, `DELETE /user`) or by cap eviction.
    # A revoked row can no longer refresh anything and is swept on the next hourly pass.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
