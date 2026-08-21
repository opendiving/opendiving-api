import uuid as uuid_pkg
from datetime import UTC, datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, LargeBinary, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin


class WebauthnCredential(Base, PublicUUIDMixin):
    """One passkey (WebAuthn discoverable credential) a user has registered.

    Deliberately *not* an `AuthenticationProvider` row. That table allows one row per
    `(user_id, provider)` and carries no per-credential state, while passkeys are
    many-per-user and each one has its own public key, signature counter and transport
    list. Nothing reads `authentication_provider` except `resolve_identity`'s
    provider-link branch, which a passkey assertion never traverses - it resolves
    `credential_id` straight to a user - so a `provider="passkey"` marker row would be
    bookkeeping (create-on-first, delete-on-last) that no query needs. This table is the
    single source of truth for "does this user have passkeys".

    Hard-deleted rather than soft-deleted, like `Trip`/`DiveSite`/`GearItem`: revoking a
    passkey has to actually stop it authenticating, and a `deleted_at` on the row the
    assertion looks up is one forgotten filter away from not doing that.
    """

    __tablename__ = "webauthn_credential"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, primary_key=True, init=False)

    # `CASCADE` from day one: the account purge deletes accounts with a
    # `DELETE FROM "user"`, and a credential that outlived its user would still be a live
    # sign-in path pointing at a row that no longer exists.
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)

    # The assertion's lookup key, kept as the raw bytes the ceremony produces - base64url
    # is an encoding for the HTTP edge, not a storage format. Unique across all users:
    # a sign-in ceremony names no account, so this column alone has to identify one.
    #
    # `LargeBinary` here (and on `public_key`) does not contradict "uploaded payloads go
    # on the files volume, never in a column": these are a few hundred bytes of protocol
    # material this server verified itself, not a payload anyone uploaded, and every read
    # of the row needs them.
    credential_id: Mapped[bytes] = mapped_column(LargeBinary, unique=True, index=True)

    # The COSE-encoded public key, exactly as py_webauthn hands it over - it is also what
    # `verify_authentication_response` wants back, so nothing re-encodes it.
    public_key: Mapped[bytes] = mapped_column(LargeBinary)

    name: Mapped[str] = mapped_column(String(50))

    # The authenticator's signature counter. `BigInteger` because the spec's field is a
    # 32-bit unsigned integer and a plain `Integer` is signed in Postgres - a security key
    # that ever passes 2^31 would otherwise overflow the column rather than the counter.
    # Synced passkeys report 0 forever, which is legal and stays 0 here.
    sign_count: Mapped[int] = mapped_column(BigInteger, default=0)

    # Transport hints ("internal", "hybrid", "usb", ...) echoed back into the
    # `excludeCredentials` descriptors, which is what lets a browser skip the "insert your
    # security key" prompt for an authenticator that is already registered. Advisory
    # metadata from the client, so nullable and never trusted for anything security-bearing.
    transports: Mapped[list[str] | None] = mapped_column(JSON, default=None)

    # The authenticator model's identifier. Stored because it is free at registration time
    # and is what a "signed in with your iPhone" icon would eventually need; nothing reads
    # it in v1, and no attestation or AAGUID policy is enforced anywhere - deliberately,
    # since enforcing one would restrict which authenticators a diver may use.
    aaguid: Mapped[uuid_pkg.UUID | None] = mapped_column(Uuid, default=None)

    # The spec's backup-state flag: true for a passkey synced to a cloud keychain, false
    # for one bound to a single device. Refreshed on every assertion, since a credential
    # can start device-bound and later be backed up.
    backed_up: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
