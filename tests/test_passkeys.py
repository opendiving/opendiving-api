"""Unit tests for the passkey ceremonies (`api.v1.auth`'s two anonymous routes and
`api.v1.passkeys`), the challenge store, and the service that verifies them.

Every ceremony here is signed for real by `tests.helpers.webauthn.SoftAuthenticator`
against the real py_webauthn verifier - the app's crypto path is not mocked anywhere. Only
Redis, the CRUD singletons and the rate limiter are stood in for, which is the house
pattern (`tests/test_auth.py`).

`FakeRedis` is deliberately a real `GETDEL`: challenge single-use is the property the whole
design rests on, and a stub that just returned a stored value would assert it away.
"""

import logging
import uuid as uuid_pkg
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from src.app.api.v1.auth import passkey_sign_in_options, passkey_sign_in_verify
from src.app.api.v1.passkeys import (
    erase_passkey,
    passkey_registration_options,
    passkey_registration_verify,
    patch_passkey,
    read_passkeys,
)
from src.app.core.config import settings
from src.app.core.exceptions.http_exceptions import (
    BadRequestException,
    DuplicateValueException,
    NotFoundException,
    UnauthorizedException,
)
from src.app.schemas.webauthn_credential import (
    PasskeyRegistrationVerifyRequest,
    PasskeySignInVerifyRequest,
    WebauthnCredentialReadInternal,
    WebauthnCredentialUpdate,
)
from src.app.services.passkey_challenges import ChallengeStoreUnavailable
from tests.helpers.mocks import stub_claim
from tests.helpers.webauthn import SoftAuthenticator

USER_UUID = uuid_pkg.uuid4()
CREDENTIAL_UUID = uuid_pkg.uuid4()

# `src/.env` and the shipped default agree on this, and both halves of the ceremony are
# derived from it - so a test that hardcoded "localhost" separately would stop testing the
# derivation the moment it changed.
RP_ID = settings.passkey_rp_id
ORIGIN = settings.passkey_origin


class FakeRedis:
    """The three commands the challenge store uses, in a dict.

    `fail_with` turns every call into that exception, which is how the fails-closed
    behaviour is exercised - `redis-py` connects lazily, so a real outage arrives as a
    `RedisError` from an otherwise ordinary-looking client, not as a missing one.
    """

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.values: dict[str, bytes] = {}
        self.expiries: dict[str, int | None] = {}
        self.fail_with = fail_with

    async def set(self, key: str, value: bytes, ex: int | None = None) -> bool:
        if self.fail_with:
            raise self.fail_with
        self.values[key] = value
        self.expiries[key] = ex
        return True

    async def getdel(self, key: str) -> bytes | None:
        if self.fail_with:
            raise self.fail_with
        self.expiries.pop(key, None)
        return self.values.pop(key, None)


def _request(ip: str = "1.2.3.4") -> Mock:
    request = Mock()
    request.client = Mock(host=ip)
    return request


def _user(**overrides: Any) -> dict[str, Any]:
    user = {"id": 7, "uuid": USER_UUID, "email": "diver@example.com", "name": "A Diver"}
    user.update(overrides)
    return user


def _stored(**overrides: Any) -> WebauthnCredentialReadInternal:
    row: dict[str, Any] = {
        "id": 1,
        "uuid": CREDENTIAL_UUID,
        "user_id": 7,
        "credential_id": b"credential-bytes",
        "public_key": b"cose-bytes",
        "name": "iPhone",
        "sign_count": 0,
        "transports": ["internal"],
        "backed_up": True,
        "created_at": datetime.now(UTC),
        "last_used_at": None,
    }
    row.update(overrides)
    return WebauthnCredentialReadInternal(**row)


class _Ceremony:
    """A registered credential plus everything the routes need to be told about it.

    Registration is run for real here rather than faked, so `public_key` is a key the
    verifier will actually accept and `credential_id` is what the assertion will name.
    """

    def __init__(self, device: SoftAuthenticator, stored: WebauthnCredentialReadInternal) -> None:
        self.device = device
        self.stored = stored


@pytest.fixture
def redis_client():
    """Point the challenge store at an in-memory Redis for the duration of a test."""
    fake = FakeRedis()
    with patch("src.app.core.utils.cache.client", fake):
        yield fake


@pytest.fixture
def no_rate_limits():
    with (
        patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock),
        patch("src.app.api.v1.passkeys.enforce_rate_limit", new_callable=AsyncMock),
    ):
        yield


async def _register_credential(mock_db, redis_client, *, user: dict[str, Any] | None = None) -> _Ceremony:
    """Drive a full registration and hand back the credential row it would have written."""
    account = user or _user()
    device = SoftAuthenticator(rp_id=RP_ID, origin=ORIGIN)

    with patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud:
        crud.get_multi = AsyncMock(return_value={"data": []})
        crud.create = AsyncMock(
            side_effect=lambda **kwargs: _stored(
                **kwargs["object"].model_dump(),
                uuid=CREDENTIAL_UUID,
                id=1,
                created_at=datetime.now(UTC),
                last_used_at=None,
            )
        )

        options = (await passkey_registration_options(account, mock_db)).options
        attestation = device.register(options)

        with patch("src.app.api.v1.passkeys.send_passkey_added_email", new_callable=AsyncMock):
            created = await passkey_registration_verify(
                PasskeyRegistrationVerifyRequest(credential=attestation, name="iPhone"), account, mock_db
            )

    stored = _stored(
        credential_id=device.credential_id,
        public_key=crud.create.call_args.kwargs["object"].public_key,
        name=created.name,
        sign_count=0,
    )
    return _Ceremony(device, stored)


