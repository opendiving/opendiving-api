from sqlalchemy import CheckConstraint, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class DiveMixture(Base):
    __tablename__ = "dive_mixture"

    id: Mapped[int] = mapped_column(autoincrement=True, nullable=False, primary_key=True, init=False)
    dive_id: Mapped[int] = mapped_column(ForeignKey("dive.id", ondelete="CASCADE"), index=True)

    # All three nullable, and NULL is "the source never recorded this" rather than a
    # missing value - the same third state `dive.utc_offset_minutes` carries. A cylinder
    # whose file gave a gas and no vessel is a real record: UDDF's `<tankvolume>` is
    # optional and its exporters routinely omit it, and a mix a source spelled as a gas
    # link alone has no size to store. Filling one in would put a number a diver plans gas
    # off into a column nothing recorded it in, so the column takes the absence instead.
    volume: Mapped[float | None] = mapped_column(Float, default=None)
    start_pressure: Mapped[float | None] = mapped_column(Float, default=None)
    end_pressure: Mapped[float | None] = mapped_column(Float, default=None)
    oxygen: Mapped[float | None] = mapped_column(Float, default=None)
    helium: Mapped[float | None] = mapped_column(Float, default=None)

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
    # *How* the cylinder was breathed, which is the orthogonal fact to `role` and the one
    # gas consumption needs - see `TankUsage` (`schemas/dive_mixture.py`). `parallel` is a
    # sidemount pair or independent doubles, breathed alternately at the same depth, whose
    # litres are additive; `staged` is a bottle breathed at its own depth, which is not.
    # Null means "not recorded", which is every row until a diver says otherwise: no file
    # format this app parses carries the distinction.
    usage: Mapped[str | None] = mapped_column(String(20), default=None)

    __table_args__ = (
        # A DB-level backstop for the same bounds the API schemas and the web form apply,
        # so direct API calls or bugs can't insert nonsensical gas mixtures.
        #
        # The four constraints over `volume`, `oxygen` and `helium` spell their null case
        # out, like every other nullable column here, and that is a **restatement rather
        # than a change**: SQL `CHECK` passes on UNKNOWN, so `volume > 0` already admitted
        # a NULL and `oxygen + helium <= 100` already admitted a row with one operand
        # missing. Left implicit they would be the only four on this table that go quiet
        # on a null without saying so, which is the reading the next person has to redo.
        CheckConstraint("volume IS NULL OR volume > 0", name="ck_dive_mixture_volume_positive"),
        CheckConstraint("oxygen IS NULL OR (oxygen >= 0 AND oxygen <= 100)", name="ck_dive_mixture_oxygen_range"),
        CheckConstraint("helium IS NULL OR (helium >= 0 AND helium <= 100)", name="ck_dive_mixture_helium_range"),
        CheckConstraint(
            "oxygen IS NULL OR helium IS NULL OR oxygen + helium <= 100", name="ck_dive_mixture_oxygen_helium_sum"
        ),
        CheckConstraint(
            "start_pressure IS NULL OR end_pressure IS NULL OR end_pressure <= start_pressure",
            name="ck_dive_mixture_pressure_order",
        ),
        # The asymmetry is physical, and is the whole shape of these two: **you cannot
        # start a dive on an empty cylinder, but you can finish one on an empty
        # cylinder.** An out-of-gas ascent, a drained stage and an SPG pegged at zero are
        # real dives worth logging, so `end_pressure` takes 0; a `start_pressure` of 0 is
        # a file's absent-marker, a client bug or a typo, and all three are better stopped
        # than stored (see `DiveMixtureSchema._drop_unpressurized` for the corpus).
        #
        # Both are bands rather than one-sided, for the same reason `po2_limit` is: 350
        # bar sits above any real 300 bar DIN fill, so the only things it rejects are a
        # unit error (the DM5 millibar bug stored 205203) and a sidemount pair summed as
        # one cylinder. The upper clause is also what keeps a `NaN` out - Postgres sorts
        # `NaN` above every number, so `> 0` alone admits it, and one stored `NaN` turns
        # `GET /dives` into a 500 (see `_ParserOutput`).
        CheckConstraint(
            "start_pressure IS NULL OR (start_pressure > 0 AND start_pressure <= 350)",
            name="ck_dive_mixture_start_pressure_range",
        ),
        CheckConstraint(
            "end_pressure IS NULL OR (end_pressure >= 0 AND end_pressure <= 350)",
            name="ck_dive_mixture_end_pressure_range",
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
