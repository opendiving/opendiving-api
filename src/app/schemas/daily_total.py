"""The daily totals' vocabulary, and what `GET /admin/stats` answers with."""

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .join_channel import JoinChannelRead


class DailyMetric(StrEnum):
    """What a `daily_total` row counts. The values are wire vocabulary: the stats route
    returns them as field names, and the web's chart is written against them."""

    # Accounts created that day, keyed by the door: a join-channel slug or one of
    # `core.config.AccountSource`. Bumped inside `POST /auth/complete`'s transaction.
    ACCOUNTS_CREATED = "accounts_created"
    # Distinct accounts with a successful sign-in that day, recomputed hourly from the
    # audit trail for as long as it still covers the day.
    SIGN_INS = "sign_ins"
    # Distinct accounts with a session used that day, snapshotted by the hourly session
    # sweep before it deletes anything.
    ACTIVE_ACCOUNTS = "active_accounts"


# The key a metric that does not split is stored under. The empty string rather than `NULL`
# because the row's identity is `(day, metric, key)` and a `NULL` conflicts with nothing.
NO_KEY = ""


class DailyStatsDay(BaseModel):
    day: date
    # Source to count, only for the sources that created an account that day.
    accounts_created: dict[str, int]
    sign_ins: int
    active_accounts: int


class DailyStatsTotals(BaseModel):
    # Every `user` row, an account inside its deletion grace period included.
    accounts: int
    # Distinct accounts holding a session that is neither revoked nor past `expires_at`.
    active_now: int


class DailyStatsRead(BaseModel):
    """`GET /admin/stats`: every day of the range, zero-filled, and the figures beside it.

    `channels` is the configured list, for the legend. A key under `accounts_created` may
    name a slug no longer in it, and is shown by the slug.
    """

    model_config = ConfigDict(validate_by_name=True, serialize_by_alias=True)

    # `from` is a Python keyword, hence the alias; the wire name is `from`.
    from_: date = Field(alias="from")
    to: date
    channels: list[JoinChannelRead]
    days: list[DailyStatsDay]
    totals: DailyStatsTotals
