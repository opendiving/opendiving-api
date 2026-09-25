"""Cross-resource cache invalidation.

A dive's cached representation embeds *summaries of other resources* - its dive
sites' names, locations and positions (`DiveSiteInfo`) and its gear items'
names/brands/types (`GearItemInfo`). So renaming a dive site, dragging its marker
or renaming a gear item makes every cached dive that references it stale, even
though the dive row itself never changed.

The same holds for a course: a dive read and a certification read each carry the
uuid of the course they point at, so deleting one changes what all three
families say.

A contact is the same shape again, five hosts wide: a dive, a course, a certification,
a service record and a trip part carry its uuid.

These helpers live here rather than in the route modules so `dives.py`,
`gear_items.py`, `dive_sites.py`, `courses.py` and `contacts.py` can all reach them without
importing each other (which would be circular - `dives.py` already invalidates
gear caches, and gear now has to invalidate dive caches).

Both work by pattern, which is only possible because every affected cache key is
user-scoped. See `read_dive`/`_cached_read_dives` and `gear_items.py` for the key
shapes themselves.
"""

import uuid as uuid_pkg
from collections.abc import Iterable

from ..core.utils.cache import delete_keys_by_pattern
from ..core.utils.owned_resource_cache import OwnedResourceCache


async def invalidate_dive_caches(user_id: int) -> None:
    """Drop every cached dive read for a user: the paginated list pages
    (`user_{id}_dives:page_...`) and the individual dives (`user_{id}_dive:{uuid}`).

    Called after any mutation to a dive *or* to something a dive read embeds - a
    dive site or a gear item.

    Deliberately two patterns rather than one `user_{id}_dive*`: that shorter
    pattern would also sweep `user_{id}_dive_sites:page_...` (the dive *site* list
    cache), which is a different resource and needn't be dropped just because a
    dive changed. Harmless if it happened, but it would quietly cost every dive
    edit an extra dive-site list rebuild.
    """
    await delete_keys_by_pattern(f"user_{user_id}_dives:*")
    await delete_keys_by_pattern(f"user_{user_id}_dive:*")


async def invalidate_certification_caches(user_id: int) -> None:
    """Drop every cached certification read for a user.

    Both cache keys share the `user_{id}_certification` prefix
    (`..._certifications:page_...` and `..._certification:{uuid}`), so one pattern
    covers the lot and nothing else starts with it.

    Called after any metadata mutation *and* after every card file upload or delete:
    `CertificationRead` embeds each stored file's metadata (`CertificationFileInfo`), so
    photographing a card changes what a cached list page should say even though no
    `certification` column moved.
    """
    await delete_keys_by_pattern(f"user_{user_id}_certification*")


async def invalidate_course_caches(user_id: int) -> None:
    """Drop every cached course read for a user.

    Both cache keys share the `user_{id}_course` prefix (`..._courses:page_...` and
    `..._course:{uuid}`), so one pattern covers the lot and nothing else starts with it -
    the same shape as certifications, and deliberately *not* the dive prefix's problem,
    where `user_{id}_dive*` would also sweep the dive-*site* list.

    Deleting a course additionally calls `invalidate_dive_caches` and
    `invalidate_certification_caches`: both those reads carry the course's uuid, and the
    `ON DELETE SET NULL` just made every one of them read back `course_uuid: null`.
    """
    await delete_keys_by_pattern(f"user_{user_id}_course*")


async def invalidate_contact_caches(user_id: int) -> None:
    """Drop every cached contact read for a user.

    Both key shapes share the `user_{id}_contact` prefix (`..._contacts:page_...` and
    `..._contact:{uuid}`), so one pattern covers the lot, as for courses.

    A contact's reads embed nothing, and the five hosts that reference one carry only its
    uuid, so a rename reaches no other family. Deleting a contact drops all five hosts'
    families, the `ON DELETE SET NULL` having rewritten their rows.
    """
    await delete_keys_by_pattern(f"user_{user_id}_contact*")


async def invalidate_trip_items(trip_uuids: Iterable[uuid_pkg.UUID]) -> None:
    """Drop the single-trip reads of these trips.

    `trip_cache:{uuid}` carries no user in its key, so no per-user pattern reaches it; a
    writer that changes what a trip read says from outside the trip routes - deleting a
    contact a part names - has to collect the uuids and drop them one by one.
    """
    for trip_uuid in trip_uuids:
        await delete_keys_by_pattern(f"trip_cache:{trip_uuid}")


async def invalidate_gear_caches(user_id: int) -> None:
    """Drop every cached gear read for a user.

    All gear cache keys share the `user_{id}_gear_` prefix (`..._gear_items:page_...`,
    `..._gear_item:{uuid}`, `..._gear_sets:page_...`, `..._gear_set:{uuid}`), so a
    single pattern covers the lot - and unlike the dive prefix above, nothing else
    starts with it.

    Sets embed their items' names, so an item edit has to invalidate set reads too -
    and a *dive* mutation has to call this as well, since it changes items'
    `dive_count` (see `dives.py`).
    """
    await delete_keys_by_pattern(f"user_{user_id}_gear_*")


# The two list caches that live *inside* their routers as `OwnedResourceCache` instances
# (`api/v1/dive_sites.py::_dive_site_cache`, `api/v1/trips.py::_trip_cache`) rather than
# behind a helper here, because until logbook import there was no writer outside those two
# routers - each mutation route calls its own `invalidate_list` and that was the whole
# story. An import fills both collections from a service, which has no business importing
# a route module, so the key shape is shared instead of the object: these two go through
# `OwnedResourceCache.list_cache_pattern`, the same function `invalidate_list` uses, and
# `tests/test_cache_utils.py` checks the resource names still match the real caches'.
#
# Without them a restored logbook serves empty `/dive-sites` and `/trips` pages for up to
# the 60-second list expiry - at exactly the moment the diver goes looking at what they
# just restored.
async def invalidate_dive_site_caches(user_id: int) -> None:
    """Drop every cached dive-site list page for a user."""
    await delete_keys_by_pattern(OwnedResourceCache.list_cache_pattern("dive_sites", user_id))


async def invalidate_trip_caches(user_id: int) -> None:
    """Drop every cached trip list page for a user."""
    await delete_keys_by_pattern(OwnedResourceCache.list_cache_pattern("trips", user_id))
