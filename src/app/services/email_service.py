"""Transactional email delivery over SMTP.

Used for the magic-link sign-in email (see `api.v1.auth.request_email_link`), the
email-change confirmation/notification pair (see `api.v1.users`), the passkey
added/removed security notices (see `api.v1.passkeys`), the account-deletion
confirmation that carries the purge date (see `api.v1.users.erase_user`), the gear-service
digest (see `core.worker.functions.send_gear_service_digests`), the invitation into a
closed instance (see `api.v1.invitations`), and the support form (see `api.v1.support`),
all funneling through `_send` so the "run a blocking client off the event loop" plumbing
only lives in one place.

SMTP rather than any vendor's HTTP API because it is the one interface every provider
and every self-hosted relay already speaks - Resend included, which is reachable as
`SMTP_HOST=smtp.resend.com` with the API key as the password. `smtplib` from the
standard library rather than `aiosmtplib` because it costs no dependency and the
codebase already standardizes on running blocking clients in a worker thread (see
`core.security`, which cites this module for the same treatment).
"""

import html
import logging
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage

import anyio

from ..core.config import EnvironmentOption, SMTPTLSMode, settings

logger = logging.getLogger(__name__)

# A hung relay otherwise pins a worker thread indefinitely - there is no client-side
# default here the way there was with an HTTP SDK.
SMTP_TIMEOUT_SECONDS = 10


class EmailDeliveryError(RuntimeError):
    """Raised when an email that carries a credential cannot be delivered."""


# Backstop for an invariant the senders and `Settings._require_from_address_with_smtp`
# already hold: every sender returns early unless `SMTP_HOST` is set, and an instance
# with `SMTP_HOST` set and no `EMAIL_FROM_ADDRESS` refuses to boot. It is a raise rather
# than an assert so that a future sender written without the check fails loudly instead
# of mailing `From: None` through an unresolved host.
_MISCONFIGURED = "Email is misconfigured: SMTP_HOST and EMAIL_FROM_ADDRESS must both be set to send."


def _build_message(to: str, subject: str, html_body: str, reply_to: str | None = None) -> EmailMessage:
    sender = settings.EMAIL_FROM_ADDRESS
    if not sender:
        raise EmailDeliveryError(_MISCONFIGURED)

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = _header_safe(subject)
    if reply_to is not None:
        message["Reply-To"] = reply_to
    # HTML-only, matching what this module has always sent; a plain-text alternative
    # would be a change to the mail itself, not to the transport.
    message.set_content(html_body, subtype="html")
    return message


def _send(message: EmailMessage) -> None:
    """Opens a connection, sends one message, closes it.

    No pooling: every sender here is human-triggered or one-digest-per-user-per-day, and
    a connection shared across worker threads buys nothing at that volume while costing
    real complexity. Exceptions propagate to the caller - `send_gear_service_digests`
    depends on that (it sends before it marks, so a failure means a duplicate tomorrow
    rather than a reminder that silently never arrives).
    """
    host = settings.SMTP_HOST
    if not host:
        raise EmailDeliveryError(_MISCONFIGURED)

    client: smtplib.SMTP
    if settings.SMTP_TLS_MODE == SMTPTLSMode.TLS:
        # The context is passed explicitly in both TLS modes: `smtplib` negotiates an
        # *unverified* connection otherwise, which is a silent downgrade on the one hop
        # carrying live sign-in tokens.
        client = smtplib.SMTP_SSL(
            host, settings.SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS, context=ssl.create_default_context()
        )
    else:
        client = smtplib.SMTP(host, settings.SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS)

    with client:
        if settings.SMTP_TLS_MODE == SMTPTLSMode.STARTTLS:
            client.starttls(context=ssl.create_default_context())
        if settings.SMTP_USERNAME:
            client.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD or "")
        client.send_message(message)


