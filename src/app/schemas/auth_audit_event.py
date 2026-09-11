"""The audit trail's closed vocabulary, and the shape a row is written in.

`AuthEventType` is a `StrEnum` backed by a plain `VARCHAR` column - no Postgres enum and no
`CHECK` - which is the pattern `DECISIONS.md` §"`GearItem.type` is a closed vocabulary, but
has no DB `CHECK` constraint" records. Every writer goes through `record_auth_event`, so the
enum is the enforcement; a database-level copy of the list would buy nothing and would need
a migration every time an event is added.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class AuthEventType(StrEnum):
    """Every auth event this app records, and the rule that decides membership.

    **The rule, which outranks the list.** A site emits an event when it (a) commits a row
    to an auth table, (b) mints a session, onboarding or restore token, (c) revokes a
    credential at its owner's request, (d) ends, revokes or detects the replay of a session
    at the caller's request, or (e) logs a `WARNING` for a failure the code itself deems
    rare and meaningful - which is the class the write-based criteria structurally cannot
    reach. Each such site emits exactly one event.

    **The named exclusions matter as much**, and they follow one standing rule: the
    *Nothing is logged* bullet of `DECISIONS.md` §"A refresh token is only as alive as its
    account", which contrasts "a recurring, expected event" with "the reused-token warning,
    which is rare and means something". Excluded on those grounds: a deleted account's
    devices refreshing; rate-limit rejections; ordinary refresh rotations (`last_used_at`
    on the session already records liveness); session-cap evictions; passkey renames (the
    credential's power is unchanged); the invalidation of a previous live authentication
    request (housekeeping of the new request's creation, which is the event); the provider
    row created inside `POST /auth/complete`'s transaction (part of account creation, and a
    second row would record one act twice); an invitation being accepted (same commit as
    the account creation that accepts it) and an invitation being revoked (not a
    credential, and the row records its own `revoked_at`); the registration gate refusing
    an uninvited address (neither a write, nor rare, nor a WARNING - it is the ordinary
    answer on a closed instance); failed passkey registrations and assertions
    (expected, and logged at `info` - the failure that means something on that path is the
    counter regression, which *is* here); the passkey-notice delivery failure; and the
    generic 401/400s across the auth surface, most of which carry no established identity
    to record and all of which are the uniform-oracle discipline working.
    """

    # --- pre-account, always `user_id IS NULL`, carrying only the email ---
    AUTH_REQUEST_CREATED = "auth_request_created"
    SIGN_IN_CODE_FAILED = "sign_in_code_failed"
    ONBOARDING_STARTED = "onboarding_started"
    # Criterion (a), stretched, and the stretch is worth naming: this is emitted on
    # **every** accepted request to `POST /invite-requests`, including one whose
    # on-conflict insert was a no-op and committed nothing. The IP and User-Agent it
    # carries are what bound abuse of an endpoint anybody can reach, which is the same
    # reason `AUTH_REQUEST_CREATED` above is unconditional.
    INVITE_REQUESTED = "invite_requested"

    # --- the identity is resolved by the time these are written ---
    SIGN_IN_SUCCEEDED = "sign_in_succeeded"
    RESTORE_OFFERED = "restore_offered"
    ACCOUNT_CREATED = "account_created"
    # `user_id` is the inviter, `email` the invitee - so the row names both parties to the
    # act, which is what makes it answerable later. Acceptance emits nothing of its own: it
    # is the same commit as `ACCOUNT_CREATED`, and the rule above is one event per site.
    # Nor does revocation - an invitation is not a credential, so criterion (c) does not
    # reach it, and the row keeps its own `revoked_at`.
    INVITATION_CREATED = "invitation_created"
    ACCOUNT_RESTORED = "account_restored"
    PROVIDER_LINKED = "provider_linked"
    PASSKEY_ADDED = "passkey_added"
    PASSKEY_REMOVED = "passkey_removed"
    PASSKEY_COUNTER_REGRESSED = "passkey_counter_regressed"
    EMAIL_CHANGE_REQUESTED = "email_change_requested"
    EMAIL_CHANGE_COMPLETED = "email_change_completed"
    ACCOUNT_DELETION_REQUESTED = "account_deletion_requested"
    LOGOUT = "logout"
    SESSION_REVOKED = "session_revoked"
    OTHER_SESSIONS_REVOKED = "other_sessions_revoked"

    # Left `NULL` when the account is already gone, which is the one row that satisfies
    # neither purge arm and is bounded by the user-less retention tier alone.
    #
    # One row for two things, deliberately: the detection and the session revocation it
    # triggers are one act on one site, and the rule above is one event per site - so this
    # reads as "a replay was detected and its session was ended" rather than earning a
    # `SESSION_REVOKED` beside it. That one stays for the revoke a diver asks for.
    REFRESH_REPLAY_DETECTED = "refresh_replay_detected"


class AuthAuditEventCreateInternal(BaseModel):
    """Server-composed only - there is no request body anywhere that reaches this table."""

    model_config = ConfigDict(extra="forbid")

    event_type: AuthEventType
    ip: str
    user_agent: str
    user_id: int | None = None
    email: str | None = None
    provider: str | None = None


class AuthAuditEventRead(BaseModel):
    """The row as stored. Nothing in `api/v1` returns this - the audit trail is
    persist-only in this version - so its only reader is the admin panel.
    """

    id: int
    event_type: str
    ip: str
    user_agent: str
    user_id: int | None
    email: str | None
    provider: str | None
    created_at: datetime
