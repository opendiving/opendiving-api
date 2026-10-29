"""A token verifies under the one string this server minted, and under no other spelling.

Revocation is keyed on that string, and the JWT library decodes base64url leniently - so
any other spelling it accepts is a spent refresh, onboarding or restore token that the
blacklist has never seen.
"""

import base64
import uuid as uuid_pkg
from collections.abc import Awaitable, Callable
from unittest.mock import Mock, patch

import pytest
from fastapi import Response
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.v1.auth import refresh_access_token
from src.app.core.exceptions.http_exceptions import UnauthorizedException
from src.app.core.schemas import OnboardingTokenData
from src.app.core.security import (
    TokenType,
    _decode,
    blacklist_token,
    create_onboarding_token,
    create_refresh_token,
    create_restore_token,
    verify_onboarding_token,
    verify_restore_token,
    verify_token,
)
from src.app.core.utils import cache
from src.app.services.auth_service import issue_tokens
from tests.helpers.fake_redis import FakeRedis
from tests.helpers.mocks import FakeTokenBlacklist
from tests.test_auth_refresh import CONTEXT, USER_ID, USER_UUID, SignedInAccount, _refresh_cookie, _request

_BASE64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

# How many low bits of a segment's last character encode no byte, by the segment's length mod 4.
_SPARE_BITS = {0: 0, 2: 4, 3: 2}


def _segment_bytes(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _same_bytes_last_character(token: str) -> list[str]:
    """The token with its signature's last character swapped for each other character that
    differs from it only in the bits the decoder discards: the same signature, spelled
    differently."""
    head, signature = token.rsplit(".", 1)
    spellings = 2 ** _SPARE_BITS[len(signature) % 4]
    index = _BASE64URL.index(signature[-1])
    first = index - index % spellings
    return [
        f"{head}.{signature[:-1]}{_BASE64URL[other]}" for other in range(first, first + spellings) if other != index
    ]


_RESPELLINGS: dict[str, Callable[[str], list[str]]] = {
    "same-bytes last character": _same_bytes_last_character,
    "trailing padding": lambda token: [token + "="],
    "character outside the alphabet": lambda token: [f"{token[:-5]}~{token[-5:]}"],
    "trailing newline": lambda token: [token + "\n"],
}


def _all_respellings(token: str) -> list[str]:
    return [variant for respell in _RESPELLINGS.values() for variant in respell(token)]


class TestOneSpellingPerToken:
    @pytest.mark.asyncio
    async def test_the_minted_spelling_decodes(self) -> None:
        subject = str(uuid_pkg.uuid4())
        token = await create_refresh_token({"sub": subject})

        assert _decode(token)["sub"] == subject

    @pytest.mark.asyncio
    async def test_the_last_character_respellings_carry_the_same_signature(self) -> None:
        """What makes the first respelling a respelling rather than a forgery: the bytes the
        signature check compares are unchanged. An HS256 signature is 43 characters, so
        its last one has two spare bits and three other spellings."""
        token = await create_refresh_token({"sub": str(uuid_pkg.uuid4())})
        signature = token.rsplit(".", 1)[1]

        variants = _same_bytes_last_character(token)

        assert len(variants) == 3
        for variant in variants:
            assert variant != token
            assert _segment_bytes(variant.rsplit(".", 1)[1]) == _segment_bytes(signature)

    @pytest.mark.parametrize("respell", _RESPELLINGS.values(), ids=list(_RESPELLINGS))
    @pytest.mark.asyncio
    async def test_a_respelling_is_refused(self, respell: Callable[[str], list[str]]) -> None:
        token = await create_refresh_token({"sub": str(uuid_pkg.uuid4())})

        for variant in respell(token):
            with pytest.raises(JWTError):
                _decode(variant)


async def _mint_refresh() -> str:
    return await create_refresh_token({"sub": str(uuid_pkg.uuid4())})


async def _mint_onboarding() -> str:
    return await create_onboarding_token(OnboardingTokenData(email="new@example.com", provider="email"))


async def _mint_restore() -> str:
    return await create_restore_token(uuid_pkg.uuid4())


async def _verify_refresh(token: str, db: AsyncSession) -> object:
    return await verify_token(token, TokenType.REFRESH, db)


_SPENDABLE: dict[str, tuple[Callable[[], Awaitable[str]], Callable[[str, AsyncSession], Awaitable[object]]]] = {
    "refresh": (_mint_refresh, _verify_refresh),
    "onboarding": (_mint_onboarding, verify_onboarding_token),
    "restore": (_mint_restore, verify_restore_token),
}


class TestASpentTokenStaysSpentUnderAnySpelling:
    @pytest.mark.parametrize(("mint", "verify"), _SPENDABLE.values(), ids=list(_SPENDABLE))
    @pytest.mark.asyncio
    async def test_no_respelling_of_a_blacklisted_token_verifies(
        self,
        mock_db: Mock,
        mint: Callable[[], Awaitable[str]],
        verify: Callable[[str, AsyncSession], Awaitable[object]],
    ) -> None:
        token = await mint()
        blacklist = FakeTokenBlacklist()

        with patch("src.app.core.security.crud_token_blacklist", blacklist):
            assert await verify(token, mock_db) is not None
            assert await blacklist_token(token, mock_db) is True

            for variant in _all_respellings(token):
                assert await verify(variant, mock_db) is None


class TestARespelledRefreshTokenMintsNothing(SignedInAccount):
    """The rotation the blacklist exists for, driven over the real helpers: sign in, rotate
    once, then present the rotated-out cookie under every other spelling."""

    @pytest.fixture(autouse=True)
    def redis(self, monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
        """The rotation's reuse-grace hold is really written, so a refusal below is not down
        to there being no Redis to hold the pair in."""
        fake = FakeRedis()
        monkeypatch.setattr(cache, "client", fake)
        return fake

    @pytest.mark.asyncio
    async def test_the_rotated_out_token_respelled_is_refused(self, mock_db: Mock) -> None:
        blacklist = FakeTokenBlacklist()

        with patch("src.app.core.security.crud_token_blacklist", blacklist):
            sign_in = Response()
            await issue_tokens(sign_in, USER_UUID, db=mock_db, context=CONTEXT, user_id=USER_ID)
            spent = _refresh_cookie(sign_in)
            await refresh_access_token(_request({"refresh_token": spent}), Response(), mock_db)
            assert spent in blacklist.tokens

            for variant in _all_respellings(spent):
                replay = Response()
                with pytest.raises(UnauthorizedException, match="Invalid refresh token."):
                    await refresh_access_token(_request({"refresh_token": variant}), replay, mock_db)

                assert "set-cookie" not in replay.headers
                assert variant not in blacklist.tokens