def _header_safe(value: str) -> str:
    """Flattens CR/LF so a subject can't blow up message construction.

    `EmailMessage` refuses a header containing a newline - it raises rather than emitting
    it, so there is no injection to prevent here. The problem is whose error it is: the
    support form's `subject` is typed by an unauthenticated stranger and no schema
    restricts its characters (`schemas.support`), so a `\\r\\n` in that JSON string would
    otherwise 500 in our own code before any transport was involved.

    Applied in `_build_message` rather than at that one call site, so a later sender that
    also carries user-supplied text into a subject doesn't have to remember it.
    Deliberately *not* applied to the addresses: those are `EmailStr`-validated or
    server-composed, and a newline in one is a broken configuration that should raise
    rather than be flattened into a malformed address.
    """
    return value.replace("\r", " ").replace("\n", " ")


def _refuse_to_log_credential_outside_local(what: str) -> None:
    """Guards the "no transport, so log the link instead" fallback used by the senders whose
    failure to send is worse than a 500.

    For two of the three that call it - `send_magic_link_email` and
    `send_email_change_confirmation_email` - the reason is literal: the URL they would log
    *is* a live single-use auth token. `send_invitation_email` carries no token at all (an
    invitation is an allow-list entry, not a credential) and takes this shape for the other
    half of the argument below: the consequence of a silent failure. Its own docstring says
    so.

    That fallback is a local-development convenience, and a good one - it's how you sign
    in without configuring a relay. But for the two credential-carrying senders the URL it
    prints *is* the credential, and this app's logs are read by `docker compose logs` and
    shipped to whatever collects them, so the same code path on a deployed instance would
    quietly turn a forgotten `SMTP_HOST` into sign-in tokens sitting in plaintext wherever
    those end up. Fail loudly there instead: a 500 on a sign-in attempt is recoverable and
    obvious, leaked tokens are neither. For the invitation the same raise buys something
    else - an invitee who is never told they were invited, while their inviter's quota was
    spent on it, is a failure nobody would otherwise notice.

    The line is `local`, not `production`: `local` is the one environment where reading
    the link out of the logs is the documented way to sign in, and anything else is a
    deployment whose logs more than one person can read. Whoever holds them would get a
    working sign-in link for every address that asked for one.

    Belt and braces as of the `_require_smtp_outside_local` validator in `core.config`,
    which refuses to boot such an instance with no relay at all - this stays because it
    guards the code path rather than the configuration, and because a relay that is *set*
    can still be the wrong one.
    """
    if settings.ENVIRONMENT != EnvironmentOption.LOCAL:
        raise EmailDeliveryError(f"No email transport is configured (SMTP_HOST), so {what} cannot be delivered.")


async def send_magic_link_email(email: str, magic_link_url: str, code: str) -> None:
    """Sends the sign-in email, carrying both ways to finish signing in: the magic link,
    and the six-digit `code` to type back into the tab that asked for it.

    Both are printed because they fail in opposite places. A link signs in *whichever
    device opens it*, so someone who typed their address on a desktop and reads mail on a
    phone ends up signed in inside the phone's mail-app browser - the common real-world
    magic-link failure, and a particularly bad one for an app whose reason to be at a
    desktop is a dive computer plugged into it. A code crosses that gap because a person
    carries it. The link stays first in the email because it is the stronger credential
    and the one tap fewer.

    The code is spaced as `481 052`, the shape every other service prints it in - two
    groups of three are easier to carry from one screen to another than an unbroken run.
    `POST /auth/email/verify-code` strips the separator back out, so it costs the typist
    nothing to include or omit.

    A no-op (logged, not raised) when `SMTP_HOST` isn't configured, so local
    development without a relay doesn't hard-fail `POST /auth/email/request`
    - the link and code are still generated and logged so they can be used manually.
    Anywhere but `local` that same condition raises instead, since both are live
    credentials (see `_refuse_to_log_credential_outside_local`).
    """
    if not settings.SMTP_HOST:
        _refuse_to_log_credential_outside_local("the magic-link sign-in email")
        logger.warning("SMTP_HOST not configured; magic link for %s: %s (code %s)", email, magic_link_url, code)
        return

    message = _build_message(
        to=email,
        subject="Your OpenDiving sign-in link",
        html_body=(
            "<p>Click the link below to continue signing in to OpenDiving:</p>"
            f'<p><a href="{magic_link_url}">{magic_link_url}</a></p>'
            "<p>Reading this on a different device than the one you started on? Enter this "
            "code there instead:</p>"
            f'<p style="font-size:24px;letter-spacing:3px"><strong>{code[:3]} {code[3:]}</strong></p>'
            f"<p>This link and code expire in {settings.MAGIC_LINK_TOKEN_EXPIRE_MINUTES} minutes "
            "and can only be used once. If you didn't request this, you can safely "
            "ignore this email.</p>"
        ),
    )

    # `smtplib` blocks - run it off the event loop thread so a slow or hanging relay
    # doesn't stall other requests.
    await anyio.to_thread.run_sync(_send, message)


