"""The species catalog - a global table, and the only one in `api/v1` that belongs to nobody.

Every other resource here is scoped to the caller: the route resolves their id, the query
filters on it, and somebody else's row reads as a 404. A species is a fact about the ocean
rather than about a diver, so none of that applies - these three endpoints authenticate but
never check ownership, and two accounts asking about *Amphiprion ocellaris* get byte-identical
answers. That is the point: it is what lets a dive reference a shared row, what makes
`species_seen` countable, and what a life list would be built on.

What it costs is spelled out where it bites. There is no `@cache` decorator on any of these:
the reads are a PK lookup and a small `ILIKE`, the rows are immutable in v1, and adding a
cache would mean building an invalidation surface for a problem that does not exist yet. The
service caches *upstream* answers in Redis under a deliberately un-user-scoped key, which is
a different thing - see `services.species_service`.

Search degrades and never fails; resolve is the one endpoint here that can 503, because it
must not invent a catalog row without the authoritative record behind it.
"""

import uuid as uuid_pkg
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import NotFoundException
from ...core.utils.rate_limit import enforce_rate_limit
from ...models.species import Species
from ...schemas.species import SpeciesRead, SpeciesResolveRequest, SpeciesSearchResponse
from ...services.species_service import resolve_species, search_species

router = APIRouter(tags=["species"])


async def _enforce_species_limit(user_id: int) -> None:
    """Per-user budget, shared by search and resolve, and the only thing here that can 429.

    Distinct from the per-provider caps inside the service: those bound what this instance
    does to a third party and *degrade* when hit, since they are global and one diver's
    search must not reject another's. This one bounds what a single account can make this
    instance do, so rejecting the account that spent it is exactly right.

    A plain awaited call rather than a `Depends()`, matching `_enforce_geocode_limit`: it
    needs the resolved user, and threading it through a dependency would buy nothing but a
    second place to read.
    """
    await enforce_rate_limit(
        f"species:user:{user_id}",
        settings.SPECIES_RATE_LIMIT_PER_USER,
        settings.SPECIES_RATE_LIMIT_WINDOW_SECONDS,
    )


# The two literal paths are declared **before** `/species/{uuid}` on purpose. FastAPI matches
# routes in declaration order, so with the parameterized one first, "search" and "resolve"
# would be parsed as uuids and every request to either would 422.
@router.get("/species/search", response_model=SpeciesSearchResponse)
async def read_species_search(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    q: Annotated[str, Query(min_length=2, max_length=255, description="Common, scientific or alias name")],
) -> SpeciesSearchResponse:
    """Find species by any name they go by - "clownfish", "Amphiprion", "Manta birostris".

    Searches this instance's catalog together with the World Register of Marine Species and
    Wikidata, merged into one ranked list. A result carrying a `uuid` is already in the
    catalog and can be attached to a dive immediately; one without is a live hit that has to
    be resolved (`POST /species/resolve`) first.

    Returns an empty list rather than an error for both "nothing matched" and "the registers
    are unavailable" - a diver can log the dive and add the sighting later, and a 502 here
    would make a form look broken over an optional convenience. `has_more` means the answer
    was cut: keep typing rather than expecting the rest.

    A superseded name resolves to the accepted taxon - searching *Manta birostris* returns
    *Mobula birostris*. `matched_name` then reports what actually matched, and only where the
    row's own names do not already explain it: a synonym the diver typed, or a common name in
    any language. Results are ordered by how well each row answers the query, with species
    ahead of the genera and families above them.
    """
    await _enforce_species_limit(current_user["id"])
    # Normalized here as well as in the service, so the shape the cache key is built from is
    # the shape this route accepted - the reasoning `read_dive_sites` spells out for its own
    # search parameter.
    return await search_species(db=db, query=q.strip())


@router.post("/species/resolve", response_model=SpeciesRead)
async def write_species_resolve(
    values: SpeciesResolveRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> SpeciesRead:
    """Turn a search hit into a catalog row, and return it.

    Idempotent, and a 200 rather than a 201 for that reason: the caller is naming a taxon
    that already exists in the world, and whether this instance had seen it before is not
    something they should have to care about. Calling it twice returns the same `uuid`.

    Given an unaccepted AphiaID this stores - and returns - the *accepted* taxon it defers
    to, so the catalog only ever holds current names.

    503 when the register cannot be reached and the species is not already local. This is
    the one endpoint in the species API that fails rather than degrading: a catalog row is
    shared with every account and never rewritten, so inventing one without the authoritative
    record behind it would be worse than asking the diver to try again.

    Called at pick time rather than at dive-save time, which is what keeps saving a dive from
    ever blocking on a third party.
    """
    await _enforce_species_limit(current_user["id"])
    species = await resolve_species(db=db, aphia_id=values.aphia_id)
    return SpeciesRead.model_validate(species, from_attributes=True)


@router.get("/species/{uuid}", response_model=SpeciesRead)
async def read_species(
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> SpeciesRead:
    """Return one species from the catalog.

    Authenticated but **not** ownership-checked, unlike every other `/{uuid}` route here: the
    catalog is global, so there is no owner to compare against and a species uuid is not an
    existence oracle for anything private. A 404 here means the species genuinely is not in
    this instance's catalog, rather than the "not yours" that the same status means elsewhere.
    """
    species = (await db.execute(select(Species).where(Species.uuid == uuid))).scalar_one_or_none()
    if species is None:
        raise NotFoundException("Species not found")
    return SpeciesRead.model_validate(species, from_attributes=True)
