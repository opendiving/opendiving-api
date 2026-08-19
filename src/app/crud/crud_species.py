import uuid as uuid_pkg

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.species import Species


async def resolve_species_ids(db: AsyncSession, species_uuids: list[uuid_pkg.UUID]) -> dict[uuid_pkg.UUID, int] | None:
    """Resolve species public `uuid`s to their internal `id`s. `None` if any uuid is unknown.

    The counterpart to `resolve_gear_item_ids_for_user`/`resolve_dive_site_ids_for_user`,
    minus their `user_id` filter - and the omission is the point, not an oversight. Those two
    filter by owner because attaching another diver's gear to your dive is the thing they
    exist to prevent. The species catalog is global (see `models/species.py`): every row is a
    fact about the ocean that every account may reference, so there is no owner to compare
    against and nothing to leak. A species uuid is not an existence oracle for anything
    private.

    What is still checked is existence, which is what keeps `write_dive` able to answer 422
    "Species not found." before it writes any join rows, rather than letting a bad uuid
    surface as an FK violation halfway through.
    """
    unique_uuids = set(species_uuids)
    if not unique_uuids:
        return {}

    result = await db.execute(select(Species.uuid, Species.id).where(Species.uuid.in_(unique_uuids)))
    mapping = {row.uuid: row.id for row in result}
    if mapping.keys() != unique_uuids:
        return None
    return mapping