async def send_invitation_email(email: str, inviter_name: str) -> None:
    """Tells `email` that `inviter_name` has invited them to this instance.

    **Carries no token and no query parameter**, because an invitation is an allow-list
    entry rather than a bearer credential: signing in already proves ownership of the
    address, so the invitee simply signs in *with this address* and the gate lets them
    through. What the mail therefore has to say is precisely that, which is why the address
    is in the body rather than only in the `To:` header - a person who forwards this to
    their other mailbox needs to know which one was invited.

    The inviter's name is the one thing here that is another diver's content, and it is
    what makes the invitation legible rather than a cold mail from a domain the recipient
    may not know. The legal pages carry the corresponding grant.

    **Says nothing about the registration mode**, deliberately. `POST /admin/invitations`
    carries no mode check, so an operator on an `open` instance reaches this sender - and a
    sentence asserting registration is by invitation would be false there. The copy does not
    need one: the first paragraph says who invited them, the second says which address to
    use, and both are true in either mode. A mode-conditional clause was considered and
    rejected as disproportionate to what it would restore.

    **The opening sentence is the one thing here that branches on `PROJECT_OPERATED`**, and
    it is the only branch in this module. "Invited you to *their* OpenDiving log book" is
    exactly right on a self-hosted instance, where the inviter is the diver who runs it; on
    the instance the project operates it casts whoever pressed the button as the invitee's
    personal host, which is not what somebody who signed up on the project's own landing
    page was promised. So there the mail says they were invited to OpenDiving itself, at the
    same address. The subject is deliberately *not* branched - it names the inviter and the
    app and asserts nothing about who runs either, so both instances want the same line and
    a branch would write one sentence twice. Nothing else about the mail moves, and nothing
    here reads a second setting: `core.config` promises that one grep for this field lists
    every place the app knows who runs it.

    The credential-carrying shape (log on `local`, raise elsewhere) rather than the notice
    shape, even though nothing here is a credential. The reasoning is the *consequence* of
    a silent failure rather than the sensitivity of the payload: a notice that fails to send
    costs somebody a heads-up they can live without, while an invitation that fails to send
    is an invitee who never learns they were invited and an inviter whose quota was spent
    on nothing. The row is already committed by the time this runs, so the address is
    admitted either way and the operator's job on a 5xx is to tell them another way.
    """
    sign_in_url = f"{settings.FRONTEND_URL}/signin"

    if not settings.SMTP_HOST:
        _refuse_to_log_credential_outside_local("the invitation email")
        logger.warning("SMTP_HOST not configured; invitation for %s from %s: %s", email, inviter_name, sign_in_url)
        return

    inviter = html.escape(inviter_name)
    opening = (
        f"{inviter} has invited you to OpenDiving at "
        if settings.PROJECT_OPERATED
        else f"{inviter} has invited you to their OpenDiving log book at "
    )

    message = _build_message(
        to=email,
        subject=f"{inviter_name} invited you to OpenDiving",
        html_body=(
            f"<p>{opening}"
            f'<a href="{settings.FRONTEND_URL}">{settings.FRONTEND_URL}</a>.</p>'
            f"<p>Sign in with <strong>{html.escape(email)}</strong> - "
            "the address this was sent to - and your account will be created:</p>"
            f'<p><a href="{sign_in_url}">{sign_in_url}</a></p>'
            "<p>There is no password to choose: you enter your address, and a sign-in link and code "
            "arrive in this mailbox. If you weren't expecting this, you can safely ignore it - nothing "
            "has been created in your name.</p>"
        ),
    )

    await anyio.to_thread.run_sync(_send, message)


