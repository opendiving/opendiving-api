import uuid as uuid_pkg
from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastcrud import compute_offset, paginated_response
from sqlalchemy import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession

from .cache import cache, delete_keys_by_pattern
from .search import search_clause, search_multi


class OwnedResourceCache[InternalT, PublicT]:
    """Factory for the list-caching pattern shared by per-user owned resources: the key shape
    of a paginated list endpoint, a `@cache`-wrapped read helper for the plainest of them, and
    list-cache invalidation on mutation.

    This intentionally does *not* wrap the create/patch/delete routes themselves, nor the
    ownership check: per `DECISIONS.md` ("auth before cache"), the ownership check must run
    *before* a cached read is ever called, since `@cache` can serve a cached response
    without re-running any authorization logic. Each resource's route still performs that
    check itself, then delegates the actual (cached) data-fetching to `read_list` below, and
    calls `invalidate_list` after a mutation.

    There is no single-item read here. Every single read is hand-written under
    `user_{id}_{resource}` beside its list, so that one owner-scoped pattern drops both - which
    a key of the uuid alone cannot do.

    Resources whose read/list logic does more than a straight `get_multi`/`get` plus a shape
    conversion don't fit this shape and should keep their own hand-written cache helpers
    instead of forcing themselves through this factory. Those that opt out, and why:

    - `dives.py` - enriches each row with related trips/courses/dive sites/gear and supports
      several extra filters.
    - `gear_items.py` - carries an extra `include_archived` dimension in the cache key *and*
      batches a service-schedule lookup across the page for the service badge.
    - `certifications.py` - batches a card-file lookup across the page, and resolves each
      row's course uuid in a second batched query.
    - `trips.py` - opts out for both reasons at once. It batches a `trip_part` lookup
      across the page and embeds the rows, and searches an EXISTS over that child table
      rather than columns of its own; and a trip stores no dates, so the list's
      `min(part.start_date) DESC NULLS LAST` is an aggregate over another table that no
      `sort_columns` string can name. It still constructs one of these for
      `list_cache_key_prefix`, so its hand-rolled list helper keeps the key shape this
      factory defines - and, like `dive_sites.py`, it passes no `sort_columns` at all.
    - `courses.py` - the one that opts out for a **different reason**: not enrichment, an
      ordering. `GET /courses` sorts `start_date DESC NULLS LAST` with a `uuid` tie-break,
      and no path through this factory can produce it - `get_multi` and
      `core/utils/search.py::search_multi` both resolve a sort column to a bare `desc()`,
      with no `nullslast()` reachable and (in `search_multi`'s case) only one sort column by
      signature. So one hand-written `select()` serves both its searched and unsearched
      branches. Like `trips.py` it still constructs one of these for the key shapes.
    - `passkeys.py` - the first of those that opt out for the opposite reason: not
      *enriched*, not cached at all. `GET /user/passkeys` is unpaginated and returns the
      handful of rows registration lets an account accumulate, and nothing anywhere embeds a
      credential - so there is no page to cache and no invalidation obligation to get wrong.
      Caching it would be inventing a thing that can go stale.
    - `dive_form_presets.py` - uncached, and the plainest case of it: the read *would* fit
      this factory exactly, and there is simply nothing to cache for. Nothing embeds a
      preset - no dive, no user payload, no list anywhere carries one - so there is no
      second cache to invalidate and no staleness to trade against, and the panel that
      reads the list reads it once, when it opens. What caching it would buy is one Redis
      round trip saved on an interaction that happens once per form; what it would cost is
      an invalidation obligation on five mutating routes.
    - `sessions.py` - also uncached, but for a reason none of the others has: caching
      `GET /user/sessions` would be a **correctness** bug rather than a staleness trade. The
      response carries `current: bool` per row, resolved from the requesting token's `sid`,
      so it varies by *credential* and not merely by user - and every key here is
      user-scoped by design (`user_{id}_...`, which is what pattern invalidation depends
      on). One device's "This device" marker would be served to another. The rows are also
      unpaginated, capped and embedded by nothing, so there is no page worth the risk.
    - `people.py` - uncached because its answer changes on writes its owner never makes. A
      person read carries the linked account's current username, and a rename or purge of
      that other account reaches no key of this owner's, so a cache would serve the old one
      for its TTL. The list is one query - the join, a dive count, a search over two
      tables - over tens of rows.
    - `tags.py` - uncached for `people.py`'s cost and a plainer reason: each tag carries the
      count of live dives that name it, which every dive write moves, so a cache would owe an
      invalidation to every dive route for a list of one query over tens of rows.

    - `dive_sites.py` - opts out for `trips.py`'s two reasons: each site carries its tags and
      the summary of the dives naming it, further queries zipped back into the page, and the
      list sorts on that summary, which no `sort_columns` string can name. It keeps an
      instance for the key shape.

    In each of the enriched ones the enrichment is a second query whose results have to be
    zipped back into the page before conversion, which is precisely the step this factory has
    no room for. Adding a generic hook for it would complicate the factory for its one
    straightforward user, the contact list, to serve callers that each need something
    different; the duplication is the cheaper side of that trade. Revisit if the enrichment
    shape ever converges. `courses.py` would still be outside it either way - a hook for a
    second query does not buy an `ORDER BY` clause.
    """

    def __init__(
        self,
        *,
        resource_name: str,
        crud: Any,
        to_public: Callable[[InternalT, uuid_pkg.UUID], PublicT],
        sort_columns: str | None = None,
        sort_orders: str = "asc",
        search_columns: tuple[str, ...] = (),
        list_expiration: int = 60,
    ) -> None:
        """
        Parameters
        ----------
        resource_name: str
            Plural, snake_case resource name used to build list cache keys/invalidation
            patterns, e.g. "trips" -> `user_{user_id}_trips:*`.
        crud: FastCRUD
            The resource's FastCRUD instance (must support `get_multi`).
        to_public: Callable[[InternalT, uuid.UUID], PublicT]
            Converts an internal row (as returned by `crud`) plus the owner's `user_uuid`
            into the resource's public response shape.
        sort_columns / sort_orders: str
            Passed through to `crud.get_multi` for the list endpoint. `sort_columns` may be
            omitted only by a resource that opts out of `read_list` entirely and keeps an
            instance for `list_cache_key_prefix` alone - `trips.py` and `dive_sites.py`, their
            orders being aggregates over another table rather than columns of their own.
            `read_list` then raises rather than sorting by nothing.
        search_columns: tuple[str, ...]
            Model column names a `search=` term matches against, OR'd together and matched
            case-insensitively as a substring, e.g. `("name", "location_name")`. Leave empty to
            opt out of search entirely, in which case `read_list` takes no `search` argument
            and the cache key is unchanged.
        list_expiration: int
            TTL (seconds) for the list cache.
        """
        # Public, unlike its siblings: `tests/test_cache_utils.py` reads it off the real
        # dive-site and trip caches to check that `cache_invalidation`'s hard-coded names
        # still name the resources it means to sweep.
        self.resource_name = resource_name
        self._crud = crud
        self._to_public = to_public
        self._sort_columns = sort_columns
        self._sort_orders = sort_orders
        self._search_columns = search_columns

        # A searchable resource gets the term in its cache key, so two different searches
        # can't serve each other's results. Invalidation is a `user_{id}_{resource}:*`
        # wildcard either way, so the extra key segment needs no change there. Resources
        # without search keep the original key shape - `read_list` is called without a
        # `search` kwarg for those, and `@cache` would `KeyError` on the missing value.
        self.list_cache_key_prefix = f"user_{{user_id}}_{resource_name}:page_{{page}}:items_per_page:{{items_per_page}}"
        if search_columns:
            self.list_cache_key_prefix += ":search:{search}"

        self.read_list = cache(
            key_prefix=self.list_cache_key_prefix,
            resource_id_name="user_id",
            expiration=list_expiration,
        )(self._read_list_uncached)

    @property
    def _required_sort_column(self) -> str:
        """The sort column, for the two paths that cannot proceed without one.

        Reached only through `read_list`, which a resource that declared no sort column
        has opted out of - so this raises rather than inventing an ordering that would
        silently differ from the hand-written query the route actually serves.
        """
        if self._sort_columns is None:
            raise RuntimeError(f"{self.resource_name} opted out of read_list and declared no sort column")
        return self._sort_columns

    async def _read_list_uncached(
        self,
        request: Request,
        user_id: int,
        user_uuid: uuid_pkg.UUID,
        db: AsyncSession,
        page: int,
        items_per_page: int,
        search: str | None = None,
    ) -> dict[str, Any]:
        """Fetches (and, via `read_list`, caches) a user's paginated resource list.

        Only ever reached through `read_list`, and only after the caller's authorization has
        already been checked by the route - see the class docstring.
        """
        offset = compute_offset(page, items_per_page)
        term = (search or "").strip()

        if term and self._search_columns:
            data = await self._search_multi(db=db, user_id=user_id, term=term, offset=offset, limit=items_per_page)
        else:
            data = await self._crud.get_multi(
                db=db,
                offset=offset,
                limit=items_per_page,
                user_id=user_id,
                sort_columns=self._required_sort_column,
                sort_orders=self._sort_orders,
            )
        data["data"] = [self._to_public(item, user_uuid).model_dump() for item in data["data"]]  # type: ignore[attr-defined]

        response: dict[str, Any] = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
        return response

    def search_conditions(self, *, user_id: int, term: str) -> tuple[ColumnElement[bool], ...]:
        """The `WHERE` clauses matching the user's rows against a search term.

        No liveness clause: every resource routed through this factory is hard-deleted, so the
        column this used to name exists on none of their models.
        """
        model = self._crud.model
        return (
            model.user_id == user_id,
            search_clause(model, self._search_columns, term),
        )

    async def _search_multi(
        self, *, db: AsyncSession, user_id: int, term: str, offset: int, limit: int
    ) -> dict[str, Any]:
        return await search_multi(
            db=db,
            model=self._crud.model,
            conditions=self.search_conditions(user_id=user_id, term=term),
            sort_column=self._required_sort_column,
            sort_order=self._sort_orders,
            offset=offset,
            limit=limit,
        )

    @staticmethod
    def list_cache_pattern(resource_name: str, user_id: int) -> str:
        """The wildcard that sweeps one user's cached list pages for `resource_name`.

        A `staticmethod` because there is a second caller that has no instance to ask:
        `services/cache_invalidation.py` sweeps the dive-site and trip lists after a
        logbook import, and those two caches are module-private to their routers - a
        service importing a route module would invert the layering, and importing it
        lazily to dodge that would be the same inversion with a delay in it. Sharing the
        *shape* rather than the object is what keeps the two spellings from drifting;
        `tests/test_cache_utils.py` pins that they agree.
        """
        return f"user_{user_id}_{resource_name}:*"

    async def invalidate_list(self, user_id: int) -> None:
        """Invalidates every cached list page for the given user, e.g. after a create/patch/delete."""
        await delete_keys_by_pattern(self.list_cache_pattern(self.resource_name, user_id))
