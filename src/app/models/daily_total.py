from datetime import date

from sqlalchemy import Date, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class DailyTotal(Base):
    """One anonymous count: how many of something happened on one UTC day.

    The whole of what the operator's stats read, and deliberately nothing that names an
    account. `metric` is one of `schemas.daily_total.DailyMetric`; `key` splits it where it
    splits - the door an account came in by, for `accounts_created` - and is the empty string
    for the metrics that do not. Never `NULL`: the row's identity is `(day, metric, key)`,
    every write is an upsert on it, and a `NULL` conflicts with nothing, so each hourly run
    would insert a fresh row beside the last.

    The key space is a closed vocabulary with no DB `CHECK`, the `GearItem.type` shape: the
    fixed words are `core.config.AccountSource` and every other key is a join-channel slug,
    which the setting's validator keeps apart from them. A slug removed from `JOIN_CHANNELS`
    keeps its history here.

    No `uuid` and no `id`: nothing outside addresses a row, the primary key is the identity
    the upserts conflict on, and the absence keeps it out of the hard-delete registry.
    """

    __tablename__ = "daily_total"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    metric: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