@pytest.mark.usefixtures("no_rate_limits")
class TestRegistration:
    """`POST /user/passkey/options` and `/verify` - adding a passkey from settings."""

    @pytest.mark.asyncio
    async def test_options_name_the_account_and_store_a_challenge(self, mock_db, redis_client):
        with patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud:
            crud.get_multi = AsyncMock(return_value={"data": []})

            result = await passkey_registration_options(_user(), mock_db)

        assert result.options["rp"]["id"] == RP_ID
        assert result.options["user"]["name"] == "diver@example.com"
        assert result.options["authenticatorSelection"]["residentKey"] == "required"
        assert result.options["authenticatorSelection"]["userVerification"] == "preferred"
        assert result.options["attestation"] == "none"
        assert list(redis_client.values) == ["auth:passkey-challenge:user:7"]
        assert redis_client.expiries["auth:passkey-challenge:user:7"] == settings.PASSKEY_CHALLENGE_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_the_user_handle_is_the_immutable_uuid_not_the_email(self, mock_db, redis_client):
        """Same reasoning as the token subject: a handle the authenticator stores forever
        must not be anything `PATCH /user` can change and release."""
        from webauthn.helpers import base64url_to_bytes

        with patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud:
            crud.get_multi = AsyncMock(return_value={"data": []})

            result = await passkey_registration_options(_user(), mock_db)

        assert base64url_to_bytes(result.options["user"]["id"]) == USER_UUID.bytes

    @pytest.mark.asyncio
    async def test_existing_credentials_are_excluded(self, mock_db, redis_client):
        with patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud:
            crud.get_multi = AsyncMock(return_value={"data": [_stored(credential_id=b"already-here")]})

            result = await passkey_registration_options(_user(), mock_db)

        assert len(result.options["excludeCredentials"]) == 1
        assert result.options["excludeCredentials"][0]["transports"] == ["internal"]

    @pytest.mark.asyncio
    async def test_an_unknown_stored_transport_does_not_break_the_ceremony(self, mock_db, redis_client):
        """A browser newer than py_webauthn can report a transport it has no member for,
        and that must not make registering the *next* passkey impossible."""
        with patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud:
            crud.get_multi = AsyncMock(return_value={"data": [_stored(transports=["internal", "quantum-tunnel"])]})

            result = await passkey_registration_options(_user(), mock_db)

        assert result.options["excludeCredentials"][0]["transports"] == ["internal"]

    @pytest.mark.asyncio
    async def test_the_happy_path_stores_the_verified_credential(self, mock_db, redis_client):
        ceremony = await _register_credential(mock_db, redis_client)

        assert ceremony.stored.credential_id == ceremony.device.credential_id
        assert ceremony.stored.public_key

    @pytest.mark.asyncio
    async def test_it_sends_the_security_email(self, mock_db, redis_client):
        device = SoftAuthenticator(rp_id=RP_ID, origin=ORIGIN)
        with patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud:
            crud.get_multi = AsyncMock(return_value={"data": []})
            crud.create = AsyncMock(return_value=_stored(name="Work laptop"))
            options = (await passkey_registration_options(_user(), mock_db)).options

            with patch("src.app.api.v1.passkeys.send_passkey_added_email", new_callable=AsyncMock) as send:
                await passkey_registration_verify(
                    PasskeyRegistrationVerifyRequest(credential=device.register(options), name="Work laptop"),
                    _user(),
                    mock_db,
                )

        assert send.call_args.kwargs == {"email": "diver@example.com", "passkey_name": "Work laptop"}

    @pytest.mark.asyncio
    async def test_a_failed_security_email_is_logged_not_raised(self, mock_db, redis_client, caplog):
        """The credential is already committed and the user already completed a biometric
        prompt - an SMTP timeout must not come back as "that failed"."""
        from src.app.services.email_service import EmailDeliveryError

        device = SoftAuthenticator(rp_id=RP_ID, origin=ORIGIN)
        with patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud:
            crud.get_multi = AsyncMock(return_value={"data": []})
            crud.create = AsyncMock(return_value=_stored())
            options = (await passkey_registration_options(_user(), mock_db)).options

            with (
                patch(
                    "src.app.api.v1.passkeys.send_passkey_added_email",
                    new_callable=AsyncMock,
                    side_effect=EmailDeliveryError("relay is down"),
                ),
                caplog.at_level(logging.WARNING),
            ):
                created = await passkey_registration_verify(
                    PasskeyRegistrationVerifyRequest(credential=device.register(options), name="iPhone"),
                    _user(),
                    mock_db,
                )

        assert created.name == "iPhone"
        assert "relay is down" in caplog.text

    @pytest.mark.asyncio
    async def test_the_eleventh_credential_is_refused(self, mock_db, redis_client):
        at_the_cap = [_stored(credential_id=f"key-{n}".encode()) for n in range(10)]
        with patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud:
            crud.get_multi = AsyncMock(return_value={"data": at_the_cap})

            with pytest.raises(DuplicateValueException):
                await passkey_registration_options(_user(), mock_db)

    @pytest.mark.asyncio
    async def test_the_cap_is_re_checked_after_verification(self, mock_db, redis_client):
        """Two tabs can both get options while the account is at nine. The check in
        `options` is what lets the UI say so; this one is what actually holds."""
        device = SoftAuthenticator(rp_id=RP_ID, origin=ORIGIN)
        with patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud:
            crud.get_multi = AsyncMock(return_value={"data": []})
            options = (await passkey_registration_options(_user(), mock_db)).options

            crud.get_multi = AsyncMock(
                return_value={"data": [_stored(credential_id=f"key-{n}".encode()) for n in range(10)]}
            )
            crud.create = AsyncMock()

            with pytest.raises(DuplicateValueException):
                await passkey_registration_verify(
                    PasskeyRegistrationVerifyRequest(credential=device.register(options), name="iPhone"),
                    _user(),
                    mock_db,
                )

            crud.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_replayed_attestation_is_refused(self, mock_db, redis_client):
        """The challenge is spent on the first verify, so the same attestation presented
        twice has nothing left to verify against."""
        device = SoftAuthenticator(rp_id=RP_ID, origin=ORIGIN)
        with patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud:
            crud.get_multi = AsyncMock(return_value={"data": []})
            crud.create = AsyncMock(return_value=_stored())
            options = (await passkey_registration_options(_user(), mock_db)).options
            attestation = device.register(options)

            with patch("src.app.api.v1.passkeys.send_passkey_added_email", new_callable=AsyncMock):
                await passkey_registration_verify(
                    PasskeyRegistrationVerifyRequest(credential=attestation, name="iPhone"), _user(), mock_db
                )

            with pytest.raises(BadRequestException):
                await passkey_registration_verify(
                    PasskeyRegistrationVerifyRequest(credential=attestation, name="iPhone"), _user(), mock_db
                )

    @pytest.mark.asyncio
    async def test_an_attestation_from_the_wrong_origin_is_refused(self, mock_db, redis_client):
        device = SoftAuthenticator(rp_id=RP_ID, origin=ORIGIN)
        with patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud:
            crud.get_multi = AsyncMock(return_value={"data": []})
            crud.create = AsyncMock()
            options = (await passkey_registration_options(_user(), mock_db)).options

            with pytest.raises(BadRequestException):
                await passkey_registration_verify(
                    PasskeyRegistrationVerifyRequest(
                        credential=device.register(options, origin="https://evil.example"), name="iPhone"
                    ),
                    _user(),
                    mock_db,
                )

            crud.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_registering_the_same_credential_twice_is_refused(self, mock_db, redis_client):
        """A browser honouring `excludeCredentials` never produces this; one that ignores
        it must not be able to duplicate a row either."""
        device = SoftAuthenticator(rp_id=RP_ID, origin=ORIGIN)
        with patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud:
            crud.get_multi = AsyncMock(return_value={"data": []})
            crud.create = AsyncMock()
            options = (await passkey_registration_options(_user(), mock_db)).options
            attestation = device.register(options)

            crud.get_multi = AsyncMock(return_value={"data": [_stored(credential_id=device.credential_id)]})

            with pytest.raises(DuplicateValueException):
                await passkey_registration_verify(
                    PasskeyRegistrationVerifyRequest(credential=attestation, name="iPhone"), _user(), mock_db
                )


