from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class DiveSpecies(Base):
    """Join table holding a dive's sightings: each species spotted on it, how many, and what
    the diver wrote about it.

    `position` preserves the order the diver listed them in, so a dive's sightings read back
    the way they were entered. `crud_dive_species.py` replaces a dive's whole list wholesale
    rather than diffing it, as the gear join's crud does. The unique constraint below is
    DiveJSON's one-sighting-per-species rule seen from the table.

    `count` is null for *seen, not counted* - never `1`, which is a count - and `notes` is
    empty for no note, the app's spelling of absent on every notes column. Size, life stage
    and sex are additive columns here when they are wanted.

    **Both cascades are dormant, for different reasons.** `Dive` is soft-deleted, so no
    `DELETE FROM dive` is ever issued and the `dive_id` cascade never fires - the same
    asymmetry `TripPart` documents. And nothing deletes a `Species` at all: the catalog
    is global and immutable in v1. They are declared anyway, because a table that outlives
    both of those decisions should not be the thing that has to be remembered.
    """

    __tablename__ = "dive_species"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    # No standalone index on dive_id: the composite index below (leading column dive_id)
    # already serves lookups filtered by dive_id alone, plus satisfies the ORDER BY position
    # used by get_species_for_dive/get_species_for_dives without an extra index.
    dive_id: Mapped[int] = mapped_column(ForeignKey("dive.id", ondelete="CASCADE"))
    # Indexed for `recalculate_dive_stats`'s `COUNT(DISTINCT species_id)` and for the
    # `?species_uuid=` dive filter iteration 2 adds; not a leading column of any other index
    # here. Same rationale as `DiveGearItem.gear_item_id`.
    species_id: Mapped[int] = mapped_column(ForeignKey("species.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    count: Mapped[int | None] = mapped_column(Integer, default=None)
    # The server default is what fills the outgoing build's inserts while a deploy overlaps
    # it: that build writes no note and knows no column to write one into.
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")

    __table_args__ = (
        UniqueConstraint("dive_id", "species_id", name="ux_dive_species_dive_id_species_id"),
        Index("ix_dive_species_dive_id_position", "dive_id", "position"),
        CheckConstraint("count IS NULL OR count >= 1", name="ck_dive_species_count_positive"),
    )
