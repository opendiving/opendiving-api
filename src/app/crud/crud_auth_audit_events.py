from datetime import datetime

from fastcrud import FastCRUD
from sqlalchemy import ColumnElement, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils.request_context import RequestContext
from ..models.auth_audit_event import AuthAuditEvent
from ..schemas.auth_audit_event import AuthAuditEventCreateInternal, AuthAuditEventRead, AuthEventType

CRUDAuthAuditEvent = FastCRUD[
    AuthAuditEvent,
    AuthAuditEventCreateInternal,
    AuthAuditEventCreateInternal,
    AuthAuditEventCreateInternal,
    AuthAuditEventCreateInternal,
    AuthAuditEventRead,
]
crud_auth_audit_events = CRUDAuthAuditEvent(AuthAuditEvent)


async def record_auth_event(
    db: AsyncSession,
    *,
    event_type: AuthEventType,
    context: RequestContext,
    user_id: int | None = None,
    email: str | None = None,
    provider: str | None = None,
    commit: bool = True,
) -> None:
    """Write one audit row. The single entry point to the table, so that the never-log rule
    has one place to hold.

    **Failures propagate.** No `try`, no fire-and-forget task: an audit trail that silently
    drops rows when the database is unhappy records exactly the wrong events, since the
    interesting ones cluster around the moments something is going wrong. A caller that
    cannot afford to fail is a caller that should not be emitting an event.

    `commit` is the one thing per call site that needs thought, and it has two answers:

    - **`False`** where the event's own write is in flight and the caller commits both
      together - an account created with its provider row, an email change applied with its
      claim. The event and the thing it records then land or roll back as one.
    - **`True`** (the default) where there is no such transaction to join, which includes
      every path that is about to **raise**. `async_get_db` does not commit on unwind, so a
      row left uncommitted on a 401 path is silently lost - and two of the events here fire
      on exactly such a path (the refresh replay, and the passkey counter regression, which
      `finish_sign_in` follows immediately with an `UnauthorizedException`).
      `register_failed_code_attempt` already commits before its own 401 for the same
      reason.

    Nothing token-derived may ever be passed in: not a token, not a `jti`, not a token
    hash, not a sign-in code or its digest. The fact of the artifact, never the artifact.
    """
    await crud_auth_audit_events.create(
        db=db,
        object=AuthAuditEventCreateInternal(
            event_type=event_type,
            ip=context.ip,
            user_agent=context.user_agent,
            user_id=user_id,
            email=email,
            provider=provider,
        ),
        commit=commit,
    )


def expired_event_predicate(*, account_cutoff: datetime, anonymous_cutoff: datetime) -> ColumnElement[bool]:
    """The retention sweep's two tiers, as one `WHERE`.

    Account-tied rows and user-less rows have different lifetimes on purpose. A row naming
    an address that never became an account is bounded on the same order
    `authentication_request` already bounds one, because duration is the substance of the
    storage-limitation principle this design leans on: recording "an auth request for
    `<email>`" puts in the operator's database exactly what `authentication_request.email`
    already puts there, and that equivalence holds for *how long* only if the sweeps agree.
    """
    return or_(
        and_(AuthAuditEvent.user_id.is_not(None), AuthAuditEvent.created_at < account_cutoff),
        and_(AuthAuditEvent.user_id.is_(None), AuthAuditEvent.created_at < anonymous_cutoff),
    )