@pytest.mark.usefixtures("no_rate_limits")
class TestSignInOptions:
    """`POST /auth/passkey/options` - the anonymous half of the ceremony."""

    @pytest.mark.asyncio
    async def test_it_asks_for_nobody_in_particular(self, redis_client):
        """`allowCredentials` empty is what keeps this from being an account oracle."""
        result = await passkey_sign_in_options(_request())

        assert result.options["rpId"] == RP_ID
        assert result.options.get("allowCredentials") in (None, [])
        assert result.options["userVerification"] == "preferred"
        assert list(redis_client.values) == [f"auth:passkey-challenge:flow:{result.flow_id}"]

    @pytest.mark.asyncio
    async def test_two_calls_get_independent_flows(self, redis_client):
        first = await passkey_sign_in_options(_request())
        second = await passkey_sign_in_options(_request())

        assert first.flow_id != second.flow_id
        assert first.options["challenge"] != second.options["challenge"]

    @pytest.mark.asyncio
    async def test_it_fails_closed_when_redis_is_unreachable(self):
        """Unlike rate limiting, which fails open. The challenge *is* the anti-replay
        guarantee, and email sign-in is unaffected by the same outage."""
        with patch("src.app.core.utils.cache.client", FakeRedis(fail_with=RedisConnectionError("no route"))):
            with pytest.raises(ChallengeStoreUnavailable) as exc_info:
                await passkey_sign_in_options(_request())

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_it_fails_closed_when_there_is_no_redis_client_at_all(self):
        with patch("src.app.core.utils.cache.client", None):
            with pytest.raises(ChallengeStoreUnavailable):
                await passkey_sign_in_options(_request())


