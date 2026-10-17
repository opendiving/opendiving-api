"""May this verified address create an account right now?

One predicate, asked in two places for two different reasons, with one implementation.

**At account creation, authoritatively** (`POST /auth/complete`). The check has to sit
*with* the insert, and "with" has a precise meaning: `complete_profile` calls
`release_read_transaction` between its duplicate checks and its insert, and that call
**rolls back**. A gate query, an emptiness check or a lock taken above that line is
discarded before the row is written. So `admit_or_refuse` is called below it, inside the
transaction the handler's own `db.commit()` ends - which is also what makes a revocation or
a mode flip committed between verification and completion honoured, rather than raced past.

**At the onboarding branch, for the person's benefit** (`_start_onboarding_or_sign_in`).
`refuse_uninvited` runs before an onboarding token is minted, so nobody fills in a profile
form only to be refused at the end. It is read-only and not authoritative: everything it
learns can change before the completion, which is why the check above still exists.

**Neither is an enumeration oracle.** Both run only after the caller has proven ownership
of the address - a magic link delivered to it, the code from that email, or Google's
`email_verified` claim - so the 403 tells them nothing about an address that is not theirs.
The endpoint that *is* reachable without proving anything, `POST /auth/email/request`,
stays ignorant of invitations entirely; the one thing it learns is whether the `via` it was
handed is still a live join link or a member's current invite link, which is a fact about a
link and not about an address.

**What it returns is the door.** `admit_or_refuse` answers with the source the new account
is counted under in the daily totals: `bootstrap`, `open`, `invitation`, `waitlist`, or the
join-channel slug the verified identity arrived through. `via` is that slug, or the token
of a member's invite link. In order: an empty table admits as the operator; open mode admits
under the slug if a configured one came along, else `open`, and a token attributes nothing
there; in invite mode a configured slug admits under itself, even over an invitation - the
link is what the person clicked; then a member's live invite link admits under `invitation`
while its owner is under their quota, naming the owner so the completion can write their
row; then a live invitation admits, under `waitlist` if the operator sent any of them from
the queue - including for an address that followed a link whose owner is at quota, since
the link was never what let that address in; then a link at quota is refused as spent, any
other `via` as a stale link, and anything left as uninvited. `refuse_uninvited` asks the
same questions without the lock and without an answer.

**The bootstrap exemption.** While the `user` table is empty, the address is admitted
whatever the mode, and the row it creates carries `is_superuser = true`. That resolves the
deadlock a fresh `invite`-mode instance would otherwise have - no invitations exist and no
superuser exists to create one - and gives a self-hoster an operator account without SQL.
It is a property of the table being empty rather than a one-shot flag: `_purge_one_account`
hard-deletes the `User` row, so an instance whose last account is purged is a fresh
instance again and its next signer-in is its operator, which is the same sentence the
install docs already carry.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import AccountSource, RegistrationMode, settings
from ..core.exceptions.http_exceptions import ForbiddenException
from ..crud.crud_invitations import invitation_source, live_invitation_exists
from ..models.user import User
from .invite_links import LiveInviteLink, can_still_invite, invite_link_exists, live_invite_link

logger = logging.getLogger(__name__)

# The one sentence this app says to an address nobody invited, at both gate sites, so the
# person who followed their link and the person who submitted the profile form read the
# same thing. It names the way forward rather than only the refusal - on an invite-mode
# instance the landing page is where an invitation is asked for.
NOT_INVITED = "This address hasn't been invited to this instance yet. You can request an invitation from the home page."

# What somebody holding a join link that has since been removed is told, at every site that
# can refuse one. Not `NOT_INVITED`: "hasn't been invited" reads as nonsense to a person who
# followed a link that said they were.
STALE_JOIN_LINK = "This join link is no longer active. You can request an invitation from the home page."

# `STALE_JOIN_LINK`'s sibling for an instance with no join channels configured, where a dead
# `via` can only have been a member's invite link - which is never called a join link.
STALE_INVITE_LINK = "This invite link is no longer active. You can request an invitation from the home page."

# What an address nobody invited is told when the link it followed belongs to a member who
# has used up their invitation quota.
SPENT_INVITE_LINK = (
    "This invite link can't admit anyone else right now. You can request an invitation from the home page."
)

# The advisory-lock key the bootstrap decision is serialised on. `pg_advisory_xact_lock`
# takes a namespace of the caller's choosing, so the value is arbitrary - but it is not
# arbitrary in the sense of "pick anything", because this is the app's **second** advisory
# lock: `core.setup.apply_migrations` takes `_SCHEMA_BOOTSTRAP_LOCK_KEY` to serialise the
# startup `alembic upgrade head`. Postgres scopes an advisory lock to the connected
# *database* and otherwise keys it on the value alone - the lock tag carries the database
# OID and nothing else identifying the caller - so within one instance a third key has to be
# checked against both of these rather than assumed unique. `git grep advisory_xact_lock --
# src/app` is the check. (The per-database scoping is also why the suite, which runs against
# `<db>_test`, cannot contend with the dev database on this key even though it uses it.)
_REGISTRATION_LOCK_KEY = 0x0D1E_0001


def registration_is_invite_only() -> bool:
    """Whether this instance admits only invited addresses, and whoever follows a member's
    invite link.

    A function rather than a module constant because `settings` is read at import and a
    constant computed here would be a second import-time snapshot for a test to have to
    patch in two places.
    """
    return settings.REGISTRATION_MODE is RegistrationMode.INVITE


async def _no_accounts_exist(db: AsyncSession) -> bool:
    """Whether the `user` table is empty - the bootstrap exemption's whole condition.

    Counts rather than reading `EXISTS`, and includes soft-deleted rows: an account inside
    its deletion grace period is still an account, still holds its address, and can still
    be restored, so an instance holding one is not a fresh instance.
    """
    return not await db.scalar(select(func.count()).select_from(User))


async def _hold_the_gate(db: AsyncSession) -> None:
    """Take the transaction-scoped advisory lock the bootstrap decision is made under.

    `pg_advisory_xact_lock` rather than a table lock or a `SERIALIZABLE` transaction: it is
    released by the transaction ending, whichever way it ends, so there is no unlock to
    forget on the refusal path - and it contends with nothing but another account creation.

    **Serialising every account creation, not only the bootstrap one.** Two concurrent
    first sign-ups against an empty table would otherwise both see it empty and both be
    created with `is_superuser = true`, and the emptiness check cannot be made atomic on
    its own - there is no row to lock, which is precisely the problem. Taking the lock
    before the check, unconditionally, is the version of this with one code path rather
    than two and a race between them. The cost is that account creations queue, which for
    a human-paced act on an instance with an invitation list is not a cost.
    """
    await db.execute(select(func.pg_advisory_xact_lock(_REGISTRATION_LOCK_KEY)))


def configured_channel(via: str | None) -> str | None:
    """`via` if it names a join channel configured right now, else `None`.

    Read from `settings` at every call rather than once, so a channel removed between a
    sign-in request and its completion is refused at the completion, as a revoked
    invitation is.
    """
    return via if via is not None and via in settings.join_channels else None


@dataclass(frozen=True, slots=True)
class Admission:
    """`admit_or_refuse`'s answer: the door the account is counted under, and - when a
    member's invite link admitted it - the member whose accepted invitation the completion
    writes."""

    source: str
    invite_link_owner_id: int | None = None


def _stale_link_sentence() -> str:
    """A dead `via` is called a join link only where join links exist."""
    return STALE_JOIN_LINK if settings.join_channels else STALE_INVITE_LINK


async def refuse_a_stale_join_link(db: AsyncSession, via: str | None) -> str | None:
    """What `POST /auth/email/request` records as the request's `via`, refusing a dead link.

    A `via` naming neither a configured channel nor a member's current invite link is a 403
    in invite mode, before anything is written, so the person reads it on the form they are
    looking at rather than after the mailbox hop. In open mode it is dropped and the request
    goes ahead: that mode admits the address anyway, and the sentence's way forward - the
    request form - does not exist there.

    This reveals whether a link is live, never anything about an address or an account: the
    invite-link read is `invite_link_exists`, on that table alone. Whether the link's owner
    is deleted or at quota is the gates' question, asked after the address is proven.
    """
    if via is None or configured_channel(via) is not None:
        return via
    if not registration_is_invite_only():
        return None
    if await invite_link_exists(db, token=via):
        return via
    raise ForbiddenException(_stale_link_sentence())


async def _admitting_link(db: AsyncSession, via: str | None) -> tuple[LiveInviteLink | None, bool]:
    """The live invite link `via` names, if any, and whether its owner can still invite.

    Read at the moment of each decision, as `configured_channel` is, so a link reset between a
    sign-in request and its completion is refused at the completion.
    """
    link = await live_invite_link(db, token=via) if via is not None else None
    if link is None:
        return None, False
    return link, await can_still_invite(db, link)


def _refusal(via: str | None, link: LiveInviteLink | None) -> ForbiddenException:
    if link is not None:
        return ForbiddenException(SPENT_INVITE_LINK)
    if via is not None:
        return ForbiddenException(_stale_link_sentence())
    return ForbiddenException(NOT_INVITED)


async def refuse_uninvited(db: AsyncSession, *, email: str, via: str | None = None) -> None:
    """Raise `ForbiddenException` if this address would be refused at account creation.

    The advisory check, run at the onboarding branch so the refusal arrives before the
    profile form rather than after it. No lock and no write: everything here is a read, and
    a false "you may proceed" costs only that the authoritative check refuses a moment
    later, which `ProfileCompletionForm` already surfaces.

    In `open` mode this is a no-op, and on an empty instance it admits - the bootstrap
    account has to be able to reach onboarding like anyone else.
    """
    if not registration_is_invite_only():
        return
    if configured_channel(via) is not None:
        return
    link, admits = await _admitting_link(db, via)
    if admits:
        return
    if await live_invitation_exists(db, email=email.lower()):
        return
    if await _no_accounts_exist(db):
        return

    raise _refusal(via, link)


async def admit_or_refuse(db: AsyncSession, *, email: str, via: str | None = None) -> Admission:
    """The authoritative gate. Returns the source the new account is counted under, and the
    owner of the invite link that admitted it, if one did.

    **Call it below `release_read_transaction` and inside the transaction that inserts the
    row**, or it decides nothing: the rollback in that helper discards the lock and every
    read taken above it, and a gate whose answer is thrown away before the insert is not a
    gate. The caller commits; a caller that raises must roll back, since the advisory lock
    lives until the transaction ends and `async_get_db` does not end it on unwind.

    `bootstrap` means the `user` table was empty under the lock, so the row about to be
    created is the instance's first and carries `is_superuser = true`. It is returned rather
    than re-derived by the caller because the answer is only true *inside this transaction*,
    and asking again after the insert would say otherwise - the caller's own row is now
    there. The same holds for `waitlist`: `accept_invitations` clears the flag it is read
    from, later in the same transaction - and for the link's owner, whose quota the row the
    completion writes for them then counts against.
    """
    await _hold_the_gate(db)

    if await _no_accounts_exist(db):
        logger.info("Admitting the first account on an empty instance; it will be a superuser")
        return Admission(AccountSource.BOOTSTRAP)

    channel = configured_channel(via)
    if not registration_is_invite_only():
        return Admission(channel or AccountSource.OPEN)
    if channel is not None:
        return Admission(channel)

    link, admits = await _admitting_link(db, via)
    if link is not None and admits:
        # `invitation` whatever the address's other invitations say about the queue: the
        # link is the door this person used.
        return Admission(AccountSource.INVITATION, invite_link_owner_id=link.owner_id)

    invited = await invitation_source(db, email=email.lower())
    if invited is not None:
        return Admission(invited)

    raise _refusal(via, link)
