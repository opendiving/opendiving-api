from sqlalchemy import CheckConstraint, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class DiveMixture(Base):
    __tablename__ = "dive_mixture"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, unique=True, primary_key=True, init=False)
    dive_id: Mapped[int] = mapped_column(ForeignKey("dive.id", ondelete="CASCADE"), index=True)

    volume: Mapped[float] = mapped_column(Float)
    start_pressure: Mapped[float | None] = mapped_column(Float, default=None)
    end_pressure: Mapped[float | None] = mapped_column(Float, default=None)
    oxygen: Mapped[float] = mapped_column(Float, default=21.0)
    helium: Mapped[float] = mapped_column(Float, default=0.0)
    name: Mapped[str | None] = mapped_column(String(50), default=None)

    __table_args__ = (
        # Mirrors the frontend's Zod validation (`lib/validations/dive.ts`) at the DB
        # layer, so direct API calls or bugs can't insert nonsensical gas mixtures.
        CheckConstraint("volume > 0", name="ck_dive_mixture_volume_positive"),
        CheckConstraint("oxygen >= 0 AND oxygen <= 100", name="ck_dive_mixture_oxygen_range"),
        CheckConstraint("helium >= 0 AND helium <= 100", name="ck_dive_mixture_helium_range"),
        CheckConstraint("oxygen + helium <= 100", name="ck_dive_mixture_oxygen_helium_sum"),
    )