@pytest.mark.usefixtures("no_rate_limits")
class TestSignInVerify:
    """`POST /auth/passkey/verify` - where an assertion becomes a session."""

    async def _assert_with(self, mock_db, redis_client, ceremony: _Ceremony, **kwargs: Any) -> dict[str, Any]:
        options_response = await passkey_sign_in_options(_request())
        assertion = ceremony.device.authenticate(options_response.options, **kwargs)
        return {"flow_id": options_response.flow_id, "credential": assertion}

    @pytest.mark.asyncio
    async def test_the_happy_path_signs_in_and_records_the_assertion(self, mock_db, redis_client):
        ceremony = await _register_credential(mock_db, redis_client)
        ceremony.device.sign_count = 4
        body = await self._assert_with(mock_db, redis_client, ceremony)
        stub_claim(mock_db, won=True)

        with (
            patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud,
            patch("src.app.services.passkey_service.crud_users") as users,
            patch("src.app.api.v1.auth.issue_tokens", new_callable=AsyncMock) as issue,
        ):
            crud.get = AsyncMock(return_value=ceremony.stored)
            users.get = AsyncMock(return_value={"id": 7, "uuid": USER_UUID})
            issue.return_value = {"access_token": "at", "token_type": "bearer"}

            outcome = await passkey_sign_in_verify(_request(), PasskeySignInVerifyRequest(**body), Mock(), mock_db)

        assert outcome.status == "authenticated"
        assert outcome.access_token == "at"
        # The funnel is what mints the session, and it subjects it to the user's uuid.
        assert issue.call_args.args[1] == USER_UUID
        recorded = str(mock_db.execute.call_args.args[0])
        assert "UPDATE webauthn_credential" in recorded
        assert "last_used_at" in recorded
        assert "sign_count" in recorded

    @pytest.mark.asyncio
    async def test_a_synced_passkey_reporting_zero_forever_still_signs_in(self, mock_db, redis_client):
        """iCloud/Google-synced passkeys never increment. `0 -> 0` is legal and must not
        be mistaken for a clone."""
        ceremony = await _register_credential(mock_db, redis_client)
        body = await self._assert_with(mock_db, redis_client, ceremony)
        stub_claim(mock_db, won=True)

        with (
            patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud,
            patch("src.app.services.passkey_service.crud_users") as users,
            patch("src.app.api.v1.auth.issue_tokens", new_callable=AsyncMock) as issue,
        ):
            crud.get = AsyncMock(return_value=ceremony.stored)
            users.get = AsyncMock(return_value={"id": 7, "uuid": USER_UUID})
            issue.return_value = {"access_token": "at", "token_type": "bearer"}

            outcome = await passkey_sign_in_verify(_request(), PasskeySignInVerifyRequest(**body), Mock(), mock_db)

        assert outcome.status == "authenticated"

    @pytest.mark.asyncio
    async def test_a_counter_regression_is_refused_and_logged(self, mock_db, redis_client, caplog):
        """The cloned-authenticator signal. The clone is blocked while the real device,
        whose counter is ahead, keeps working - so the log line is the whole value here.

        Asserted against the app's own stored-vs-presented comparison, never by parsing
        the library's exception message, which is a string any release can reword.
        """
        ceremony = await _register_credential(mock_db, redis_client)
        ceremony.device.sign_count = 3
        body = await self._assert_with(mock_db, redis_client, ceremony)

        with (
            patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud,
            patch("src.app.services.passkey_service.crud_users") as users,
            caplog.at_level(logging.WARNING),
        ):
            crud.get = AsyncMock(
                return_value=_stored(
                    credential_id=ceremony.stored.credential_id, public_key=ceremony.stored.public_key, sign_count=9
                )
            )
            users.get = AsyncMock()

            with pytest.raises(UnauthorizedException):
                await passkey_sign_in_verify(_request(), PasskeySignInVerifyRequest(**body), Mock(), mock_db)

            users.get.assert_not_called()

        assert "sign count 3" in caplog.text
        assert "stored count of 9" in caplog.text
        assert caplog.records[0].levelno == logging.WARNING

    @pytest.mark.asyncio
    async def test_an_ordinary_failure_does_not_cry_clone(self, mock_db, redis_client, caplog):
        """A wrong-origin assertion fails for a different reason entirely, and warning
        about a cloned authenticator there would be crying wolf."""
        ceremony = await _register_credential(mock_db, redis_client)
        ceremony.device.sign_count = 9
        body = await self._assert_with(mock_db, redis_client, ceremony, origin="https://evil.example")

        with (
            patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud,
            patch("src.app.services.passkey_service.crud_users") as users,
            caplog.at_level(logging.WARNING),
        ):
            crud.get = AsyncMock(
                return_value=_stored(
                    credential_id=ceremony.stored.credential_id, public_key=ceremony.stored.public_key, sign_count=4
                )
            )
            users.get = AsyncMock()

            with pytest.raises(UnauthorizedException):
                await passkey_sign_in_verify(_request(), PasskeySignInVerifyRequest(**body), Mock(), mock_db)

        assert "cloned" not in caplog.text

    @pytest.mark.asyncio
    async def test_the_challenge_is_single_use(self, mock_db, redis_client):
        """Even a *successful* first verify leaves nothing to replay against."""
        ceremony = await _register_credential(mock_db, redis_client)
        body = await self._assert_with(mock_db, redis_client, ceremony)
        stub_claim(mock_db, won=True)

        with (
            patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud,
            patch("src.app.services.passkey_service.crud_users") as users,
            patch("src.app.api.v1.auth.issue_tokens", new_callable=AsyncMock) as issue,
        ):
            crud.get = AsyncMock(return_value=ceremony.stored)
            users.get = AsyncMock(return_value={"id": 7, "uuid": USER_UUID})
            issue.return_value = {"access_token": "at", "token_type": "bearer"}

            await passkey_sign_in_verify(_request(), PasskeySignInVerifyRequest(**body), Mock(), mock_db)

            with pytest.raises(UnauthorizedException):
                await passkey_sign_in_verify(_request(), PasskeySignInVerifyRequest(**body), Mock(), mock_db)

    @pytest.mark.asyncio
    async def test_every_failure_answers_identically(self, mock_db, redis_client):
        """Unknown credential, tombstoned owner, unknown flow, wrong origin and wrong RP
        id must be one indistinguishable 401 - anything else answers "does this account
        exist" for whoever asks."""
        ceremony = await _register_credential(mock_db, redis_client)
        messages = set()

        async def _run(stored, user, *, flow_id=None, **assert_kwargs):
            body = await self._assert_with(mock_db, redis_client, ceremony, **assert_kwargs)
            if flow_id is not None:
                body["flow_id"] = flow_id
            with (
                patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud,
                patch("src.app.services.passkey_service.crud_users") as users,
            ):
                crud.get = AsyncMock(return_value=stored)
                users.get = AsyncMock(return_value=user)
                with pytest.raises(UnauthorizedException) as exc_info:
                    await passkey_sign_in_verify(_request(), PasskeySignInVerifyRequest(**body), Mock(), mock_db)
            messages.add(exc_info.value.detail)

        await _run(None, None)  # unknown credential
        await _run(ceremony.stored, None)  # credential fine, owner soft-deleted
        await _run(ceremony.stored, {"id": 7, "uuid": USER_UUID}, flow_id=str(uuid_pkg.uuid4()))  # unknown flow
        await _run(ceremony.stored, {"id": 7, "uuid": USER_UUID}, origin="https://evil.example")

        wrong_rp = SoftAuthenticator(rp_id="attacker.example", origin=ORIGIN)
        wrong_rp._private_key = ceremony.device._private_key
        wrong_rp.credential_id = ceremony.device.credential_id
        body = await self._assert_with(mock_db, redis_client, _Ceremony(wrong_rp, ceremony.stored))
        with (
            patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud,
            patch("src.app.services.passkey_service.crud_users") as users,
        ):
            crud.get = AsyncMock(return_value=ceremony.stored)
            users.get = AsyncMock(return_value={"id": 7, "uuid": USER_UUID})
            with pytest.raises(UnauthorizedException) as exc_info:
                await passkey_sign_in_verify(_request(), PasskeySignInVerifyRequest(**body), Mock(), mock_db)
        messages.add(exc_info.value.detail)

        assert len(messages) == 1

    @pytest.mark.asyncio
    async def test_a_soft_deleted_owner_never_reaches_the_funnel(self, mock_db, redis_client):
        """The `is_deleted=False` filter is what fails closed here until the deletion
        plan's `deletion_pending` outcome lands - the credential lookup is a resolve site
        that plan's edit list does not know about."""
        ceremony = await _register_credential(mock_db, redis_client)
        body = await self._assert_with(mock_db, redis_client, ceremony)

        with (
            patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud,
            patch("src.app.services.passkey_service.crud_users") as users,
            patch("src.app.api.v1.auth.issue_tokens", new_callable=AsyncMock) as issue,
        ):
            crud.get = AsyncMock(return_value=ceremony.stored)
            users.get = AsyncMock(return_value=None)

            with pytest.raises(UnauthorizedException):
                await passkey_sign_in_verify(_request(), PasskeySignInVerifyRequest(**body), Mock(), mock_db)

            assert users.get.call_args.kwargs["is_deleted"] is False
            issue.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_loser_of_a_concurrent_assertion_gets_no_session(self, mock_db, redis_client):
        """Both requests carry the same valid assertion; the conditional UPDATE is what
        decides which one of them mints a session."""
        ceremony = await _register_credential(mock_db, redis_client)
        body = await self._assert_with(mock_db, redis_client, ceremony)
        stub_claim(mock_db, won=False)

        with (
            patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud,
            patch("src.app.services.passkey_service.crud_users") as users,
            patch("src.app.api.v1.auth.issue_tokens", new_callable=AsyncMock) as issue,
        ):
            crud.get = AsyncMock(return_value=ceremony.stored)
            users.get = AsyncMock(return_value={"id": 7, "uuid": USER_UUID})

            with pytest.raises(UnauthorizedException):
                await passkey_sign_in_verify(_request(), PasskeySignInVerifyRequest(**body), Mock(), mock_db)

            issue.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_recording_update_is_conditional_on_the_counter(self, mock_db, redis_client):
        """An unconditional UPDATE would satisfy every other assertion here while letting
        two racing replays both advance the row."""
        ceremony = await _register_credential(mock_db, redis_client)
        body = await self._assert_with(mock_db, redis_client, ceremony)
        stub_claim(mock_db, won=True)

        with (
            patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud,
            patch("src.app.services.passkey_service.crud_users") as users,
            patch("src.app.api.v1.auth.issue_tokens", new_callable=AsyncMock) as issue,
        ):
            crud.get = AsyncMock(return_value=ceremony.stored)
            users.get = AsyncMock(return_value={"id": 7, "uuid": USER_UUID})
            issue.return_value = {"access_token": "at", "token_type": "bearer"}

            await passkey_sign_in_verify(_request(), PasskeySignInVerifyRequest(**body), Mock(), mock_db)

        assert "webauthn_credential.sign_count = " in str(mock_db.execute.call_args.args[0])