async def send_email_change_confirmation_email(new_email: str, confirm_url: str) -> None:
    """Sends the "confirm your new email address" link for `POST
    /user/email-change/request` - deliberately to `new_email`, not the
    account's current one, since the whole point is proving the caller actually
    controls the new address before the change takes effect.
    """
    if not settings.SMTP_HOST:
        _refuse_to_log_credential_outside_local("the email-change confirmation")
        logger.warning("SMTP_HOST not configured; email-change confirmation for %s: %s", new_email, confirm_url)
        return

    message = _build_message(
        to=new_email,
        subject="Confirm your new OpenDiving email address",
        html_body=(
            "<p>Click the link below to confirm this address as your new OpenDiving "
            "account email:</p>"
            f'<p><a href="{confirm_url}">{confirm_url}</a></p>'
            f"<p>This link expires in {settings.EMAIL_CHANGE_TOKEN_EXPIRE_MINUTES} minutes "
            "and can only be used once. If you didn't request this, you can safely "
            "ignore this email - your account email won't change.</p>"
        ),
    )

    await anyio.to_thread.run_sync(_send, message)


async def send_passkey_added_email(email: str, passkey_name: str) -> None:
    """Best-effort security notice that a passkey was added to an account.

    A passkey is a standalone sign-in method, so registering one is exactly the kind of
    change whose victim should hear about it in a channel the attacker may not hold. It
    carries no link and no token, which is why - unlike the magic-link and email-change
    senders - a missing `SMTP_HOST` just logs everywhere rather than raising outside
    `local`: there is no credential here to leak into a log.

    Every caller wraps this so a delivery failure is logged rather than raised. A
    registered passkey with a failed notification email must not roll back the
    registration - the user completed a biometric prompt and would be told it failed.
    """
    if not settings.SMTP_HOST:
        logger.warning("SMTP_HOST not configured; passkey-added notice for %s: %s", email, passkey_name)
        return

    message = _build_message(
        to=email,
        subject="A passkey was added to your OpenDiving account",
        html_body=(
            f"<p>A passkey named <strong>{html.escape(passkey_name)}</strong> was just added to your "
            "OpenDiving account, and can now be used to sign in.</p>"
            f'<p>If this wasn\'t you, <a href="{settings.FRONTEND_URL}/settings">remove it</a> and '
            "contact support.</p>"
        ),
    )

    await anyio.to_thread.run_sync(_send, message)


async def send_passkey_removed_email(email: str, passkey_name: str) -> None:
    """The other half of `send_passkey_added_email`: someone quietly stripping an
    account's passkeys is as much a signal as someone adding one.
    """
    if not settings.SMTP_HOST:
        logger.warning("SMTP_HOST not configured; passkey-removed notice for %s: %s", email, passkey_name)
        return

    message = _build_message(
        to=email,
        subject="A passkey was removed from your OpenDiving account",
        html_body=(
            f"<p>The passkey named <strong>{html.escape(passkey_name)}</strong> was just removed from your "
            "OpenDiving account, and can no longer be used to sign in.</p>"
            "<p>If this wasn't you, please contact support.</p>"
        ),
    )

    await anyio.to_thread.run_sync(_send, message)


