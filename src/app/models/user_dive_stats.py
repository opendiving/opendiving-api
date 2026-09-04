from sqlalchemy import BigInteger, Float, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base
from ..core.db.models import TimestampMixin


class UserDiveStats(Base, TimestampMixin):
    """Aggregate dive statistics for a user.

    One row per user, kept in sync by `services.dive_stats.recalculate_dive_stats`
    whenever a dive is created, updated, or deleted (soft or hard) for that user.
    """

    __tablename__ = "user_dive_stats"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), unique=True, index=True)

    total_dives: Mapped[int] = mapped_column(Integer, default=0)
    max_depth: Mapped[float] = mapped_column(Float, default=0)
    # `BigInteger`, unlike every other counter here, because it is the only one that is a
    # **sum of a column the caller supplies** rather than a count of rows. `dive.duration`
    # is a 32-bit `Integer`, so a few dives near its ceiling sum past it - and the write
    # that fails is this one, in the middle of whatever transaction recomputed the stats.
    # For a logbook import that means an entire restore refused over an arithmetic overflow
    # in a derived tile. The importer bounds a single dive's duration at a year for its own
    # reasons, which makes this unreachable in practice; the width is what makes it
    # unreachable in principle, and it costs four bytes a diver.
    total_time: Mapped[int] = mapped_column(BigInteger, default=0, doc="Total dive time in seconds")
    species_seen: Mapped[int] = mapped_column(Integer, default=0)
