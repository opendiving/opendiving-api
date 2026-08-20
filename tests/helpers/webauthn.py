"""A software authenticator, so ceremony tests sign real bytes instead of replaying
recorded ones.

Vendored rather than depended on. The obvious package is `soft-webauthn`, whose last
release was 2022 and which pins `fido2 <1.0.0` - that would drag a five-year-old copy of
a security library into the dev environment and cap it there forever, to save the hundred
lines below. Those lines lean on `cbor2` and `cryptography`, both of which arrive with
`webauthn` itself, so this adds no dependency at all.

Recorded fixtures were the other option and are strictly worse: a challenge is generated
per ceremony, so a fixture can only ever be replayed against a challenge pinned to match
it - which means the test can never exercise the challenge check it most wants to. This
signs whatever it is given.

Deliberately minimal, and only in the ways the app never exercises: ES256 only (which
`generate_registration_options` lists first and every platform authenticator supports),
`attestation: "none"` only (which is what the app asks for), and no CTAP transport layer -
this stands in for `navigator.credentials`, not for a USB device.
"""

import hashlib
import struct
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from typing import Any

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes

# Authenticator data flag bits, per the spec: user present, user verified, attested
# credential data included.
_UP = 0x01
_UV = 0x04
_AT = 0x40
# Backup eligible / backed up. Set together here, which is what a synced platform
# passkey reports.
_BE = 0x08
_BS = 0x10


def b64url(raw: bytes) -> str:
    """Unpadded base64url - what every field in a `PublicKeyCredential` is encoded as."""
    return urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@dataclass
class SoftAuthenticator:
    """One credential on one emulated device.

    `sign_count` is settable and is the point of several tests: a synced passkey reports
    `0` forever, a security key increments, and a *regression* is the cloned-authenticator
    signal the app logs a `WARNING` for.
    """

    rp_id: str
    origin: str
    aaguid: bytes = b"\x00" * 16
    sign_count: int = 0
    backed_up: bool = True
    user_verified: bool = True
    credential_id: bytes = field(default=b"", init=False)
    _private_key: ec.EllipticCurvePrivateKey | None = field(default=None, init=False, repr=False)

    def register(self, options: dict[str, Any], *, origin: str | None = None) -> dict[str, Any]:
        """Answer creation options with an attestation - `navigator.credentials.create()`.

        `origin` overrides the device's own so a test can present a ceremony from the
        wrong site without building a second authenticator.
        """
        self.credential_id = hashlib.sha256(options["challenge"].encode()).digest()
        self._private_key = ec.generate_private_key(ec.SECP256R1())

        authenticator_data = self._authenticator_data(include_credential=True)
        attestation_object = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": authenticator_data})

        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": self._client_data("webauthn.create", options["challenge"], origin),
                "attestationObject": b64url(attestation_object),
                "transports": ["internal", "hybrid"],
            },
            "clientExtensionResults": {},
        }

    def authenticate(self, options: dict[str, Any], *, origin: str | None = None) -> dict[str, Any]:
        """Answer assertion options with a signature - `navigator.credentials.get()`.

        Does **not** advance `sign_count` on its own: every test that cares about the
        counter sets it explicitly, and one that silently moved would make the `0 -> 0`
        synced-passkey case impossible to write.
        """
        if self._private_key is None:
            raise RuntimeError("register() first - there is no credential to assert with.")

        authenticator_data = self._authenticator_data(include_credential=False)
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        signature = self._private_key.sign(
            authenticator_data + hashlib.sha256(base64url_to_bytes(client_data)).digest(),
            ec.ECDSA(hashes.SHA256()),
        )

        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": client_data,
                "authenticatorData": b64url(authenticator_data),
                "signature": b64url(signature),
                "userHandle": None,
            },
            "clientExtensionResults": {},
        }

    def _client_data(self, ceremony: str, challenge: str, origin: str | None) -> str:
        # Built by hand rather than with `json.dumps` on a dict so the key order is fixed:
        # the signature covers these exact bytes, and both sides must agree on them.
        payload = (
            f'{{"type":"{ceremony}","challenge":"{challenge}","origin":"{origin or self.origin}","crossOrigin":false}}'
        )
        return b64url(payload.encode())

    def _authenticator_data(self, *, include_credential: bool) -> bytes:
        flags = _UP
        if self.user_verified:
            flags |= _UV
        if self.backed_up:
            flags |= _BE | _BS
        if include_credential:
            flags |= _AT

        data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", self.sign_count)
        if include_credential:
            data += (
                self.aaguid
                + struct.pack(">H", len(self.credential_id))
                + self.credential_id
                + cbor2.dumps(self._cose_key())
            )
        return data

    def _cose_key(self) -> dict[int, Any]:
        """The public key as COSE_Key: kty=EC2(2), alg=ES256(-7), crv=P-256(1), x, y."""
        assert self._private_key is not None
        numbers = self._private_key.public_key().public_numbers()
        return {
            1: 2,
            3: -7,
            -1: 1,
            -2: numbers.x.to_bytes(32, "big"),
            -3: numbers.y.to_bytes(32, "big"),
        }
