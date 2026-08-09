"""Cross-resource cache invalidation.

A dive's cached representation embeds *summaries of other resources* - its dive
sites' names/locations (`DiveSiteInfo`) and its gear items' names/brands/types
(`GearItemInfo`). So renaming a dive site or a gear item makes every cached dive
that references it stale, even though the dive row itself never changed.

These helpers live here rather than in the route modules so `dives.py`,
`gear_items.py` and `dive_sites.py` can all reach them without importing each
other (which would be circular - `dives.py` already invalidates gear caches, and
gear now has to invalidate dive caches).

Both work by pattern, which is only possible because every affected cache key is
user-scoped. See `read_dive`/`_cached_read_dives` and `gear_items.py` for the key
shapes themselves.
"""

from ..core.utils.cache import delete_keys_by_pattern


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
