import re
import uuid as uuid_pkg
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


# -------------- email magic link --------------
class EmailAuthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Annotated[EmailStr, Field(examples=["diver@example.com"])]


class EmailAuthRequestResponse(BaseModel):
    """Always the exact same message regardless of whether `email` belongs to an
    existing account - see `POST /auth/email/request`. Never add a field here that
    could let a caller distinguish the two cases (e.g. "user found"/"user created").

    `request_id` does not distinguish them: a row is minted for every address, account or
    not, so its public uuid is a fresh random value either way. It is the handle the
    caller needs to redeem the six-digit code from the same email
    (`POST /auth/email/verify-code`), and handing it *only* to the browser that asked is
    what keeps that endpoint out of reach of anyone else - see `verify_email_code`.
    """

    message: str = "Check your email for the next step."
    request_id: uuid_pkg.UUID


class EmailVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str


class EmailCodeVerifyRequest(BaseModel):
    """`POST /auth/email/verify-code` - the six-digit code from the sign-in email, plus
    the `request_id` that `POST /auth/email/request` handed back to the tab that asked.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: Annotated[uuid_pkg.UUID, Field(examples=["0198f0c1-4b6f-7c3a-9d2e-5a1b7c8d9e0f"])]
    code: Annotated[str, Field(examples=["481052"])]

    @field_validator("code")
    @classmethod
    def _six_digits_however_they_were_typed(cls, value: str) -> str:
        """The email prints the code as `481 052`, so a copy-paste arrives with the space
        in it - and a hyphen or a non-breaking space is just as plausible from a mail
        client that reflows the body. Strip everything that isn't a digit, then insist on
        exactly six of them.

        Rejecting a malformed code here rather than counting it as a guess is deliberate:
        `code_attempts` bounds *guesses at the secret*, and a five-character string was
        never one. It also cannot be an oracle - the answer depends only on what the
        caller typed, never on the row.
        """
        digits = re.sub(r"\D", "", value)
        if len(digits) != 6:
            raise ValueError("The sign-in code is six digits.")
        return digits


class LinkCheckResponse(BaseModel):
    """Side-effect-free response for the `GET .../verify/check` precheck endpoints
    (`api.v1.auth.check_email_link`, `api.v1.users.check_email_change_link`) - used
    by the corresponding confirmation page *before* it shows its "Sign in"/"Confirm
    email change" button, so re-opening an already-used, invalidated, or expired
    link (e.g. via the browser's back button) shows an error immediately instead of
    a misleadingly clickable button. `email` is only set when `valid` is `True`, so
    the page can show what it's about to sign in as / change the address to.

    `deletion_pending` is `valid=True` plus a flag rather than a fourth way to be invalid,
    and the choice is deliberate. The link *works*: redeeming it reaches the restore
    screen, which is somewhere worth going. `valid=False` is reserved for "this link is
    dead, ask for another one", so the page keeps its existing "not valid" branch first and
    adds a second one that relabels the button - *Restore my account* rather than *Sign in*
    - instead of having to move it.

    Only `api.v1.auth.check_email_link` ever sets it. The email-change precheck shares this
    schema and leaves both fields at their defaults: it runs behind a live session, and
    there is no such thing as a pending-deletion account with one.
    """

    valid: bool
    email: str | None = None
    deletion_pending: bool = False
    purge_after: datetime | None = None


# -------------- google --------------
class GoogleAuthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The ID token (a JWT) returned to the frontend by Google Identity Services after
    # the user picks an account.
    credential: str


# -------------- profile completion --------------
class ProfileCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    onboarding_token: str
    name: Annotated[str, Field(min_length=2, max_length=30, examples=["User Userson"])]
    username: Annotated[str, Field(min_length=2, max_length=20, pattern=r"^[a-z0-9]+$", examples=["userson"])]


# -------------- shared outcome --------------
class AuthOutcome(BaseModel):
    """Unified response for every entry point into the app (`/auth/email/verify`,
    `/auth/email/verify-code`, `/auth/google`, `/auth/passkey/verify`, `/auth/complete`,
    `/auth/restore`), and it has three shapes:

    - `status="authenticated"` - signed in, with a fresh access token and a
      `refresh_token` cookie set on the response.
    - `status="onboarding_required"` - no account exists yet for the verified identity, so
      `onboarding_token` must be carried forward to `POST /auth/complete` to create one.
    - `status="deletion_pending"` - an account exists and is inside its deletion grace
      period. **No session was issued and nothing was changed**; `restore_token` is carried
      to `POST /auth/restore`, which is the one thing that brings the account back, and
      `purge_after` is the date it stops being recoverable at all.

    A client that does not know the third status must not treat it as a sign-in: there is
    no `access_token` in it.
    """

    status: Literal["authenticated", "onboarding_required", "deletion_pending"]

    # Set only when status == "authenticated".
    access_token: str | None = None
    token_type: str | None = None

    # Set only when status == "onboarding_required". There is deliberately no `avatar`
    # here: a Google identity's picture rides the *onboarding token* to
    # `POST /auth/complete`, which fetches and stores it as the new account's avatar
    # (`services/user_avatars.py`). It was never rendered by anything, and an account's
    # picture is now served from this instance rather than named by a third-party URL.
    onboarding_token: str | None = None
    name: str | None = None

    # Set only when status == "deletion_pending". `purge_after` is null only for a row
    # flagged with no clock to count from - see `services.auth_service.DeletionPending`.
    restore_token: str | None = None
    purge_after: datetime | None = None

    # Set for both "onboarding_required" and "deletion_pending": the address the identity
    # was proven with, so each screen can say which account it is talking about.
    email: str | None = None


class RestoreRequest(BaseModel):
    """`POST /auth/restore` - the explicit second step that undoes a deletion, carrying the
    `restore_token` from a `deletion_pending` outcome.
    """

    model_config = ConfigDict(extra="forbid")

    restore_token: str
