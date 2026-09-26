import asyncio
import logging
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import uvloop
from arq.worker import Worker
from sqlalchemy import CursorResult, and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...crud.crud_auth_audit_events import expired_event_predicate
from ...crud.crud_user_sessions import swept_session_predicate
from ...models.auth_audit_event import AuthAuditEvent
from ...models.authentication_request import AuthenticationRequest
from ...models.certification import Certification
from ...models.certification_file import CertificationFile
from ...models.checkin_link import CheckinLink
from ...models.dive import Dive
from ...models.dive_file import DiveFile
from ...models.gear_item import GearItem
from ...models.gear_service_schedule import GearServiceSchedule
from ...models.invitation import Invitation
from ...models.invite_request import InviteRequest
from ...models.user import User
from ...models.user_picture import UserPicture
from ...models.user_session import UserSession
from ...schemas.gear_service import ServiceStatus
from ...services import blob_store
from ...services.checkin_links import swept_checkin_link_predicate
from ...services.email_service import (
    send_gear_service_digest_email,
    send_renewal_reminder_email,
    send_year_in_review_email,
)
from ...services.gear_service import (
    SERVICE_DUE_SOON_DAYS,
    SERVICE_DUE_SOON_DIVES,
    service_kind_label,
    service_status,
    should_notify,
)
from ...services.renewals import (
    CERTIFICATION_EXPIRING_SOON_DAYS,
    certification_label,
    expiry_stage,
    expiry_text,
    insurance_label,
    should_remind,
)
from ...services.year_in_review import YEAR_IN_REVIEW_BATCH_SIZE, reviewed_year, year_in_review, year_window
from ..config import configure_logging, settings
from ..db.crud_token_blacklist import crud_token_blacklist
from ..db.database import local_session

asyncio.set_event_loop(uvloop.new_event_loop())

configure_logging(settings.LOG_LEVEL)

# How long an `authentication_request` row is kept *after* its own `expires_at` has
# passed. Not a knob: it exists to protect one documented leniency, and an operator
# tuning it would be tuning that leniency without knowing they were.
#
# Deleting at `expires_at` would be the obvious rule and is wrong by a week.
# `verify_email_change` deliberately tolerates a replay of an already-used email-change
# link while the address it names is still the account's current one
# (`api.v1.users._replay_result_or_reject`) - a mail scanner detonating the link, a
# double click, or the user opening yesterday's mail again all land there and get told
# the change they asked for was applied. That leniency lives entirely in this row, so
# removing the row turns it into `"This confirmation link is invalid."`.
#
# Seven days is far past any race and past a human re-reading recent mail, while still
# bounding the table at roughly a week of sign-in traffic. Past it, a replay reports
# invalid rather than confirming - the same answer `check_email_change_link` already
# gives that link, since it rejects a used row outright.
AUTHENTICATION_REQUEST_RETENTION = timedelta(days=7)

# How long an audit event is kept, in two tiers - and the split is the substance rather than
# a refinement of it.
#
# **Ninety days for a row tied to an account.** Long enough to answer "when did somebody
# last sign in to this, and from where" after the fact, which is the whole reason an auth
# trail exists, and bounded because storing an IP indefinitely is storing an online
# identifier indefinitely.
#
# **Seven days for a row with `user_id IS NULL`.** Those are the genuinely pre-account
# events - an auth request created, a sign-in code failed, onboarding started, an invite
# requested - and each carries an email address typed by somebody who may never have signed
# up. Recording "an auth request for `<email>`" puts in the operator's database exactly what
# `authentication_request.email` already puts there, which is a defensible equivalence only
# if it holds for *duration* as well as for content: an address that survives thirteen times
# longer here than it does there is a new retention decision wearing an old one's clothes.
# So this tier matches `AUTHENTICATION_REQUEST_RETENTION` deliberately, and the two should
# move together or not at all.
#
# Neither is a `Settings` field, following `AUTHENTICATION_REQUEST_RETENTION`'s own
# deliberate non-configurability - and with the same second benefit: no new setting means
# nothing to add to the install bundle's `example.env` or its configuration reference,
# which are in a different repository.
AUTH_AUDIT_RETENTION = timedelta(days=90)
AUTH_AUDIT_ANONYMOUS_RETENTION = AUTHENTICATION_REQUEST_RETENTION

# How long an invitation nobody has accepted is kept, counted from `created_at`, revoked
# ones included. Not a knob, following `AUTHENTICATION_REQUEST_RETENTION`'s own deliberate
# non-configurability and with the same second benefit: no new setting means nothing to add
# to the install bundle's `example.env` or its configuration reference, which are in another
# repository.
#
# **Ninety rather than unbounded**, and the reasoning is the anonymous audit tier's one
# paragraph up. An invitation row holds the same category of datum a request row does - a
# non-user's address, plus who invited them - and the audit row that already names that
# address expires at ninety days, so an invitation table that grew forever would be a new
# retention decision made silently, one table over from the one that refused it. What the
# sweep costs the invitee is that an invitation ignored for three months stops admitting
# them until somebody invites them again.
#
# An **accepted** invitation is never swept: it belongs to two accounts, and goes when
# either of them does - the inviter's by the FK cascade, the invitee's by the by-address
# arm of `_purge_one_account`.
INVITATION_RETENTION = timedelta(days=90)

