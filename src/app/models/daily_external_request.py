from datetime import date

from sqlalchemy import BigInteger, Date, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class DailyExternalRequest(Base):
    """What this instance asked of one other service on one UTC day, by kind and by how it
    ended: how many, and for the ones it sent, how long they took altogether and at the longest.

    `provider`, `kind` and `outcome` are the slugs of `core.utils.external_requests`, a closed
    vocabulary with no DB `CHECK`: a slug a later build stops declaring keeps its history here.
    A row is only written with a count, so none holds zero; an event that sent nothing holds
    `0` in both durations.

    No `uuid` and no `id`, as `daily_total`: the primary key is the identity the move's upsert
    conflicts on, and the absence keeps it out of the hard-delete registry.
    """

    __tablename__ = "daily_external_request"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    outcome: Mapped[str] = mapped_column(String(16), primary_key=True)
    count: Mapped[int] = mapped_column(Integer)
    # Whole milliseconds. A day's total can pass what an `Integer` holds; one request's cannot.
    duration_ms_total: Mapped[int] = mapped_column(BigInteger)
    duration_ms_max: Mapped[int] = mapped_column(Integer)