@pytest.mark.usefixtures("no_rate_limits")
class TestManagement:
    """`GET /user/passkeys`, `PATCH` and `DELETE /user/passkey/{uuid}`."""

    @pytest.mark.asyncio
    async def test_the_list_is_scoped_to_the_caller_and_capped(self, mock_db):
        with patch("src.app.api.v1.passkeys.crud_webauthn_credentials") as crud:
            crud.get_multi = AsyncMock(return_value={"data": [_stored()]})

            rows = await read_passkeys(_user(), mock_db)

        assert crud.get_multi.call_args.kwargs["user_id"] == 7
        assert crud.get_multi.call_args.kwargs["limit"] == settings.PASSKEY_MAX_CREDENTIALS_PER_USER
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_a_rename_goes_through_the_ownership_check(self, mock_db):
        with (
            patch("src.app.api.v1.passkeys.crud_webauthn_credentials") as crud,
            patch("src.app.api.v1.passkeys.fetch_owned_or_raise", new_callable=AsyncMock) as owned,
        ):
            owned.return_value = _stored()
            crud.update = AsyncMock()

            result = await patch_passkey(CREDENTIAL_UUID, WebauthnCredentialUpdate(name="Backup key"), _user(), mock_db)

        assert result == {"message": "Passkey updated"}
        assert owned.call_args.kwargs["uuid"] == CREDENTIAL_UUID
        assert crud.update.call_args.kwargs["object"].name == "Backup key"

    @pytest.mark.asyncio
    async def test_renaming_somebody_elses_passkey_is_a_404(self, mock_db):
        with (
            patch("src.app.api.v1.passkeys.crud_webauthn_credentials") as crud,
            patch(
                "src.app.api.v1.passkeys.fetch_owned_or_raise",
                new_callable=AsyncMock,
                side_effect=NotFoundException("Passkey not found"),
            ),
        ):
            crud.update = AsyncMock()

            with pytest.raises(NotFoundException):
                await patch_passkey(CREDENTIAL_UUID, WebauthnCredentialUpdate(name="Mine now"), _user(), mock_db)

            crud.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_empty_patch_writes_nothing(self, mock_db):
        with (
            patch("src.app.api.v1.passkeys.crud_webauthn_credentials") as crud,
            patch("src.app.api.v1.passkeys.fetch_owned_or_raise", new_callable=AsyncMock) as owned,
        ):
            owned.return_value = _stored()
            crud.update = AsyncMock()

            await patch_passkey(CREDENTIAL_UUID, WebauthnCredentialUpdate(), _user(), mock_db)

            crud.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_deleting_removes_the_row_and_notifies(self, mock_db):
        with (
            patch("src.app.api.v1.passkeys.crud_webauthn_credentials") as crud,
            patch("src.app.api.v1.passkeys.fetch_owned_or_raise", new_callable=AsyncMock) as owned,
            patch("src.app.api.v1.passkeys.send_passkey_removed_email", new_callable=AsyncMock) as send,
        ):
            owned.return_value = _stored(name="Old phone")
            crud.delete = AsyncMock()

            result = await erase_passkey(CREDENTIAL_UUID, _user(), mock_db)

        assert result == {"message": "Passkey removed"}
        crud.delete.assert_called_once()
        assert send.call_args.kwargs == {"email": "diver@example.com", "passkey_name": "Old phone"}

    @pytest.mark.asyncio
    async def test_a_failed_removal_email_is_logged_not_raised(self, mock_db, caplog):
        from src.app.services.email_service import EmailDeliveryError

        with (
            patch("src.app.api.v1.passkeys.crud_webauthn_credentials") as crud,
            patch("src.app.api.v1.passkeys.fetch_owned_or_raise", new_callable=AsyncMock) as owned,
            patch(
                "src.app.api.v1.passkeys.send_passkey_removed_email",
                new_callable=AsyncMock,
                side_effect=EmailDeliveryError("relay is down"),
            ),
            caplog.at_level(logging.WARNING),
        ):
            owned.return_value = _stored()
            crud.delete = AsyncMock()

            result = await erase_passkey(CREDENTIAL_UUID, _user(), mock_db)

        assert result == {"message": "Passkey removed"}
        assert "relay is down" in caplog.text

    @pytest.mark.asyncio
    async def test_deleting_somebody_elses_passkey_is_a_404(self, mock_db):
        with (
            patch("src.app.api.v1.passkeys.crud_webauthn_credentials") as crud,
            patch(
                "src.app.api.v1.passkeys.fetch_owned_or_raise",
                new_callable=AsyncMock,
                side_effect=NotFoundException("Passkey not found"),
            ),
        ):
            crud.delete = AsyncMock()

            with pytest.raises(NotFoundException):
                await erase_passkey(CREDENTIAL_UUID, _user(), mock_db)

            crud.delete.assert_not_called()