# How long a pending invite request is kept, counted from `created_at`. Ninety days is the
# account-tied audit tier's figure and long enough for a slow rollout of a closed beta; the
# alternative that was refused is unbounded retention "because it is the operator's queue",
# which would have been the same silent retention decision.
#
# A request row has four exits and no others: it is invited (deleted in the transaction that
# creates the invitation, from either route), the operator removes it, the account its
# address belongs to is purged, or this sweep takes it.
INVITE_REQUEST_RETENTION = timedelta(days=90)


# -------- background tasks --------
async def purge_expired_tokens(ctx: dict[Any, Any]) -> str:
    """Delete rows from `token_blacklist` whose `expires_at` is in the past.

    Blacklist entries only need to be kept until the token they reference
    would have expired naturally, since an expired JWT is already rejected
    on its own. Without this cleanup the table grows unbounded, as every
    logout/account-deletion inserts new rows and nothing ever removes them.

    UTC-aware, like every other timestamp comparison here: `expires_at` is a
    `DateTime(timezone=True)` column written by `core.security._blacklist_one`, so a
    naive local `datetime.now()` would compare against it off by the host's UTC offset.
    """
    async with local_session() as db:
        now = datetime.now(UTC)
        expired_count = await crud_token_blacklist.count(db, expires_at__lt=now)
        if expired_count == 0:
            logging.info("No expired blacklisted tokens to purge")
            return "No expired tokens to purge"

        await crud_token_blacklist.delete(db, allow_multiple=True, expires_at__lt=now)
        logging.info("Purged %d expired blacklisted token(s)", expired_count)
        return f"Purged {expired_count} expired token(s)"


async def purge_expired_authentication_requests(ctx: dict[Any, Any]) -> str:
    """Delete `authentication_request` rows whose expiry passed more than
    `AUTHENTICATION_REQUEST_RETENTION` ago.

    The sibling of `purge_expired_tokens`, and the table needed it more: every magic-link
    request stores the email address it was sent to, `#98` added a sign-in code's digest
    beside it, and nothing has ever removed a row. On one developer's machine the table
    held 565 rows - 464 of them `purpose="sign_in"`, whose `user_id` is `NULL` by design
    and so cannot even be reached by the `ON DELETE CASCADE` an account deletion follows -
    and it had doubled in a day. This is the primary mechanism keeping that bounded.

    `expires_at` is the right clock rather than `used_at`/`invalidated_at`: an expired row
    cannot sign anyone in or confirm anything regardless of which of those it carries, and
    a row that is spent but still live is the one both verify endpoints read to say *why*
    it was rejected.

    One `DELETE` reporting its own `rowcount`, where `purge_expired_tokens` counts first -
    not an inconsistency to tidy up. That job counts because FastCRUD's `delete()` raises
    `NoResultFound` when nothing matches, which is the normal case on an hourly sweep;
    Core has no such objection, so the count and its check-then-act window are avoidable
    here.

    UTC-aware for the reason `purge_expired_tokens` spells out: `expires_at` is a
    `DateTime(timezone=True)`, so a naive `datetime.now()` would compare against it off by
    the host's UTC offset.

    One cost worth naming: a link older than the retention window now reports "invalid"
    where it used to report "expired", because the row that carried the distinction is
    gone. Both are 401s and both are true.
    """
    cutoff = datetime.now(UTC) - AUTHENTICATION_REQUEST_RETENTION
    async with local_session() as db:
        result = cast(
            CursorResult,
            await db.execute(delete(AuthenticationRequest).where(AuthenticationRequest.expires_at < cutoff)),
        )
        # Read before the commit: the count belongs to the statement, not the transaction.
        purged = result.rowcount
        await db.commit()

    if purged == 0:
        logging.info("No expired authentication requests to purge")
        return "No expired authentication requests to purge"

    logging.info("Purged %d expired authentication request(s)", purged)
    return f"Purged {purged} expired authentication request(s)"


async def purge_expired_invitations(ctx: dict[Any, Any]) -> str:
    """Delete invitations nobody accepted, `INVITATION_RETENTION` after they were sent.

    `created_at` is the clock rather than `revoked_at`, and both arms of that matter. A
    revoked invitation is swept at the same age as a live one - it has been admitting
    nobody since it was revoked, and re-clocking it on the revoke would keep the address
    around *longer* for having been withdrawn. And a live one is swept too: no invitation
    carries a per-invitation expiry the invitee races against, so this sweep is the only
    thing that bounds the address at all.

    `accepted_at IS NULL` is the whole guard on the other side. An accepted invitation is
    two accounts' shared history, not a pending allow-list entry, and it goes with either of
    them rather than on a clock.

    One `DELETE` reporting its own `rowcount`, for the reason
    `purge_expired_authentication_requests` gives: FastCRUD's `delete()` raises
    `NoResultFound` when nothing matches, which on an hourly sweep is the ordinary case, and
    Core has no such objection.
    """
    cutoff = datetime.now(UTC) - INVITATION_RETENTION
    async with local_session() as db:
        result = cast(
            CursorResult,
            await db.execute(
                delete(Invitation).where(Invitation.accepted_at.is_(None), Invitation.created_at < cutoff)
            ),
        )
        # Read before the commit: the count belongs to the statement, not the transaction.
        purged = result.rowcount
        await db.commit()

    if purged == 0:
        logging.info("No unaccepted invitations to purge")
        return "No unaccepted invitations to purge"

    logging.info("Purged %d unaccepted invitation(s)", purged)
    return f"Purged {purged} unaccepted invitation(s)"


