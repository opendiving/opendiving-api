from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# -------------- email magic link --------------
class EmailAuthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Annotated[EmailStr, Field(examples=["diver@example.com"])]


class EmailAuthRequestResponse(BaseModel):
    """Always the exact same message regardless of whether `email` belongs to an
    existing account - see `POST /auth/email/request`. Never add a field here that
    could let a caller distinguish the two cases (e.g. "user found"/"user created").
    """

    message: str = "Check your email for the next step."


class EmailVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str


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