class TestRateLimits:
    """The limits themselves, which every other class here patches away."""

    @pytest.mark.asyncio
    async def test_sign_in_options_are_limited_per_ip(self, redis_client):
        with patch("src.app.api.v1.auth.enforce_rate_limit", new_callable=AsyncMock) as limit:
            await passkey_sign_in_options(_request("9.9.9.9"))

        assert limit.call_args.args[0] == "auth:passkey-options:ip:9.9.9.9"
        assert limit.call_args.args[1] == settings.PASSKEY_OPTIONS_RATE_LIMIT_PER_IP

    @pytest.mark.asyncio
    async def test_registration_is_limited_per_user(self, mock_db, redis_client):
        with (
            patch("src.app.api.v1.passkeys.enforce_rate_limit", new_callable=AsyncMock) as limit,
            patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud,
        ):
            crud.get_multi = AsyncMock(return_value={"data": []})
            await passkey_registration_options(_user(), mock_db)

        assert limit.call_args.args[0] == "passkey-register:user:7"
        assert limit.call_args.args[1] == settings.PASSKEY_REGISTER_RATE_LIMIT_PER_USER

    @pytest.mark.asyncio
    async def test_the_options_ceiling_matches_the_refresh_precedent(self):
        """A conditional-UI options call fires more often than `/auth/refresh` does, and
        an office NAT is one IP to both counters - so anything lower than that precedent
        would throttle a whole office off the login page."""
        assert settings.PASSKEY_OPTIONS_RATE_LIMIT_PER_IP == settings.AUTH_REFRESH_RATE_LIMIT_PER_IP