async def purge_expired_invite_requests(ctx: dict[Any, Any]) -> str:
    """Delete pending invite requests `INVITE_REQUEST_RETENTION` after they were made.

    The table this app is least able to bound any other way: `POST /invite-requests` is
    anonymous, so every row here was written by somebody with no account, and the only other
    things that remove one are an operator acting on it and an invitation being sent.

    The address is never told. There is no state to keep and nothing to notify - a person
    whose request ages out may simply ask again, and the rate limits on the endpoint are
    what bound that.
    """
    cutoff = datetime.now(UTC) - INVITE_REQUEST_RETENTION
    async with local_session() as db:
        result = cast(CursorResult, await db.execute(delete(InviteRequest).where(InviteRequest.created_at < cutoff)))
        purged = result.rowcount
        await db.commit()

    if purged == 0:
        logging.info("No expired invite requests to purge")
        return "No expired invite requests to purge"

    logging.info("Purged %d expired invite request(s)", purged)
    return f"Purged {purged} expired invite request(s)"


async def purge_expired_user_sessions(ctx: dict[Any, Any]) -> str:
    """Delete `user_session` rows that can no longer authenticate anything - past their own
    `expires_at`, or revoked.

    The criterion is exactly the complement of the liveness predicate the refresh path and
    the list endpoint share (`crud_user_sessions._live`), which is why both live in that one
    module: the two drifting apart would either strand rows forever or delete live ones.

    No retention margin, unlike `purge_expired_authentication_requests` - and the asymmetry
    is the point. That table's margin protects a documented *leniency*, where a spent row is
    still read to explain itself; nothing here reads a dead session for any reason. A
    revoked row is deleted rather than kept as a tombstone because the audit trail is what
    records that a session was revoked, and this table is not a second copy of it.

    `expires_at` is `DateTime(timezone=True)`, so the comparison is UTC-aware for the reason
    `purge_expired_tokens` spells out.

    One `DELETE` reporting its own `rowcount`, like the sibling above it: Core has no
    objection to matching nothing, so there is no count-then-delete window to open.
    """
    now = datetime.now(UTC)
    async with local_session() as db:
        result = cast(CursorResult, await db.execute(delete(UserSession).where(swept_session_predicate(now))))
        # Read before the commit: the count belongs to the statement, not the transaction.
        purged = result.rowcount
        await db.commit()

    if purged == 0:
        logging.info("No dead sessions to purge")
        return "No dead sessions to purge"

    logging.info("Purged %d dead session(s)", purged)
    return f"Purged {purged} dead session(s)"


async def purge_expired_checkin_links(ctx: dict[Any, Any]) -> str:
    """Delete `checkin_link` rows no one can open any more - past their own `expires_at`, or
    revoked.

    `swept_checkin_link_predicate` is the complement of the liveness predicate the check-in
    routes read, as for sessions. No retention margin: nothing reads a dead link, and a desk
    holding one sees the same 404 whether its row is still here or not.
    """
    async with local_session() as db:
        result = cast(
            CursorResult, await db.execute(delete(CheckinLink).where(swept_checkin_link_predicate(datetime.now(UTC))))
        )
        # Read before the commit: the count belongs to the statement, not the transaction.
        purged = result.rowcount
        await db.commit()

    if purged == 0:
        logging.info("No dead check-in links to purge")
        return "No dead check-in links to purge"

    logging.info("Purged %d dead check-in link(s)", purged)
    return f"Purged {purged} dead check-in link(s)"


async def purge_expired_auth_audit_events(ctx: dict[Any, Any]) -> str:
    """Delete `auth_audit_event` rows past their tier's retention.

    Two tiers in one statement - see `AUTH_AUDIT_RETENTION` and
    `AUTH_AUDIT_ANONYMOUS_RETENTION` for why an account-tied row lives thirteen times longer
    than a user-less one, and why the shorter number is not independently chosen.

    This is the *only* thing that bounds a user-less row. The account purge's by-email arm
    reaches the ones whose address later became an account and was then deleted; every other
    address that was typed into a sign-in form and never went anywhere is bounded here and
    nowhere else - the same division `authentication_request` already has, where the cron is
    the primary mechanism and the per-account delete covers what is younger than it.

    Both cutoffs come from one `now`, so a single sweep reasons about a single instant.
    """
    now = datetime.now(UTC)
    async with local_session() as db:
        result = cast(
            CursorResult,
            await db.execute(
                delete(AuthAuditEvent).where(
                    expired_event_predicate(
                        account_cutoff=now - AUTH_AUDIT_RETENTION,
                        anonymous_cutoff=now - AUTH_AUDIT_ANONYMOUS_RETENTION,
                    )
                )
            ),
        )
        purged = result.rowcount
        await db.commit()

    if purged == 0:
        logging.info("No expired auth audit events to purge")
        return "No expired auth audit events to purge"

    logging.info("Purged %d expired auth audit event(s)", purged)
    return f"Purged {purged} expired auth audit event(s)"


