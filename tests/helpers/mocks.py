from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from unittest.mock import AsyncMock, Mock

from fastapi.encoders import jsonable_encoder

from src.app import models
from src.app.core.config import settings
from src.app.core.schemas import TokenBlacklistCreate, TokenBlacklistRead
from src.app.schemas.auth import GoogleAuthRequest
from tests.conftest import fake

# The shortest PKCE verifier RFC 7636 §4.1 allows, which is also what 32 random bytes
# base64url-encode to and therefore what the web app actually sends.
GOOGLE_CODE_VERIFIER = "a" * 43


def awaited_kwargs(recorder: Any, index: int = -1) -> dict[str, Any]:
    """The keyword arguments of one awaited call, narrowed for mypy and for the reader.

    `AsyncMock.await_args` is typed `_Call | None`, so reading `.kwargs` off it does not
    type-check - and asserting it is not `None` first is worth doing anyway: a call that
    never happened then fails as "the mock was never awaited" rather than as an
    `AttributeError` on `None`, which names neither the mock nor the expectation.
    """
    calls = recorder.await_args_list
    assert calls, "the mock was never awaited"
    return dict(calls[index].kwargs)


def fake_request(ip: str = "1.2.3.4", user_agent: str = "Mozilla/5.0 (X11; Linux x86_64) TestAgent/1.0") -> Mock:
    """A stand-in for `Request` carrying the two things every auth route now reads off one.

    `test_auth.py` and `test_account_restore.py` each had their own three-line copy of this
    when `client_ip` was the only reader; `RequestContext` added a second attribute and made
    the third copy the moment to share it.

    **`headers` has to be a real mapping**, which is the part a bare `Mock()` gets wrong in
    a way that is hard to read: `.get("user-agent", "")` on a mock answers with another
    mock, `RequestContext` slices that to its length bound, and the `TypeError` surfaces
    from inside whatever route is under test rather than pointing at the double. `client` is
    a mock because `client_ip` only reaches `.host` on it.
    """
    request = Mock()
    request.client = Mock(host=ip)
    request.headers = {"user-agent": user_agent}
    return request


def google_auth_body(**overrides: Any) -> GoogleAuthRequest:
    """A `POST /auth/google` body whose `redirect_uri` this instance will accept.

    That URI is read off the setting rather than written out, because it is derived from
    `FRONTEND_URL` and no test here should depend on what the `src/.env` a given run picked
    up happens to say. `tests.test_auth.TestGoogleRedirectUriIsChecked` pins the derivation
    itself against known values instead.
    """
    body: dict[str, Any] = {
        "code": "an-authorization-code",
        "code_verifier": GOOGLE_CODE_VERIFIER,
        "redirect_uri": settings.google_redirect_uri,
    }
    return GoogleAuthRequest(**{**body, **overrides})


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

    Whole rows rather than a bare set of strings, because `core.security.revocation_time`
    reads `revoked_at` back off the row: a fake that only remembered *whether* a token was
    revoked could not exercise the reuse warning at all.
    """

    def __init__(self) -> None:
        self.entries: dict[str, TokenBlacklistRead] = {}

    @property
    def tokens(self) -> set[str]:
        """The token strings on file, for the tests that only ask "is this revoked?"."""
        return set(self.entries)

    async def exists(self, db: Any, token: str) -> bool:
        return token in self.entries

    async def create(self, db: Any, object: TokenBlacklistCreate) -> None:
        self.entries[object.token] = TokenBlacklistRead(id=len(self.entries) + 1, **object.model_dump())

    async def get(self, db: Any, token: str, **kwargs: Any) -> TokenBlacklistRead | None:
        """`**kwargs` swallows FastCRUD's `schema_to_select`/`return_as_model`, which the
        caller passes and which this fake has no use for - it only ever holds one shape.
        """
        return self.entries.get(token)

    def backdate_revocation(self, token: str, *, by: timedelta) -> None:
        """Move one row's `revoked_at` back, so that a presentation of that token now reads
        as having arrived `by` later than it really did.

        How long after a revocation the token comes back is the only thing separating the
        two-tab rotation race from a stolen cookie, and everything `api.v1.auth` does past
        that line hangs off the gap. The far side of it is seconds away in wall-clock time,
        so a test that reached it by waiting would pay those seconds to exercise a clock
        nobody is testing. `FrozenSecurityClock` cannot serve here either: it freezes the
        mint and the revocation together, which fixes the gap at whatever the suite's
        import time happens to make it rather than at a value the test chose.
        """
        entry = self.entries[token]
        entry.revoked_at = entry.revoked_at - by


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

    Searched out of `call_args_list` by table rather than read off `call_args`, which is
    only ever the *last* statement. The sign-in paths now execute more than one: minting a
    session runs a cap eviction against `user_session` after the claim, so the last
    statement is no longer the one being asserted on - and reading it would have made this
    helper quietly assert something else.
    """
    statements = [str(call.args[0]) for call in mock_db.execute.call_args_list if call.args]
    claims = [statement for statement in statements if "UPDATE authentication_request" in statement]
    assert claims, f"no authentication_request UPDATE was executed; saw: {statements}"
    return claims[-1]