class TestChallengeStoreFailsClosed:
    """Redis is the one dependency this feature cannot degrade past. Rate limiting fails
    open because it is defense-in-depth; the challenge is the anti-replay guarantee, so
    every path that touches it answers 503 instead of proceeding without one.
    """

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("no_rate_limits")
    async def test_registration_options_fail_closed(self, mock_db):
        with (
            patch("src.app.core.utils.cache.client", FakeRedis(fail_with=RedisConnectionError("no route"))),
            patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud,
        ):
            crud.get_multi = AsyncMock(return_value={"data": []})

            with pytest.raises(ChallengeStoreUnavailable) as exc_info:
                await passkey_registration_options(_user(), mock_db)

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("no_rate_limits")
    async def test_a_verify_fails_closed_rather_than_skipping_the_challenge(self, mock_db, redis_client):
        """The dangerous direction: a read that could not be performed must never be
        treated as "no challenge stored, carry on"."""
        ceremony = await _register_credential(mock_db, redis_client)
        options_response = await passkey_sign_in_options(_request())
        assertion = ceremony.device.authenticate(options_response.options)

        with patch("src.app.core.utils.cache.client", FakeRedis(fail_with=RedisConnectionError("no route"))):
            with pytest.raises(ChallengeStoreUnavailable):
                await passkey_sign_in_verify(
                    _request(),
                    PasskeySignInVerifyRequest(flow_id=options_response.flow_id, credential=assertion),
                    Mock(),
                    mock_db,
                )

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("no_rate_limits")
    async def test_the_outage_is_logged_once_it_happens(self, mock_db, caplog):
        with (
            patch("src.app.core.utils.cache.client", FakeRedis(fail_with=RedisConnectionError("no route"))),
            caplog.at_level(logging.WARNING),
            pytest.raises(ChallengeStoreUnavailable),
        ):
            await passkey_sign_in_options(_request())

        assert "ConnectionError" in caplog.text


