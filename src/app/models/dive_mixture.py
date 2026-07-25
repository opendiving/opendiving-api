from sqlalchemy import Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class DiveMixture(Base):
    __tablename__ = "dive_mixture"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, unique=True, primary_key=True, init=False)
    dive_id: Mapped[int] = mapped_column(ForeignKey("dive.id", ondelete="CASCADE"), index=True)

    volume: Mapped[float] = mapped_column(Float)
    start_pressure: Mapped[float | None] = mapped_column(Float, default=None)
    end_pressure: Mapped[float | None] = mapped_column(Float, default=None)
    po2: Mapped[float] = mapped_column(Float, default=1.4)
    oxygen: Mapped[float] = mapped_column(Float, default=21.0)
    name: Mapped[str | None] = mapped_column(String(50), default=None)
