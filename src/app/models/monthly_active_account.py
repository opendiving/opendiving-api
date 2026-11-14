from datetime import date

from sqlalchemy import Date, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class MonthlyActiveAccount(Base):
    """One account with a session used in the current UTC calendar month.

    The one per-person usage record the stats keep, and only for as long as a distinct
    count needs it: the hourly session sweep adds the month's accounts, and the first sweep
    of the next month folds them into one anonymous `daily_total` row and deletes them
    (`crud_daily_totals.record_monthly_active_accounts`). No `uuid` and no `id`, for
    `DailyTotal`'s reason.
    """

    __tablename__ = "monthly_active_account"

    # The month's first day.
    month: Mapped[date] = mapped_column(Date, primary_key=True)
    # `CASCADE` because a deleted account leaves no trace of having been active; the
    # month's stored count is never lowered, so it keeps that account counted.
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), primary_key=True, index=True)
