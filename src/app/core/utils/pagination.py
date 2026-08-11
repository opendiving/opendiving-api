"""Bounds on the `page`/`items_per_page` query parameters shared by every list endpoint
in `api.v1`.

These arrive straight off the query string, so they are attacker-controlled: left alone,
`?items_per_page=999999999` asks the database for the caller's entire table in one
response, and `?page=-1` asks for a negative OFFSET. Neither is a request any real
client makes.

The clamping lives here rather than in each route because it was previously copied into
three of the eight list endpoints and simply missing from the other five - the kind of
inconsistency that is invisible until someone finds the endpoint that lacks it.
"""

DEFAULT_MAX_ITEMS_PER_PAGE = 100


def clamp_pagination(
    page: int, items_per_page: int, *, max_items_per_page: int = DEFAULT_MAX_ITEMS_PER_PAGE
) -> tuple[int, int]:
    """Returns `(page, items_per_page)` forced into sane bounds.

    Deliberately clamps rather than rejecting: a client asking for more than the ceiling
    gets the ceiling, not a 422. These are convenience parameters, not a contract, and
    silently capping them keeps existing callers working.
    """
    return max(page, 1), min(max(items_per_page, 1), max_items_per_page)
