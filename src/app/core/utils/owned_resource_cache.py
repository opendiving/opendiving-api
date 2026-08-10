import uuid as uuid_pkg
from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastcrud import compute_offset, paginated_response
from sqlalchemy.ext.asyncio import AsyncSession

from ..exceptions.http_exceptions import NotFoundException
from .cache import cache, delete_keys_by_pattern


class OwnedResourceCache[InternalT, PublicT]:
    """Factory for the read-caching pattern shared by simple per-user owned resources
    (trips, dive sites, ...): a paginated list endpoint and a single-item endpoint, each
    backed by a `@cache`-wrapped read helper, plus list-cache invalidation on mutation.

    This intentionally does *not* wrap the create/patch/delete routes themselves, nor the
    ownership check: per `DECISIONS.md` ("auth before cache"), the ownership check must run
    *before* a cached read is ever called, since `@cache` can serve a cached response
    without re-running any authorization logic. Each resource's route still performs that
    check itself, then delegates the actual (cached) data-fetching to `read_list`/`read_item`
    below, and calls `invalidate_list` after a mutation.

    Resources whose read/list logic does more than a straight `get_multi`/`get` plus a shape
    conversion (e.g. `dives.py`, which also enriches results with related trips/dive sites and
    supports extra filters) don't fit this shape and should keep their own hand-written cache
    helpers instead of forcing themselves through this factory.
    """

    def __init__(
        self,
        *,
        resource_name: str,
        resource_label: str,
        item_cache_prefix: str,
        crud: Any,
        schema_to_select: type[InternalT],
        to_public: Callable[[InternalT, uuid_pkg.UUID], PublicT],
        sort_columns: str,
        sort_orders: str = "asc",
        list_expiration: int = 60,
    ) -> None:
        """
        Parameters
        ----------
        resource_name: str
            Plural, snake_case resource name used to build list cache keys/invalidation
            patterns, e.g. "trips" -> `user_{user_id}_trips:*`.
        resource_label: str
            Human-readable singular label used in `NotFoundException` messages, e.g. "Trip".
        item_cache_prefix: str
            Cache key prefix for the single-item cache, e.g. "trip_cache".
        crud: FastCRUD
            The resource's FastCRUD instance (must support `get_multi`/`get`).
        schema_to_select: type
            The internal read schema passed as `schema_to_select` to `crud.get`/`get_multi`.
        to_public: Callable[[InternalT, uuid.UUID], PublicT]
            Converts an internal row (as returned by `crud`) plus the owner's `user_uuid`
            into the resource's public response shape.
        sort_columns / sort_orders: str
            Passed through to `crud.get_multi` for the list endpoint.
        list_expiration: int
            TTL (seconds) for the list cache. The single-item cache has no expiration, matching
            the existing `trip_cache`/`dive_site_cache`/`dive_cache` behavior.
        """
        self._resource_name = resource_name
        self._resource_label = resource_label
        self._crud = crud
        self._schema_to_select = schema_to_select
        self._to_public = to_public
        self._sort_columns = sort_columns
        self._sort_orders = sort_orders

        self.read_list = cache(
            key_prefix=f"user_{{user_id}}_{resource_name}:page_{{page}}:items_per_page:{{items_per_page}}",
            resource_id_name="user_id",
            expiration=list_expiration,
        )(self._read_list_uncached)

        self.read_item = cache(key_prefix=item_cache_prefix, resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)(
            self._read_item_uncached
        )

    async def _read_list_uncached(
        self,
        request: Request,
        user_id: int,
        user_uuid: uuid_pkg.UUID,
        db: AsyncSession,
        page: int,
        items_per_page: int,
    ) -> dict[str, Any]:
        """Fetches (and, via `read_list`, caches) a user's paginated resource list.

        Only ever reached through `read_list`, and only after the caller's authorization has
        already been checked by the route - see the class docstring.
        """
        data = await self._crud.get_multi(
            db=db,
            offset=compute_offset(page, items_per_page),
            limit=items_per_page,
            user_id=user_id,
            is_deleted=False,
            sort_columns=self._sort_columns,
            sort_orders=self._sort_orders,
        )
        data["data"] = [self._to_public(item, user_uuid).model_dump() for item in data["data"]]  # type: ignore[attr-defined]

        response: dict[str, Any] = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
        return response

    async def _read_item_uncached(
        self, request: Request, uuid: uuid_pkg.UUID, owner_uuid: uuid_pkg.UUID, db: AsyncSession
    ) -> PublicT:
        """Fetches (and, via `read_item`, caches) a single resource by uuid, regardless of owner.

        Only ever reached through `read_item`, and only after the caller's authorization has
        already been checked by the route - see the class docstring.
        """
        db_item = await self._crud.get(
            db=db, uuid=uuid, is_deleted=False, schema_to_select=self._schema_to_select, return_as_model=True
        )
        if db_item is None:
            raise NotFoundException(f"{self._resource_label} not found")

        return self._to_public(db_item, owner_uuid)

    async def invalidate_list(self, user_id: int) -> None:
        """Invalidates every cached list page for the given user, e.g. after a create/patch/delete."""
        await delete_keys_by_pattern(f"user_{user_id}_{self._resource_name}:*")
