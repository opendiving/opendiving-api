import uuid as uuid_pkg
from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastcrud import compute_offset, paginated_response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from ..exceptions.http_exceptions import NotFoundException
from .cache import cache, delete_keys_by_pattern

LIKE_ESCAPE_CHAR = "\\"


def escape_like(term: str) -> str:
    """Escapes the `LIKE`/`ILIKE` wildcards in a user-supplied search term, so a site
    named "50%" is searchable and typing "%" alone doesn't match everything.

    Pairs with `.ilike(pattern, escape=LIKE_ESCAPE_CHAR)`. The escape character itself has
    to be doubled first, or escaping the wildcards would produce new unescaped ones.
    """
    for char in (LIKE_ESCAPE_CHAR, "%", "_"):
        term = term.replace(char, LIKE_ESCAPE_CHAR + char)
    return term


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
        search_columns: tuple[str, ...] = (),
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
        search_columns: tuple[str, ...]
            Model column names a `search=` term matches against, OR'd together and matched
            case-insensitively as a substring, e.g. `("name", "location")`. Leave empty to
            opt out of search entirely, in which case `read_list` takes no `search` argument
            and the cache key is unchanged.
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
                is_deleted=False,
                sort_columns=self._sort_columns,
                sort_orders=self._sort_orders,
            )
        data["data"] = [self._to_public(item, user_uuid).model_dump() for item in data["data"]]  # type: ignore[attr-defined]

        response: dict[str, Any] = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
        return response

    def search_conditions(self, *, user_id: int, term: str) -> tuple[ColumnElement[bool], ...]:
        """The `WHERE` clauses matching the user's non-deleted rows against a search term."""
        model = self._crud.model
        pattern = f"%{escape_like(term)}%"
        return (
            model.user_id == user_id,
            model.is_deleted.is_(False),
            or_(*(getattr(model, column).ilike(pattern, escape=LIKE_ESCAPE_CHAR) for column in self._search_columns)),
        )

    async def _search_multi(
        self, *, db: AsyncSession, user_id: int, term: str, offset: int, limit: int
    ) -> dict[str, Any]:
        """The `search=`-filtered counterpart to `crud.get_multi`, returning the same
        `{"data": [...], "total_count": n}` shape.

        Hand-written rather than expressed as `get_multi` filter kwargs because those are
        AND'd together: matching a term against *either* the name or the location needs an
        OR across two columns, which the `__`-suffix filter syntax can't express.
        """
        model = self._crud.model
        conditions = self.search_conditions(user_id=user_id, term=term)

        total_count = await db.scalar(select(func.count()).select_from(model).where(*conditions))

        sort_column = getattr(model, self._sort_columns)
        # Selecting the columns rather than the entity keeps the rows as plain dicts, the
        # same shape `get_multi` hands back when called without a `schema_to_select`.
        stmt = (
            select(*model.__table__.columns)
            .where(*conditions)
            .order_by(sort_column.desc() if self._sort_orders == "desc" else sort_column.asc())
            .offset(offset)
            .limit(limit)
        )
        rows = (await db.execute(stmt)).mappings().all()

        return {"data": [dict(row) for row in rows], "total_count": total_count or 0}

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
