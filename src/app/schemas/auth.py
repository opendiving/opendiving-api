import re
import uuid as uuid_pkg
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
    """

    valid: bool
    email: str | None = None


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
    `/auth/google`, `/auth/complete`): either the caller is signed straight in
    (`status="authenticated"`, with a fresh access token and a `refresh_token` cookie
    set on the response), or no account exists yet for the verified identity
    (`status="onboarding_required"`), in which case `onboarding_token` must be carried
    forward to `POST /auth/complete` to actually create one.
    """

    status: Literal["authenticated", "onboarding_required"]

    # Set only when status == "authenticated".
    access_token: str | None = None
    token_type: str | None = None

    # Set only when status == "onboarding_required".
    onboarding_token: str | None = None
    email: str | None = None
    name: str | None = None
    avatar: str | None = None
