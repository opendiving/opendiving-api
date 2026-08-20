"""Schemas for the `webauthn_credential` table and the two passkey ceremonies.

Two vocabularies on purpose: the row schemas keep the spec's name, because that is what
the columns are (`credential_id`, `sign_count`, an attested public key), while the
request/response schemas the API actually speaks say "passkey", because that is the word
on the button. `api.v1.passkeys` is the only module that sees both.
"""

import uuid as uuid_pkg
from datetime import datetime
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ..core.schemas import PublicUUIDSchema, RejectsExplicitNulls

# What the client may put in the `name` field, and what the column takes.
NAME_MAX_LENGTH = 50


# -------------------- rows --------------------
class WebauthnCredentialCreateInternal(BaseModel):
    """Server-composed only - every field here comes out of a verified ceremony, except
    `name`, which is the one thing the user chose.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: int
    credential_id: bytes
    public_key: bytes
    name: str
    sign_count: int = 0
    transports: list[str] | None = None
    aaguid: uuid_pkg.UUID | None = None
    backed_up: bool = False


class WebauthnCredentialUpdate(RejectsExplicitNulls):
    """`PATCH /user/passkey/{uuid}` - renaming is the only thing about a credential a
    user can change. Everything else on the row is either the authenticator's to report
    or fixed at registration.
    """

    model_config = ConfigDict(extra="forbid")

    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("name",)

    name: Annotated[str | None, Field(default=None, min_length=1, max_length=NAME_MAX_LENGTH)]


class WebauthnCredentialUpdateInternal(BaseModel):
    """The post-assertion bump. Not a subclass of `WebauthnCredentialUpdate`: renaming
    and "this credential was just used" have no field in common, and inheriting would
    let a rename body reach the counter.
    """

    model_config = ConfigDict(extra="forbid")

    sign_count: int
    backed_up: bool
    last_used_at: datetime


class WebauthnCredentialReadInternal(PublicUUIDSchema):
    """Mirrors the table, integer keys and raw ceremony bytes included - server-side
    lookups only. `WebauthnCredentialRead` is the public shape.
    """

    id: int
    user_id: int
    credential_id: bytes
    public_key: bytes
    name: str
    sign_count: int
    transports: list[str] | None
    backed_up: bool
    created_at: datetime
    last_used_at: datetime | None


class WebauthnCredentialRead(PublicUUIDSchema):
    """What `GET /user/passkeys` returns per row.

    `credential_id` and `public_key` are deliberately absent: they are the credential's
    identity to the *authenticator*, they identify an account to anyone holding them,
    and no client has any use for them.
    """

    name: str
    backed_up: bool
    created_at: datetime
    last_used_at: datetime | None


# -------------------- ceremonies --------------------
class PasskeySignInOptions(BaseModel):
    """`POST /auth/passkey/options`.

    `options` is spec-shaped JSON straight from py_webauthn's `options_to_json_dict`, so
    `@simplewebauthn/browser` consumes it verbatim and neither side hand-rolls base64url.
    `flow_id` names the challenge that was just stored; without it the assertion cannot
    be verified against anything.
    """

    flow_id: str
    options: dict[str, Any]


class PasskeyRegistrationOptions(BaseModel):
    """`POST /user/passkey/options`. No flow id - a registration challenge is keyed by
    the account that asked for it, which the bearer token already names.
    """

    options: dict[str, Any]


class PasskeySignInVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow_id: str
    # The raw `PublicKeyCredential` from `navigator.credentials.get()`, passed through
    # untouched: py_webauthn parses and validates it, and re-declaring its shape here
    # would be a second parser to keep in step with the spec.
    credential: dict[str, Any]


class PasskeyRegistrationVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential: dict[str, Any]
    name: Annotated[str, Field(min_length=1, max_length=NAME_MAX_LENGTH, examples=["iPhone"])]
