from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dive_species import DiveSpecies
from ..models.species import Species
from ..schemas.dive import SightingRead

# The columns making up a `SightingRead` - the species' summary, then the sighting's own - in
# the order `sighting_from_row` unpacks them. Kept here rather than in `crud_species.py`
# - unlike gear, whose columns are shared with the gear-set join - this join table is the
# only reader. The sighting's two are labelled because a result row is a tuple, and
# `row.count` is the tuple's own method.
SIGHTING_COLUMNS = (
    Species.uuid,
    Species.scientific_name,
    Species.common_name,
    Species.rank,
    # The digest, not the key and not a URL: it is both "there is a photo" and which version,
    # and the client builds the URL. See `SpeciesInfo.photo_sha256`.
    Species.photo_sha256,
    DiveSpecies.count.label("sighting_count"),
    DiveSpecies.notes.label("sighting_notes"),
)


@dataclass(frozen=True, slots=True)
class StoredSighting:
    """One sighting as the join table stores it: the catalog row, the count, the note."""

    species_id: int
    count: int | None = None
    notes: str = ""


def sighting_from_row(row: Any) -> SightingRead:
    """Build a `SightingRead` from a result row selecting `SIGHTING_COLUMNS`."""
    return SightingRead(
        uuid=row.uuid,
        scientific_name=row.scientific_name,
        common_name=row.common_name,
        rank=row.rank,
        photo_sha256=row.photo_sha256,
        count=row.sighting_count,
        notes=row.sighting_notes,
    )


async def get_species_for_dive(db: AsyncSession, dive_id: int) -> list[SightingRead]:
    """Return a dive's sightings, in the order the diver listed them.

    Nothing hides here and nothing can: species are never deleted, so unlike the gear join
    there is not even a cascade to reason about. Same shape as `get_gear_items_for_dive`.
    """
    result = await db.execute(
        select(*SIGHTING_COLUMNS)
        .join(DiveSpecies, DiveSpecies.species_id == Species.id)
        .where(DiveSpecies.dive_id == dive_id)
        .order_by(DiveSpecies.position)
    )
    return [sighting_from_row(row) for row in result]


async def get_species_for_dives(db: AsyncSession, dive_ids: list[int]) -> dict[int, list[SightingRead]]:
    """Batched version of `get_species_for_dive`.

    Nothing calls this with more than one id yet - sightings embed on the single-dive read
    only, deliberately (see `DiveReadWithMixtures.sightings`). Written batched anyway, because
    the day a list surface wants species chips the choice must be "call this per page", not
    "write the batched version now and hope nobody shipped an N+1 in the meantime".

    Pre-seeds the per-dive lists empty so a dive with no sightings comes back with `[]`
    rather than dropping out of the mapping.
    """
    species_by_dive: dict[int, list[SightingRead]] = {dive_id: [] for dive_id in dive_ids}
    if not dive_ids:
        return species_by_dive

    result = await db.execute(
        select(DiveSpecies.dive_id, *SIGHTING_COLUMNS)
        .join(Species, Species.id == DiveSpecies.species_id)
        .where(DiveSpecies.dive_id.in_(dive_ids))
        .order_by(DiveSpecies.dive_id, DiveSpecies.position)
    )
    for row in result:
        species_by_dive[row.dive_id].append(sighting_from_row(row))
    return species_by_dive


async def replace_species_for_dive(
    db: AsyncSession, dive_id: int, sightings: Sequence[StoredSighting], commit: bool = True
) -> None:
    """Replace a dive's sightings with the given ordered list - the same delete-and-reinsert
    approach as `replace_gear_items_for_dive`.

    **Written as handed, a species twice included.** One sighting per species is the
    callers' rule, each on its own terms - the write schemas refuse a repeat, the importer
    keeps the first and reports the rest - because only a caller knows which answer it owes.
    `ux_dive_species_dive_id_species_id` refuses what reaches it anyway.

    Safe to hand a full list: nothing deletes a species, so what `get_species_for_dive` hands
    out is the whole truth and echoing it back destroys nothing.
    """
    await db.execute(delete(DiveSpecies).where(DiveSpecies.dive_id == dive_id))
    for position, sighting in enumerate(sightings):
        db.add(
            DiveSpecies(
                dive_id=dive_id,
                species_id=sighting.species_id,
                position=position,
                count=sighting.count,
                notes=sighting.notes,
            )
        )
    if commit:
        await db.commit()
