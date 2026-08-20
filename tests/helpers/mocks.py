from datetime import UTC, datetime, tzinfo
from typing import Any
from unittest.mock import AsyncMock, Mock

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


def stub_claim(mock_db: Any, *, won: bool = True) -> Any:
    """Give a mocked `AsyncSession` an `execute` whose result carries a `rowcount`.

    `claim_authentication_request` (`crud/crud_authentication_requests.py`) spends a
    magic-link token with a Core `UPDATE ... WHERE used_at IS NULL` and reads `rowcount`
    to learn whether it won the race - the one thing a `Mock(spec=AsyncSession)` won't
    produce on its own. Without this the attribute is a bare child mock, which compares
    unequal to `0` and so reads as a win *by accident*; `won=False` is the only way to
    reach the race-lost branches at all.
    """
    mock_db.execute = AsyncMock(return_value=Mock(rowcount=1 if won else 0))
    return mock_db


def claimed_used_at_sql(mock_db: Any) -> str:
    """The claim statement a `stub_claim`ed session was handed, as SQL.

    Enough to assert the `used_at IS NULL` predicate is actually in the `WHERE` clause,
    which is the entire point of the statement - an unconditional `UPDATE` would satisfy
    every other assertion in these tests.
    """
    return str(mock_db.execute.call_args.args[0])