async def send_account_deletion_email(email: str, purge_after: datetime) -> None:
    """Confirms a deletion request and names the date the account stops being recoverable.

    This is where the countdown lives, and for now it is the *only* place. The app itself
    goes dark the instant the button is pressed - deliberately - so there is no in-app
    banner to carry "12 days left", and nothing else tells the user the date. That makes
    this mail the one artifact somebody who changes their mind has to work from; see the
    last paragraph for what it can honestly ask them to do about it today.

    Best-effort, on the `send_passkey_added_email` model rather than the magic-link one:
    it carries no token, so a missing `SMTP_HOST` logs everywhere instead of raising off
    `local`. Callers wrap it too - `erase_user` has already committed the deletion by the
    time this runs, and a relay failure that turned into a 500 would leave the user locked
    out *and* never told the date, which is strictly worse than no email.

    The zero-grace branch is not cosmetic. At `ACCOUNT_DELETION_GRACE_DAYS=0` the purge
    deadline is already past when this is composed, so telling the user there is still time
    would be advice that cannot be followed - the next sweep takes the account.

    **The way back is the user's own, and the copy now says so.** Signing in during the
    window - by any of the four routes - reaches a screen offering the account back rather
    than the dead end it used to (`/auth/complete`'s "An account with this email already
    exists" against the tombstone). The mail deliberately does not carry a restore link of
    its own: a restore token is minted only against a freshly verified identity, and one
    sitting in an inbox for a fortnight is a standing key to an account its owner has
    already asked to have destroyed.
    """
    if not settings.SMTP_HOST:
        logger.warning(
            "SMTP_HOST not configured; account-deletion confirmation for %s (purge after %s)", email, purge_after
        )
        return

    if settings.ACCOUNT_DELETION_GRACE_DAYS <= 0:
        body = (
            "<p>Your OpenDiving account has been deleted.</p>"
            "<p>This instance keeps no grace period, so your dives, dive sites, "
            "courses, certifications, contacts and gear are being erased now and cannot be "
            "recovered.</p>"
        )
    else:
        body = (
            "<p>Your OpenDiving account has been deleted, and the app has already stopped "
            "letting you in.</p>"
            "<p><strong>Nothing has been erased yet.</strong> Your account, your dives, your "
            "dive sites, your courses, your certifications, your contacts and your gear will be "
            "permanently erased on "
            f"<strong>{purge_after:%-d %B %Y}</strong>.</p>"
            "<p>If you deleted your account by mistake, sign in again before that date and "
            "you'll be offered it back. After that date, nothing can be restored.</p>"
        )

    message = _build_message(to=email, subject="Your OpenDiving account has been deleted", html_body=body)

    await anyio.to_thread.run_sync(_send, message)


async def send_gear_service_digest_email(email: str, lines: list[tuple[str, str, str]]) -> None:
    """Sends the "your gear needs servicing" digest.

    One email per user per run, never one per item - a diver whose whole kit comes due
    the same week should get a single list, not six separate emails.

    Each entry in `lines` is `(gear_item_label, due_text, gear_item_uuid)`, already
    ordered and phrased by the caller. This function deliberately does no status
    arithmetic of its own, so exactly one place (`services.gear_service`) decides what
    "overdue" means.

    The label and detail are escaped: unlike the magic-link or email-change bodies, they
    are built from `GearItem.brand`/`name` and `GearServiceSchedule.label`, which the
    diver typed and which no schema restricts to safe characters (see
    `core.worker.functions.send_gear_service_digests`). Same reasoning as
    `send_support_request_email` below - content a person typed gets escaped, content this
    server composed doesn't.
    """
    if not settings.SMTP_HOST:
        logger.warning("SMTP_HOST not configured; gear service digest for %s: %s", email, lines)
        return

    items = "".join(
        f'<li><a href="{settings.FRONTEND_URL}/gear/{html.escape(item_uuid)}">'
        f"<strong>{html.escape(label)}</strong></a> - {html.escape(detail)}</li>"
        for label, detail, item_uuid in lines
    )
    subject = "Your dive gear needs servicing" if len(lines) == 1 else f"{len(lines)} pieces of gear need servicing"

    message = _build_message(
        to=email,
        subject=subject,
        html_body=(
            "<p>A quick heads-up before your next trip - this gear is due for service:</p>"
            f"<ul>{items}</ul>"
            f'<p><a href="{settings.FRONTEND_URL}/gear">Review your gear</a>, or '
            f'<a href="{settings.FRONTEND_URL}/settings">turn these reminders off</a>.</p>'
        ),
    )

    await anyio.to_thread.run_sync(_send, message)


