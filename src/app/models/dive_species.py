from sqlalchemy import ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class DiveSpecies(Base):
    """Join table linking a dive to the species spotted on it.

    `position` preserves the order the diver listed them in, so a dive's sightings read back
    the way they were entered. Mirrors `DiveGearItem` field for field - see
    `crud_dive_species.py`, which replaces a dive's whole species list wholesale rather than
    diffing it.

    Species-only in v1: no count, no size, no per-sighting note. Those are additive columns
    here when they are wanted, and the dive's own `notes` field is the escape hatch
    meanwhile.

    **Both cascades are dormant, for different reasons.** `Dive` is soft-deleted, so no
    `DELETE FROM dive` is ever issued and the `dive_id` cascade never fires - the same
    asymmetry `TripLocation` documents. And nothing deletes a `Species` at all: the catalog
    is global and immutable in v1. They are declared anyway, because a table that outlives
    both of those decisions should not be the thing that has to be remembered.
    """

    __tablename__ = "dive_species"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, unique=True, primary_key=True, init=False)
    # No standalone index on dive_id: the composite index below (leading column dive_id)
    # already serves lookups filtered by dive_id alone, plus satisfies the ORDER BY position
    # used by get_species_for_dive/get_species_for_dives without an extra index.
    dive_id: Mapped[int] = mapped_column(ForeignKey("dive.id", ondelete="CASCADE"))
    # Indexed for `recalculate_dive_stats`'s `COUNT(DISTINCT species_id)` and for the
    # `?species_uuid=` dive filter iteration 2 adds; not a leading column of any other index
    # here. Same rationale as `DiveGearItem.gear_item_id`.
    species_id: Mapped[int] = mapped_column(ForeignKey("species.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("dive_id", "species_id", name="ux_dive_species_dive_id_species_id"),
        Index("ix_dive_species_dive_id_position", "dive_id", "position"),
    )