@pytest.mark.usefixtures("no_rate_limits")
class TestMalformedInput:
    """A `credential` body is passed to py_webauthn untouched, so the handful of things
    this module reads off it directly are the only places a malformed one can reach our
    own code first. None of them may be a 500.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "credential",
        [
            {},
            {"rawId": None},
            {"rawId": "!!!not base64url!!!"},
            {"rawId": 17},
        ],
        ids=["missing", "null", "unparseable", "wrong-type"],
    )
    async def test_an_unusable_raw_id_is_the_same_401(self, mock_db, redis_client, credential):
        options_response = await passkey_sign_in_options(_request())

        with patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud:
            crud.get = AsyncMock(return_value=None)

            with pytest.raises(UnauthorizedException) as exc_info:
                await passkey_sign_in_verify(
                    _request(),
                    PasskeySignInVerifyRequest(flow_id=options_response.flow_id, credential=credential),
                    Mock(),
                    mock_db,
                )

        assert exc_info.value.detail == "That passkey could not be used to sign in."

    @pytest.mark.asyncio
    async def test_a_missing_raw_id_is_not_even_looked_up(self, mock_db, redis_client):
        """The one case the decode itself catches. `base64url_to_bytes` is deliberately
        lenient - it pads and decodes almost anything - so every *other* malformed id
        becomes bytes that simply match no row, which is the same 401 by a longer route.
        """
        options_response = await passkey_sign_in_options(_request())

        with patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud:
            crud.get = AsyncMock(return_value=None)

            with pytest.raises(UnauthorizedException):
                await passkey_sign_in_verify(
                    _request(),
                    PasskeySignInVerifyRequest(flow_id=options_response.flow_id, credential={}),
                    Mock(),
                    mock_db,
                )

            crud.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unreadable_counter_does_not_break_the_failure_path(self, mock_db, redis_client, caplog):
        """`_presented_sign_count` runs on an already-failing assertion, so a response too
        malformed to parse must fall through quietly rather than raise over the 401."""
        options_response = await passkey_sign_in_options(_request())
        credential = {"rawId": "AAAA", "response": {"authenticatorData": "not-parseable"}}

        with (
            patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud,
            caplog.at_level(logging.WARNING),
        ):
            crud.get = AsyncMock(return_value=_stored(sign_count=5))

            with pytest.raises(UnauthorizedException):
                await passkey_sign_in_verify(
                    _request(),
                    PasskeySignInVerifyRequest(flow_id=options_response.flow_id, credential=credential),
                    Mock(),
                    mock_db,
                )

        assert "cloned" not in caplog.text

    @pytest.mark.asyncio
    async def test_transports_that_are_not_a_list_are_dropped(self, mock_db, redis_client):
        """Advisory metadata from the client. A browser sending something unexpected must
        not fail a registration over a field nothing security-bearing reads.
        """
        device = SoftAuthenticator(rp_id=RP_ID, origin=ORIGIN)
        with patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud:
            crud.get_multi = AsyncMock(return_value={"data": []})
            crud.create = AsyncMock(return_value=_stored())
            options = (await passkey_registration_options(_user(), mock_db)).options
            attestation = device.register(options)
            attestation["response"]["transports"] = "internal"

            with patch("src.app.api.v1.passkeys.send_passkey_added_email", new_callable=AsyncMock):
                await passkey_registration_verify(
                    PasskeyRegistrationVerifyRequest(credential=attestation, name="iPhone"), _user(), mock_db
                )

        assert crud.create.call_args.kwargs["object"].transports is None

    @pytest.mark.asyncio
    async def test_a_credential_with_no_transports_is_still_excludable(self, mock_db, redis_client):
        """A security key may report none at all - the descriptor is still valid without
        them, and dropping the credential from `excludeCredentials` would let it be
        registered twice."""
        with patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud:
            crud.get_multi = AsyncMock(return_value={"data": [_stored(transports=None)]})

            result = await passkey_registration_options(_user(), mock_db)

        assert len(result.options["excludeCredentials"]) == 1
        assert "transports" not in result.options["excludeCredentials"][0]

    def test_an_unparseable_aaguid_is_dropped_rather_than_fatal(self):
        """py_webauthn hands this over already formatted, so this is pure defense - but
        nothing reads the column in v1, and losing an icon hint must never cost someone a
        registration they completed a biometric prompt for."""
        from src.app.services.passkey_service import _aaguid_from

        assert _aaguid_from("00000000-0000-0000-0000-000000000000") == uuid_pkg.UUID(int=0)
        assert _aaguid_from("not-a-uuid") is None
        assert _aaguid_from(None) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_an_all_zero_aaguid_is_still_stored_as_one(self, mock_db, redis_client):
        """Self-attested credentials report all zeros, which is a legal uuid and the
        overwhelmingly common case - nothing reads the column in v1 either way.
        """
        device = SoftAuthenticator(rp_id=RP_ID, origin=ORIGIN)
        with patch("src.app.services.passkey_service.crud_webauthn_credentials") as crud:
            crud.get_multi = AsyncMock(return_value={"data": []})
            crud.create = AsyncMock(return_value=_stored())
            options = (await passkey_registration_options(_user(), mock_db)).options

            with patch("src.app.api.v1.passkeys.send_passkey_added_email", new_callable=AsyncMock):
                await passkey_registration_verify(
                    PasskeyRegistrationVerifyRequest(credential=device.register(options), name="iPhone"),
                    _user(),
                    mock_db,
                )

        assert crud.create.call_args.kwargs["object"].aaguid == uuid_pkg.UUID(int=0)


class TestDerivedRelyingParty:
    """The RP id and origin are computed from `FRONTEND_URL`, and there is exactly one way
    for that to go wrong quietly."""

    def test_a_trailing_slash_does_not_break_the_origin(self):
        """The most ordinary way to write a URL variable. Browsers put a bare
        `scheme://host[:port]` in `clientDataJSON.origin`, so an origin taken raw from the
        setting would 401 every ceremony while magic links kept working."""
        with patch.object(settings, "FRONTEND_URL", "https://dive.example.com/"):
            assert settings.passkey_origin == "https://dive.example.com"
            assert settings.passkey_rp_id == "dive.example.com"

    def test_the_port_stays_in_the_origin_but_not_in_the_rp_id(self):
        """A dev instance is `http://localhost:3000`, whose RP id is bare `localhost` -
        browsers treat it as a secure context, so the feature works locally over HTTP."""
        with patch.object(settings, "FRONTEND_URL", "http://localhost:3000"):
            assert settings.passkey_origin == "http://localhost:3000"
            assert settings.passkey_rp_id == "localhost"
