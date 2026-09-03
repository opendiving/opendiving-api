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
stays ignorant of invitations entirely.

**The bootstrap exemption.** While the `user` table is empty, the address is admitted
whatever the mode, and the row it creates carries `is_superuser = true`. That resolves the
deadlock a fresh `invite`-mode instance would otherwise have - no invitations exist and no
superuser exists to create one - and gives a self-hoster an operator account without SQL or
a script that is not in the shipped image. It is a property of the table being empty rather
than a one-shot flag: `_purge_one_account` hard-deletes the `User` row, so an instance whose
last account is purged is a fresh instance again and its next signer-in is its operator,
which is the same sentence the install docs already carry.
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import RegistrationMode, settings
from ..core.exceptions.http_exceptions import ForbiddenException
from ..crud.crud_invitations import live_invitation_exists
from ..models.user import User

logger = logging.getLogger(__name__)

# The one sentence this app says to an address nobody invited, at both gate sites, so the
# person who followed their link and the person who submitted the profile form read the
# same thing. It names the way forward rather than only the refusal - on an invite-mode
# instance the landing page is where an invitation is asked for.
NOT_INVITED = "This address hasn't been invited to this instance yet. You can request an invitation from the home page."

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
    """Whether this instance admits only invited addresses.

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


async def refuse_uninvited(db: AsyncSession, *, email: str) -> None:
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
    if await live_invitation_exists(db, email=email.lower()):
        return
    if await _no_accounts_exist(db):
        return

    raise ForbiddenException(NOT_INVITED)


async def admit_or_refuse(db: AsyncSession, *, email: str) -> bool:
    """The authoritative gate. Returns whether this account is the bootstrap one.

    **Call it below `release_read_transaction` and inside the transaction that inserts the
    row**, or it decides nothing: the rollback in that helper discards the lock and every
    read taken above it, and a gate whose answer is thrown away before the insert is not a
    gate. The caller commits; a caller that raises must roll back, since the advisory lock
    lives until the transaction ends and `async_get_db` does not end it on unwind.

    `True` means the `user` table was empty under the lock, so the row about to be created
    is the instance's first and carries `is_superuser = true`. It is returned rather than
    re-derived by the caller because the answer is only true *inside this transaction*, and
    asking again after the insert would answer `False` - the caller's own row is now there.
    """
    await _hold_the_gate(db)

    if await _no_accounts_exist(db):
        logger.info("Admitting the first account on an empty instance; it will be a superuser")
        return True

    if registration_is_invite_only() and not await live_invitation_exists(db, email=email.lower()):
        raise ForbiddenException(NOT_INVITED)

    return False
