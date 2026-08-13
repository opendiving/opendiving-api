from sqlalchemy import CheckConstraint, Float, ForeignKey, Integer, String
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

    # The ppO2 this gas was planned to (bar) - a dive computer's own exposure limit for
    # the cylinder, which is what its MOD is actually derived from. Not a revival of the
    # `po2` column removed back when mixtures tracked a set-point instead of `helium`
    # (see DECISIONS.md): that one *was* the gas description, this one sits alongside
    # `oxygen`/`helium` and only says how conservatively the diver planned to breathe it.
    # A Suunto records 1.4 for the back gas and 1.6 for the deco bottle on the same dive.
    po2_limit: Mapped[float | None] = mapped_column(Float, default=None)
    # How the source export identifies this cylinder, and the join key to the profile's
    # per-cylinder pressure channels (`dive_profile.data.pressure[].gas_number`).
    #
    # **A label, never an index**, and the corpus is emphatic about it: a Suunto Ocean
    # numbers from 0, while the parsers that synthesize a position because their format
    # carries no number at all (XML, FIT) count from 1. Nullable, because a hand-entered
    # cylinder has no position in any file.
    gas_number: Mapped[int | None] = mapped_column(Integer, default=None)
    # What the cylinder was carried for - see `GasRole` (`schemas/dive_mixture.py`) for
    # the vocabulary and for why this is not the gas *name* synthesis that was rejected.
    role: Mapped[str | None] = mapped_column(String(20), default=None)

    __table_args__ = (
        # Mirrors the frontend's Zod validation (`lib/validations/dive.ts`) at the DB
        # layer, so direct API calls or bugs can't insert nonsensical gas mixtures.
        CheckConstraint("volume > 0", name="ck_dive_mixture_volume_positive"),
        CheckConstraint("oxygen >= 0 AND oxygen <= 100", name="ck_dive_mixture_oxygen_range"),
        CheckConstraint("helium >= 0 AND helium <= 100", name="ck_dive_mixture_helium_range"),
        CheckConstraint("oxygen + helium <= 100", name="ck_dive_mixture_oxygen_helium_sum"),
        CheckConstraint(
            "start_pressure IS NULL OR end_pressure IS NULL OR end_pressure <= start_pressure",
            name="ck_dive_mixture_pressure_order",
        ),
        # Range rather than "positive": 0.4 bar is roughly the hypoxic floor a diluent
        # sits at and 2.0 the highest ppO2 any real table contemplates, so a value
        # outside this is a unit error (a Suunto JSON export writes 140000 Pa for 1.4
        # bar) rather than an aggressive gas plan.
        CheckConstraint(
            "po2_limit IS NULL OR (po2_limit >= 0.4 AND po2_limit <= 2.0)", name="ck_dive_mixture_po2_limit_range"
        ),
        # `>= 0`, not `>= 1`. This started life as a 1-based check and the backfill
        # rejected the real corpus on its first run: a Suunto Ocean numbers its cylinders
        # from **0**, and the already-stored profiles label their pressure channels `0`
        # to match. A 1-based floor would have forced the mixture to carry a number its
        # own curve doesn't have, breaking the join it exists for. See DECISIONS.md.
        CheckConstraint("gas_number IS NULL OR gas_number >= 0", name="ck_dive_mixture_gas_number_non_negative"),
    )