# One sweep's worth of accounts. The work per account is a cascade delete over every dive,
# site, gear item and c-card the diver ever had - user 1 on one developer's machine carries
# 531 dives and 147 dive sites - so an unbounded batch is a job that can hold locks for
# minutes. Whatever is left over is named in the log line and taken by the next pass an
# hour later, which is soon enough for a deadline measured in days.
ACCOUNT_PURGE_BATCH_SIZE = 100


async def _collect_stored_file_keys(db: AsyncSession, user_id: int) -> list[str]:
    """Every blob key the account owns, read *before* anything is deleted.

    This is the step the cascade cannot do for us, and nothing warns you that it can't.
    `DELETE FROM "user"` retires `dive_file` and `certification_file` rows **inside
    Postgres**, through the FK cascades - SQLAlchemy never sees those rows, no service
    function runs, and `blob_store.delete_after_commit` is therefore never called. The
    purge would commit cleanly, report success, and leave every dive-computer export and
    every c-card scan sitting on the volume. `src/scripts/sweep_orphaned_files.py` would
    reclaim them, but it is a manual script nothing invokes - for a GDPR purge, "an
    operator might run a script one day" is not an answer.

    `certification_file` has no `user_id` of its own (it hangs off `certification`), which
    is why the second query joins rather than filtering.

    The two pictures are the third source, each row holding a rendition's key and usually
    an original's. They are here for the same reason as the other two - a purge that leaves
    the diver's face in the store is a privacy hole inside an erasure feature - and it is
    why `DELETE /user` can leave them alone: that route only flags the row, and
    `POST /auth/restore` inside the grace period should bring back a whole account rather
    than a faceless one.
    """
    dive_file_keys = (await db.execute(select(DiveFile.storage_key).where(DiveFile.user_id == user_id))).scalars().all()
    certification_file_keys = (
        (
            await db.execute(
                select(CertificationFile.storage_key)
                .join(Certification, Certification.id == CertificationFile.certification_id)
                .where(Certification.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    picture_keys = (
        await db.execute(
            select(UserPicture.rendition_storage_key, UserPicture.original_storage_key).where(
                UserPicture.user_id == user_id
            )
        )
    ).all()
    picture_file_keys = [key for row in picture_keys for key in row if key]
    return [*dive_file_keys, *certification_file_keys, *picture_file_keys]


async def _purge_one_account(db: AsyncSession, *, user_id: int, email: str, cutoff: datetime) -> bool:
    """Destroy one account inside its own transaction. Returns whether it actually went.

    The `DELETE` repeats the selection's predicate rather than naming the id alone, and
    that is the load-bearing line here. The batch is selected once and then deleted one
    account at a time, so `POST /auth/restore` can clear both columns and commit in the
    window between - and a bare `DELETE FROM "user" WHERE id = :id` would then hard-delete
    a live, just-restored account and every dive behind it. DECISIONS.md names the shape:
    *"an invariant that holds within a snapshot is not a guarantee across statements"*.
    Zero rows affected is the normal "they came back" outcome, not an error.

    `cutoff` is the run's, not a fresh `now()`: one sweep reasons about one instant, so an
    account cannot be spared by the selection and taken by the delete a second later.

    The `authentication_request` delete is by email because those rows carry a `NULL`
    `user_id` for `purpose="sign_in"` by design, so the cascade cannot reach them. It is
    partial by construction and the sweep above is the primary mechanism for that table:
    `verify_email_change` rewrites `user.email` in place, so rows created under an address
    the account has since moved off carry an email this cannot name and no `user_id` to
    follow. Nothing short of storing every historical address would close that, and the
    seven-day sweep bounds it anyway.

    **`auth_audit_event` needs the identical second arm, and for the identical reason.**
    Its account-tied rows go down the FK cascade with the `DELETE FROM "user"` below, but
    the pre-account ones - auth request created, sign-in code failed, onboarding started,
    invite requested - are written with `user_id IS NULL` on purpose (`request_email_link` structurally cannot
    know whether an account exists, and making it find out would be the enumeration
    guarantee reversed), so no cascade will ever reach them. Without this statement, "audit
    rows are erased with the account" would be false for exactly the rows that name an
    address - and it is partial in the same way, and bounded by the same kind of sweep.
    """
    keys = await _collect_stored_file_keys(db, user_id)

    await db.execute(delete(AuthenticationRequest).where(AuthenticationRequest.email == email))
    await db.execute(delete(AuthAuditEvent).where(AuthAuditEvent.email == email))
    # **Lowercased, unlike the two deletes directly above, and the difference is not an
    # inconsistency to tidy away.** `email` here is the stored `User.email`, which
    # `POST /auth/complete` inserts verbatim from the onboarding token - so a Google-born
    # account's may carry capitals. The two tables above store whatever the sign-in path
    # wrote, which for those paths is already lowercased; both invitation tables store
    # lowercase unconditionally. Comparing the raw address against them would leave a purged
    # person's address sitting in the allow-list, which is the one outcome this arm exists
    # to prevent.
    #
    # The invitation delete is by address rather than by cascade for the same reason the
    # `authentication_request` one is: these rows name the *invitee*, whose `user_id` column
    # holds the inviter. An invitee's purge cannot reach them down any foreign key. The
    # inviter's own rows do go down the cascade with the `DELETE FROM "user"` below.
    purged_email = email.lower()
    await db.execute(delete(Invitation).where(Invitation.email == purged_email))
    await db.execute(delete(InviteRequest).where(InviteRequest.email == purged_email))
    result = cast(
        CursorResult,
        await db.execute(
            delete(User).where(
                User.id == user_id,
                User.is_deleted.is_(True),
                User.deleted_at.is_not(None),
                User.deleted_at < cutoff,
            )
        ),
    )
    if result.rowcount == 0:
        # Rolls back every by-address delete above with it - a restored account keeps its
        # sign-in history, its audit trail and the invitation that let it in like any other.
        await db.rollback()
        logging.info("Account %d was restored before the purge reached it; nothing deleted", user_id)
        return False

    # After the statement that retired the rows and before the commit, per
    # `blob_store.delete_after_commit`'s own rule: registering earlier would leave the
    # unlinks standing across the rollback above, and unlinking the files of an account
    # that is alive again is the one outcome here that cannot be undone.
    blob_store.delete_after_commit(db, keys)
    await db.commit()
    return True


async def purge_deleted_accounts(ctx: dict[Any, Any]) -> str:
    """Hard-delete accounts whose grace period has run out.

    The other half of `DELETE /user`, which only flags the row. `ACCOUNT_DELETION_GRACE_DAYS`
    after the request the account stops being recoverable and this destroys it: one
    `DELETE FROM "user"` per account, carrying every dive, dive site, certification,
    course, gear item, trip and stats row down the cascades declared for exactly this,
    plus the stored files those rows pointed at.

    Hourly rather than daily, so `ACCOUNT_DELETION_GRACE_DAYS=0` behaves the way an
    operator setting it to zero expects. No `run_at_startup`, unlike the two sweeps beside
    it in `core/worker/settings.py`: those are idempotent housekeeping, this one destroys
    logbooks, and a restart loop must never be the thing that decides an account's fate
    early.

    `deleted_at IS NOT NULL` is not defensive noise. `deleted_at < :cutoff` is NULL for a
    row flagged without its clock, so such an account would be dark forever and nothing
    would say so - hence the warning below, which is what would catch a future
    admin-suspension feature borrowing this column instead of getting its own.

    `ORDER BY deleted_at` with a limit makes "N left for the next pass" a real queue rather
    than a nondeterministic sample, and the leftover count is logged rather than silently
    capped.

    **No Redis sweep here, deliberately**, and it is worth saying why the obvious line is
    absent. `delete_keys_by_pattern` returns silently when `cache.client is None`, and that
    global is only ever set by the API's lifespan - which this process does not go through
    (see *"The profile backfill is a script, not an arq job"* in `DECISIONS.md`, which
    names the same trap for the same reason). A call here would be a permanent no-op
    wearing a comment about ordering and failure semantics. It is also unnecessary:
    `erase_user` swept `user_{id}_*` from inside the API when the account was flagged, and
    nothing can have repopulated it since, because every read for a deleted account 401s.
    """
    cutoff = datetime.now(UTC) - timedelta(days=settings.ACCOUNT_DELETION_GRACE_DAYS)

    async with local_session() as db:
        stranded = (
            await db.execute(
                select(func.count()).select_from(User).where(User.is_deleted.is_(True), User.deleted_at.is_(None))
            )
        ).scalar_one()
        if stranded:
            logging.warning(
                "%d deleted account(s) carry no deleted_at and will never be purged; "
                "only DELETE /user should write either column",
                stranded,
            )

        due = (
            await db.execute(
                select(User.id, User.email, User.deleted_at)
                .where(User.is_deleted.is_(True), User.deleted_at.is_not(None), User.deleted_at < cutoff)
                .order_by(User.deleted_at)
                .limit(ACCOUNT_PURGE_BATCH_SIZE)
            )
        ).all()

    if not due:
        logging.info("No accounts past their deletion grace period")
        return "No accounts to purge"

    purged = 0
    for row in due:
        gone = False
        # One session, and so one transaction, per account: a single failure strands that
        # account for the next pass rather than taking the batch with it. Leaving the block
        # on an exception closes the session, which rolls the transaction back - and the
        # rollback is also what drops any blob unlinks registered inside it.
        async with local_session() as db:
            try:
                gone = await _purge_one_account(db, user_id=row.id, email=row.email, cutoff=cutoff)
            except Exception:
                logging.exception("Could not purge account %d", row.id)

        if gone:
            purged += 1
            # The id and the request date, never the address: writing the email being
            # erased into logs that outlive the purge would be a self-inflicted wound.
            logging.info("Purged account %d (requested %s)", row.id, row.deleted_at)

    if len(due) == ACCOUNT_PURGE_BATCH_SIZE:
        logging.info("Purge batch was full; more accounts may be due on the next pass")

    logging.info("Purged %d deleted account(s)", purged)
    return f"Purged {purged} deleted account(s)"


def _due_text(row: Any, status: ServiceStatus, today: date) -> str:
    """One line of the digest, phrased from whichever interval arm is the urgent one.

    A schedule can have both a date and a dive threshold, and only one of them is
    usually the reason it turned up here - saying "due 1 Mar" about a regulator that has
    actually run out of dives would be worse than useless. So the overdue arm wins when
    the schedule is overdue, and otherwise the calendar is reported if it has a date at
    all (which is the arm divers think in).
    """
    kind = service_kind_label(row.kind)
    detail = f"{kind} ({row.label})" if row.label else kind

    date_overdue = row.next_due_on is not None and today >= row.next_due_on
    dives_remaining = row.next_due_at_dive_count - row.dive_count if row.next_due_at_dive_count is not None else None
    dives_overdue = dives_remaining is not None and dives_remaining <= 0

    if status is ServiceStatus.OVERDUE and dives_overdue and not date_overdue:
        over_by = -dives_remaining  # type: ignore[operator]  # dives_overdue implies not None
        return f"{detail} overdue by {over_by} dive{'' if over_by == 1 else 's'}"

    if date_overdue:
        return f"{detail} overdue since {row.next_due_on:%-d %b %Y}"

    if row.next_due_on is not None:
        return f"{detail} due {row.next_due_on:%-d %b %Y}"

    if dives_remaining is not None:
        return f"{detail} due in {dives_remaining} dive{'' if dives_remaining == 1 else 's'}"

    return detail


async def send_gear_service_digests(ctx: dict[Any, Any]) -> str:
    """Email each user one digest of the gear they need to get serviced.

    Runs daily on a cron (see `core/worker/settings.py`) but sends far less often than
    that: `should_notify` fires once per threshold crossing, so a diver gets one email
    when something enters "due soon", one when it goes overdue, and then only a
    quarterly nudge while it stays overdue.

    Three filters matter. Archived gear is skipped, so retiring a piece of kit silences it
    without the diver having to also pause every rule on it; `is_active` pauses one rule
    without touching the item; and `user.gear_service_emails` is the opt-out. There is no
    liveness filter on the gear halves any more - a deleted item takes its schedules with
    it - and `User.is_deleted` is the one that remains, since users still soft-delete.

    "Today" is UTC - `User` has no timezone column, and at date granularity with a
    30-day lead time being a few hours out either way changes nothing. If that ever
    matters, the upgrade is to run hourly and gate on the offset of the user's most
    recent dive.
    """
    today = datetime.now(UTC).date()
    now = datetime.now(UTC)
    soon = today + timedelta(days=SERVICE_DUE_SOON_DAYS)

    async with local_session() as db:
        rows = (
            await db.execute(
                select(
                    User.id.label("user_id"),
                    User.email,
                    GearServiceSchedule.id.label("schedule_id"),
                    GearServiceSchedule.kind,
                    GearServiceSchedule.label,
                    GearServiceSchedule.next_due_on,
                    GearServiceSchedule.next_due_at_dive_count,
                    GearServiceSchedule.notified_stage,
                    GearServiceSchedule.notified_for_due_on,
                    GearServiceSchedule.notified_for_due_at_dive_count,
                    GearServiceSchedule.notified_at,
                    GearItem.uuid.label("gear_item_uuid"),
                    GearItem.name,
                    GearItem.brand,
                    GearItem.dive_count,
                )
                .join(GearItem, GearItem.id == GearServiceSchedule.gear_item_id)
                .join(User, User.id == GearServiceSchedule.user_id)
                .where(
                    GearServiceSchedule.is_active.is_(True),
                    GearItem.is_archived.is_(False),
                    User.is_deleted.is_(False),
                    User.gear_service_emails.is_(True),
                    # A cheap pre-filter only - `should_notify` below makes the real
                    # decision. The date arm is served by
                    # `ix_gear_service_schedule_next_due_on`; the dive arm compares two
                    # tables and can't be indexed, so this is a scan of the (small)
                    # schedule table. Materializing "dives remaining" onto the row would
                    # fix that, at the cost of writing to every schedule on every dive -
                    # see DECISIONS.md for why that trade wasn't taken.
                    or_(
                        GearServiceSchedule.next_due_on <= soon,
                        and_(
                            GearServiceSchedule.next_due_at_dive_count.is_not(None),
                            GearItem.dive_count >= GearServiceSchedule.next_due_at_dive_count - SERVICE_DUE_SOON_DIVES,
                        ),
                    ),
                )
                .order_by(User.id, GearServiceSchedule.next_due_on.asc().nulls_last())
            )
        ).all()

    by_user: dict[int, dict[str, Any]] = defaultdict(lambda: {"email": "", "lines": [], "marks": []})
    for row in rows:
        status = service_status(
            next_due_on=row.next_due_on,
            next_due_at_dive_count=row.next_due_at_dive_count,
            dive_count=row.dive_count,
            today=today,
        )
        if not should_notify(
            status=status,
            next_due_on=row.next_due_on,
            next_due_at_dive_count=row.next_due_at_dive_count,
            notified_stage=row.notified_stage,
            notified_for_due_on=row.notified_for_due_on,
            notified_for_due_at_dive_count=row.notified_for_due_at_dive_count,
            notified_at=row.notified_at,
            now=now,
        ):
            continue

        bucket = by_user[row.user_id]
        bucket["email"] = row.email
        label = f"{row.brand} {row.name}" if row.brand else row.name
        bucket["lines"].append((label, _due_text(row, status, today), str(row.gear_item_uuid)))
        bucket["marks"].append((row.schedule_id, status.value, row.next_due_on, row.next_due_at_dive_count))

    if not by_user:
        logging.info("No gear service reminders to send")
        return "No gear service reminders to send"

    sent_users = 0
    sent_schedules = 0
    async with local_session() as db:
        for bucket in by_user.values():
            # Send first, mark second. If delivery fails the exception propagates before
            # the mark, so the worst case is a duplicate email tomorrow rather than a
            # reminder that silently never arrives - for gear safety that's the right
            # way round.
            await send_gear_service_digest_email(bucket["email"], bucket["lines"])

            # One executemany for the whole bucket rather than a round trip per schedule
            # - a diver whose entire kit comes due at once was previously N statements.
            # Matches how `services.dive_stats` and `services.gear_stats` write.
            #
            # No `.where()`, and the primary key travels in the dicts under its own name:
            # that is SQLAlchemy's ORM "bulk UPDATE by primary key", which lifts `id` out
            # of each dict for the WHERE clause and SETs the rest. Matching the id through
            # a `bindparam` instead compiles fine and passes a mocked session, but adds
            # *additional* WHERE criteria, which that path refuses to execute at all. →
            # DECISIONS.md, "The digest's mark is an ORM bulk UPDATE by primary key".
            marks = [
                {
                    "id": schedule_id,
                    "notified_stage": stage,
                    "notified_for_due_on": due_on,
                    "notified_for_due_at_dive_count": due_at_dive_count,
                    "notified_at": now,
                }
                for schedule_id, stage, due_on, due_at_dive_count in bucket["marks"]
            ]
            if marks:
                await db.execute(update(GearServiceSchedule), marks)
            await db.commit()
            sent_users += 1
            sent_schedules += len(bucket["lines"])

    logging.info("Sent %d gear service digest(s) covering %d schedule(s)", sent_users, sent_schedules)
    return f"Sent {sent_users} gear service digest(s) covering {sent_schedules} schedule(s)"


async def send_renewal_reminders(ctx: dict[Any, Any], today: date | None = None) -> str:
    """Email each diver one list of the certifications and insurance they need to renew.

    The gear digest's shape, cloned onto dates. The subjects are every live certification
    with an `expires_on` and the account's dive insurance when it has an expiry; each is
    sent once on entering the window and once on expiring (`services.renewals.should_remind`),
    with no re-nag after that. A renewal moves the date, and the stored pair then no longer
    matches, so the next window re-arms without anything clearing it.

    `user.renewal_reminder_emails` is the opt-out, and a soft-deleted account has no
    subjects. "Today" is UTC for the digest's reason, at a lead three times as long.

    Unbatched: it sends at most one email per diver per run. The first run over an existing
    instance is therefore a sweep of every card already expired or inside the window, once.
    """
    today = today or datetime.now(UTC).date()
    horizon = today + timedelta(days=CERTIFICATION_EXPIRING_SOON_DAYS)

    async with local_session() as db:
        # `expires_on <= horizon` implies a date is set; `expiry_stage` below makes the call.
        certifications = (
            await db.execute(
                select(
                    User.id.label("user_id"),
                    User.email,
                    Certification.id.label("certification_id"),
                    Certification.agency,
                    Certification.agency_other,
                    Certification.name,
                    Certification.expires_on,
                    Certification.expiry_notified_stage,
                    Certification.expiry_notified_for,
                )
                .join(User, User.id == Certification.user_id)
                .where(
                    Certification.is_deleted.is_(False),
                    User.is_deleted.is_(False),
                    User.renewal_reminder_emails.is_(True),
                    Certification.expires_on <= horizon,
                )
            )
        ).all()
        insurances = (
            await db.execute(
                select(
                    User.id.label("user_id"),
                    User.email,
                    User.insurance_provider,
                    User.insurance_expires_on,
                    User.insurance_notified_stage,
                    User.insurance_notified_for,
                ).where(
                    User.is_deleted.is_(False),
                    User.renewal_reminder_emails.is_(True),
                    User.insurance_expires_on <= horizon,
                )
            )
        ).all()

    by_user: dict[int, dict[str, Any]] = defaultdict(lambda: {"email": "", "lines": [], "marks": [], "insurance": None})
    for row in certifications:
        stage = expiry_stage(row.expires_on, today)
        if stage is None or not should_remind(
            stage=stage,
            expires_on=row.expires_on,
            notified_stage=row.expiry_notified_stage,
            notified_for=row.expiry_notified_for,
        ):
            continue
        bucket = by_user[row.user_id]
        bucket["email"] = row.email
        label = certification_label(agency=row.agency, agency_other=row.agency_other, name=row.name)
        bucket["lines"].append((row.expires_on, label, expiry_text(stage, row.expires_on), "/certifications"))
        bucket["marks"].append(
            {"id": row.certification_id, "expiry_notified_stage": stage.value, "expiry_notified_for": row.expires_on}
        )
    for policy in insurances:
        stage = expiry_stage(policy.insurance_expires_on, today)
        if stage is None or not should_remind(
            stage=stage,
            expires_on=policy.insurance_expires_on,
            notified_stage=policy.insurance_notified_stage,
            notified_for=policy.insurance_notified_for,
        ):
            continue
        bucket = by_user[policy.user_id]
        bucket["email"] = policy.email
        text = expiry_text(stage, policy.insurance_expires_on)
        # `/settings`, where the policy is entered, as the Renewals card's insurance row links.
        bucket["lines"].append(
            (policy.insurance_expires_on, insurance_label(policy.insurance_provider), text, "/settings")
        )
        bucket["insurance"] = (stage.value, policy.insurance_expires_on)

    if not by_user:
        logging.info("No renewal reminders to send")
        return "No renewal reminders to send"

    sent_users = 0
    sent_subjects = 0
    async with local_session() as db:
        for user_id, bucket in by_user.items():
            # Soonest first, cards and insurance in one list, as the Renewals card orders them.
            lines = [(label, text, path) for _, label, text, path in sorted(bucket["lines"])]
            # Send, then mark, for the digest's reason: a failed send is a duplicate tomorrow
            # rather than a reminder that never arrives.
            await send_renewal_reminder_email(bucket["email"], lines)

            # An ORM bulk UPDATE by primary key with no `.where()`, as the digest's mark is -
            # DECISIONS.md, "The digest's mark is an ORM bulk UPDATE by primary key".
            if bucket["marks"]:
                await db.execute(update(Certification), bucket["marks"])
            if bucket["insurance"] is not None:
                stage_value, expires_on = bucket["insurance"]
                await db.execute(
                    update(User)
                    .where(User.id == user_id)
                    .values(insurance_notified_stage=stage_value, insurance_notified_for=expires_on)
                )
            await db.commit()
            sent_users += 1
            sent_subjects += len(lines)

    logging.info("Sent %d renewal reminder(s) covering %d subject(s)", sent_users, sent_subjects)
    return f"Sent {sent_users} renewal reminder(s) covering {sent_subjects} subject(s)"


async def send_year_in_review(ctx: dict[Any, Any], today: date | None = None) -> str:
    """Email divers their previous calendar year in figures, through January.

    Acts only while the UTC month is January (`services.year_in_review.reviewed_year`), and
    sends at most `YEAR_IN_REVIEW_BATCH_SIZE` reviews a run so the day's mail fits the relay's
    ceiling; the rest are taken on the following days. The order is account creation, oldest
    first, so a batch is a queue rather than a sample.

    A diver is eligible with `user.year_in_review_emails` on, a live account, no review sent
    for that year yet, and at least one live dive in it by the dive's own local day. The query
    pre-filters on a window of stored instants a day wider than the year; `year_in_review`
    decides, and a diver whose only dives in the window fall outside the year is skipped
    without counting against the batch.

    Marked after each send and committed per diver, as the digest is: a send the relay refuses
    raises before its mark, ends the run, and leaves that diver for the next one.
    """
    today = today or datetime.now(UTC).date()
    year = reviewed_year(today)
    if year is None:
        return "No year in review outside January"

    window_start, window_end = year_window(year)
    dived_in_window = (
        select(Dive.id)
        .where(
            Dive.user_id == User.id,
            Dive.is_deleted.is_(False),
            Dive.start_time >= window_start,
            Dive.start_time < window_end,
        )
        .exists()
    )

    sent = 0
    async with local_session() as db:
        candidates = (
            await db.execute(
                select(User.id, User.email, User.units)
                .where(
                    User.is_deleted.is_(False),
                    User.year_in_review_emails.is_(True),
                    or_(User.year_in_review_sent_for.is_(None), User.year_in_review_sent_for < year),
                    dived_in_window,
                )
                .order_by(User.created_at, User.id)
            )
        ).all()

        for candidate in candidates:
            if sent == YEAR_IN_REVIEW_BATCH_SIZE:
                break
            review = await year_in_review(db, candidate.id, year)
            if review is None:
                continue
            await send_year_in_review_email(candidate.email, review, candidate.units)
            await db.execute(update(User).where(User.id == candidate.id).values(year_in_review_sent_for=year))
            await db.commit()
            sent += 1

    if sent == YEAR_IN_REVIEW_BATCH_SIZE:
        logging.info("Year-in-review batch was full; more divers may be due on the next run")
    logging.info("Sent %d year-in-review email(s) for %d", sent, year)
    return f"Sent {sent} year-in-review email(s) for {year}"


# -------- base functions --------
async def startup(ctx: Worker) -> None:
    """Prove the blob store is reachable and writable before any cron runs.

    The same probe the API's lifespan makes, and it is here because the worker is not a
    read-only service: `purge_deleted_accounts` deletes every blob a purged diver owned,
    through `blob_store.delete_after_commit`. Without this, a worker whose credentials are
    wrong - or whose volume is mounted but root-owned - starts cleanly, logs "Worker
    Started", and fails silently at :30 past the first hour that has an account to purge: a
    GDPR erasure that reports success while leaving the diver's c-card scans behind.

    **It does not catch a `local` volume that was never mounted**, and that is worth stating
    because the probe looks like it would. The image creates `/data/files` owned by uid 1000
    before dropping to that user, so an unmounted worker writes its probe into its own
    container layer and passes. Only `docker-compose.yml` prevents that one, with the mount
    on this service, and the comment there says so.

    Raising takes the worker down, which is the point: arq surfaces a failed startup rather
    than running the crons anyway, so the container restarts into the same loud error
    instead of quietly doing half its job.
    """
    await asyncio.to_thread(blob_store.ensure_storage_ready)
    logging.info("Worker Started")


async def shutdown(ctx: Worker) -> None:
    logging.info("Worker end")
