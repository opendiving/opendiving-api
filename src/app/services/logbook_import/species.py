"""Filling the species catalog before the import's transaction opens.

A DiveJSON document carries a *snapshot* of each species it references, and that snapshot
is never a source this app creates catalog rows from: `Species` is a global, ownerless
table filled one WoRMS pick at a time, so an importer writing to it from a document would
make every authenticated caller a writer to a table every account shares - and it could not
even be done honestly, because `status` is `NOT NULL` and the format has no member for it.
The interchange identity is the AphiaID (spec §6.11), and this is the sanctioned way to
turn one into a row: the same `resolve_species` call `POST /species/resolve` makes.

**Three reasons it runs here rather than anywhere else.** It cannot run during preview,
which stores nothing. It cannot run inside the apply transaction, because `resolve_species`
rolls its own transaction back before going outbound and commits when it succeeds. And its
worst case is long: two `_RESOLVE_BUDGET_SECONDS` passes (the synonym branch) plus
`_ENRICHMENT_BUDGET_SECONDS`, on the order of a minute per unknown AphiaID. So it is a
batch, before the write, under one total budget sized against that ceiling rather than
against the typical cost.

Its rows are the second deliberate exception to the import's all-or-nothing guarantee, and
a harmless one: they are global, idempotent, and identical to what a diver picking the same
animal by hand would have created. A failed apply leaves them behind and the next attempt
reuses them.
"""

import logging
import time

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...models.species import Species
from ..species_service import resolve_species

logger = logging.getLogger(__name__)


async def resolve_catalog_gaps(db: AsyncSession, *, aphia_ids: list[int]) -> frozenset[int]:
    """Add every AphiaID this instance does not hold yet, and say which were added.

    Whatever is left unresolved when the budget runs out - or when the register cannot be
    reached - simply stays missing, and the planner turns that into a skipped species and a
    note on each dive that named it. **Nothing raised in here becomes the request's
    response**: `resolve_species` signals both budget expiry and a WoRMS outage as a raised
    503, which is the right answer to `POST /species/resolve` and the wrong one to an
    import of two hundred dives that happens to mention one animal nobody can look up.
    """
    if not aphia_ids:
        return frozenset()

    known = set((await db.execute(select(Species.aphia_id).where(Species.aphia_id.in_(aphia_ids)))).scalars())
    missing = [aphia_id for aphia_id in aphia_ids if aphia_id not in known]
    if not missing:
        return frozenset()

    deadline = time.monotonic() + settings.IMPORT_SPECIES_BUDGET_SECONDS
    resolved: set[int] = set()
    for aphia_id in missing:
        if time.monotonic() >= deadline:
            logger.warning(
                "Species pre-pass ran out of budget with %d AphiaIDs unresolved", len(missing) - len(resolved)
            )
            break
        try:
            await resolve_species(db=db, aphia_id=aphia_id)
        except HTTPException:
            # 503 from `resolve_species`: the register did not answer, or its own budget
            # expired. Logged rather than propagated - see the docstring.
            logger.warning("Species %s could not be resolved during an import", aphia_id, exc_info=True)
            continue
        except Exception:
            # Deliberately bare. A species link is the one thing an import may drop without
            # losing a dive, so no failure in here is worth the whole logbook.
            logger.exception("Unexpected error resolving species %s during an import", aphia_id)
            continue
        resolved.add(aphia_id)
    return frozenset(resolved)