async def send_support_request_email(name: str, email: str, category_label: str, subject: str, message: str) -> None:
    """Forwards a support-form submission to `CONTACT_FORM_EMAIL`.

    Every other sender in this module mails content this server composed itself; this
    one mails content a *stranger* typed, so it's the one place that has to escape its
    inputs - an unescaped `<a href=...>` in the message body would otherwise render as
    a live link in the recipient's mail client. It's also the only one whose text
    reaches a *header* - see `_header_safe`, which `_build_message` applies.

    `Reply-To` is the submitter's address, so hitting Reply in the inbox answers the
    diver rather than the no-reply `From` address. The message is never sent *as* them
    (`From` stays `EMAIL_FROM_ADDRESS`): the domain's SPF/DKIM only covers our own
    address, and spoofing an arbitrary sender is what gets a domain blocklisted.

    A no-op (logged, not raised) when `SMTP_HOST` isn't configured, matching the
    rest of this module - the whole submission is written to the log in that case, so a
    local instance without a relay can still see what would have been sent.

    An unset `CONTACT_FORM_EMAIL` raises rather than no-ops, and `api.v1.support` answers
    503 before it ever gets here: there is no inbox to fall back to, and the value of the
    log line above is that a developer can read what *would* have been sent - there is no
    equivalent consolation for mail with no recipient.
    """
    recipient = settings.CONTACT_FORM_EMAIL
    if not recipient:
        raise EmailDeliveryError("CONTACT_FORM_EMAIL is not set, so there is nowhere to forward this submission.")

    if not settings.SMTP_HOST:
        logger.warning(
            "SMTP_HOST not configured; support request from %s <%s> [%s] %s: %s",
            name,
            email,
            category_label,
            subject,
            message,
        )
        return

    body = html.escape(message).replace("\n", "<br>")
    mail = _build_message(
        to=recipient,
        subject=f"[{category_label}] {subject}",
        reply_to=email,
        html_body=(
            f"<p><strong>From:</strong> {html.escape(name)} &lt;{html.escape(email)}&gt;<br>"
            f"<strong>Category:</strong> {html.escape(category_label)}<br>"
            f"<strong>Subject:</strong> {html.escape(subject)}</p>"
            "<hr>"
            f"<p>{body}</p>"
        ),
    )

    await anyio.to_thread.run_sync(_send, mail)


async def send_email_changed_notification(old_email: str, new_email: str) -> None:
    """Best-effort security notice sent to an account's *previous* email address once
    a change completes, so the previous owner of that inbox finds out even if they
    weren't the one who made the change.
    """
    if not settings.SMTP_HOST:
        logger.warning("SMTP_HOST not configured; email-change notice for %s -> %s", old_email, new_email)
        return

    message = _build_message(
        to=old_email,
        subject="Your OpenDiving account email was changed",
        html_body=(
            f"<p>Your OpenDiving account email was just changed to <strong>{html.escape(new_email)}</strong>.</p>"
            "<p>If you made this change, no action is needed. If you didn't, please "
            "contact support immediately.</p>"
        ),
    )

    await anyio.to_thread.run_sync(_send, message)
