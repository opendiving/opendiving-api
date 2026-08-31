from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dive_species import DiveSpecies
from ..models.species import Species
from ..schemas.dive import SpeciesInfo

# The `species` columns making up a `SpeciesInfo` (the summary shape embedded in a dive), in
# the order `species_info_from_row` unpacks them. Kept here rather than in `crud_species.py`
# - unlike gear, whose columns are shared with the gear-set join - this join table is the
# only reader.
SPECIES_INFO_COLUMNS = (
    Species.uuid,
    Species.scientific_name,
    Species.common_name,
    Species.rank,
    # The digest, not the key and not a URL: it is both "there is a photo" and which version,
    # and the client builds the URL. See `SpeciesInfo.photo_sha256`.
    Species.photo_sha256,
)


def species_info_from_row(row: Any) -> SpeciesInfo:
    """Build a `SpeciesInfo` from a result row selecting `SPECIES_INFO_COLUMNS`."""
    return SpeciesInfo(
        uuid=row.uuid,
        scientific_name=row.scientific_name,
        common_name=row.common_name,
        rank=row.rank,
        photo_sha256=row.photo_sha256,
    )


async def get_species_for_dive(db: AsyncSession, dive_id: int) -> list[SpeciesInfo]:
    """Return the species spotted on a dive, in the order the diver listed them.

    Nothing hides here and nothing can: species are never deleted, so unlike the gear join
    there is not even a cascade to reason about. Same shape as `get_gear_items_for_dive`.
    """
    result = await db.execute(
        select(*SPECIES_INFO_COLUMNS)
        .join(DiveSpecies, DiveSpecies.species_id == Species.id)
        .where(DiveSpecies.dive_id == dive_id)
        .order_by(DiveSpecies.position)
    )
    return [species_info_from_row(row) for row in result]


async def get_species_for_dives(db: AsyncSession, dive_ids: list[int]) -> dict[int, list[SpeciesInfo]]:
    """Batched version of `get_species_for_dive`.

    Nothing calls this with more than one id yet - species embed on the single-dive read
    only, deliberately (see `DiveReadWithMixtures.species`). Written batched anyway, because
    the day a list surface wants species chips the choice must be "call this per page", not
    "write the batched version now and hope nobody shipped an N+1 in the meantime".

    Pre-seeds the per-dive lists empty so a dive with no sightings comes back with `[]`
    rather than dropping out of the mapping.
    """
    species_by_dive: dict[int, list[SpeciesInfo]] = {dive_id: [] for dive_id in dive_ids}
    if not dive_ids:
        return species_by_dive

    result = await db.execute(
        select(DiveSpecies.dive_id, *SPECIES_INFO_COLUMNS)
        .join(Species, Species.id == DiveSpecies.species_id)
        .where(DiveSpecies.dive_id.in_(dive_ids))
        .order_by(DiveSpecies.dive_id, DiveSpecies.position)
    )
    for row in result:
        species_by_dive[row.dive_id].append(species_info_from_row(row))
    return species_by_dive


async def replace_species_for_dive(db: AsyncSession, dive_id: int, species_ids: list[int], commit: bool = True) -> None:
    """Replace all species for a dive with the given ordered list.

    Duplicate ids are silently deduplicated (keeping each id's first occurrence, which
    determines its position) to avoid a unique-constraint violation - the same
    delete-and-reinsert approach as `replace_gear_items_for_dive`. A diver picking the same
    species twice meant "I saw it", not "I saw two", and v1 stores no count to hold the
    difference.

    Safe to hand a full list: nothing deletes a species, so what `get_species_for_dive` hands
    out is the whole truth and echoing it back destroys nothing.
    """
    unique_ids = list(dict.fromkeys(species_ids))
    await db.execute(delete(DiveSpecies).where(DiveSpecies.dive_id == dive_id))
    for position, species_id in enumerate(unique_ids):
        db.add(DiveSpecies(dive_id=dive_id, species_id=species_id, position=position))
    if commit:
        await db.commit()
