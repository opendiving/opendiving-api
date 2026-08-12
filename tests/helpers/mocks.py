from datetime import UTC, datetime, tzinfo
from typing import Any

from fastapi.encoders import jsonable_encoder

from src.app import models
from src.app.core.schemas import TokenBlacklistCreate
from tests.conftest import fake


class FrozenSecurityClock:
    """Stand-in for the `datetime` class in `app.core.security`, whose `now()` never
    advances - so every token minted while it is patched in lands on the same one-second
    `exp`. That is the condition JWT collisions need (see `core.security._new_jti`), and
    it can't be produced reliably by minting two tokens quickly and hoping.

    A plain class supplying the two attributes that module actually reaches for, rather
    than a `datetime` subclass: overriding a classmethod on `datetime` fights typeshed's
    `Self` return type for no benefit here.

    Pinned to import time rather than a literal date so tokens minted under it are
    neither already expired nor implausibly far ahead, whenever the suite happens to run.
    """

    _NOW = datetime.now(UTC).replace(tzinfo=None)

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        return cls._NOW.replace(tzinfo=tz)

    @staticmethod
    def fromtimestamp(timestamp: float, tz: tzinfo | None = None) -> datetime:
        return datetime.fromtimestamp(timestamp, tz)


class FakeTokenBlacklist:
    """In-memory stand-in for `crud_token_blacklist`, keyed on the token string exactly
    as the real table is (`TokenBlacklist.token` is unique).

    Enough to exercise revocation end to end - mint, spend, replay - without a database,
    which is what it takes to catch a token colliding with one already blacklisted.
    """

    def __init__(self) -> None:
        self.tokens: set[str] = set()

    async def exists(self, db: Any, token: str) -> bool:
        return token in self.tokens

    async def create(self, db: Any, object: TokenBlacklistCreate) -> None:
        self.tokens.add(object.token)


def get_current_user(user: models.User) -> dict[str, Any]:
    # `jsonable_encoder` is typed as returning `Any`; a model always encodes to a dict,
    # and the callers here index into it as one.
    encoded: dict[str, Any] = jsonable_encoder(user)
    return encoded


def oauth2_scheme() -> str:
    token = fake.sha256()
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return token  # type: ignore
