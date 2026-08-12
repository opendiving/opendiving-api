# Backend Decisions & Gotchas

Notes on non-obvious choices and pitfalls hit while building out the API, so the
reasoning survives independently of any particular chat/agent session. Keep this
updated as new gotchas are discovered.

## Schema changes have no migration tool

This project does not use Alembic (despite `alembic.ini` existing in `src/`) for
day-to-day schema changes made during this development phase. `Base.metadata.create_all()`
runs on startup and will create **brand new tables**, but it will **not** alter
existing tables (add columns, add indexes, etc.).

Practical workflow used throughout this project for adding a column to an existing table:
1. Add the field to the SQLAlchemy model (`models/*.py`) and Pydantic schema (`schemas/*.py`).
2. Restart the `api` container (`docker compose restart api`) - this creates any
   brand-new tables via `create_all()`.
3. Manually run the equivalent `ALTER TABLE ... ADD COLUMN ...` against the live
   DB: `docker compose exec -T db psql -U postgres -d opendive -c "ALTER TABLE ..."`.

If this project grows past the prototyping stage, introducing real Alembic
migrations is worth doing so schema changes are versioned and repeatable.

## Domain `CheckConstraint`s need a manual `ALTER TABLE` on existing DBs

`DiveMixture` (`oxygen`/`helium` 0-100, `oxygen + helium <= 100`, `volume > 0`,
`end_pressure <= start_pressure`) and `Dive` (`duration > 0`, `visibility >= 0`,
`max_depth > 0`, `avg_depth > 0`) declare `CheckConstraint`s in `__table_args__`
as DB-level backstops for validation that otherwise only lives in the frontend
Zod schemas (`lib/validations/dive.ts`). `max_depth`/`avg_depth`/`visibility`/
`start_pressure`/`end_pressure` are nullable columns; Postgres already treats a
`CHECK` as satisfied whenever it evaluates to `NULL` (e.g. plain `max_depth >
0` when `max_depth` is `NULL`), so the explicit `<col> IS NULL OR <col> > 0`
(or `>= 0` for `visibility`, or `... IS NULL OR ... IS NULL OR ...` for the
pressure ordering check) isn't strictly required, but is kept for clarity
about the intent.
`create_all()` never alters existing tables (see above), these constraints
only apply to brand-new `dive`/`dive_mixture` tables. On an already-running
dev DB, add them by hand:

```sql
ALTER TABLE dive_mixture ADD CONSTRAINT ck_dive_mixture_volume_positive CHECK (volume > 0);
ALTER TABLE dive_mixture ADD CONSTRAINT ck_dive_mixture_oxygen_range CHECK (oxygen >= 0 AND oxygen <= 100);
ALTER TABLE dive_mixture ADD CONSTRAINT ck_dive_mixture_helium_range CHECK (helium >= 0 AND helium <= 100);
ALTER TABLE dive_mixture ADD CONSTRAINT ck_dive_mixture_oxygen_helium_sum CHECK (oxygen + helium <= 100);
ALTER TABLE dive_mixture ADD CONSTRAINT ck_dive_mixture_pressure_order CHECK (start_pressure IS NULL OR end_pressure IS NULL OR end_pressure <= start_pressure);
ALTER TABLE dive ADD CONSTRAINT ck_dive_duration_positive CHECK (duration > 0);
ALTER TABLE dive ADD CONSTRAINT ck_dive_visibility_non_negative CHECK (visibility IS NULL OR visibility >= 0);
ALTER TABLE dive ADD CONSTRAINT ck_dive_max_depth_positive CHECK (max_depth IS NULL OR max_depth > 0);
ALTER TABLE dive ADD CONSTRAINT ck_dive_avg_depth_positive CHECK (avg_depth IS NULL OR avg_depth > 0);
```

A constraint violation surfaces as an `IntegrityError`/`asyncpg.CheckViolationError`
from the DB layer, not a Pydantic validation error - if adding more of these,
make sure callers (or a shared exception handler) turn that into a sensible
4xx response instead of a raw 500.

## Dive sites are many-to-many with dives via a join table

A dive can be logged at more than one dive site (e.g. a drift dive that crosses
several named sites), so `dive` does **not** have a `dive_site_id` column.
Instead `dive_dive_site` (model `DiveDiveSite`) joins `dive`/`dive_site`, with a
`position` column preserving the order the sites were visited in (0 = primary
site, used wherever only one site can be shown, e.g. "Site Name +2" in list
views). Both FKs are `ON DELETE CASCADE` - deleting a dive removes its rows in
the join table (not the sites), and hard-deleting a dive site (superuser only;
normal deletes are soft) removes it from any dive's site list without
affecting the rest of that dive.

`crud_dive_dive_sites.py` mirrors the `crud_dive_mixtures.py` pattern:
`replace_dive_sites_for_dive()` deletes-and-reinserts a dive's full site list on
every create/update (never diffed/upserted), exactly like mixtures. The API's
`dive_site_id` query filter on `GET /dives` still works the same from the
client's perspective, but now matches any dive that *includes* that site
(via a subquery over the join table) rather than an exact single-column match.

Applying this schema change to an existing local DB (per the "no migration
tool" workflow above): the new `dive_dive_site` table is created automatically
by `create_all()`, but you'll need to manually backfill it from the old
`dive.dive_site_id` column and drop that column, e.g.:
```sql
INSERT INTO dive_dive_site (dive_id, dive_site_id, position)
SELECT id, dive_site_id, 0 FROM dive WHERE dive_site_id IS NOT NULL;
ALTER TABLE dive DROP COLUMN dive_site_id;
```

`dive_dive_site.dive_id` originally had its own single-column index, but the
`UniqueConstraint("dive_id", "dive_site_id")` already produces a composite
index with `dive_id` as its leading column, which fully serves the `WHERE
dive_id = ...` filter used by `get_dive_sites_for_dive`/`get_dive_sites_for_dives`
- making the standalone index pure write overhead. It was replaced with a
composite `Index("ix_dive_dive_site_dive_id_position", "dive_id", "position")`,
which additionally lets Postgres satisfy those queries' `ORDER BY position`
from the index instead of sorting. `dive_site_id` keeps its own index since
it's not a leading column of any other index. On an existing local DB, apply
with:
```sql
DROP INDEX ix_dive_dive_site_dive_id;
CREATE INDEX ix_dive_dive_site_dive_id_position ON dive_dive_site (dive_id, position);
```

## Hot list queries needed composite indexes, not independent single-column ones

The paginated `GET /dives`, `/trips`, `/dive-sites` list endpoints all run the
same shape of query: `WHERE user_id = ... AND is_deleted = false ORDER BY
<some column> LIMIT ... OFFSET ...`. `dive`/`trip`/`dive_site` each only had
independent single-column indexes on `user_id` and `is_deleted`; Postgres can
use at most one of those per scan and still has to sort the result
separately - the `is_deleted` index in particular was nearly useless as a
leading column since the vast majority of rows have `is_deleted = false`.

Each table's standalone `is_deleted` index was replaced with a single partial
composite index matching its list endpoint's actual filter/sort, serving the
whole query directly (filter, exclude soft-deleted rows, *and* the
`ORDER BY` - no separate sort step):
```python
# Dive: _cached_read_dives / GET /dives, ORDER BY start_time DESC
Index("ix_dive_user_id_start_time", "user_id", start_time.desc(), postgresql_where=is_deleted.is_(False))
# Trip: read_trips / GET /trips, ORDER BY start_date DESC
Index("ix_trip_user_id_start_date", "user_id", start_date.desc(), postgresql_where=is_deleted.is_(False))
# DiveSite: read_dive_sites / GET /dive-sites, ORDER BY name ASC
Index("ix_dive_site_user_id_name", "user_id", "name", postgresql_where=is_deleted.is_(False))
```
`is_deleted` isn't a column in any of these - the partial `WHERE` predicate
already pins it to `false`, which is both smaller and more selective than a
3-column `(user_id, is_deleted, <sort col>)` index would be. This mirrors the
`func.lower(name)` partial unique indexes on `Trip`/`DiveSite` (see
"Case-insensitive per-user uniqueness" above), just non-unique. Note
`ix_dive_site_user_id_name` is deliberately a *separate* index from the
existing `ux_dive_site_user_id_name_location_lower` unique index, not a
replacement for it - the unique index is keyed on `lower(name)` and can't
satisfy a plain (case-sensitive) `ORDER BY name`.

The old standalone `is_deleted` index on all three tables was dropped
entirely: every other lookup on these tables filters by the `id` primary key
instead (or, for `trip_name_exists`/`dive_site_name_exists`, is already
covered by the `user_id`-leading unique indexes), so it had no other use.
Each table's `user_id` single-column index is kept, since not every query on
these tables excludes soft-deleted rows (e.g. `recalculate_dive_stats`'s
aggregate does, but relies on the new composite index the same way
`_cached_read_dives` does).

Verified by compiling and comparing against the intended DDL before applying,
same as the other partial indexes in this file. On an existing local DB,
apply with:
```sql
DROP INDEX ix_dive_is_deleted;
CREATE INDEX ix_dive_user_id_start_time ON dive (user_id, start_time DESC) WHERE is_deleted = false;

DROP INDEX ix_trip_is_deleted;
CREATE INDEX ix_trip_user_id_start_date ON trip (user_id, start_date DESC) WHERE is_deleted = false;

DROP INDEX ix_dive_site_is_deleted;
CREATE INDEX ix_dive_site_user_id_name ON dive_site (user_id, name) WHERE is_deleted = false;
```

`User.is_deleted` was deliberately left as a standalone index: `read_users`
(superuser-only, not a hot path) filters by `is_deleted` alone with no other
column to build a composite index around, so there's no equivalent win
available there.

## `Mapped[X]` vs `Mapped[X | None]` on `MappedAsDataclass`

`Base` extends both `DeclarativeBase` and `MappedAsDataclass`. On this setup,
**`Mapped[X]` without `| None` always compiles to `NOT NULL` at the actual
Postgres level**, even if you pass `mapped_column(default=None)`. The `default=`
only affects the Python dataclass `__init__` default, not nullability.

Always use `Mapped[X | None]` for any column that should be nullable. This bit
several fields during development (`Dive.max_depth/avg_depth/bottom_temperature/
visibility`, `DiveMixture.start_pressure/end_pressure`) before the pattern was
established.

## Two-pass Pydantic validation needs `X | None` on *both* type and default

Endpoints like `write_dive` do `SomeCreate(...).model_dump()` and then re-validate
the dict against an internal schema (`SomeCreateInternal(**dict)`). On this second
pass, Pydantic validates every key in the dict against the field's declared type -
including keys whose value is `None`. A field declared as `Field(default=None)`
**without** `X | None` in the type annotation will fail on this second pass even
though it "worked" on the first pass (first-pass validation only checks
explicitly-provided values, but the dumped dict always includes the key).

Always pair `Field(default=None)` with an `X | None` type annotation.

## `DiveMixture.po2` was replaced with `helium`

Mixtures originally tracked a PO₂ set-point (bar); this was replaced with a
`helium` percentage (for trimix), handled identically to `oxygen` - a plain
required `float` on both the model and `DiveMixtureBase`, defaulting to `0.0`
(vs. oxygen's `21.0`), with the same 0-100 range validation on the frontend
(`diveMixtureSchema`). This was a genuine schema swap (not an added column), so
applying it to the live dev DB per the "no migration tool" workflow above took
two manual steps rather than one:
```sql
ALTER TABLE dive_mixture ADD COLUMN helium DOUBLE PRECISION NOT NULL DEFAULT 0.0;
ALTER TABLE dive_mixture DROP COLUMN po2;
```
There's no meaningful way to backfill `helium` from old `po2` values (they
measure different things), so existing mixtures just got `helium = 0`.

## Mixtures are always replaced wholesale, never upserted by id

`crud_dive_mixtures.replace_mixtures_for_dive()` deletes all of a dive's existing
mixtures and re-inserts the given list on every save (create *and* update). The
mixture `id` returned by `GET` is purely informational for the client (e.g. React
key) - **never send it back** in a create/update payload. `DiveMixtureCreate` has
`extra="forbid"` and no `id` field, so echoing it back causes a
`422 extra_forbidden` error. (The web frontend's `normalizeMixtures()` strips it
before every submit.)

## Case-insensitive per-user uniqueness (trips, dive sites)

`Trip.name` and `DiveSite.name` are unique per user, case-insensitively, but only
among **non-soft-deleted** rows (so a name becomes reusable again once its
previous record is "deleted"). This is enforced two ways:
- A partial functional unique index at the DB level:
  `CREATE UNIQUE INDEX ... ON table (user_id, lower(name)) WHERE is_deleted = false;`
  (see `__table_args__` on `Trip`/`DiveSite` models - the SQLAlchemy `Index(...,
  func.lower(name), postgresql_where=is_deleted.is_(False))` pattern reproduces
  this DDL exactly; verified by compiling and comparing before applying).
- An application-level check (`trip_name_exists` / `dive_site_name_exists`) before
  insert/update, so violations return a friendly `422 DuplicateValueException`
  instead of a raw DB integrity error.

If adding a new "named, user-owned, soft-deletable" entity, mirror this pattern.

## `trips.py`/`dive_sites.py` caching mirrors `dives.py`

`trips.py`/`dive_sites.py` list/read endpoints use the same `@cache` decorator
(Redis-backed) as `dives.py`, at first by copying its structure exactly and
now via a shared `OwnedResourceCache` factory
(`core/utils/owned_resource_cache.py`) that both routers instantiate once,
since the two had become byte-for-byte identical aside from names/schemas:
- List (`GET /trips`, `GET /dive-sites`): `OwnedResourceCache.read_list`, a
  `@cache(key_prefix="user_{user_id}_...", resource_id_name="user_id",
  expiration=60)`-wrapped `get_multi` + public-shape conversion, called from
  the public route only *after* the `current_user["id"] != user_id` ownership
  check.
- Single item (`GET /trip/{id}`, `GET /dive-site/{id}`):
  `OwnedResourceCache.read_item`, a `@cache(key_prefix="trip_cache"/
  "dive_site_cache", resource_id_name="id")`-wrapped `get` + public-shape
  conversion, called only after the fetched object's owner has been checked
  against `current_user["id"]`.
- Same "auth before cache" rule as `dives.py` (see the `@cache`/authorization
  gotcha further below) - never put the ownership check inside a
  `@cache`-decorated function. `OwnedResourceCache` enforces this by design:
  it only exposes the cached read helpers, not the ownership check, which
  stays in each route.

This was previously skipped on purpose: the dive form's trip/dive site combobox
creates a new record and expects to immediately see/select it in the list, and a
stale cached list would break that. It's safe now because every mutation
invalidates the relevant cache keys, same as `dives.py`:
- `write_trip`/`write_dive_site` call `OwnedResourceCache.invalidate_list(user_id)`
  (which runs `delete_keys_by_pattern(f"user_{user_id}_trips:*")` /
  `f"user_{user_id}_dive_sites:*"`) right after creating the record (there's no
  single-item cache to invalidate yet, since the id is new).
- `patch_trip`/`erase_trip` and `patch_dive_site`/`erase_dive_site` are decorated
  directly with `@cache("trip_cache"/"dive_site_cache", resource_id_name="id")`
  (exactly like `patch_dive`/`erase_dive`), which auto-invalidates the single-item
  cache key on any non-GET call. They *additionally* call
  `OwnedResourceCache.invalidate_list(owner_id)` manually in the handler body to
  invalidate the list cache, since the list key's `user_id` is only known after
  fetching the record (for `patch`/`erase`) and can't be expressed via the
  decorator's kwarg-templated `to_invalidate_extra`. `patch_trip`/`patch_dive_site`
  only invalidate the list when `update_data` is non-empty, since an empty patch
  changes nothing worth invalidating.

`OwnedResourceCache` intentionally covers only this read/cache/invalidate slice,
not the full create/patch/delete routes: those still differ per resource (e.g.
per-user name uniqueness checks, extra fields like `dive_site`'s `location`),
so each resource keeps writing its own route bodies. `dives.py` itself still
hand-rolls its own `_cached_read_dives`/`_cached_read_dive`, since its list/read
logic does more than a straight `get_multi`/`get` (extra `trip_id`/`dive_site_id`
filters, enrichment with related trip/dive-site uuids, mixtures) - it doesn't fit
`OwnedResourceCache`'s shape, so it wasn't forced through it. New simple
per-user owned resources should use `OwnedResourceCache` rather than
hand-copying this pattern again.

## The dive form's pickers search server-side, because they used to fetch whole tables

The dive form's dive site, trip and gear pickers all filtered client-side, so each
paged through *every* row the user owns - `items_per_page=100` in a loop until
`has_more` was false - before its dropdown was usable. A diver with a few hundred
logged sites paid several sequential round-trips on every form open. (The trip
picker didn't even loop: it fetched one page of 100 and dropped the rest silently,
so a 101st trip couldn't be selected at all.)

All three endpoints now take `search=`, a case-insensitive substring match, with
`items_per_page` capped at 100 (`MAX_DIVE_SITES_PER_PAGE`, `MAX_TRIPS_PER_PAGE`,
`MAX_GEAR_ITEMS_PER_PAGE`) so no single request can pull a table anyway:

| Endpoint | Columns matched | Why the second column |
|---|---|---|
| `GET /dive-sites` | `name`, `location` | How people recall sites they haven't dived in a while ("that wall in Dahab") |
| `GET /trips` | `name`, `location` | A trip is as often remembered by where it went as by what it was called |
| `GET /gear-items` | `name`, `brand` | Divers name kit inconsistently ("MK25", "my reg") but recall the brand |

Gear deliberately matches `brand` rather than `type`: `type` is a closed
vocabulary with its own filter surface, and folding it into free-text search
would make "reg" match every regulator regardless of name.

Four things about the implementation are non-obvious:

- **The search query is hand-written, not `get_multi` filter kwargs.** FastCRUD's
  `__`-suffix filters (`name__ilike=...`) are AND'd together, and its `__or`
  operator groups *operators on one column*, not columns. Matching either column
  needs a real cross-column `OR`, so `core/utils/search.py` builds the `select()`
  itself and returns `get_multi`'s `{"data": [...], "total_count": n}` shape. It
  selects `model.__table__.columns` rather than the entity, so rows come back as
  plain dicts exactly like the unsearched path - each caller's public-shape
  conversion sees one shape either way, and gear can still read the internal `id`
  it needs to batch its service-schedule lookup.
- **The term is escaped for `LIKE`.** `escape_like()` backslash-escapes `\`, `%`
  and `_` (in that order - escaping the wildcards first would produce new live
  ones), paired with `.ilike(pattern, escape="\\")`. Without it a site named
  "50%" is unsearchable and a bare `%` matches everything.
- **`search` is part of every list cache key**, appended after the existing
  `user_{id}_{resource}:page_{n}:items_per_page:{n}` prefix - so it still falls
  under the `user_{id}_{resource}:*` wildcard that invalidation purges, and no
  invalidation logic changed. Each route lowercases/strips the term before passing
  it down, so `" Blue "` and `"blue"` share one entry. A resource that passes no
  `search_columns` keeps the original key shape: `OwnedResourceCache.read_list` is
  called without a `search` kwarg for those, and `@cache` would `KeyError` on a
  placeholder it can't fill.
- **Dive sites and trips go through `OwnedResourceCache`; gear doesn't.** Gear's
  list read was already hand-rolled (it batches service schedules per page), so it
  calls the same `search_clause`/`search_multi` helpers directly. That's the split
  the factory's docstring already describes - resources whose reads do more than a
  straight `get_multi` keep their own helpers.

Each search is a plain filtered scan - the existing composite indexes can't serve a
leading-wildcard `ILIKE`. That's fine at the scale these tables have per user
(hundreds, not millions, and always narrowed by `user_id` first). If it ever isn't,
the fix is a `pg_trgm` GIN index on the searched columns, not a different query
shape.

## `/{username}/...` resource routes were flattened to `/...` + explicit ids

`dives.py`/`dive_sites.py`/`trips.py`/`dive_stats.py` used to nest every route
under `/{username}/...` (e.g. `POST /{username}/dive`, `GET /{username}/dives`,
`GET /{username}/dive/{id}`) and resolve `username` to a user id via a `crud_users`
lookup on every request. These were changed to flat routes that take the user id
directly instead of a username:
- `POST /dive`, `/trip`, `/dive-site`: the request body now carries `user_id`
  directly (added to `DiveCreateRequest`/`TripCreate`/`DiveSiteCreate`). The
  handler checks `current_user["id"] == body.user_id` and raises `403` on mismatch
  - it does not trust the body's `user_id` on its own.
- `GET /dives`, `/trips`, `/dive-sites`: take `user_id` as a query
  param instead of a path segment. The handler checks `current_user["id"] ==
  user_id` and raises `403` on mismatch. These endpoints are no longer public -
  they previously had no auth dependency at all (readable by anyone who knew a
  username).
  `/dive-stats` was later moved again, to `GET /user/{uuid}/dive-stats` - see
  "`GET /dive-stats` was moved under `/user/{uuid}/...`" below.
- `GET/PATCH/DELETE /dive/{id}`, `/trip/{id}`, `/dive-site/{id}`: no longer take
  a username at all. The handler fetches the object by `id` alone, then checks
  the fetched object's `user_id` against `current_user["id"]`, raising `404` if
  the object doesn't exist and `403` if it exists but belongs to someone else.

This flattening also surfaced a pre-existing route collision: the superuser-only
hard-delete `erase_db_dive` used to sit at the exact same
`DELETE /{username}/dive/{id}` path/method as the regular owner `erase_dive`, so
FastAPI (which matches routes in registration order) always dispatched to
`erase_dive` and `erase_db_dive` was unreachable dead code. Rather than give it
its own path, `erase_db_dive` (and its `users.py` counterpart `erase_db_user`,
which lived at a working but separate `DELETE /db_user/{username}`) were removed
entirely: there is intentionally **no way to hard-delete a dive, dive site,
trip, or user through the API** - `crud.delete()` (soft delete, flips
`is_deleted`/`deleted_at`) is the only delete path exposed to API clients for
any model with `PersistentDeletion`. A real hard-purge (e.g. for GDPR erasure
requests) should be done directly against the DB or via the `crudadmin` panel
(which is a separate system - see below - and unaffected by this), not exposed
as a REST endpoint.

## `/user/{username}/...` routes were changed to `/user/{id}/...`

`users.py`'s single-user routes (`GET/PATCH/DELETE /user/{username}`,
`GET /user/{username}/rate_limits`, `GET/PATCH /user/{username}/tier`) took a
username path segment, unlike every other resource (`/dive/{id}`, `/trip/{id}`,
`/dive-site/{id}`) which takes a numeric id. These were changed to `/user/{id}`
for consistency. `patch_user`'s ownership check changed from comparing
`current_user["username"]` against the *target* username in the path (which
could itself be changed by the same request body) to comparing
`current_user["id"]` against the path `id` directly - a request can still change
its own `username` via the body, that's unrelated to which user is being edited.
`GET /user/me` (an exact literal path, not a `{username}`/`{id}` placeholder) is
unaffected and still resolves the caller's own record from their token.

## All `/user*`/`/users`/`/dive/parse` endpoints require auth, except signup

`GET /users`, `GET /user/{id}`, and `GET /user/{id}/tier` used to have no auth
dependency at all - readable by anyone, unauthenticated. `POST /dive/parse`
(formerly `/dive/parse-xml`) was the same. These now all require `Depends(get_current_user)` (added via the
route's `dependencies=[...]`, since the handlers don't otherwise need the
current user's identity - they aren't per-owner checks, just "must be logged in").
`GET /user/{id}/rate_limits` and `PATCH /user/{id}/tier` already required
`get_current_superuser` (a stricter form of auth), so were left as-is.

**`POST /user` (signup) is the one deliberate exception** - it cannot require
auth, since a brand-new user has no token yet; that's the whole point of the
endpoint. If self-service signup is ever disabled in favor of admin-only user
creation, this would need `get_current_superuser` instead, but that's a product
decision, not a security fix.

**Gotcha - `@cache` and per-request authorization don't mix directly.** The
`@cache` decorator short-circuits GET requests by returning the cached response
*before* the wrapped function body ever runs (see `core/utils/cache.py`). If the
authorization check lived inside a `@cache`-decorated function, an unauthorized
user could request the exact same cache key (e.g. `GET /dive/{id}` for someone
else's dive, or `GET /dives?user_id=<victim>`) and get served the cached victim's
data straight from Redis without the check ever executing. `dives.py` avoids this
by splitting each cached GET into a private `_cached_read_*` helper (pure data
fetch, no auth) and a public route function that performs the `403` check *first*
and only calls the cached helper once the request is already authorized. Do not
put authorization logic inside a `@cache`-decorated function - always gate access
in the (uncached) caller.

## Single-resource path params were renamed from `{resource}_uuid` to `{uuid}`

`GET/PATCH/DELETE /user/{user_uuid}`, `/trip/{trip_uuid}`, `/dive-site/{dive_site_uuid}`,
`/dive/{dive_uuid}`, and `GET /task/{task_id}` all repeated the resource name from the
path segment inside the path parameter itself (e.g. `/trip/{trip_uuid}` - the `trip_`
prefix is redundant once you're already inside `/trip/...`). These were renamed to a
bare `{uuid}` (`{id}` for `/task`, which isn't a uuid) - `/trip/{uuid}`,
`/dive-site/{uuid}`, `/dive/{uuid}`, `/user/{uuid}`, `/task/{id}` - matching the
intent already noted above under "`/user/{username}/...` routes were changed to
`/user/{id}/...`". This only affects the *path* parameter name (and therefore the
generated OpenAPI docs/client code); request/response body fields such as
`user_uuid`/`trip_uuid` on `Dive`/`Trip`/`DiveSite` payloads, and the `user_uuid`/
`trip_uuid`/`dive_site_uuid` *query* params on the `/dives`, `/trips`, `/dive-sites`
list endpoints, keep their prefixed names since those refer to a *different* resource
than the one in the path segment, so the prefix there is disambiguating, not redundant.
Each affected module's internal `_cached_read_*` helper and its `@cache(...,
resource_id_name=...)` argument were renamed to match (e.g. `resource_id_name="uuid"`),
since FastAPI requires the handler's parameter name to match the path template
placeholder, and it's cleaner for the cached helper's parameter to mirror it exactly.
This is also why `uuid` (the stdlib module) is imported as `uuid_pkg` throughout these
files - so a path parameter can be named `uuid` without shadowing the module.

## `GET /dive-stats` was moved under `/user/{uuid}/...`

`GET /dive-stats` (a flat route taking `user_uuid` as a query param, per the
flattening decision above) was changed to `GET /user/{uuid}/dive-stats`, matching
the path-based shape already used for every other single-user route (`GET/PATCH/
DELETE /user/{uuid}`, `POST /user/{uuid}/email-change/request`). The handler's
ownership check is unchanged in substance - it still compares `current_user["uuid"]`
against the path `uuid` and raises `403` on mismatch - only the parameter's source
(path segment instead of query string) and name (`uuid` instead of `user_uuid`,
per the `{resource}_uuid` -> `{uuid}` renaming decision above) changed. Unlike
`/dives`, `/trips`, `/dive-sites`, this endpoint doesn't take any *other* id that
would need disambiguating from the path segment, so there's no reason left for it
to stay flat with the others.

Once the route itself lived at `/user/{uuid}/dive-stats`, keeping its handler in a
separate single-endpoint `dive_stats.py` module (tagged `"dive-stats"`) no longer made
sense either - by that point it was, structurally, just another single-user route
under `/user/{uuid}/...`, like `email-change/request` or the plain `GET /user/{uuid}`.
`read_dive_stats` was moved into `users.py` and now shares that module's `"users"`
tag; `dive_stats.py` no longer exists, and `api/v1/__init__.py` no longer registers a
separate router for it. This only affects where the *route* lives - `models/`,
`schemas/user_dive_stats.py`, `crud/crud_user_dive_stats.py`, and
`services/dive_stats.py` (the recalculation logic invoked from `dives.py`) are
unrelated internals and keep their existing names/locations.

## Date-only vs datetime fields

`Dive.start_time` is a full `DateTime(timezone=True)` (ISO8601 with time).
`Trip.start_date`/`end_date` are plain `Date` (no time component, `YYYY-MM-DD`).
Don't mix these up when adding new date fields - decide up front whether a field
is a point in time (`datetime`) or a calendar date (`date`), since the frontend
handles each very differently (see the web app's `DECISIONS.md`).

## `start_time`'s UTC offset is stored separately, but the API only ever sees one field

A dive logged at 09:00 in Bangkok (+07:00) should always *display* as 09:00, regardless
of what timezone the viewer happens to be in - but a `timestamptz` column only stores an
absolute instant, not the offset it was originally expressed in. So `Dive` has two
columns: `start_time` (the UTC instant) and `utc_offset_minutes` (e.g. `120` for
`+02:00`), the latter defaulting to `0` purely so existing call sites that construct a
`Dive(...)` without it (tests, the admin panel) don't break.

Critically, **the API itself never exposes `utc_offset_minutes` as its own field**. Every
input/output `start_time` is a single offset-aware ISO 8601 string, e.g.
`"2021-04-04T10:04:47.910+02:00"` - a naive datetime (no offset) is rejected by
`DiveBase`/`DiveUpdate`'s `start_time` validation (`require_utc_offset` in
`core/utils/datetime_offset.py`). The two DB columns are purely an internal storage
detail:
- On write (`write_dive`/`patch_dive` in `api/v1/dives.py`), `split_start_time()` splits
  the incoming offset-aware `start_time` into the UTC instant + offset minutes to store.
- On read (`_to_public_start_time()` in `api/v1/dives.py`), `combine_start_time()` re-attaches
  the stored offset to the stored UTC instant before building the public `DiveRead`/
  `DiveReadWithMixtures` response.

This keeps the public API contract simple (one field, not two that could disagree with
each other) while still letting the DB index/sort/filter on `start_time` as a normal
absolute-instant column. `DiveCreateInternal`/`DiveUpdateInternal`/`DiveReadInternal` (the
schemas that actually mirror the two DB columns 1:1) are the only place
`utc_offset_minutes` appears as an explicit field - never on `DiveCreate`/`DiveUpdate`/
`DiveRead`.

Applying this to an existing local DB (per the "no migration tool" workflow above):
```sql
ALTER TABLE dive ADD COLUMN utc_offset_minutes INTEGER NOT NULL DEFAULT 0;
```
Existing rows have no way to recover what offset they were originally logged in (that
information was simply never captured before), so they backfill to `0` (UTC) - meaning
they'll display in UTC rather than their "real" original timezone until re-saved. This
is an acceptable one-time loss of precision for old data given the alternative (guessing).

The web frontend defaults the offset to the browser's own `Date.getTimezoneOffset()` for
new dives, and to whatever offset (if any) is embedded in a dive-computer export file's
`StartTime`/equivalent field when importing one - see the web app's `DECISIONS.md`.

## `recalculate_dive_stats`'s aggregate is backed by a covering index, not incremental counters

`services/dive_stats.py`'s `recalculate_dive_stats` reruns `COUNT`/`MAX`/`SUM`
over *all* of a user's non-deleted dives on every create/update/delete,
synchronously in the request path. That's O(n) in the user's dive count on
every write.

An incremental (delta-based) version of this was prototyped - maintaining
`total_dives`/`total_time` as O(1) running counters, with `max_depth` falling
back to a `MAX(max_depth)` re-scan only when the dive that changed/was
deleted was at the current max - but it was rejected as more complexity than
this project's actual scale (dive counts in the thousands, rarely tens of
thousands, per user) justifies: it also trades the old approach's immunity to
lost updates (full recompute always overwrites the row with a value freshly
derived from `dive`, so concurrent writers converge safely) for read-modify-write
counters that need explicit row locking to stay correct.

Instead, `ix_dive_user_id_stats` - `(user_id) INCLUDE (max_depth, duration)
WHERE is_deleted = false` - lets the aggregate run as an index-only scan
(no heap fetch per dive) rather than changing its algorithmic shape. It's
still O(n), just with a much smaller constant factor per row, which is a
reasonable trade for keeping `recalculate_dive_stats` simple and correct by
construction. `max_depth`/`duration` are `INCLUDE`d rather than key columns -
they're only read by this aggregate, never filtered or sorted on, so they
don't need to be part of the index's sort key (which would also make it
unusable for a plain `MAX(max_depth)` backward-scan optimization if that's
ever needed standalone).

The aggregate also had to switch from `func.count(Dive.id)` to `func.count()`
(`COUNT(*)`): `id` isn't in the index (key or `INCLUDE`d), so counting it
would force a heap fetch per row and silently defeat the covering index even
though the two forms are equivalent here (`id` is a non-null primary key).
Verified with `EXPLAIN (ANALYZE, BUFFERS)` against the docker-compose `db`
service (temporarily dropping the competing `ix_dive_user_id` inside a
rolled-back transaction to force the planner's hand) - only after that change
does the plan show `Index Only Scan using ix_dive_user_id_stats` with `Heap
Fetches: 0`. With `Dive.id` still in the `SELECT`, Postgres used a regular
`Index Scan` (heap fetches happened) despite `ix_dive_user_id_stats` existing.

On an existing local DB, apply with:
```sql
CREATE INDEX ix_dive_user_id_stats ON dive (user_id) INCLUDE (max_depth, duration) WHERE is_deleted = false;
```

Note that on a small table (a few hundred rows in local dev), the planner
correctly prefers a plain sequential scan over any index - that's expected,
not a sign the index isn't working. It becomes worthwhile as a user's dive
count grows large enough that random heap access (via the old row-only
indexes) costs more than a sequential index-only walk.

Worth revisiting if a true bulk-import endpoint is added (recompute once per
batch instead of once per row) or if per-user dive counts grow enough that
even an index-only scan becomes a bottleneck.

## The Arq worker now does one real thing: purging expired `token_blacklist` rows

The worker (`core/worker/`) previously only registered a `sample_background_task`
demo job, exercised solely by a matching `POST/GET /tasks` API (`api/v1/tasks.py`)
that existed only to enqueue and poll it. `token_blacklist` (see "Domain
`CheckConstraint`s" above for how other tables get DB-level backstops) grows one
row per logout/account-deletion (`blacklist_token(s)` in `core/security.py`) and
nothing ever deleted rows once their `expires_at` had passed - blacklist entries
only need to be kept until the token they reference would have expired
naturally, since an expired JWT is already rejected on its own regardless of the
blacklist check.

`purge_expired_tokens` (`core/worker/functions.py`) now runs as an hourly cron
job (`core/worker/settings.py`, `arq.cron.cron(..., minute=0, run_at_startup=True)`)
and deletes any row with `expires_at < now()`. It checks `count()` before calling
`crud_token_blacklist.delete()` because `fastcrud`'s `delete()` raises
`NoResultFound` when zero rows match its filters - not an error condition here,
since most hourly runs will have nothing to purge.

The now-pointless demo path was removed as part of this: `sample_background_task`,
`api/v1/tasks.py` (and its `tasks_router` registration in `api/v1/__init__.py`),
and `schemas/job.py`. That in turn left the API-side Arq "queue" plumbing
(`core/utils/queue.py`, `create_redis_queue_pool`/`close_redis_queue_pool` and
their `RedisQueueSettings` wiring in `core/setup.py`) with zero callers - nothing
in the API enqueues jobs anymore, since the only job that exists now runs purely
on a cron schedule inside the worker process itself - so that was deleted too.
`RedisQueueSettings`/`REDIS_QUEUE_HOST`/`REDIS_QUEUE_PORT` stay in `core/config.py`;
the worker process still needs them to build its own `RedisSettings` connection in
`core/worker/settings.py`, independent of the API process.

`TokenBlacklist.expires_at` (`core/db/token_blacklist.py`) picked up `index=True`
so the cron job's `WHERE expires_at < ...` (both the `count()` check and the
`delete()`) isn't a sequential scan as the table grows. Per the "no migration
tool" section above, this only takes effect for brand-new tables via
`create_all()`; on an existing local DB, add it by hand:
```sql
CREATE INDEX ix_token_blacklist_expires_at ON token_blacklist (expires_at);
```

## Google sign in/up shares one endpoint, and treats a verified email as proof of ownership

`POST /login/google` (`api/v1/login.py`) is the single endpoint behind both the
"Continue with Google" button on `/signin` and `/signup` on the frontend - Google
Identity Services itself doesn't distinguish sign in from sign up (there's one
button, one `credential` JWT), so the backend mirrors that: find-or-create, then
issue tokens exactly like `/login` does.

The frontend never talks to Google's OAuth endpoints directly for this - it only
loads Google's Identity Services *button* (via `@react-oauth/google`), which
hands back a signed ID token (`credential`, a JWT) once the user picks an
account. That JWT is forwarded verbatim to `/login/google`, which verifies it
server-side with `google-auth`'s `id_token.verify_oauth2_token()` - this checks
the signature against Google's published public keys, expiry, issuer, and (via
the `audience` argument) that the token was issued for *this* app's OAuth client
ID (`GOOGLE_CLIENT_ID`/`NEXT_PUBLIC_GOOGLE_CLIENT_ID` - the same value on both
sides; it's not a secret). The backend never sees or handles a Google client
secret - the ID-token flow doesn't need one.

Account matching, in order:
1. Look up by `User.google_id` (the token's `sub` claim) - the common case for a
   returning Google user.
2. Otherwise look up by email. If found, link `google_id` onto that existing
   (presumably password-based) account rather than erroring or creating a
   duplicate - this is safe specifically because Google only issues an ID token
   with `email_verified: true` for an address it has itself confirmed the user
   controls (`verify_google_id_token` in `core/security.py` rejects anything
   else), so it's equivalent to the user proving ownership of that email again.
3. Otherwise create a new account: `name` from the token's `name` claim (falling
   back to the email's local part), `username` auto-generated from the email's
   local part via `_generate_unique_username` (sanitized to `UserBase.username`'s
   `^[a-z0-9]+$` pattern, with a numeric suffix appended on collision), and no
   password.

This is why `User.hashed_password` (`models/user.py`) is nullable - Google-only
accounts never set one. `authenticate_user` (`core/security.py`) treats a `None`
hashed password as "password sign-in unavailable", rather than passing `None`
to `bcrypt.checkpw()`. A Google-only user who wants a password later would need
a dedicated "set password" flow - not implemented yet, since nothing currently
prompts for it.

Applying this to an existing local DB (per the "no migration tool" section
above) - `hashed_password` is only made nullable in the SQLAlchemy model, which
`create_all()` never alters on an existing table:
```sql
ALTER TABLE "user" ALTER COLUMN hashed_password DROP NOT NULL;
ALTER TABLE "user" ADD COLUMN google_id VARCHAR;
CREATE UNIQUE INDEX ix_user_google_id ON "user" (google_id);
```

**Superseded** by the unified auth flow below - `/login`, `/login/google`, and
`POST /user` (password signup) no longer exist, and `User` no longer has
`hashed_password`/`google_id` columns at all.

## Unified auth flow: no passwords, no separate sign up, one `User` row per identity

The entire password-based login/signup system (`POST /login`, `POST /login/google`,
`POST /user`) was replaced with a single flow entered through either an email magic
link or Google - see `api/v1/auth.py`. The driving requirement: **authentication
("who are you?") must be fully separated from account creation ("tell us about
you")**, so no unverified/incomplete user can ever end up in the `user` table, and a
verified email or Google account is looked up *before* deciding whether to sign in or
start onboarding - never the other way around.

Three new pieces make this work:

- **`AuthenticationRequest`** (`models/authentication_request.py`) - a purely
  temporary, table-backed record for the email magic-link flow. Stores a SHA-256
  hash of the token (never the raw token - see `core.security.hash_token`), an
  `expires_at` (30 min, `settings.MAGIC_LINK_TOKEN_EXPIRE_MINUTES`), and a nullable
  `used_at` that makes it single-use. Deliberately holds no reference to `User` -
  proving you control an email address must never, by itself, create or touch a user
  row.
- **`AuthenticationProvider`** (`models/authentication_provider.py`) - one row per
  provider a `User` has linked (`provider="email"`, `provider="google"` with
  `provider_user_id` set to Google's `sub`, and any future provider needs no schema
  change - just a new `provider` value). This is what lets the same account be
  reached via either method: `services.auth_service.resolve_identity` looks up by
  provider identity first (Google's `sub`, when present), falls back to looking up
  by email, and links the current provider onto that account if it isn't linked yet.
  Two `UniqueConstraint`s enforce the invariants that matter: `(provider,
  provider_user_id)` stops the same Google account from ever being linked to two
  users (Postgres treats each row's `NULL` `provider_user_id` - i.e. every "email"
  row - as distinct from every other `NULL`, so this doesn't block multiple users
  each having their own "email" row), and `(user_id, provider)` stops a user from
  linking the same provider twice.
- **Onboarding tokens** (`core.security.create_onboarding_token`/
  `verify_onboarding_token`) - a short-lived JWT (`TokenType.ONBOARDING`, 30 min via
  `settings.ONBOARDING_TOKEN_EXPIRE_MINUTES`) carrying a verified-but-accountless
  identity (email, provider, provider-specific id, prefill name/avatar) from
  `/auth/email/verify` or `/auth/google` to `/auth/complete`. Never persisted -
  same as access/refresh tokens, it's just a signed, self-contained blob - but it's
  recorded in the existing `token_blacklist` table once used (`blacklist_token`),
  making it single-use exactly like a magic-link token. This is also what backstops
  "no unverified users": a `User` row is created in exactly one place
  (`complete_profile`), and only after this token has been validated.

`services.auth_service.resolve_identity` is the one place both `/auth/email/verify`
and `/auth/google` funnel through to answer "does an account already exist, and if
so is this provider linked to it yet" identically for both - see its docstring for
the three-step lookup order. It returns either `AuthenticatedUser` (sign in
immediately) or `OnboardingRequired` (mint an onboarding token, no DB write).

`complete_profile` (`POST /auth/complete`) creates the `User` row and its first
`AuthenticationProvider` row together, using FastCRUD's `create(..., commit=False)`
followed by one explicit `db.commit()`, with `except IntegrityError: await
db.rollback()` around both - this is what makes account creation transactional and
race-safe: two concurrent completions of the same onboarding token (or two signups
racing for the same username) both pass the pre-emptive `crud_users.exists(...)`
checks, but only one of them can win the DB-level unique constraint on `email`/
`username`; the loser's `IntegrityError` is turned into a `DuplicateValueException`
rather than a 500 or (worse) a duplicate account.

`POST /auth/email/request` invalidating previous pending tokens hit the same
`NoResultFound` pitfall as the token-blacklist purge job (`core/worker/functions.py`):
FastCRUD's `update(..., allow_multiple=True)` raises `NoResultFound` when zero rows
match, and the common case here - a first-time request - has nothing pending to
invalidate. Fixed the same way: `count()` first, only call `update()` if it's non-zero.

Rate limiting (`core.utils.rate_limit.enforce_rate_limit`) is a fixed-window Redis
counter (`INCR` + `EXPIRE`) applied per-email and per-IP on `/auth/email/request`,
and per-IP on `/auth/email/verify`/`/auth/google` - see `MagicLinkSettings` in
`core/config.py` for the limits/window. It's a soft dependency: if `cache.client`
is `None` (Redis unreachable/not configured), it's a no-op rather than a hard
failure, since the actual security boundary is token expiry + single-use +
`RESEND_API_KEY`-gated sending, not the rate limiter.

`POST /auth/email/request` always returns the exact same
`EmailAuthRequestResponse` message regardless of whether the email belongs to an
existing account, and - unlike the old `/login`/`POST /user` - never even queries
`crud_users`. Enumeration protection here isn't a response-shaping trick bolted on
afterwards; the code path genuinely can't distinguish the two cases, because
whether a magic link will eventually sign someone in or send them to onboarding is
only decided later, in `/auth/email/verify`.

CSRF: the only cookie-authenticated endpoint in this flow is `POST /auth/refresh` (the
httpOnly `refresh_token` cookie set by `issue_tokens`) - every other endpoint here is
either unauthenticated (the whole point of `/auth/*`) or authenticated via a Bearer
access token, which browsers never attach automatically, so it isn't CSRF-able at
all. `/auth/refresh`'s cookie is `samesite="lax"`, which browsers refuse to attach on
cross-site `fetch`/XHR (only top-level navigations), so a malicious page can't
silently trigger it with the victim's session - this was already the design before
this rewrite, just re-verified as still sufficient given the new endpoints don't
change that picture.

## `/refresh` and `/logout` moved from their own `login.py`/`logout.py` modules into `/auth`

`POST /refresh` and `POST /logout` used to live in their own single-endpoint modules
(`api/v1/login.py`, `api/v1/logout.py`), both tagged `"login"` in the generated
OpenAPI docs (Swagger/Redoc) - a leftover from the old password-based flow, from
back when a `POST /login` endpoint actually existed there. Once that flow was
replaced (see "Unified auth flow" below), the `"login"` tag no longer corresponded
to any real endpoint, and having every other auth-adjacent operation grouped under
the `"auth"` tag while these two sat off on their own under a stale tag name made the
docs' endpoint grouping actively misleading.

Both were merged directly into `api/v1/auth.py` and now hang off that module's
existing `APIRouter(prefix="/auth", tags=["auth"])`, so they're reachable at
`POST /auth/refresh` and `POST /auth/logout` and show up under the same `"auth"`
tag as `/auth/email/request`, `/auth/google`, etc. `login.py`/`logout.py` no longer
exist; their routes were removed from `api/v1/__init__.py` accordingly. Callers
(the web app's `client.ts`/`auth.ts`) were updated to the new paths - there is no
backwards-compatible redirect from the old `/refresh`/`/logout` paths, since these
are same-origin API calls from apps we control, not a public integration surface.

While touching tags, `dive_sites.py`'s tag was also renamed from `"dive_sites"` to
`"dive-sites"`, matching the dash-separated convention every other module's tag
already followed (`"dive-stats"`, `"dive-site"`-style URL segments) - it was the one
holdout still using an underscore.

## Changing an account's email requires confirming the new address first

`PATCH /user/{uuid}` (`api/v1/users.py`) no longer accepts `email` at all -
`UserUpdate` dropped the field entirely, so submitting it is a 422
(`extra="forbid"`), not a silently-ignored no-op. Changing an account's email is a
two-step confirmation flow instead, reusing the exact same `AuthenticationRequest`
mechanics as the sign-in magic link (single-use, hashed token, short expiry) but with
a different `purpose` (`"email_change"` vs `"sign_in"`) and, crucially, a `user_id` -
the already-existing account requesting the change, which `"sign_in"` rows never have
since that flow works before any `User` row exists.

- `POST /user/email-change/request` (authenticated; operates on the caller's own
  account from the access token, no `{uuid}` path param - there's no other account to
  target) emails a confirmation link to the **new** address, not the current one -
  proving control of the new address is the entire point. Always returns the same
  generic message regardless of whether the new address already belongs to someone
  else (mirrors `/auth/email/request`'s enumeration protection).
- `POST /user/email-change/verify` (no auth required - the token itself, tied to a
  specific `user_id`, is what authorizes the change) applies it: `crud_users.update`
  the row's `email`, mark the `AuthenticationRequest` used, and send a best-effort
  "your email was changed" notice to the **old** address (`send_email_changed_notification`)
  so its owner finds out even if they weren't the one who changed it. Wrapped in a
  try/except `IntegrityError` -> rollback -> `DuplicateValueException`, same
  race-safety pattern as `/auth/complete`, for two verifications racing to claim the
  same address.

`POST /user/email-change/request` originally lived at `POST /user/{uuid}/email-change/request`
and compared `current_user["uuid"]` against the path `uuid`, raising `403` on
mismatch - the same ownership-check pattern as `PATCH`/`DELETE /user/{uuid}`. Unlike
those routes, this one never had a legitimate reason to target anyone other than the
caller (there's no admin/moderation path that changes *someone else's* email), so the
`{uuid}` was dropped entirely and the handler now always operates on `current_user`
from the access token. This removed the only way to call it "wrong" (mismatched path
uuid) along with the `ForbiddenException` branch that guarded against it.

The admin panel needed its own `UserAdminUpdate` schema (`UserUpdate` plus `email`
back) for its `update_schema`, since a trusted superuser should still be able to fix
up an account's email directly without the confirmation dance - `admin/views.py`
uses this instead of the public `UserUpdate`.

`purpose`/`user_id` were added to the *existing* `authentication_request` table, so -
per the "no migration tool" section above - `create_all()` won't add them to an
already-running dev DB (surfaces as `asyncpg.exceptions.UndefinedColumnError: column
authentication_request.user_id does not exist`). Add them by hand:
```sql
ALTER TABLE authentication_request ADD COLUMN purpose VARCHAR(20) NOT NULL DEFAULT 'sign_in';
ALTER TABLE authentication_request ADD COLUMN user_id INTEGER REFERENCES "user"(id) ON DELETE CASCADE;
CREATE INDEX ix_authentication_request_user_id ON authentication_request (user_id);
```

### A confirmed change can still show "invalid or expired" - because something already used the link

A real report: a user clicked the confirmation link, saw "this link is invalid or
expired", but their email *had* actually changed in the DB. Root cause: many mail
clients (Outlook/Microsoft Defender "Safe Links", iOS Mail's rich link previews,
etc.) "detonate" or preview-render links using a real, JS-executing browser *before*
a human ever clicks them - which, when the link target auto-fires the verify call
from a `useEffect` on page load (this app's design from the start, since a page -
unlike a bare API link - already defeats *simple*, non-JS-executing scanners), can
silently consume the token first. The real user's subsequent click then used to get a
hard "already used" error - a technically-accurate but confusing one, since the
change they wanted had, in fact, already gone through.

A same-browser "pairing" cookie (set when the link is requested, checked when it's
opened) was tried and rejected: it's extremely common to *request* a link on one
device/browser (e.g. a laptop) and *open* it from another (e.g. a phone's mail app),
which would just relabel "legitimate cross-device use" as "unpaired" and push it down
the same degraded path as an actual scanner. Auto-verifying unconditionally on load
was also tried, relying solely on the idempotent-reuse handling below to paper over a
scanner having already consumed the token - genuinely zero-click, but it still lets
automation silently trigger the *real* sign-in/email-change before a human ever acts,
which is the actual thing worth protecting against, not just the confusing error
message.

The fix that stuck mirrors what the sign-up flow already gets "for free": completing
a *new* account requires a real person to fill in and submit the profile-completion
form, something automation won't do - so the frontend's `/auth/verify` and
`/settings/confirm-email` pages (see the web app's `DECISIONS.md`) now require an
explicit "Sign in"/"Confirm email change" button click before they ever call
`POST /auth/email/verify`/`POST /user/email-change/verify`. A preview/scan can load
the page, but it can't fake a real click, so no session is issued and no email is
changed without genuine user interaction.

On top of that, as defense-in-depth (e.g. a double click, or a slow network retry
re-submitting the same request): `AuthenticationRequest` distinguishes `used_at`
(informational - when a token was first successfully verified) from `invalidated_at`
(when a *newer* request supersedes it - see `request_email_link`/`request_email_change`,
which invalidate any previous live request for the same email/user). Verifying an
already-used-but-not-invalidated token is deliberately **not** an error - it's a
harmless repeat, since re-running `resolve_identity`/re-applying the same email
change produces the exact same outcome every time. Only an *invalidated* or
*expired* token is rejected. This is safe specifically because neither flow grants
an escalated or different outcome on replay within the token's own (short) validity
window - it's the same account either way - so there's no meaningful security
downgrade from allowing the repeat, just the removal of a confusing failure mode.

`invalidated_at` was added to the *existing* `authentication_request` table - same
"no migration tool" caveat as `purpose`/`user_id` above applies on an already-running
dev DB:
```sql
ALTER TABLE authentication_request ADD COLUMN invalidated_at TIMESTAMPTZ;
```

### The confirm-email button shouldn't even be shown for a link that's already been used

Follow-on report: pressing the browser's **back** button after already confirming
an email change lands back on `/settings/confirm-email` with the "Confirm email
change" button still showing - and pressing it *again* succeeds, silently, because
of the idempotent-reuse leniency described above. That leniency exists to tolerate
*races* (a double click, a scanner detonation followed by a genuine click a moment
later) - it was never meant to make a stale, already-actioned link look repeatedly
actionable to a human who revisits it long after the fact.

The fix: `GET /auth/email/verify/check` and `GET /user/email-change/verify/check`
(`check_email_link`/`check_email_change_link`) are new, side-effect-free precheck
endpoints - they look up the token and report whether it's still live (not found,
invalidated, *already used*, or expired all count as "not live"), and, if it is,
which email it's for. Unlike the POST verify endpoints, **`used_at` alone is enough
to make the precheck say "invalid"** - there's no races to tolerate here, since
nothing has been submitted yet. The frontend calls this on page load, *before*
showing the confirm button at all, so a revisited/already-used link shows an error
immediately rather than a clickable button (see the web app's `DECISIONS.md`).

This also gives the frontend a place to show the *target* email up front (in both
the check response and, for email changes, echoed back from the POST verify
response), so a user confirming a change can see which address they're about to
switch to - and, on success, which one they switched to.

Separately, `verify_email_change`'s idempotent (`used_at is not None`) branch was
tightened: it used to unconditionally report success on replay. It now fetches the
account's *current* email first and only treats the replay as a harmless repeat if
`current_email == new_email` (i.e. the change this token represents was in fact the
last thing applied) - otherwise it's a genuine, rejected reuse (e.g. the account's
email was changed *again* since, by a different, later request), and raises same as
an invalidated token would. `verify_email_link` (sign-in) didn't need the equivalent
change - `resolve_identity` is a pure function of the verified email, so replaying it
can't "drift" the way a mutable `email` column can.

The admin panel (`admin/views.py`) lost its `password_transformer`/`PasswordTransformer`
for the `User` view - there's no password field to transform. Admin-created users
authenticate afterwards the same way as anyone else, via their `email`. Similarly,
`scripts/create_first_superuser.py` no longer sets a `hashed_password`; it now also
inserts a matching `authentication_provider` row (`provider="email"`) so the
admin account it creates can actually sign in.

Applying this to an existing local DB (per the "no migration tool" section above) -
`create_all()` creates the two new tables automatically, but won't touch the
existing `user` table:
```sql
ALTER TABLE "user" DROP COLUMN hashed_password;
ALTER TABLE "user" DROP COLUMN google_id;
```

## Current-user routes moved off `/user/me` and `/user/{uuid}` onto a bare `/user`

The long-term goal is for "my account" and "someone else's public profile" to be
two distinct, differently-shaped endpoints - the former returns full data (incl.
`email`) from the access token, the latter (not built yet) will return a limited,
public subset with no `email`, keyed by `{uuid}`. Reusing `/user/{uuid}` for both
(gated by an `if current_user["uuid"] != uuid` check, as it worked before) doesn't
scale to that: it's a runtime check that's easy to forget on a new route, rather
than a distinction baked into the URL shape itself.

As a first step (public profile routes are a separate, later piece of work),
`users.py`'s current-user-only routes were moved off `{uuid}`/`me` entirely onto a
bare `/user`, always resolving the account from the access token instead of a path
parameter:
- `GET /user/me` -> `GET /user` (`read_users_me` renamed to `read_current_user`).
- `PATCH /user/{uuid}` -> `PATCH /user`. The ownership check (`current_user["uuid"]
  != uuid` -> `403`) is gone entirely, not just relaxed - there's no `uuid` param
  left to mismatch against.
- `DELETE /user/{uuid}` -> `DELETE /user`, same reasoning. This also removed the
  route's own `crud_users.get`/`NotFoundException` check - `current_user` already
  came from a fresh, non-deleted lookup inside `get_current_user`, so re-fetching
  the same row by `uuid` just to check it still exists was redundant.
- `GET /user/{uuid}/dive-stats` -> `GET /user/dive-stats`, for the same reason
  `email-change/request` dropped its `{uuid}` (see above): it already 403'd on any
  `uuid` other than the caller's, so the path param was never anything but
  decoration. If public dive stats are ever wanted as part of a future public
  profile, that's a new route (e.g. `GET /profile/{uuid}/dive-stats` or similar),
  not a relaxation of this one.

`GET /user/{uuid}` (the plain single-user lookup) and `GET /users` (the paginated
list, which returned full `UserRead` including `email` for every user) were both
removed outright rather than repurposed - there is intentionally **no way to fetch
any user's data other than your own through this API right now**. Public-profile-
shaped replacements (limited fields, no `email` - both a single lookup and a
listing) are planned but deliberately out of scope for this change. `read_users`
no longer exists in `users.py`; the `PaginatedListResponse`/`compute_offset`/
`paginated_response` imports it was the only user of were removed along with it.

`opendiving-web` (`authAPI.getCurrentUser`/`updateProfile` in `lib/api/auth.ts`)
and `opendiving-ios` (`AuthAPI.currentUser()`) were updated to match - see their
respective `DECISIONS.md` entries.

## `CORSMiddleware` was missing entirely - every cross-origin request 405'd on preflight

`core/setup.py` never configured `CORSMiddleware`, despite `opendiving-web` always
having run on a different origin than the API (`localhost:3000` vs `localhost:8000`
in local dev, per `FRONTEND_URL`/`NEXT_PUBLIC_API_URL`). `apiClient` (`lib/api/
client.ts`) sends `withCredentials: true` plus an `Authorization` header on most
requests and JSON bodies on writes - all of which make the browser preflight with
`OPTIONS` before the real request. With no CORS middleware, FastAPI has no
`OPTIONS` handler for any route, so every preflight - and therefore every real
cross-origin request from a browser - got a bare `405 Method Not Allowed`, with
no CORS-related response headers at all.

Fixed by adding `CORSMiddleware` in `create_application`, gated on
`isinstance(settings, FrontendSettings)` (same pattern as the existing
`ClientSideCacheSettings`/`EnvironmentSettings` checks): `allow_origins=
[settings.FRONTEND_URL]` (the same setting already used to build the magic-link
URL - there's exactly one trusted frontend origin, so no new config was added),
`allow_credentials=True` (required for the refresh-token cookie), and
`allow_methods`/`allow_headers` left as `["*"]` - both are fine to wildcard even
with `allow_credentials=True` per the Fetch/CORS spec; only `allow_origins=["*"]`
is disallowed together with credentials, and this doesn't do that.
`lifespan_factory`'s and `create_application`'s `settings` parameter type unions
both gained `FrontendSettings` to match (mypy caught the missing one from the
other, since both now need to accept the same combined `Settings` instance).

`tests/test_cors.py` builds its own app via `create_application(...,
create_tables_on_start=False)` rather than using `conftest.py`'s `client` fixture
(session-scoped `TestClient(app)` importing `src.app.main`), since that fixture's
app runs the real startup lifespan (`create_tables()` against `POSTGRES_URI`,
which resolves to the `db` docker-compose hostname) and isn't otherwise used by
any existing test - not worth requiring a live Postgres connection just to check
middleware headers on an `OPTIONS` request.

## `/dive/parse-xml` renamed to `/dive/parse`, added a Suunto JSON parser

The upload-and-parse endpoint (`dives.py`) was renamed from `POST
/dive/parse-xml` to `POST /dive/parse` since it's no longer XML-only: a
`SuuntoJsonParser` (`services/dive_parsers/suunto_json.py`) was added to parse
Suunto app / Suunto Ocean JSON exports (`DeviceLog.Header`), alongside the
existing `SuuntoXmlParser` for DM5-style XML exports. `parse_dive_file()`
already dispatched by trying each registered parser's `can_parse()` in turn, so
adding the new format only meant registering the class in `dive_parsers/
__init__.py`'s `_PARSERS` list - no dispatch logic changed. The handler
function itself was renamed `parse_dive_xml` -> `parse_dive` to match.

The JSON header format reports temperature in Kelvin (SI units) rather than
Celsius like the XML export, so `SuuntoJsonParser` converts it
(`value - 273.15`). It only maps `DeviceLog.Header`'s summary fields for now -
unlike `SuuntoXmlParser`, it does not parse per-sample depth/temperature
profiles or gas mixtures (`mixtures`/`samples` come back as `[]`), since the
JSON export this was built against only had header data. If/when a full
Suunto JSON export with a samples array is available, extend `SuuntoJsonParser`
rather than adding a second JSON parser class, so `.json` files still only need
one `can_parse()` check.

`SuuntoJsonParser.can_parse()` deviates from `SuuntoXmlParser`'s pattern (a
pure filename check) - `.json` alone isn't distinctive enough, so `can_parse`
also parses the content and checks for `DeviceLog.Header`, returning `False`
(not raising) for invalid JSON or a mismatched shape. `parse()` no longer
raises `UnsupportedDiveFileError` for a missing `DeviceLog`/`Header` - that
detection now lives entirely in `can_parse`, so `parse_dive_file()`'s dispatch
loop never calls `parse()` on a file this parser doesn't recognize in the
first place. `parse()` still guards against being called directly (as the unit
tests do) with unrecognized data, but now reports that as `DiveParseError`
(a generic "malformed" catch-all) rather than `UnsupportedDiveFileError`, since
format recognition is no longer its job. `DiveParser.can_parse`'s docstring
(`dive_parsers/base.py`) was loosened accordingly: a pure filename check is
still preferred, but parsers may inspect `content` there if the extension
alone is ambiguous, as long as they turn any failure into `False` rather than
an exception.

All call sites needed updating for the rename: the frontend
(`lib/api/dives.ts`'s `parseDiveFile`, `components/dives/dive-file-import.tsx`'s
`accept` attribute, now `.xml,.json`), `README.md`, `DIVE_FUNCTIONALITY.md`, and
the now-stale-named `tests/test_dive_upload.py` (endpoint paths only; the file
name and its focus on the upload size guard are still accurate for both
formats).

`services/dive_parsers/suunto.py` was renamed to `suunto_xml.py` to mirror
`suunto_json.py` now that there are two Suunto parsers - `suunto.py` on its own
no longer indicated which format it handled. While making that change,
`SuuntoXmlParser.parse()` picked up the same defensive wrapping
`SuuntoJsonParser.parse()` already had: extracting fields (`_int`/`_float`
converting element text) is now wrapped in a `try/except (TypeError,
ValueError)` that re-raises as `DiveParseError`, since a well-formed `<Dive>`
element with non-numeric content in a numeric field (e.g. `<MaxDepth>not-a-
number</MaxDepth>`) previously raised an unhandled `ValueError` straight out of
`parse()` - a real bug, not just an inconsistency, since it meant a malformed
upload could 500 instead of getting the normal `422`.

`SuuntoXmlParser.can_parse()` also gained the same kind of structure check
`SuuntoJsonParser.can_parse()` has: it now defensively parses the XML and
checks the root tag is a namespaced `<Dive>`, returning `False` (never
raising) for anything else, on top of the existing `.xml` extension check.
Unlike the JSON parser, this didn't replace the equivalent check in `parse()`
(`root.tag != _tag("Dive")` still raises `UnsupportedDiveFileError` there) -
kept as defense-in-depth for direct `parse()` calls, since XML element lookups
fail silently (returning `None`) rather than raising for a mismatched
namespace/root, unlike the JSON parser's `dict` indexing which naturally
raises on a structural mismatch. Without that fallback, calling `parse()`
directly on non-Suunto XML would silently return an all-`None` `ParsedDiveSchema`
instead of failing.

## `SuuntoJsonParser` gas mixtures come from `Header.Diving.Gases`, in SI units

Suunto's JSON export has (at least) two header shapes: a "clean"/header-only
one with everything directly under `Header` (no gas data at all), and a
D5-style one that nests most dive stats - including gas mixtures - under
`Header.Diving`. `SuuntoJsonParser` now reads `Header.Diving.Gases` (defaulting
to `[]` if `Diving` or `Gases` is absent, so the header-only shape still works
unchanged) and maps each entry to a `DiveMixtureSchema`.

Unlike the rest of the header, `Diving.Gases` reports values in raw SI units
rather than the more human-scaled units used elsewhere (or in the XML export):
pressure in Pascal (not bar), tank size in cubic meters (not liters), and
oxygen/helium as a 0-1 fraction (not a 0-100 percentage). Converting these -
like the existing Kelvin-to-Celsius conversion - goes through `Decimal`
arithmetic (`_pascals_to_bar`/`_cubic_meters_to_liters`/`_fraction_to_percent`,
built on shared `_decimal_multiply`/`_decimal_divide` helpers) rather than raw
float math, for the same reason: dividing/multiplying by these round SI factors
in plain floats can introduce binary representation noise (e.g. `0.21 * 100`
not landing exactly on `21.0`), which Decimal avoids since it operates on the
exact decimal digits of the JSON literal instead of its binary float
approximation.

A few mixture fields have no equivalent in `Diving.Gases`: there's no free-text
name the way the XML export's `<Name>Air</Name>` has, so `State` (e.g.
"Primary", "Deco") is used as the closest available label; there's no
equivalent of the XML `<Type>` tag distinguishing mixture types, so it's
hardcoded to `0`; and there's no per-gas-change-event data, so `gas_changes`
is always `[]`. `StartPressure`/`EndPressure`/`TransmitterID` are genuinely
optional per gas (e.g. an untransmitted backup cylinder may only have
`Oxygen`/`Helium`/`TankSize`), and come back as `None` rather than `0`/a crash
when absent.

`duration` differs between the two header shapes too, and now falls back the
same way `avg_depth` already did (`header.get("DepthAverage", depth.get("Avg"))`)
- prefer the "clean"-style key, fall back to the D5-style one if absent:
`header.get("DiveTime", header.get("Duration"))`, since D5-style exports have
no `DiveTime`, only a top-level `Duration`. (An equivalent fallback for
`ascent_time`, via `Diving.AlgorithmAscentTime`, was added and then removed
again - see "`ParsedDiveSchema`/`DiveMixtureSchema` trimmed to fields the
backend models actually support" below.)

## `ParsedDiveSchema`/`DiveMixtureSchema` trimmed to fields the backend models actually support

`ParsedDiveSchema` and `DiveMixtureSchema` (`schemas/parsed_dive.py`) originally
mirrored the *source* export formats' full field sets (Suunto DM5 XML's
algorithm/tissue-loading/CNS/OTU/CNS stats, PO2 set points, per-mixture gas-change
events, per-sample depth/temperature profiles, etc.) rather than what the `Dive`/
`DiveMixture` backend models (`models/dive.py`, `models/dive_mixture.py`) can
actually persist. Since nothing downstream - not the DB models, not the frontend
form (`applyParsedDiveToForm` in `dive-file-import.tsx` only ever read `dive_
number`/`start_time`/`duration`/`max_depth`/`avg_depth`/`bottom_temperature`) -
could do anything with the extra fields, they were dropped rather than carried
as dead weight:

- `ParsedDiveSchema` kept only `avg_depth`, `bottom_temperature`, `dive_number`,
  `duration`, `max_depth`, `start_time`, `mixtures` (all of which have a direct
  `Dive` column equivalent). Everything else (`algorithm`, `altitude_mode`,
  `ascent_mode`, `ascent_time`, `battery_level`, `bottom_time`, `cns_end`,
  `cns_start`, `cylinder_volume`, `cylinder_work_pressure`, `desaturation_time`,
  `diving_days_in_row`, `end_pressure` (top-level), `end_temperature`,
  `last_deco_stop_depth`, `mode`, `olf_end`, `otu_end`, `otu_start`,
  `personal_mode`, `previous_max_depth`, `sample_interval`, `serial_number`,
  `software`, `source`, `start_temperature`, `surface_pressure`, `surface_time`)
  was removed.
- `DiveMixtureSchema` kept only `end_pressure`, `helium`, `name`, `oxygen`,
  `start_pressure`, and `size` renamed to `volume` (matching `DiveMixtureBase.
  volume` exactly, rather than using a different name for the same concept).
  `po2` (already noted above as having no `DiveMixture` column - it was replaced
  by `helium`), `transmitter_id`, `type`, and `gas_changes` were removed.
- `DiveGasChangeSchema` and `DiveSampleSchema` were deleted outright, along with
  `ParsedDiveSchema.samples` - there's no backend model for either gas-change
  events or per-sample depth/temperature profiles at all, so a parser populating
  them was always a dead end.

Both parsers (`suunto_xml.py`, `suunto_json.py`) were trimmed to match: they
simply stop extracting the removed fields, rather than extracting them and
having the schema discard them. `SuuntoXmlParser` also dropped its now-unused
`_parse_sample` method and `DiveGasChanges`/`DiveSamples` XML traversal
entirely. The frontend's `ParsedDive` interface (`lib/api/dives.ts`) had
`source`/`serial_number`/`software` removed to match (nothing read them; the
catch-all `[key: string]: unknown` index signature was kept for forward
compatibility, but the concrete fields shouldn't claim to exist if the backend
no longer sends them).

`DiveMixtureSchema.name` was kept in the schema (it does have a `DiveMixture.
name` column), but both parsers now always set it to `None` rather than
guessing at it - `SuuntoXmlParser` no longer reads the XML export's `<Name>`
tag (e.g. `<Name>Air</Name>`), and `SuuntoJsonParser` no longer uses `Gases[].
State` (e.g. "Primary") as a stand-in name. `State` in particular was a poor
proxy - it describes a gas's *role* (primary/deco/bailout), not an actual gas
label a diver would recognize - and even the XML export's real `<Name>` tag is
left for the diver to fill in/edit themselves on the create/edit form instead
of being pre-filled from a guess.

`DiveMixtureSchema.start_pressure`/`end_pressure`/`oxygen`/`helium` are now
rounded to 2 decimal places in both parsers (a `_round2_or_none` helper
duplicated in each file - not a shared module, since each already has its own
small set of parser-local rounding helpers). This matters most for
`SuuntoJsonParser`: converting the D5-style JSON export's Pascal pressures to
bar via `_pascals_to_bar` (division by 100000) or its 0-1 oxygen/helium
fractions to a percentage can produce more decimal digits than a dive-computer
gauge is meaningfully precise to (e.g. `20714062 Pa -> 207.14062 bar`).
`SuuntoXmlParser` gained the same rounding for consistency, even though its
DM5 XML source data is already low-precision in practice - avg_depth/max_depth/
bottom_temperature were deliberately left unrounded, matching the source data's
own precision (2 decimals in every fixture seen so far), for the same reason
the JSON parser's depth/temperature fields aren't explicitly rounded either.

`_round2_or_none` quantizes via `Decimal` (`Decimal(str(value)).quantize(
Decimal("0.01"))`) rather than plain `round(value, 2)`, matching the
Decimal-based approach `_kelvin_to_celsius`/`_pascals_to_bar`/etc. already use
to avoid reintroducing binary floating-point noise at the last step - e.g.
`round(2.675, 2)` on a raw float can land on `2.67` instead of `2.68` due to
`2.675`'s own imprecise binary representation, whereas `Decimal("2.675")` is
exact and rounds predictably.

## Gear is `GearItem` + `GearSet`, not a single `Gear` table

"Gear" is what divers actually call their equipment ("dive gear", "gear list"),
so it wins over "Equipment" as the domain name - but *gear* is a mass noun, so
"a gear" reads wrong for a single regulator or wing. The tables are therefore
`gear_item` (one physical piece of kit: brand, name, notes, `rented`) and
`gear_set` (a named grouping), with the API exposing `/gear-item(s)` and
`/gear-set(s)` and the frontend labelling them simply "Gear" and "Gear Sets".

Both are joined to their dependents the same way dive sites are (see "Dive sites
are many-to-many with dives via a join table"): `dive_gear_item` links a dive to
the items used on it, `gear_set_item` links a set to its members, each with a
`position` column preserving list order and `ON DELETE CASCADE` on both FKs.
`crud_dive_gear_items.py`/`crud_gear_set_items.py` mirror
`crud_dive_dive_sites.py` exactly: `replace_*_for_*()` deletes and re-inserts the
whole list on every write, de-duplicating ids (first occurrence wins) rather
than diffing or upserting.

All four tables are brand new, so `Base.metadata.create_all()` creates them (and
their indexes) on startup - unlike a column added to an existing table, this
needed no manual `ALTER TABLE` (see "Schema changes have no migration tool").

## A dive references gear items, never the gear set they came from

Gear sets exist purely to save typing in the dive form: selecting one replaces
the form's gear list with the set's items, after which the diver can add or
remove items for that dive without touching the stored set. `Dive` therefore has
no `gear_set_id` and `DiveRead` no `gear_set` field - only `gear_items`.

That's deliberate rather than an omission. If a dive pointed at a set, renaming,
re-scoping or deleting the set would silently rewrite (or orphan) history for
every dive that ever used it, and "the gear I actually dived with" would stop
being answerable from the dive alone. Keeping the set out of the dive means sets
stay freely editable and disposable, and `DELETE /gear-set/{uuid}` can be a
genuinely cheap operation that touches neither gear nor dives.

## Archiving gear is separate from soft-deleting it

`gear_item` carries both `is_deleted`/`deleted_at` (the usual `SoftDeleteMixin`)
and its own `is_archived`/`archived_at`. They mean different things:

- **Archived** - retired, sold, returned to the rental shop. The item is hidden
  from `GET /gear-items` (unless `include_archived=true`) so the dive form's
  picker won't offer it for a *new* dive, but it stays on every dive and in every
  set that already references it, and keeps its `dive_count`.
- **Deleted** - gone from the user's gear list entirely.

`resolve_gear_item_ids_for_user()` deliberately resolves archived items
normally: archiving must not make an existing dive or set unsavable just because
it references gear the diver has since retired. Only the *listing* filters them.

`archived_at` is derived server-side in `patch_gear_item` from the `is_archived`
flag in the request body (and cleared on unarchive) rather than being accepted
from the caller, so the two can't drift apart.

## `gear_item.dive_count` is denormalized, and recalculated exactly like `user_dive_stats`

Every gear list row shows how many dives that item was used on, so computing it
per request would mean a join + `GROUP BY` on every cache miss. It's stored on
`gear_item` instead and refreshed by `services.gear_stats.recalculate_gear_dive_counts()`
after every dive create, update and delete - the same "recompute from scratch,
never increment" approach as `recalculate_dive_stats` (see "recalculate_dive_stats's
aggregate is backed by a covering index"), which keeps it immune to drift.

It's a single `UPDATE ... SET dive_count = (correlated COUNT subquery)` over the
user's items, guarded by `WHERE dive_count IS DISTINCT FROM (...)` so the common
case - editing a dive without touching its gear - doesn't rewrite every row the
user owns for nothing. Only non-deleted dives count, so soft-deleting a dive
decrements its gear's counts on the next recalculation.

Because it's derived, the admin panel's `GearItem` view can technically edit
`dive_count`, but the value only survives until the owner's next dive mutation.

## Every gear cache key is user-scoped under one `user_{id}_gear_` prefix

Unlike `dive_site_cache:{uuid}`/`trip_cache:{uuid}`, the single-item gear caches
are keyed `user_{user_id}_gear_item:{uuid}` / `user_{user_id}_gear_set:{uuid}`,
alongside the list caches `user_{user_id}_gear_items:page_...` and
`user_{user_id}_gear_sets:page_...`. That makes
`invalidate_gear_caches()` (`api/v1/gear_items.py`) a single
`delete_keys_by_pattern("user_{id}_gear_*")` covering all four.

The user scoping isn't cosmetic - it's what makes that invalidation possible at
all. Two things force it:
- a gear set read embeds its items' names/brands, so editing an *item* has to
  invalidate *set* reads too;
- a gear item read carries `dive_count`, so creating, editing or deleting a
  **dive** has to invalidate gear reads (hence the `invalidate_gear_caches()`
  calls in `dives.py`). A dive mutation knows the owner's `user_id` but not
  which gear uuids changed, so without the user prefix the only matching pattern
  would be `gear_item_cache:*` - every user's gear, not just this one's.

The gear-item list cache key also includes `archived_{include_archived}`, so the
picker's (active-only) view and the management page's (full) view can't serve
each other's cached results. This is the same reason `_cached_read_dives` keys on
its `trip_id`/`dive_site_id`/`gear_item_id` filters.

This same reasoning was later applied to the dive caches to fix stale embedded
names - see "Renaming a dive site or gear item invalidates that user's dive
caches" below.

## `GET /dives` gained a `gear_item_uuid` filter alongside `dive_site_uuid`

`crud_dives`' `custom_filters` grew a `with_gear_item` entry mirroring
`at_dive_site` - one `id IN (SELECT dive_id FROM dive_gear_item WHERE
gear_item_id = ...)` condition rather than a separate round trip to resolve
matching dive ids. It backs the gear detail page's "Dives with this Gear" list,
which is what makes the `dive_count` statistic clickable rather than just a
number.

## `GearItem.type` is a closed vocabulary, but has no DB `CHECK` constraint

Gear carries a broad category - fins, wetsuit, regulator, ... - as `GearType`
(`schemas/gear_item.py`), a `StrEnum` rather than free text. Free text would let
the same kind of kit be spelled three different ways in one diver's list
("Fins"/"fins"/"Fin"), which defeats the point: the category exists so the UI can
group, filter and scan by it. `OTHER` is the escape hatch.

Members are declared in the order kit is normally listed rather than
alphabetically, so callers that want that order (the frontend's type picker) can
take it straight from the enum instead of maintaining a second sorted list.

Unlike `dive`/`dive_mixture`'s numeric ranges, this is deliberately **not**
mirrored by a `CheckConstraint`. Those constraints exist because their only other
validation lives in the frontend's Zod schemas, so a direct API call could
otherwise write nonsense. A gear type has no such gap: `GearType` is a Pydantic
field, so every write through the API (and through the admin panel, which uses
the same schemas) is already rejected server-side. A DB-level copy of the list
would buy nothing and would need a `DROP`/`ADD CONSTRAINT` every time a category
is added. The column is a plain `VARCHAR(32)`.

`type` is nullable and optional throughout. Gear logged before the column existed
has none, and requiring a diver to categorize a one-off piece of kit before they
can save it would be friction for no gain - so the UI shows "No type" rather
than forcing a choice.

Applying to an existing local DB (per "Schema changes have no migration tool"):
```sql
ALTER TABLE gear_item ADD COLUMN type VARCHAR(32);
```


## Renaming a dive site or gear item invalidates that user's dive caches

A dive's cached representation embeds *summaries of other resources*: its dive
sites' names/locations (`DiveSiteInfo`) and its gear items' names/brands/types
(`GearItemInfo`). So renaming a dive site or a gear item makes every cached dive
that references it stale, even though no dive row changed. Both the paginated
list (`_cached_read_dives`) and the single-dive read carry those summaries, so
both go stale.

This was originally shipped as a known limitation, because the single-dive cache
key was a flat `dive_cache:{uuid}`. The renaming endpoint knows only the owner's
`user_id`, not which of their dives reference the renamed thing, so there was no
pattern that could target them - the only match would have been
`dive_cache:*`, i.e. every user's dives.

The fix was to scope that key by user, exactly like the gear caches:
`dive_cache:{uuid}` -> `user_{user_id}_dive:{uuid}`. `services/cache_invalidation.py`
can then express the invalidation as a pattern, and `dive_sites.py`/`gear_items.py`
call `invalidate_dive_caches(owner_id)` after a patch or delete.

Two things to know if you touch this:

- **The helpers live in `services/cache_invalidation.py`, not in the route
  modules.** `dives.py` already invalidates gear caches (a dive changes each
  item's `dive_count`) and gear now invalidates dive caches, so keeping them in
  the routers would be a circular import.
- **`invalidate_dive_caches` uses two patterns, not one.** The obvious
  `user_{id}_dive*` would also sweep `user_{id}_dive_sites:page_...` - the dive
  *site* list cache, a different resource. Harmless, but it would quietly cost
  every dive edit an extra dive-site list rebuild. So it deletes
  `user_{id}_dives:*` and `user_{id}_dive:*` separately. `user_{id}_gear_*` has
  no such neighbour and stays a single pattern.

`patch_dive`/`erase_dive` lost their `@cache("dive_cache", ...)` decorators in the
process. That decorator existed purely to delete the flat item key on a non-GET;
the key is now user-scoped and built from a `user_id` the decorator can't reach
from route kwargs, so both routes invalidate explicitly instead - matching what
`gear_items.py` already did.

Renaming a *trip* needs none of this: `DiveRead` carries `trip_uuid` only, never
the trip's name.

Entries cached under the old `dive_cache:*` prefix are simply never read again
and expire on their own; a local dev Redis can be flushed to be rid of them.

## Weight is a `dive` column, not a gear item

`dive.weight` is a nullable `Float` holding the total ballast carried on the
dive, in kilograms - the same shape as `max_depth`/`visibility` next to it.

The tempting alternative was a `GearType.WEIGHT` gear item, or a `quantity`
column on `dive_gear_item`. Both were rejected:

- A `gear_item` is an *identity* - `ux_gear_item_user_id_brand_name_lower`
  enforces one row per (brand, name), and `dive_count` is denormalized per item.
  "4 kg" isn't a thing the diver owns, so a diver's list would fill up with
  "2kg"/"4kg"/"4.5kg" rows whose dive counts mean nothing.
- The entire point of logging weight is comparing it numerically across dives
  ("6 kg with the 5mm in salt, 4 kg in fresh"), which a name string can't answer.
- A weight *belt* or an integrated weight *pocket* genuinely is a gear item - it
  is owned, it can be rented, it wears out. The amount of lead in it is a
  different fact, and keeping the two apart is the point.
- `dive_gear_item.quantity` would generalize to nothing else, and would force
  the diver to own a "weights" item just to record a number.

`ck_dive_weight_non_negative` is `>= 0`, not `> 0` like the depth constraints:
diving with no lead at all is a real, deliberate entry (a drysuit with a heavy
undergarment, a freedive), and it's worth distinguishing from `NULL` ("didn't
record it"). `Float` rather than `Integer` because half-kilo increments are
normal and pound-based weights don't convert to whole kilos.

Being a new column on an existing table, this needed a manual `ALTER TABLE` on
any existing database (see "Schema changes have no migration tool"):

```sql
ALTER TABLE dive ADD COLUMN weight double precision;
ALTER TABLE dive ADD CONSTRAINT ck_dive_weight_non_negative
    CHECK (weight IS NULL OR weight >= 0);
```

## `gear_set.weight` is a default, `dive.weight` is the record

`gear_set` carries its own nullable `weight` (kg): the ballast the diver normally
uses with that configuration. Loading a set into the dive form fills in the
dive's weight the same way it fills in the item list - and just like the item
list, it's a starting point. The dive stores its own copy and never reads back
from the set, so renaming, re-weighting or deleting a set can't rewrite what a
past dive says the diver actually carried (see "A dive references gear items,
never the gear set they came from" - same reasoning, same guarantee).

`NULL` on a set means "this set makes no claim about weight", and loading it
leaves whatever's on the dive alone. That's why it's nullable rather than
defaulting to 0: a set of fins and a mask shouldn't silently zero out the dive's
weight.

Unlike `dive.weight`, the bound lives in Pydantic (`ge=0` on `GearSetBase`), not
in a DB `CheckConstraint`. This is the `GearItem.type` rule, not an oversight:
the API schema already rejects a negative value on every write, so a DB-level
copy would buy nothing. `dive.weight` goes the other way because `DiveBase`
declares no numeric bounds at all - all of them are enforced by the DB and
mirrored in the frontend's Zod schemas, and one field breaking that pattern would
mean one field returning a different 422 shape from its neighbours.

Also a new column on an existing table, so:

```sql
ALTER TABLE gear_set ADD COLUMN weight double precision;
```

## Gear service is a schedule (the rule) plus records (the history), never one table

A cylinder needs an annual visual inspection **and** a five-year hydrostatic test at
the same time. That single fact rules out every "one interval per item" design -
columns on `gear_item`, or a single `service_interval_months` - so servicing is two
tables:

- `gear_service_schedule` - the rule. One row per (item, kind, label), enforced by
  `ux_gear_service_schedule_item_kind_label` (COALESCE-ing a NULL label to `''`
  exactly like `ux_gear_item_user_id_brand_name_lower` does for a NULL brand, so two
  label-less "service" rules collide but two differently-labelled "other" rules don't).
- `gear_service_record` - the history. What was actually done, when, by whom.

Keeping them apart is what makes both halves work. A diver can set a reminder on
brand-new kit that has never been serviced (the baseline is the schedule's
`starts_on`), and can change an interval without rewriting history. Conversely a
record can exist with no rule at all - logging "hydro done" on a cylinder you never
set a reminder for is a real thing to want - which is why `kind`/`label` are **copied**
onto the record at write time rather than read back through
`gear_service_schedule_id`, and why that FK is `ON DELETE SET NULL` rather than
`CASCADE`. Deleting a reminder must never throw away the receipts.

Both tables are brand new, so `Base.metadata.create_all()` creates them, their
indexes and their `CheckConstraint`s on startup - no manual `ALTER TABLE` (see
"Schema changes have no migration tool"). The one column this feature adds to an
existing table is `user.gear_service_emails`, below.

## A service interval is months OR dives, whichever trips first

`interval_months` and `interval_dives` are two independent thresholds on the same
rule, not alternatives: regulator servicing is routinely specified as "annually or
every 100 dives, whichever comes first". `ck_gear_service_schedule_has_an_interval`
requires at least one - a rule with neither could never become due, so it would sit in
the table producing nothing forever.

Unlike `GearItem.type` (a closed vocabulary already enforced by Pydantic - see
"`GearItem.type` is a closed vocabulary, but has no DB `CHECK` constraint"), these
*do* get DB-level `CheckConstraint`s, alongside `interval_months > 0` /
`interval_dives > 0`. They're genuine domain invariants rather than a duplicated list,
and they're mirrored in `GearServiceScheduleBase`'s `require_an_interval` validator and
the frontend's Zod refine.

`services.gear_service.recalculate_service_schedule` derives both due fields from a
single baseline - the latest non-deleted record for the schedule, ordered
`(serviced_on DESC, id DESC)`, falling back to `starts_on`/`dive_count_at_start` when
the item has never been serviced. The `id` tie-break matters: two services entered for
the same day would otherwise resolve arbitrarily.

`add_months` is hand-rolled with `calendar.monthrange` clamping rather than pulling in
`dateutil`: "serviced 31 August, again in 6 months" has to land on 28 (or 29) February,
not overflow into March. `timedelta` can't express months at all.

## `dive_count_at_service` is a snapshot, so back-filling old dives inflates it

A dive-based threshold needs a baseline dive count, and both `dive_count_at_start`
(on the schedule) and `dive_count_at_service` (on the record) are snapshots of
`gear_item.dive_count` taken server-side at write time. Neither is in the `Create`
schema - both are `extra="forbid"` - because a caller able to set them could move
their own due threshold arbitrarily.

Being snapshots of a *lifetime* counter, they have two known inaccuracies, both
accepted deliberately:

- Back-filling a 2019 dive next week inflates "dives since this service". The exact
  alternative - `COUNT(*)` over `dive_gear_item JOIN dive WHERE start_time::date >
  serviced_on` - is correct but can't be compared against a stored threshold and needs
  a per-item aggregate on every read.
- Deleting dives drives `dive_count` back down, so a naive subtraction can go negative.
  `services.gear_service.dives_since` clamps at 0 (as does `divesSince` in the web
  app's `lib/gear-service.ts`); "-3 dives since service" is nonsense to display and
  worse to compare.

## `next_due_on`/`next_due_at_dive_count` are stored; `ServiceStatus` deliberately is not

The two due fields are denormalized onto the schedule and recalculated on write, the
same "recompute from scratch, never increment" approach as `recalculate_dive_stats`
and `recalculate_gear_dive_counts`. Storing `next_due_on` is what makes the digest
job's `WHERE next_due_on <= today + 30` an indexed scan
(`ix_gear_service_schedule_next_due_on`) rather than a Python filter over every
schedule in the database.

`ServiceStatus` (ok / due_soon / overdue) is the opposite: it is **never** stored, and
never computed inside a `@cache`-decorated read. It's a function of *today's date*, and
the single-gear-item cache uses the decorator's 3600s default while the list uses 60s -
either would happily serve yesterday's countdown after a quiet night. The API therefore
returns only clock-stable facts (`next_due_on`, `next_due_at_dive_count`,
`last_service_on`, plus the item's existing `dive_count`), every one a pure function of
stored data and safe to cache indefinitely.

Same reasoning drives `GET /gear-service-due` taking **no `within_days` parameter**. A
server-side "due within N days" horizon would bake today's date into the cached
response, which then goes quietly wrong at midnight. It returns every active schedule
(capped at `DUE_OVERVIEW_LIMIT`) and the client buckets.

## Service status is computed twice on purpose: in the browser, and in the digest job

Because status can't be an API field, whoever displays it has to derive it.
`services.gear_service.service_status` and `serviceStatus` in the web app's
`src/lib/gear-service.ts` are near-line-for-line twins. This is the feature's only
duplication and it is deliberate: the browser needs it for every badge, and the digest
job needs it with no browser to ask.

The constants are named identically on both sides (`SERVICE_DUE_SOON_DAYS = 30`,
`SERVICE_DUE_SOON_DIVES = 10`) so a single `grep SERVICE_DUE_SOON` finds the pair.
Change one and you must change the other; both have the same truth-table tests.

## A dive moves `gear_item.dive_count`, but never `next_due_at_dive_count`

`next_due_at_dive_count` is an **absolute threshold** (`baseline + interval_dives`),
not a remaining count. It depends only on snapshots, so logging, editing or deleting a
dive changes what the status *displays as* without requiring a write to
`gear_service_schedule` at all - the comparison `dive_count >= next_due_at_dive_count`
happens at read time (browser) and query time (cron).

`services/gear_stats.py` is therefore untouched by this feature. Storing "dives
remaining" instead would have forced a write to every one of a user's schedules on
every dive mutation and coupled two recalculation services together, for no gain.

The one thing this has to preserve: a dive that trips a dive-based interval must still
be able to fire an email even though nothing wrote to the schedule. It does, because
`should_notify` is evaluated fresh each run against the item's live `dive_count`.

## Service routes are flat, and every one checks ownership before touching the cache

`/gear-service-schedule(s)`, `/gear-service-record(s)` and `/gear-service-due` are
top-level, not nested under `/gear-item/{uuid}/...`. That matches the two earlier
flattenings ("`/{username}/...` resource routes were flattened", "`/user/{username}/...`
routes were changed to `/user/{id}/...`"): each resource has its own `uuid`, and
nesting would give it a second identity. Filtering by item is a query parameter,
exactly like `GET /dives?gear_item_uuid=`.

Create bodies carry `gear_item_uuid` rather than `user_uuid` - ownership derived from
the item is strictly stronger than trusting a user id in the body, since the caller
can't name an item that isn't theirs. `_owned_gear_item` answers identically (422, "Gear
item not found.") for "doesn't exist" and "isn't yours", mirroring `_resolve_item_ids`
in `gear_sets.py`, so someone else's gear uuids stay unprobeable.

Every read route resolves the row and checks ownership *uncached* first, then calls a
private `_cached_read_*` helper - see the `@cache`/authorization gotcha under "All
`/user*`/`/users`/`/dive/parse` endpoints require auth".

## Every service cache key starts with `user_{id}_gear_`, so invalidation needed no change

The five new cache keys (`..._gear_service_schedules:page_...`,
`..._gear_service_schedule:{uuid}`, `..._gear_service_records:page_...`,
`..._gear_service_record:{uuid}`, `..._gear_service_due`) all sit under the existing
`user_{id}_gear_` prefix, so `invalidate_gear_caches`' single
`delete_keys_by_pattern(f"user_{user_id}_gear_*")` already sweeps them.
`services/cache_invalidation.py` gained documentation, not code. If a sixth key is ever
added, it must stay under that prefix.

`invalidate_dive_caches` is deliberately **not** called from the service routes: dive
reads embed `GearItemInfo`, which carries no service fields. If service data is ever
added to `GearItemInfo`, these routes must start calling it.

`GearItemRead` gained a `service: list[GearServiceScheduleInfo]`, resolved by
`get_schedules_for_gear_items` - one batched query per page, mirroring
`get_gear_items_for_dives` - so the gear list can badge every row without an N+1.
`GearItemInfo` itself was left alone: it's embedded in every dive and gear set and has
to stay lean.

## The service digest fires once per threshold, tracked in four columns on the schedule

`send_gear_service_digests` runs daily but sends far less often. `should_notify`
compares the schedule's current `(status, next_due_on, next_due_at_dive_count)` against
the stored `(notified_stage, notified_for_due_on, notified_for_due_at_dive_count)` and
sends only when they differ. That one comparison covers everything worth covering: one
email entering "due soon", one entering "overdue", a fresh cycle after a service is
logged, and a fresh notification when an edited interval moves the due date.

Critically, `recalculate_service_schedule` clears all four notify columns **in the same
statement** that moves `next_due_*`. That's what guarantees they can't drift: a reminder
is always armed for the due date currently stored, never a superseded one.

The one gap the tuple can't close is a schedule that stays overdue forever - the tuple
never changes, so it would go silent. `SERVICE_OVERDUE_RENAG_DAYS` (90) re-sends
quarterly. Deliberately overdue-only: nagging about something that isn't due yet is
what trains people to ignore the emails.

Two ordering/scoping decisions in the job itself:

- **One email per user, never per item.** A diver whose whole kit comes due the same
  week gets a single list.
- **Send first, mark second.** If Resend fails, the exception propagates before the
  mark, so the worst case is a duplicate email tomorrow rather than a reminder that
  silently never arrives. For gear safety that's the right way round.

The cron entry has **no `run_at_startup=True`**, unlike `purge_expired_tokens`: that
one is idempotent housekeeping, this one sends email, and a worker restart must never
blast a round of reminders out.

The query's `OR` is only half indexable. The date arm is served by
`ix_gear_service_schedule_next_due_on`; the dive arm compares `gear_item.dive_count`
against a `gear_service_schedule` column and can't be. That's fine at this scale (a
handful of schedules per user), and it's a pre-filter anyway - `should_notify` makes the
real decision. The escape hatch, if it ever matters, is materializing "dives remaining"
onto the schedule inside `recalculate_gear_dive_counts`; that was rejected for the
reasons under "A dive moves `gear_item.dive_count`" above.

## The digest's "today" is UTC, and that's fine at date granularity

`User` has no timezone column - `Dive.utc_offset_minutes` is per-dive, not per-user - so
`send_gear_service_digests` treats "today" as UTC and runs at
`GEAR_SERVICE_DIGEST_HOUR` (default 07:00 UTC, mid-morning across Europe). With a
30-day lead time and date-granular thresholds, being a few hours out either way changes
nothing. If it ever matters, the upgrade is to run hourly and gate on the offset
inferred from the user's most recent dive.

## Archived or soft-deleted gear never generates a reminder

Three exclusions in the digest query and in `get_due_overview_for_user`, each for its
own reason:

- `gear_item.is_archived` - retiring a piece of kit has to silence it without the
  diver also having to pause every rule on it. Consistent with archiving already
  hiding an item from the dive form's picker.
- `gear_item.is_deleted` / `gear_service_schedule.is_deleted` - the obvious ones.
- `user.gear_service_emails` - the opt-out, below.

`gear_service_schedule.is_active` is a fourth, different thing: pausing one rule
without deleting it or touching the item. Three separate flags on the same axis sounds
redundant but each answers a distinct question - is this *item* retired, is this *rule*
paused, is this *user* opted out.

## Soft-deleting a gear item soft-deletes its schedules, with a raw `UPDATE`

`gear_item.is_deleted` is application-level, so the `ON DELETE CASCADE` on
`gear_service_schedule.gear_item_id` never fires for it (that only happens on a hard
delete, which the API doesn't expose). Without an explicit cascade the digest would
keep emailing about gear the diver can no longer see, so `erase_gear_item` calls
`soft_delete_schedules_for_gear_item` first.

It's a plain `update(...).values(is_deleted=True, ...)` rather than
`crud.delete(allow_multiple=True)`: fastcrud raises `NoResultFound` when zero rows
match, and the overwhelmingly common case - deleting an item that never had a schedule -
matches zero rows. `purge_expired_tokens` works around the same pitfall with a
`count()` first; a raw `UPDATE` avoids the extra round trip entirely.

Service *records* are deliberately left alone. They're unreachable once the item is
gone, and a soft delete is meant to be recoverable - discarding the history would make
it a good deal less so.

## `user.gear_service_emails` is the only new column on an existing table

Opt-*out*, not opt-in: a reminder nobody switched on is a reminder that never arrives,
and the entire point of the feature is reaching a diver who isn't currently in the app.

It has to be added to **both** `UserRead` (so `GET /user` feeds the settings toggle;
the `= True` default also covers the window before the `ALTER` runs) and `UserUpdate`.
Missing the second is the easy mistake: `UserUpdate` is `extra="forbid"`, so the
toggle would 422 rather than save.

Per "Schema changes have no migration tool", apply to an existing local DB with:

```sql
ALTER TABLE "user" ADD COLUMN gear_service_emails BOOLEAN NOT NULL DEFAULT true;
```

That is the whole manual-DDL list for this feature. Both new tables, all their
indexes, all three `CheckConstraint`s and both FKs arrive via `create_all()` on
restart, because they are brand-new tables.

## Certification card files live in Postgres, not object storage

The whole point of storing c-cards is to remove a reason to keep PADI's or SSI's app
installed. We can't *issue* certifications, but we can hold a photo of the card.

The bytes go in a `bytea` column (`certification_file.data`). There is no bucket, no
storage client and no credentials to manage, because at this size there is nothing to
gain: a diver has well under ten cards of a few hundred KB to a few MB each, so the
existing database and its backups cover the lot. Object storage would add
infrastructure, configuration, a new failure mode and orphaned-object cleanup to store
what amounts to a few MB per user.

This will not stay true. When photo galleries arrive - hundreds of multi-megabyte dive
photos per diver - blobs in Postgres become a real problem for backup size and restore
time. What makes that migration tractable is that `services/certification_files.py` is
the **only** module that touches `data`: routes call `store_certification_file` /
`load_certification_file` / `delete_certification_file` and never see a column. Moving
to S3 means rewriting those functions and adding a nullable `storage_key`. Deliberately
no abstract `FileStorage` base class in the meantime - there is one implementation and
no configuration selecting between two, so an interface would be scaffolding for a
migration that hasn't happened. The module boundary is the seam.

## Card files are a separate table, hard-deleted, with a deferred `data` column

`certification` itself carries **no binary columns**, so listing a diver's
certifications can never drag megabytes through the query. Two `bytea` columns on the
main table would have worked with `deferred()`, but deferred columns are easy to load by
accident from a `get_multi` or an admin view; a separate table makes that structurally
impossible and gives front and back identical handling instead of duplicated column
pairs. `data` is *also* `deferred()`, so even a direct query for a file row returns
metadata only unless `undefer` asks for the bytes - which only `load_certification_file`
does.

`CertificationFile` has no `SoftDeleteMixin`, unlike every other model here. A
soft-deleted blob keeps occupying its bytes forever with nothing able to read it. So
files are hard-deleted - including when their parent certification is deleted, which
`erase_certification` does explicitly: `is_deleted` is application-level, so no `DELETE
FROM certification` ever runs and the FK's `ON DELETE CASCADE` never fires. Same trap as
`soft_delete_schedules_for_gear_item`.

Two gotchas worth keeping:

- `PublicUUIDMixin`'s `uuid` has a `default_factory`, which is a **dataclass**-level
  default applied when the ORM constructs an instance. `store_certification_file` uses a
  Core-level `pg_insert(...)` for its `ON CONFLICT` upsert, which constructs nothing, so
  the `uuid` has to be passed explicitly or Postgres gets a NULL. It is set on insert
  only and left out of the `DO UPDATE` set, so replacing a card's photo swaps the bytes
  without the file changing identity.
- Wrapping a column in `deferred()` hides the `Mapped[bytes]` annotation from
  SQLAlchemy's nullability inference, so `data` needs an explicit `nullable=False`.

## Uploaded content types are sniffed, never taken from the client

`sniff_content_type` identifies an upload from its leading bytes and rejects anything
that isn't JPEG, PNG, WEBP or PDF. The sniffed value is what gets stored *and* what the
download route later serves the file back as - so trusting the uploader's
`Content-Type` would let someone have us serve arbitrary bytes under a type of their
choosing. A `.jpg` whose contents are HTML is a 415, not a stored `text/html`.

HEIC is detected separately and rejected with its own message naming the fix. It is what
an iPhone stores natively, so a bare "unsupported file type" would read as a bug to
anyone who just photographed their card; in practice iOS transcodes to JPEG when
uploading through a file input, so this mostly catches files picked out of the Files app.
Supporting it properly needs `pillow-heif`, which is a bigger change than this feature.

The download response carries `Content-Disposition: attachment`, `X-Content-Type-Options:
nosniff` and `Content-Security-Policy: default-src 'none'; sandbox`. `attachment` rather
than `inline` because the web app fetches these through its API client and renders from a
blob URL, never navigating to the URL - so nothing is lost, and a malicious PDF opened
directly in a tab can't execute in the same-origin viewer. The `filename` on that header
is built by `content_disposition_attachment`, not interpolated - see "Non-ASCII filenames
need RFC 6266, because Starlette encodes headers as latin-1" for what interpolating it
cost.

## The card download endpoint is never Redis-cached

`@cache` stores serialized API responses; parking multi-megabyte binaries in it would
evict everything else the cache exists for. The endpoint uses `ETag`/`If-None-Match`
instead, which does the equivalent job in the browser where the bytes are wanted anyway.
The `ETag` is the stored `sha256`, and `get_certification_file_sha256` fetches just that
column, so a conditional request costs one narrow query rather than a full read that gets
thrown away.

The metadata reads *are* cached, hand-written rather than built with
`OwnedResourceCache` - that factory's own docstring rules out resources whose list does
more than a `get_multi` plus a shape conversion, and `CertificationRead` embeds each
row's file metadata (batched by `get_file_infos_for_certifications`, the
`get_schedules_for_gear_items` pattern). `invalidate_certification_caches` runs after
file uploads and deletes too, not just metadata writes: photographing a card changes
what a cached list page should say even though no `certification` column moved.

## `agency_other` is validated against `agency` in two places

`CertificationBase` rejects `agency_other` unless `agency` is `other`, and requires it
when it is - in both directions, so a stored row can never carry a second agency name
that some future read path might display. `CertificationUpdate` can't do this: a PATCH
may carry either field alone, so the pairing is only checkable once merged over the
stored row. `patch_certification` does that with `_validate_agency_pairing`.

Note the asymmetry this creates for clients: switching a certification away from `other`
must send `agency_other: null` explicitly, or the merged result still has both set and
422s.

## No manual DDL for this feature

`certification` and `certification_file` are both brand-new tables, so per "Schema
changes have no migration tool" they and all their indexes arrive via `create_all()` on
restart. There is nothing to `ALTER`.

`CertificationFile` is deliberately **not** registered in `admin/views.py`: its rows are
mostly one multi-megabyte `bytea` the generic list/detail views would try to render as
text, and there's no admin form that could meaningfully accept a file upload.

## Dive source files are stored only once a dive exists

`POST /dive/parse` stays exactly what it was: it reads the upload, parses it, and drops
the bytes. Storage happens in a second request, `PUT /dive/{uuid}/file`, which the web
app sends after `POST /dive` (or `PATCH /dive/{uuid}`) has succeeded.

Two reasons it isn't folded into the create call. `DiveCreateRequest` is
`extra="forbid"` JSON, so a file can't ride along in the body, and turning the one
resource-creating route in the app into a multipart endpoint to carry an optional
attachment is a poor trade. And a separate `PUT` is idempotent, which is what makes
re-sending the same file harmless.

The consequence is that a file which never becomes a dive is never kept - including
every file that failed to parse, which is arguably the most interesting kind for parser
work. That's a deliberate narrowing: the point of the corpus is to develop new
extractions and *backfill the dives they'd improve*, and a file with no dive attached
can't be backfilled into anything.

The web app treats a failed attach as non-fatal - a toast, not a rollback. The dive
exists and is correct; the file is a nicety for future parsing work, and undoing a save
the diver already made to preserve it would be much worse than losing it.

## A signed parse token, not `can_parse`, decides what may be stored

`PUT /dive/{uuid}/file` requires a `file_token` - a short-lived JWT
(`create_dive_file_token`, `TokenType.DIVE_FILE`) minted by `/dive/parse` binding
`(user uuid, sha256 of the bytes, the parser that succeeded)`. The store path re-hashes
the body it receives and refuses anything whose digest doesn't match.

The obvious alternative was to re-run the parser registry on upload and accept whatever
`can_parse` recognized. That was rejected on two counts. `can_parse` answers "could some
parser plausibly read this?", so the endpoint would accept any blob shaped like an
export - free `bytea` storage in the app's own database, and no guarantee the stored
file is the one that pre-filled the dive's form. And `parse_dive_file` deliberately
falls through to the next parser when one raises `UnsupportedDiveFileError` from
`parse()`, so the first parser to *match* is not always the one that *read* the file;
recording the former would mislabel rows. `parse_dive_file_with_parser` exists to return
the parser that actually succeeded, with `parse_dive_file` kept as a thin wrapper so no
existing caller changed.

What the token proves is narrow and worth stating: this server parsed these exact bytes
for this user, recently. It does **not** prove the diver left the pre-filled values
alone afterwards. Provenance is "this dive was imported from this file", not "these
field values are derived from this file".

It is not a security boundary on its own - the route still checks dive ownership - and
it is deliberately **not** blacklisted after use, unlike `create_onboarding_token`:
re-uploading the same file to the same dive is an idempotent no-op by design, and the
token can only ever store bytes its holder already owns a parse of. `DIVE_FILE_TOKEN_
EXPIRE_MINUTES` defaults to 24 h rather than the 30 minutes the auth tokens use, because
it bounds staleness rather than credential lifetime: a diver may import a file and then
spend an evening filling in sites, gear and notes before saving.

`content_type` is deliberately *not* a claim. It ends up in a response header, so it is
resolved from `parser_key` through `PARSER_BY_KEY` at store time - a value the
application owns, from a closed set it declares. A forged token can never dictate it.
This replaces byte-sniffing here but not for cards: a successful parse is a strictly
stronger guarantee than magic bytes, which is why `sniff_content_type` still governs
certification uploads, where nothing parses the content.

## A dive has at most one source file, and identical bytes are stored once per diver

Two unique indexes on `dive_file`: `ux_dive_file_dive_id` and
`ux_dive_file_user_id_sha256`. Together they reduce every upload to three cases, which
`reconcile()` in `services/dive_files.py` returns as a `Literal` so the decision is
testable without a database:

- same bytes, same dive → **no-op**. `PUT` is idempotent; nothing is rewritten, not even
  `original_filename`.
- same bytes, *another* dive → **409**, naming nothing but saying so plainly.
- unseen bytes → **insert**, hard-deleting whatever that dive had.

The 409 is the interesting one. Re-pointing the row at the new dive would silently strip
the file off the dive that already has it; storing a second copy would defeat the
dedupe. An export normally holds a single dive, so the realistic cause is logging one
file as two dives - a data-quality problem the diver wants told about, and one that
costs nothing to report because the dive is already saved by the time the attach runs.

There is deliberately no fourth "unlinked row you could re-claim" case, because
`dive_id` is NOT NULL. Retaining a replaced file as an unlinked or superseded corpus row
was considered and rejected: it needs a state column, a partial unique index and a
fourth branch, and every such row would be unreachable through an API where every route
resolves through a dive - the exact trap "Card files are a separate table, hard-deleted"
describes. If replaced-file retention ever earns its keep, a nullable `superseded_at`
plus `UNIQUE (dive_id) WHERE superseded_at IS NULL` is the additive way to add it.

The write wraps `IntegrityError` into a 409 rather than taking a lock: there is one user
per dive and the UI disables the button in flight, so a retry beats `SELECT ... FOR
UPDATE` on this path.

## Deleting a dive hard-deletes its source file

`erase_dive` calls `delete_files_for_dive` before `crud_dives.delete`, for the same
reason `erase_certification` does: deletion is application-level (`is_deleted`), so no
`DELETE FROM dive` ever runs and the FK's `ON DELETE CASCADE` never fires.

Leaving the row would do more than strand bytes. It would keep the file's slot in *both*
unique indexes, so re-importing the same export into a fresh dive would 409 against a
dive the diver can no longer see. `DELETE /dive/{uuid}/file` likewise hard-deletes
rather than unlinking - a "delete" button that only hides the file would be a worse
trade than losing it from the corpus, and it's a raw device export, which is more
identifying than a card scan.

## `source_file` is on the dive detail response only

`DiveFileInfo` hangs off `DiveReadWithMixtures`, not `DiveRead`. Note the inheritance
trap: `DiveReadWithMixtures` *extends* `DiveRead`, so putting the field on the parent
would put it on the paginated list response too and force a second query into
`_cached_read_dives` - the hottest path in the app - for something only the detail page
renders.

`get_file_infos_for_dives` is still written batched (explicit columns, `dive_id.in_()`)
even though it's only ever called with one id, because that's the
`get_file_infos_for_certifications` shape, it makes the never-select-the-`bytea`
discipline the default, and it's what an attachment marker in the dive list would need
without a rewrite.

## No manual DDL for the dive-file feature

`dive_file` is a brand-new table, so it and both unique indexes arrive via `create_all()`
on restart. No column was added to `dive` or any other existing table - the FK lives on
`dive_file` - so there is nothing to `ALTER`. `DIVE_FILE_TOKEN_EXPIRE_MINUTES` has a
default in `config.py`, so no `.env` change is needed either.

`DiveFile` is deliberately **not** registered in `admin/views.py`, for the same reason as
`CertificationFile`.

## Gas use is computed on read, never stored, and deliberately refuses multi-tank dives

`services/dive_gas.py`'s `compute_gas_use()` derives a dive's surface-normalized
consumption (`gas_used`, `rmv`, `sac_bar_per_min`) from `duration`, `avg_depth` and the
one mixture's pressures, and `_to_public_dive_with_mixtures()` attaches it as `gas_use`.
Both callers of that helper (creating a dive, and `_cached_read_dive`) go through it, so
the formula has exactly one call site.

**Computed rather than denormalized**, unlike `user_dive_stats` and
`gear_item.dive_count`: those exist because recomputing them per request means a join +
`GROUP BY` over a user's whole history. This is arithmetic on columns already loaded for
the response, so a stored column would buy nothing and cost a hand-written `ALTER TABLE`
(see "Schema changes have no migration tool") plus a recalculation hook on every mixture
replace. The feature ships with **no DDL at all**.

**Safe to cache, unlike gear service status.** `serviceStatus` can't be an API field
because it depends on today's date and the cache outlives the day (see the web app's
"Service status is derived in the browser"). Gas use depends on nothing but the row, so
computing it before `@cache` stores the response is correct, and there is no browser-side
twin of the formula to keep in step - only of the *reasons it's absent*, which is a UI
concern (`lib/dive-gas.ts`).

**`None` rather than a best guess**, whenever the dive doesn't pin every litre to a known
depth over a known time: not exactly one mixture, no usable `avg_depth`, either pressure
missing, or no pressure drop at all (a carried-but-unbreathed tank is not a 0 L/min
diver, and averaging one into a dashboard trend would drag it down). Returned as a whole
nested object or not at all, so there's no half-populated version to misread - divers
plan gas off these numbers.

The multi-tank refusal is the interesting one, and it is *not* about summing litres.
A dive records no clue as to **how** its cylinders were breathed, so a 50% deco bottle
emptied over five minutes at 6 m and a back gas breathed for forty at 30 m would both be
divided by the whole dive's average depth. Manifolded twins already work, because
`VOLUME_OPTIONS` in the web app logs them as one mixture at the pair's combined water
capacity with a single shared pressure - which is exactly right. Unlocking genuinely
staged cylinders needs, at minimum, a `parallel`/`staged` discriminator on
`dive_mixture`, and for the staged case per-mixture time-on-gas and depth-on-gas; the
exact version needs depth and per-transmitter pressure *samples*, which the stored
dive-computer exports already contain but nothing parses out yet. Note also that a
partial sum would be worse than nothing in the common real case - a staged bottle whose
pressures simply weren't logged would leave the numerator short against a full-dive
denominator and quietly report an RMV that's too *low*.

Three approximations are baked into `METERS_PER_BAR = 10.0` and documented there: salt
water (10.06 m/bar) vs fresh (10.33), a 1 bar surface (wrong at an altitude lake), and
ideal-gas behaviour (~5% optimistic at a 230 bar fill). All three are what every other
dive log does, none is correctable without data the app doesn't collect, and all apply
uniformly across a user's dives, so the trend is unaffected.

## `GET /user/gas-use-history` is one unpaginated series, cached under the dive prefix

The dashboard graph needs a whole diving career, so this returns every dive that yields
a figure, oldest first, with no pagination - a page of ten dives is not a trend. It can't
be served off `GET /dives` in any case: that response carries no mixtures, and the fix
would be a batched mixture lookup on the hottest path in the app.

The cache key is `user_{id}_dives:gas_use_history`, deliberately under the *dives* prefix
even though the route hangs off `/user/...`. `invalidate_dive_caches()` already sweeps
`user_{id}_dives:*` after every dive create, update and delete, so the series drops with
them and that function needed no change at all. A prefix of its own would have been a
third pattern to remember to add there, and the bug from forgetting is a graph that
silently keeps plotting deleted dives.

`gas_use_history()` runs two queries - the user's dives, then their mixtures batched -
and lets `compute_gas_use` decide per dive in Python. Pushing its conditions into SQL
(`HAVING count(*) = 1`, `avg_depth IS NOT NULL`, both pressures present) would keep the
un-derivable majority from coming back at all: on a real 506-dive log, 343 dives qualify,
so roughly a third of the rows are fetched and discarded. That's left on the table on
purpose. It would be a second copy of the rules in a second language, and when the two
drift the symptom is a dive quietly missing from a graph - which nobody notices, unlike a
crash. Same scale judgment as `recalculate_dive_stats` re-aggregating on every write.

Note that `_cached_gas_use_history` carries the same authorization caveat as
`_cached_read_dives`: `@cache` serves a hit without re-running the wrapped function, so it
must only ever be called with the calling user's own id.

## A dive profile is stored per channel, not per sample and not on a shared axis

`dive_profile` holds one row per dive with each channel's series in a JSONB `data`
payload:

```json
{"depth":       {"t": [0, 10, 20], "v": [139, 372, 632]},
 "temperature": {"t": [0, 1, 2],   "v": [219, 219, 218]},
 "pressure":    [{"gas_number": 1, "t": [0, 10], "v": [2052, 2041]}]}
```

**Per channel rather than one shared time axis with nulls**, because the channels are
independently sampled and the corpus is emphatic about it: a 2026 Suunto Ocean export has
8 292 sample objects, of which 395 carry `Depth`/`Ceiling`/`Cylinders`, 3 933 carry
`Temperature` at 1 Hz, and the rest carry only events, GPS or battery telemetry. A union
axis on that dive is ~4 300 entries with depth null in about 90 % of them. Per-channel is
roughly half the size, each series is naturally sorted, and nothing has to handle nulls on
write. The chart pays one binary search per channel instead of one shared index lookup -
see `nearestSampleIndex` in the web app.

A consequence of the same fact, worth stating because it looks like corruption: **the
union of an export's sample timestamps is not monotonic.** Adjacent entries go backwards
by up to 0.7 s, because the separate sensor streams are appended out of order. Each
channel's *own* timestamps are monotonic, so parsers group by channel and sort there, and
out-of-order union timestamps are never treated as a parse error.

**Not a row-per-sample table.** ~400 rows per dive on a Suunto D5 and several thousand at
1 Hz, for data that is only ever fetched whole, for one dive, and drawn. The table would
exist purely to be `ORDER BY t`-ed back into the arrays above.

**JSONB rather than a packed `bytea`.** This is the codebase's first JSONB column
(`grep JSONB src/` was previously empty), so the reasoning is worth recording. A packed
int16 encoding would save perhaps 3x on a column Postgres already TOASTs and compresses;
`SELECT data->'depth' FROM dive_profile WHERE ...` from `psql` is worth more than that
during parser work, and the entire file-retention rationale in `models/dive_file.py` is
"develop new extractions against real data".

**Integer-scaled values, no floats.** Depth in centimeters, temperature in tenths of a
degree, pressure in tenths of a bar. Same reasoning as `_kelvin_to_celsius`: a float
round-trip produces `20.600000000000023`-class noise, several thousand times per dive. All
three resolutions comfortably exceed any dive computer's real precision. The scale lives in
the format (`schemas/dive_profile.py`, mirrored by the web app's `PROFILE_CHANNELS`); the
API returns the stored integers verbatim and the chart divides once per point while it is
already mapping through its scale functions. Conversions go through `Decimal(str(value))`
with `ROUND_HALF_UP`, like every other unit conversion in the parsers.

**`t` is integer elapsed seconds** from the first sample across all channels - one origin
for all of them, since they share the chart's x axis and shifting them independently would
slide temperature off the depth curve. At 720 px across an hour, one second is a fifth of
a pixel, already finer than the chart can draw. Where two readings round onto the same
second (1 Hz temperature with sub-second jitter does this constantly), the later one wins;
averaging would invent a reading the sensor never took.

**No nulls inside a series.** A sensor dropout is a gap in `t`, and the chart breaks the
polyline where the delta exceeds a threshold derived from the series' own cadence. Real:
`Dive_2025-06-02-1155.xml` records pressure on 224 of 441 samples, and
`Dive_2025-03-08-1440.xml` has a 1 341-second hole in the middle of its pressure series.

**Pressure is a list keyed by `gas_number`, not a scalar channel.** A Suunto Ocean reports
five cylinder slots per sample with one populated, and gas numbering differs between device
generations (1 on the 2025 D5, 0 on the Ocean). It is a label to display, not an index to
trust. Only cylinders with at least one non-null reading are stored.

The summary columns (`duration_seconds`, `depth_sample_count`, and the five extremes) are
deliberately *outside* `data`, because they are what answers "does this dive have a profile,
and which curves would a chart draw" for the dive detail response without decoding tens of
KB. `channels` on the read schema is derived from which extremes are non-NULL rather than
stored - a column saying which curves a row carries is a column that can disagree with the
row.

## Profiles are capped at 1 200 points per channel by min/max bucketing, never LTTB

`MAX_POINTS_PER_CHANNEL = 1200`, applied per channel, server-side at extraction.

The one thing a depth profile must never lose is its maximum depth and the shape of the
deepest excursion. Min/max bucketing *guarantees* both extremes of every bucket survive -
a property a test asserts directly (`max(downsampled.v) == max(original.v)`). LTTB
optimizes visual similarity and offers no such invariant: it can drop a one-sample spike,
which on a dive profile is the single most important sample in the file. Same argument for
minimum temperature, which is what `bottom_temperature` means.

Buckets are chosen on **time**, not index, so a channel with an irregular cadence isn't
unevenly weighted, and each bucket emits its min and its max in time order.

Not hypothetical: 2026-ocean temperature is 3 933 points on one dive and hits the cap
today. Depth (395) never does.

The same guarantee cuts the other way and that is accepted on purpose: a transmitter glitch
that reports 0.6 bar mid-dive survives downsampling and stretches the pressure axis. That
is the file's own data faithfully drawn, and inventing a plausibility filter would mean
silently discarding readings - the opposite of the property the bucketing was chosen for.

## `parse_profile` is separate from `parse`, and never runs on the `/dive/parse` path

`DiveParser` gained a **non-abstract** `parse_profile(content) -> ParsedProfileSchema | None`
rather than growing `ParsedDiveSchema` a samples field.

It is never called from `POST /dive/parse`. A profile is thousands of readings the browser
has no use for while filling in a form, and it would have to be posted back to be stored -
which would make the stored samples client-supplied and reopen the exact trust problem the
parse token exists to close. The "`ParsedDiveSchema`/`DiveMixtureSchema` trimmed..."
decision already deleted `DiveSampleSchema` for this reason; this is its complement. It is
called server-side from `PUT /dive/{uuid}/file`, the only place with both the bytes and
proof of where they came from.

Non-abstract so a new format can ship header-only and grow a profile extraction later
without a flag day. `None` means "this file carries no samples"; malformed samples raise
`DiveParseError`. Dispatch is free - `PARSER_BY_KEY` already resolves a stored `parser_key`
back to its parser.

`SuuntoXmlParser.parse_profile` **re-parses the XML from scratch via `defusedxml`.** A
second entry point into XML parsing is a second place to forget the XXE/entity-expansion
guard, so the billion-laughs and XXE fixtures are re-run against it in
`tests/test_dive_profiles.py`.

Two things are deliberately *not* read. `AveragedTemperature` in the XML export - the raw
`Temperature` is what the sensor saw, and smoothing is a chart decision that shouldn't be
baked into storage. And `DeviceInternalAbsPressure` in the JSON export, which sits right
next to `Cylinders[].Pressure` in the same sample object and is the device's own *ambient*
sensor (~96 400 Pa at the surface): labelling it "tank pressure" on a chart divers plan gas
from would be actively wrong, so it gets a prominent comment rather than a silent omission.

A DM5 export reports one `<Pressure>` per sample with no cylinder identity, so it becomes a
single-entry pressure list labelled gas number 1 - which is what the *same dive* exported as
JSON reports for it. Not the mixture's `<TransmitterId>`: that is a device serial (e.g.
2411100050), so using it would read as nonsense in a legend and make one dive disagree with
itself depending on which export it was imported from.

## DM5 XML expresses every pressure in millibar, and `_parse_mixture` used to read them as bar

Cross-checked against the same dive exported both ways (`Dive_2025-05-31-1259.xml` /
`685013accbecd72812f3d840.json`):

| | XML | JSON | Truth |
|---|---|---|---|
| `DiveMixture/StartPressure` | `205203` | `20520312` Pa | 205.2 bar |
| `DiveMixture/EndPressure` | `86781` | `8678125` Pa | 86.78 bar |
| sample `<Pressure>` | `205200` | `Cylinders[0].Pressure: 20520000` | 205.2 bar |

`CylinderWorkPressure = 200000` (200 bar) and `SurfacePressure = 105500` (1.055 bar) confirm
millibar throughout, and no XML file in the corpus carries a bar-scale mixture pressure: of
353 `StartPressure` values, 255 are `0` and 96 are 5-6 digits. The JSON parser was already
correct (`_pascals_to_bar`); the XML one was not, and it went unnoticed because
pre-2025 exports have no transmitter and write `0`.

The consequence was that any dive imported from a 2025+ DM5 XML stored
`start_pressure ~ 205203` bar and a correspondingly meaningless `gas_use`/RMV. Fixed here
rather than separately because without it the *same file* would yield 205.2 bar on the
profile chart and 205 203 bar in the gas mixtures table on the same page.

No corrective `UPDATE` was run against stored `dive_mixture` rows: on this database no row
has a pressure above 500 bar (the highest is a plausible hand-entered 415), because the
existing dives were bulk-imported by `export/import.py`, which never uploaded a file. Should
such rows appear on another database, the correction is unambiguous - divide any pressure
above ~500 bar by 1000 - since no real cylinder reaches it.

## Profile extraction is idempotent on (file digest, extractor version), and never fails an upload

`should_extract(existing, sha256=, version=)` is the whole test, split out of its callers so
the table of cases is testable without a database. A profile is a pure function of the bytes
it came from and the extractor that read them, so `dive_profile.source_sha256` +
`extractor_version` is both the idempotency key and the `ETag` the read route serves.

In `store_dive_file`:

- **The `insert` branch** deletes any existing profile alongside the `DiveFile` delete and
  stores the new one in the *same transaction*: a dive must never end up with a stored file
  and a profile extracted from a different one. The delete is unconditional, so a
  replacement export with no samples clears the previous export's curves rather than leaving
  them attributed to it.
- **The `noop` branch** (same bytes re-uploaded) calls `should_extract`. A repeated `PUT`
  stays a no-op, but a `PUT` after `PROFILE_EXTRACTOR_VERSION` is bumped opportunistically
  upgrades the profile from bytes already in hand.
- **Extraction runs before the `try` block**, not inside it: an exception in there would be
  caught by the `IntegrityError` handler and reported to the diver as a concurrent-upload
  conflict, which a parse failure is not.

**A failed extraction must not fail the upload.** `extract_profile` catches `DiveParseError`
(and anything unexpected), logs it with the parser key, and returns `None`. The file is the
durable artifact and can be re-extracted after the extractor is fixed; refusing the attach
would discard the very corpus entry needed to fix it.

**Extraction never writes back to `dive` columns.** A profile's maximum depth may disagree
with `dive.max_depth` - and does: `Dive_2021-03-28-1049.xml` reports `<MaxDepth>14.9</MaxDepth>`
in its header while its deepest *sample* is 14.89 m. `dive.max_depth` is the diver's record
and may have been hand-edited. The parse token proves "this dive was imported from this
file", not "these values are derived from it". For the same reason `duration_seconds` on the
profile is the span of the recorded samples, not `dive.duration`; a computer keeps logging
for ~20 s after the dive ends.

## The profile's `ON DELETE CASCADE` never fires, so two explicit deletes do the work

`dive_profile.dive_id` carries `ON DELETE CASCADE`, and it is decoration: dive deletion is
application-level (`is_deleted`), so no `DELETE FROM dive` ever runs - the same trap
`delete_files_for_dive` and `soft_delete_schedules_for_gear_item` already exist to work
around. Said so in the model docstring, because a reader who trusts the FK will conclude the
rows are cleaned up when they aren't.

What actually removes them is `delete_profile_for_dive`, called from `delete_dive_file`.
That covers **both** paths for free: the explicit `DELETE /dive/{uuid}/file`, and
`delete_files_for_dive`, which `erase_dive` calls when a dive is deleted. Verified by
reading `delete_files_for_dive` rather than assumed.

A profile goes with its file rather than outliving it: nothing cascades from removing the
export, and a profile whose source is gone can never be re-derived or checked against
anything.

## `GET /dive/{uuid}/profile` uses an ETag, not `@cache`

Modelled on `read_dive_file`, and for a sharper reason than that route's.

A profile is **immutable** for a given (source file, extractor version) pair, which makes it
the ideal `ETag` case and the worst Redis case. Every dive cache key lives under
`user_{id}_dive*`, and `invalidate_dive_caches` sweeps the lot on every dive edit and every
dive-site or gear rename - none of which can change a profile. Caching it would mean
evicting and refetching tens of KB per dive for nothing.

The version is checked before the payload is loaded (`get_profile_version` is a two-column
query), so a conditional request costs one narrow query rather than decoding tens of KB of
JSONB only to discard it. The 304 returns a bare `Response`, which bypasses `response_model`
validation - the same thing `read_dive_file` relies on. `Cache-Control: private, max-age=300`
is set by the endpoint, and `ClientCacheMiddleware` never overrides a `Cache-Control` an
endpoint set for itself. `v` is declared and ignored, so the contract is visible: the client
varies it with the profile's `updated_at` to give each re-extraction its own cache entry.

`DiveProfileInfo` goes on `DiveReadWithMixtures` and **never on `DiveRead`** - the same
inheritance trap `source_file` documents. On the parent it would land on the paginated list
and cost `_cached_read_dives` another query per page. `get_profile_infos_for_dives` is
written batched though only ever called with one id, matching `get_file_infos_for_dives`,
which is what a profile sparkline in the dive list would need without a rewrite.

## The profile backfill is a script, not an arq job

"The Arq worker now does one real thing" records that the API-side queue plumbing was
deliberately deleted and the worker runs crons only. A backfill finishes once per extractor
version, so scheduling it as a cron would mean rescanning the whole corpus forever for a job
that is already done.

`src/scripts/backfill_dive_profiles.py`, mirroring `create_first_superuser.py`:

```bash
docker compose exec api python -m src.scripts.backfill_dive_profiles --parser-key suunto_xml
```

It selects `dive_file` LEFT JOIN `dive_profile` where no profile exists, the extractor
version is behind, or `source_sha256` differs - with **explicit columns, never
`select(DiveFile)`**, or the `bytea` would ride along for every row in the corpus before a
single profile was extracted. One file at a time via `load_dive_file`, committing in batches
of 50, reporting `examined / extracted / skipped / no_samples / failed`. All five counts
always, because a run that reports only successes hides the parser that stopped working.

**The gotcha it exists to avoid:** `delete_keys_by_pattern` silently returns when
`cache.client is None`, and that is only ever set by the API's lifespan - which a script
does not go through. Without `create_redis_cache_pool()` first, every backfilled dive's
cached detail response would keep claiming the dive has no profile for up to an hour, with
no error anywhere.

`src/scripts/` is now bind-mounted into the `api` service (`./src:/code/src`) so that
command works at all; the app itself is still served from `/code/app` and is unaffected.

## No manual DDL for the dive-profile feature

`dive_profile` is a brand-new table with one unique index, so both arrive via `create_all()`
on restart. No column was added to `dive` or any other existing table - the FK lives on
`dive_profile` - so there is nothing to `ALTER`, and no `.env` change is needed.

`DiveProfile` is deliberately **not** registered in `admin/views.py`, for the same reason as
`DiveFile` and `CertificationFile`.

## The contact form is an API endpoint, not a `mailto:`

`POST /api/v1/contact` (`api/v1/contact.py`) is the only endpoint here that mails a
*human* rather than a user, and the only one that both accepts anonymous input and sends
mail as a result. It exists because the frontend is a pure static-ish Next.js client with
no mail provider of its own - Resend lives here, so the form has to post here.

Four things about it are deliberate:

- **Unauthenticated.** The person most likely to need it is the one who can't sign in.
  That makes it the obvious relay-abuse target, hence fixed-window rate limits keyed
  *both* by submitted email and by client IP (`CONTACT_FORM_RATE_LIMIT_*`, defaulting to
  a one-hour window). The submitted address is never verified, so the `From:` line in the
  resulting mail is a claim, not an identity.
- **Nothing is stored.** There is no inbox in this app to read a contact message from, so
  a table would be a write-only pile nobody ever opens. The operator's mailbox is the
  system of record.
- **`reply_to`, never a spoofed `from`.** `EMAIL_FROM_ADDRESS` is the only address this
  domain's SPF/DKIM covers; sending *as* the submitter is what gets a sending domain
  blocklisted. Hitting Reply in the inbox still answers the diver.
- **`send_contact_form_email` escapes its inputs, and it's the only sender that does.**
  Every other function in `services/email_service.py` interpolates content this server
  composed. This one interpolates prose a stranger typed, so it runs `html.escape` over
  the name, subject and body (then turns newlines into `<br>`). If you add another sender
  that carries user-supplied text, do the same.

`CONTACT_FORM_EMAIL` (default `contact@opendiving.app`) is the recipient - a self-hosted
instance should point it at its own operator. It is *not* the same setting as
`AppSettings.CONTACT_EMAIL`, which is OpenAPI document metadata shown in `/docs` and
nothing else; the two are separate so that publishing a maintainer address in the API docs
doesn't silently reroute a stranger's support mail.

With `RESEND_API_KEY` unset the send is a logged no-op, like every other sender here - but
the whole submission is written to the log, so a local instance can still see what would
have gone out. The endpoint still reports success in that case: it reports that the
message was *accepted*, and it deliberately never tells an anonymous caller anything about
the recipient inbox or downstream delivery.

## `dive_number` is a label, not an identity or an ordering key

Nothing in this codebase reads `dive_number` except to print it. Chronology is owned by
`start_time` everywhere - `_cached_read_dives` sorts by it, `gas_use_history` sorts by
it, and `ix_dive_user_id_start_time` exists to serve exactly that. Keep it that way: the
moment something joins, sorts or paginates on `dive_number`, everything below becomes
unsafe.

It has to stay a free-form label because the two things a diver wants from it are in
direct conflict:

- **Gaps are real data.** A diver whose first 46 dives are in a paper logbook starts this
  log at #47, and #100-149 may stay on paper forever. Normalizing that to 1..N destroys
  information nothing else records.
- **A fully backfilled log should read 1..N**, with nothing missing.

Both are served by never renumbering automatically, and offering an explicit renumber
instead. `services/dive_numbering.py` is the whole of it:

- `suggest_dive_number` proposes (`GET /dives/next-number`), and the form can overwrite it.
- `summarize_numbering` reports (`GET /dives/numbering`), and the diver can ignore it.
- `renumber_dives` rewrites, and only ever when asked (`POST /dives/renumber`).

Three consequences worth knowing before changing any of it:

- **There is deliberately no unique constraint on `(user_id, dive_number)`.** Duplicates
  are a normal transient state while back-filling, and a renumber shifting a run of
  numbers down by one collides with itself halfway through. Enforcing uniqueness would
  need a deferrable constraint and would buy nothing the numbering summary doesn't already
  say out loud. Duplicates are *reported*, not blocked.
- **The suggestion is positional, not `MAX(dive_number) + 1`.** It takes the number of the
  dive that chronologically precedes the one being logged and adds 1. For the ordinary
  case - logging the dive you just did - the two agree. They diverge exactly where the
  naive rule is wrong: back-filling a 2019 dive into a log that reaches #212 should
  suggest #12. (This replaced a frontend `lastDive.dive_number + 1`, which suggested #213.)
  It is returned even when it collides, because back-filling a run of old dives collides
  by construction.
- **`renumber_dives` writes one `UPDATE ... FROM` over a CTE**, not a write per dive. A
  partial run would leave numbering in a state neither the diver nor `summarize_numbering`
  could make sense of, and a row-by-row pass would hit collisions mid-shift. `dry_run`
  computes the same change list through the same code, so the confirmation dialog and the
  write it confirms can't disagree. Both order by `(start_time, id)` - the `id` tie-break
  matters, since two dives can share a start time and Postgres is otherwise free to return
  them in either order.

The renumber endpoint invalidates only the dive caches. Unlike every other dive write it
can't have moved `recalculate_dive_stats`'s figures or any gear item's `dive_count` - it
changed a label on dives that already existed.

## A dive computer's own counter is not the diver's dive number

`SuuntoXmlParser` used to import `DiveNumberInSerie` as `dive_number`, while
`SuuntoJsonParser` left it null. The XML behaviour was the wrong one: that field is the
*device's* counter, which starts at 1 on a new or factory-reset computer and restarts
again on the next one, so importing it stamps a #5 onto someone's 300th dive. Both parsers
now leave it null and the number comes from `GET /dives/next-number`, derived from the
dive's own date.

`dive_number` stays on `ParsedDiveSchema` rather than being trimmed away under the
"fields the backend models actually support" rule above: it has a direct `Dive` column,
and a format that carries a real lifetime dive number (Subsurface's XML does) can populate
it. It is simply always null for the two Suunto parsers.

## The admin panel is off by default, and refuses to boot insecurely in production

`CRUD_ADMIN_ENABLED` used to default to `True` and `ADMIN_PASSWORD` to the literal
`"!Ch4ng3Th1sP4ssW0rd!"` inherited from the upstream boilerplate, with `main.py` mounting
the panel at `/admin` unconditionally. Unlike `/docs`, which `core/setup.py` disables in
production and gates behind a superuser in staging, there was no `ENVIRONMENT` check at
all. A deployment that set `SECRET_KEY` (startup fails without it) but never thought about
`ADMIN_PASSWORD` therefore served a full create/update/delete interface over `User`,
`Dive`, `GearItem` and everything else in `admin/views.py`, behind a password published in
this repository.

Three changes, in order of how much they matter:

1. **`ADMIN_PASSWORD` has no default** (`str | None = None`). Unset now means "no admin
   account", which `admin/initialize.py` already handled - it just never happened, because
   the field always had a value.
2. **`CRUD_ADMIN_ENABLED` defaults to `False`.** The panel bypasses every ownership check
   in `api/v1` by talking to the models directly, so it should be something an operator
   turns on, not something a fresh deploy inherits. `src/.env.example` enables it for local
   development.
3. **`Settings._reject_insecure_admin_config`** raises at startup when the panel is enabled
   in production with a missing or boilerplate password. Chosen over silently disabling the
   panel: an operator who *meant* to have it wants to know, and a hard failure at boot is
   the loudest, cheapest place to find out. The missing IP allowlist warns rather than
   raising - plenty of deployments front the panel with a VPN, and refusing to start would
   break those for no gain.

## `/auth/refresh` rotates the refresh token instead of reusing it

The endpoint used to mint a new access token and hand the same refresh cookie back. A
refresh token leaked from a browser (an XSS on the frontend, a shared machine, a stolen
backup) therefore stayed usable for its full `REFRESH_TOKEN_EXPIRE_DAYS` - seven days by
default - with nothing to revoke it and no way to notice it was being used.

It now blacklists the presented token and issues a fresh pair via `issue_tokens`, the same
helper every sign-in path uses. The blacklist entry costs one row that
`purge_expired_tokens` already cleans up.

The cost is real and worth stating: **two tabs refreshing at the same instant will race,
and the loser gets a 401.** That is inherent to rotation without a grace window. It's
accepted here because the access token lives 30 minutes, so refreshes are rare enough for
a collision to be unlikely, and the failure mode is a re-login rather than data loss. If it
turns out to bite in practice, the standard fix is a short reuse-detection window (accept a
just-rotated token once more, and treat a *third* use as evidence of theft) rather than
reverting to no rotation.

The token is spent before its replacement is minted, so a crash between the two leaves the
caller signed out rather than holding two live refresh tokens.

That two-tab race is a genuine concurrency loss and is not the same thing as the
same-second token *collision* described in the next section, which looked identical from
the outside (a random logout) but happened with a single tab and had nothing to do with
concurrency.

## Every revocable token carries a `jti`

`/auth/refresh` could hand back a refresh token that was already blacklisted, signing the
user out on their next page load.

The claims separating one token from another were `sub`, `token_type` and `exp` - and JWT
`exp` has one-second resolution. Two tokens minted for the same subject inside the same
wall-clock second therefore encoded to the *same string*. Since revocation stores the token
string itself (`token_blacklist.token`, unique), identical tokens share one blacklist entry,
and `refresh_access_token` blacklists the presented cookie before minting its replacement -
so a collision handed the caller a token that had just been revoked, and the refresh after
that returned 401. Sign in, navigate within the same second, get bounced to /signin.

Nothing about the failure pointed at the cause: it was intermittent by construction (only
when two issuances landed in the same second), the 401 arrived on a *later* request than
the broken one, and the rotation race documented above was a ready-made false explanation.
What settled it was seeing a `/auth/refresh` whose request cookie and `Set-Cookie` response
hashed identically.

`core.security._new_jti` now puts a `uuid4().hex` in every access, refresh and onboarding
token. That is what makes keying the blacklist on the token string a *per-issuance* key -
without it, "revoke this token" silently meant "revoke every token that happens to be
byte-identical to it", which also let `/auth/logout` on one session kill a sibling session
minted in the same second. Anything minted here that should be revocable needs the claim;
dive-file tokens deliberately don't have one, because nothing revokes them by value and two
identical parse receipts are simply the same receipt.

Two things worth knowing:

- **No one is signed out by deploying it.** Nothing reads `jti` back - `verify_token` never
  looks at it - so tokens minted before the claim existed keep verifying until they expire.
- **The blacklist table did not change.** Keying it on a `jti` column instead of the whole
  token would be the tidier schema, but it buys nothing once issuances are unique, and it
  would need a hand-written `ALTER TABLE` (see *"Schema changes have no migration tool"*)
  plus a `jti` on every token type that gets blacklisted.

The frontend carried a workaround for this: `sleepPastTheSecond()` in
`opendiving-web/scripts/screenshots.mjs`, and *"The one-second refresh-token collision"* in
`opendiving-web/DECISIONS.md`. Both can go once this is deployed.

## Blacklist expiries are UTC-aware, and so is the purge that reads them

`core/security` wrote blacklist rows with `datetime.fromtimestamp(exp)` - no tzinfo, so the
JWT's UTC `exp` was rendered in the *host's* local zone and stored into a
`DateTime(timezone=True)` column. `purge_expired_tokens` then compared against a naive
`datetime.now()`. The two errors cancelled out on a UTC host and on any host as long as
both stayed wrong in the same direction, which is why nothing looked broken: a server in
UTC+2 filed every entry two hours late and deleted it two hours early, consistently.

Both are now `UTC`-aware. Worth noting because fixing *either one alone* makes the bug
worse rather than better - the offsets stop cancelling, and revoked tokens get purged while
still valid.

The column itself was the third piece, and it was missed at the time: `token_blacklist.expires_at`
was still plain `DateTime` (the only naive timestamp left in the schema), i.e. Postgres
`TIMESTAMP WITHOUT TIME ZONE`. asyncpg does not silently coerce there - binding an aware
datetime to a naive column raises `DataError: can't subtract offset-naive and offset-aware
datetimes`, so *both* halves of the fix above failed outright: every logout/deletion insert
and every run of `purge_expired_tokens`. It is now `DateTime(timezone=True)` like everything
else.

Any rows already in the table predate the fix and were written in the host's local zone, so
`AT TIME ZONE 'UTC'` reinterprets them off by that offset. That is harmless here and not
worth a smarter `USING`: the table only holds entries until the token they name would have
expired anyway, and the first purge after the change clears them out.

## Pagination bounds live in `core/utils/pagination`, not in each route

`page`/`items_per_page` come off the query string, and three of the eight list endpoints
clamped them while five passed them straight into `crud.get_multi`. `GET /dives?items_per_page=999999999`
was a request for the caller's entire dive log in one response; a negative value reached
the database as a negative LIMIT.

`clamp_pagination` is now called by all eight. It clamps rather than rejecting, so an
existing client asking for too much gets the ceiling instead of a new 422. The three
per-module `MAX_*_PER_PAGE = 100` constants collapsed into one
`DEFAULT_MAX_ITEMS_PER_PAGE`, with the per-call override kept for a resource that ever
needs a different bound.

## Ownership checks go through one `fetch_owned_or_raise`

The "fetch by public uuid, 404 if missing, 403 if it belongs to someone else" block existed
in seven route files: three as differently-named private helpers (`_get_owned_dive`,
`_get_owned_certification`, `_owned_gear_item`) and four inlined three times each. Roughly
fifteen copies of six lines.

That matters more than ordinary duplication because of the invariant the copies each
restated in a comment: **the check must run before any `@cache`-wrapped read**, since
`@cache` serves a hit without re-running authorization. An invariant documented in fifteen
places is one that can be left out of the sixteenth.

`api/dependencies.fetch_owned_or_raise` is now the only implementation. Each route file
keeps a thin per-entity wrapper (`_get_owned_trip`, `_get_owned_gear_set`, ...) so call
sites stay readable and each resource keeps its own not-found wording and return type;
those wrappers are three lines of delegation with no logic.

Deliberately *not* converted, because they answer differently on purpose:

- `gear_service._owned_gear_item` and `gear_sets._resolve_item_ids` answer **422** for both
  "missing" and "not yours". There the uuid is a reference inside a request body rather
  than the resource being addressed, and one answer for both keeps someone else's uuids
  unprobeable.
- `gear_service`'s schedule/record routes scope by `user_id` down in
  `resolve_schedule_for_user`, so there is no separate 403 to make.

`tests/test_ownership.py` asserts no route file reintroduces the inline form.

## `mypy` runs over `tests/`, with `call-arg` disabled there

CI checked only `src`. Pointing it at `tests/` surfaced 109 errors, 108 of them
`[call-arg]` - and all 108 were false.

Every schema in `app/schemas` declares optional fields as
`Annotated[T | None, Field(default=None)]`. Pydantic's mypy plugin only reads defaults out
of the `x: T = Field(default=...)` assignment form, not out of `Annotated`, so it
synthesizes an `__init__` in which those fields are required, and every test that builds a
schema while omitting an optional field looks like a missing argument. (The plugin is
configured and does load; this is a gap in what it handles, not a misconfiguration.
Verified by diffing plugin-on against plugin-off output - identical.)

So `[[tool.mypy.overrides]] module = "tests.*"` disables `call-arg` there, and only there.
`arg-type`, `attr-defined`, `no-any-return` and the rest still apply to the suite;
production code is untouched. Rewriting 108 call sites to pass explicit `None`s would have
been a large diff that made the tests worse to read in order to satisfy a tooling gap. Drop
the override once the plugin understands `Annotated` defaults.

Two mypy invocations, not one: the app is reachable as both `app.*` (via `mypy_path`) and
`src.app.*` (how the tests import it), and `mypy src tests` refuses with "source file found
twice under different module names".


## The runtime image now contains the app, and ships gunicorn

Two things were wrong with the image and neither showed up until it was actually run
rather than merely built.

**The final stage contained no application code.** It copied `/app/.venv` and nothing else,
leaving `WORKDIR /code` empty, so `app.main:app` was unimportable. The container only ever
worked because `docker-compose.yml` bind-mounts `./src/app` over `/code/app` - i.e. the
image ran in local development and nowhere else. This predates the switch to gunicorn: the
old `uvicorn app.main:app --reload` CMD failed identically without the bind mount. The final
stage now `COPY`s the package in; the compose bind mount still overlays it for live editing,
so it is a convenience rather than a requirement.

**`--reload` is gone from the image.** It runs a single worker plus a filesystem watcher and
restarts on any write. `docker-compose.yml` still overrides `command:` with the uvicorn
`--reload` form, so local development is unchanged.

## Two things raced once the image ran four workers

Switching the image to `gunicorn -w 4` turned a class of latent bug into a crash loop: the
FastAPI lifespan runs once *per worker*, and it was doing two things that are deployment
steps, not per-process steps. Both are fixed; recording them because the shape recurs.

**Admin panel setup.** `admin.initialize()` (create the panel's tables, seed the initial
admin) ran in a custom lifespan in `main.py`. Four workers raced it, and the losers died
with `table admin_user already exists` or
`UNIQUE constraint failed: admin_user.username`, taking the container with them. It is now
a one-shot, `src/scripts/initialize_admin.py`, wired into `docker-compose.yml` as the
`admin_init` service that `api` waits on via `service_completed_successfully` - so local
development still needs no manual step. Constructing `CRUDAdmin` still registers all its
routes (`__init__` calls the synchronous `setup()`), so every worker can mount the panel
without any of them touching the database.

**`Base.metadata.create_all()`.** The same per-worker problem, and not admin-specific at
all: on a *cold* database four workers call `create_all` at once, and `checkfirst=True` does
not save you - it inspects the catalog and then issues `CREATE TABLE`, so two workers can
both look, both see nothing, and both try. The loser dies with
`duplicate key value violates unique constraint "pg_type_typname_nsp_index"`. It is now
serialized behind a transaction-scoped Postgres advisory lock in `core/setup.create_tables`,
which makes the check-and-create pair atomic across processes. Deliberately kept in the
lifespan rather than moved to a one-shot, so the `docker compose restart api` workflow at the
top of this file still picks up brand-new tables. Only ever contended on a cold database.

Note this was invisible on a warm database - the earlier multi-worker test passed simply
because the tables already existed. It only reproduced against a freshly created one.

## Running the admin panel on more than one worker

Two pieces of the panel's state are per-process. Verified with 4 workers against Postgres:

- **Its tables** (`admin_user`, `admin_session`, ...). `CRUD_ADMIN_DB_URL` unset means a
  SQLite file inside one container's filesystem, which is both raced by sibling workers and
  invisible to any other container - including the `admin_init` one-shot, which would then
  helpfully initialize a database nobody reads. Point it at the app's Postgres. **Required**
  for multi-worker.
- **Its sessions.** These need a store shared by all workers. `CRUD_ADMIN_TRACK_SESSIONS`
  defaults to `true` and persists them to the `admin_session` table, which already satisfies
  this - so there is nothing to configure, only something not to switch off.
  `CRUD_ADMIN_REDIS_ENABLED=true` is an equivalent alternative, worth it to keep session
  lookups off Postgres, but it is **not** required.

Measured, 12 authenticated requests each: Redis sessions 12/12, DB-tracked sessions 12/12,
both disabled **1/12** - the single request that happened to land on the worker holding the
in-memory session, the rest bounced to `/admin/login?error=Session+expired`.

Two traps when testing this by hand, both of which produced false passes first time round:
`GET /admin` returns a redirect to `/admin/` whether or not you are signed in, so an HTTP
client that follows redirects reports the login page's `200` and looks like success - probe
`/admin/` and don't follow redirects. And `SESSION_SECURE_COOKIES` defaults to `true`, so
over plain HTTP a well-behaved client stores the session cookie and then never sends it,
making every configuration look broken.

## Rate limiting fails open on a Redis *outage*, not just a missing client

`enforce_rate_limit` has always documented that it degrades rather than hard-fails when
Redis isn't available - throttling here is defense-in-depth, not the primary security
boundary, so an outage should not take sign-in down. The check it did was
`if cache.client is None`, and that check is nearly never true.

`create_redis_cache_pool` builds the client with `redis.Redis.from_pool`, which connects
**lazily**. In any shipped configuration `cache.client` is therefore a perfectly ordinary
object whether or not Redis is up; `None` only ever means "the pool was never created",
i.e. unit tests. A real outage instead surfaced as `ConnectionError` raised from `incr`,
which propagated out of every rate-limited endpoint - so a Redis blip turned the entire
auth surface into 500s, which is precisely the failure mode the fail-open branch existed
to prevent.

Measured in a container against a stopped Redis: six `POST /auth/email/request` calls
returned `500, 500, 500, 500, 500, 500` before, and `200 x6` after.

The handler wraps only the `incr`/`expire` pair. `RateLimitException` is raised outside it
on purpose - that is the function's normal signal, not a Redis failure, and a `try` drawn
one line wider would swallow it and disable rate limiting completely.

The warning fires on *entering* the degraded state and is re-armed on recovery, rather
than once per process: during an outage the throttled endpoints are exactly the ones being
hammered, so per-call logging buries the signal, but a latch that never resets means a
second outage hours later passes silently.

The old test suite only ever exercised `client = None`, which is why none of this showed
up. `tests/test_rate_limit.py::TestRedisIsDown` now simulates the connection failure.

## Per-IP rate limits need to know which proxy to believe

`request.client.host` is the socket peer. Behind the bundled nginx - or any load balancer -
that is the proxy's address, identically for every caller, so all the per-IP buckets
collapse into one global bucket. One bot exhausting the magic-link limit locked sign-in for
the whole instance. Nothing in the repo set `FORWARDED_ALLOW_IPS` or uvicorn's
`--proxy-headers`, and nginx's `X-Forwarded-For` was written but never read.

Trusting `X-Forwarded-For` unconditionally would be worse than the bug: the header is
caller-supplied, so on a directly-reachable deployment anyone could forge a fresh identity
per request and skip the limits entirely. `core.utils.client_ip` therefore consults it only
when the socket peer is a proxy the operator explicitly declared in `TRUSTED_PROXY_IPS`, and
takes the **right-most** entry that isn't itself one of those proxies - a client can prepend
anything it likes, so only the hops appended by trusted infrastructure, at the end, mean
anything. Unset (the default) is exactly the old socket-peer behaviour.

Verified end-to-end through the real nginx config: with the proxy trusted, six callers get
six separate buckets and a bot burning its own 15 leaves other callers at `200`. With no
trusted proxy, eighteen requests carrying eighteen forged `X-Forwarded-For` values produced
a **single** bucket keyed on the real peer, still capped at 15.

`TRUSTED_PROXY_IPS` is a comma-separated *string*, not a `list[str]`. For a complex field
type pydantic-settings parses the environment variable itself and expects JSON, so
`TRUSTED_PROXY_IPS=172.16.0.0/12` failed validation at startup regardless of the `cast=`
passed to `config()` - which only produces the default. `CRUD_ADMIN_ALLOWED_IPS_LIST` above
it is a bare annotation with no `config()` call for the same reason. Unit tests that patch
the attribute directly cannot catch this; `TestTheSettingParsesFromTheEnvironment` goes
through `Settings`.

## `ClientCacheMiddleware` inferred "not user-specific" from the wrong signal

It decided whether a response was publicly cacheable purely from whether the *request*
carried an `Authorization` header. That is backwards for the endpoints that mint
credentials: `POST /auth/email/verify`, `/auth/google`, `/auth/complete` and `/auth/refresh`
are unauthenticated by nature - the caller has no token yet, that is the whole point - and
each returns one in the body. All four were being labelled `public, max-age=60`.

`public` now additionally requires a safe method (GET/HEAD). That covers the four POSTs, but
a safe method is not sufficient on its own: `GET /auth/email/verify/check` and
`GET /user/email-change/verify/check` are anonymous, side-effect-free GETs that take a
single-use token in the query string and return the account's email address. Those set
`Cache-Control: private, no-store` themselves, which the middleware's existing
"never overwrite an explicit header" rule preserves.

Verified against a running container: all six now return `private, no-store`, while
`GET /api/v1/health` still returns `public, max-age=60` - the fix is targeted, not a blanket
disable of client caching.

## `.dockerignore`, or: `.gitignore` does not apply to Docker

The builder stage does `COPY . /app` and there was no `.dockerignore`, so the entire working
tree went into an image layer - including the developer's real `src/.env`. Confirmed by
inspection: `SECRET_KEY`, `POSTGRES_PASSWORD` and `ADMIN_PASSWORD` were present in the
builder layer, and the `test` stage inherits from `builder`, which makes them part of a
distributable artifact. A file can be correctly untracked by git and still be baked into
every image.

This became sharper when the runtime stage started copying the app package in (see above):
`src/app/logs/app.log` came with it. With `RESEND_API_KEY` unset - the documented
local-development setup - that file contains magic-link URLs with live sign-in tokens. The
copy that happened to be on disk had none, which was luck rather than design.

`.dockerignore` now excludes `.env` (but not `.env.example`), logs, `__pycache__`, the local
venv and the tool caches. Verified after the change: no `.env` in the builder layer, no logs
or `__pycache__` anywhere in the runtime image, `.env.example` still present.

## `GET /certifications-expiring` mirrors `/gear-service-due`, deliberately

The dashboard's renewal card needs the few certifications with expiry dates on them. It
used to get there by paging the diver's *entire* certification list client-side, because
an expiry is just as likely to sit on the oldest card as the newest and `GET
/certifications` sorts by neither. Gear had already solved the same problem with
`/gear-service-due`; certifications simply had no equivalent.

This one is built to match, including the part that looks like an omission: **it takes no
`within_days` parameter.** A server-side horizon would bake today's date into a response
cached for 60 seconds, which then goes quietly wrong at midnight. With no date input the
response is a pure function of stored rows, so it can be cached safely and the client
buckets it into expiring-soon/expired itself - the same trade `GearServiceDueResponse`
documents.

Two ways it deliberately differs from its gear twin:

* **Undated cards are excluded by the query**, not sorted last. Most recreational
  certifications never expire, so for a typical diver that is most of the list, and none
  of them can ever appear on a renewals card.
* **It carries no card-file metadata.** The renewal card renders a name, an agency and a
  date; embedding `files` would mean the batched file lookup `_cached_read_certifications`
  does, for something nothing on that card shows.

The cache key is `user_{id}_certifications_expiring`, which starts with
`user_{id}_certification` - so `invalidate_certification_caches` already sweeps it and no
new invalidation call was needed. That prefix overlap is load-bearing; renaming the key to
something outside it would leave a stale renewals list after every card edit.

## The dashboard overviews say when they are truncated

`GearServiceDueResponse` and `CertificationExpiringResponse` both return every matching
row rather than a date-filtered slice, so both need a row cap (`DUE_OVERVIEW_LIMIT` /
`EXPIRING_OVERVIEW_LIMIT`, both 200). The gear one had the cap but no way to say it had
been hit, so a diver past it saw a dashboard card that looked complete while some overdue
kit simply wasn't in it. For a safety-adjacent card that is the wrong direction to fail
in, so both now carry a `truncated` flag and the web client says the list is partial.

Both queries select `limit + 1` rows and drop the extra, so `truncated` is *exact*.
Comparing `len(rows) == limit` instead would report a list that happens to end on the
boundary as truncated, which is the kind of false alarm that gets ignored.

`truncated` defaults to `False` so an older client - or a cached response written before
the field existed - doesn't read as "the list is partial".

## `DiveUpdate` refuses an explicit null for a `NOT NULL` column

Every field on `DiveUpdate` is typed `T | None`, because that is how "omit it to leave it
alone" is spelled in a PATCH body. But four of them - `dive_number`, `start_time`,
`duration`, `notes` - map to `NOT NULL` columns, so an explicit `null` is a different
thing entirely and the database refuses it.

It used to be refused all the way down at the driver. The null survived `exclude_unset`,
reached Postgres, and the `IntegrityError` came back through `_fk_error_detail` as a 422
reading **"Invalid reference: a related record does not exist."** - a foreign-key message
for a not-null problem, which is close to the least helpful thing it could have said.

`start_time` was worse than misleading. `patch_dive`'s guard was `if values.start_time is
not None`, so an explicit null *skipped* the `split_start_time` branch, still reached the
database from `model_dump`, and left `utc_offset_minutes` describing the previous start
time - a wrong-but-plausible offset on a dive, not an error.

A `model_validator` on `DiveUpdate` now rejects those four with a message naming the
field, and `patch_dive`'s `start_time` branch is keyed off `model_fields_set` to match the
`trip_uuid` branch beside it. The nullable fields are untouched: clearing `max_depth` back
to "not recorded" is a real operation, and `trip_uuid: null` is the *only* way to detach a
dive from its trip.

No client was sending these nulls, so this was latent - but the contract advertised them,
and the web client has since learned that "explicit null clears a field" from the
`trip_uuid` fix. That is exactly the assumption that would have walked into it.

## The `trip_uuid` detach path has a test now

`PATCH /dive/{uuid}` detaching a dive from its trip depends entirely on

    if "trip_uuid" in values.model_fields_set:
        if values.trip_uuid is None:
            update_data["trip_id"] = None

and `grep trip_uuid tests/` was empty. The web client's "remove from trip" is built on it,
and without the branch the request succeeds, reports "Dive updated", and changes nothing -
a silent no-op, the worst shape for a bug to have.

`tests/test_dive_update.py` pins both halves: an explicit null detaches, an omitted key
leaves the trip alone. Both were checked by deleting the branch and confirming the first
fails. It needs no database - `patch_dive`'s collaborators are stubbed and the assertions
are on the `update_data` handed to `crud_dives.update`.

## Test users get uuid-derived names, because nothing cleans them up

`create_user` writes a real row to whatever database `POSTGRES_SERVER` points at - in
practice the developer's own - and nothing deletes it afterwards. The
`docker-compose.test.yml` overlay does the same. So the `user` table accumulates: one
machine had 919 rows from previous runs.

`fake.user_name()`/`fake.email()` draw from a small vocabulary, and both columns are
`unique=True`. Past a few hundred rows the birthday problem catches up and runs start
failing with an `IntegrityError` **in fixture setup** - intermittently, at roughly two
runs in five, and reading like a broken test rather than a broken fixture.
`fake.unique` does not help: it de-duplicates within one process, not against rows
already in the table.

`unique_username()`/`unique_email()` in `conftest.py` derive from uuid7 instead, so they
are unique across runs and machines rather than merely unlikely to repeat. They are also
shaped to satisfy the app's own rule for a username (`^[a-z0-9]+$`, 2-20 characters) -
the ORM does not enforce it, but a fixture writing values the API would reject is a trap
for whoever next asserts on one.

The address is `@example.com`, not something under `.test`. Both are reserved by RFC 2606
and neither reaches a real inbox, but `email-validator` - which backs Pydantic's
`EmailStr` - rejects `.test` as a special-use name, so any test round-tripping such an
address through a schema fails validation. That cost one test
(`test_rejects_unchanged_email`) before it was spotted.

This stops the bleeding; it does not tidy up. Rows already in the table stay until
someone runs `docker compose down -v`. The real fix is fixtures that roll back what they
write, which is a larger change than this was.

## A session's subject is the user's `uuid`, because a username can change hands

`issue_tokens` minted both tokens with `data={"sub": username}`, and `get_current_user`
resolved that string back to an account by looking the username up. Nothing else in an
access or refresh token identified anyone: `create_access_token`/`create_refresh_token`
add only `exp` and `token_type`, and `verify_token` does no database lookup at all. So
that one username lookup was the root of the entire ownership model - `fetch_owned_or_raise`
compares against the `id` it returns.

A username is not a stable identifier. `PATCH /user` changes it and releases the old one
the same instant, with no reservation and no cooldown, so the subject of a live token can
come to name a *different* account than the one it was issued for. Three consequences,
in ascending order of severity:

1. **The renaming user is signed out permanently.** Their own tokens name a username that
   no longer resolves, so every request 401s - and `/auth/refresh` keeps re-minting the
   dead subject rather than breaking, because it passes the subject through without ever
   checking that it still resolves to a live user.
2. **The reverse direction.** Claim a username the moment its holder renames away, and
   their still-valid tokens now authenticate as *your* account - their subsequent writes
   land in your logbook.
3. **Dormant takeover.** Sign in as a desirable username, rename away to free it, then
   call `/auth/refresh` weekly to keep a token with that subject alive indefinitely (it
   costs nothing - refresh never asks whether the subject exists). When a real user later
   claims that username at `/auth/complete`, the dormant token arms itself and reads and
   writes their account. No victim interaction, no timing window, and it seeds cheaply
   across as many handles as you like.

The subject is now `str(user.uuid)` - the `uuid7` from `PublicUUIDMixin`, which is
immutable, already unique-indexed, and already the resource's public identity everywhere
else in the API. `get_current_user` is a single `crud_users.get(uuid=..., is_deleted=False)`,
and `TokenData.user_uuid` is typed `uuid.UUID` so the assumption can't quietly revert to a
string that happens to hold a name.

`verify_token` catches `ValueError` alongside `JWTError`, because `uuid.UUID("someuser")`
raises it: without that, every token minted before this change - and every forged `sub` -
would be a 500 rather than a 401. Those older tokens are all invalidated by the cutover,
which is why this was worth doing pre-launch rather than after.

The tempting smaller fix - blacklist the caller's tokens inside `patch_user`, mirroring
`erase_user` - is **not** sufficient. It closes (1) and (2), but not (3): the attacker's
orphaned refresh token lives in a separate cookie jar and is simply never presented on the
rename request, so there is nothing to blacklist. Only a subject that cannot change hands
closes it.

`PATCH /user` also gained a per-user rate limit on the username branch specifically. Its
"Username not available" is the same availability oracle `/auth/complete` is already
throttled for (see `AUTH_COMPLETE_RATE_LIMIT_PER_IP` and that route's docstring), and a
signed-in caller could walk a wordlist through it without even needing a fresh onboarding
token. It's scoped to the branch that actually answers the question - throttling the whole
endpoint would 429 a settings page toggling `gear_service_emails`, which reveals nothing.

## Non-ASCII filenames need RFC 6266, because Starlette encodes headers as latin-1

`safe_filename` keeps printable non-ASCII on purpose - `original_filename` is also what
the clients display, so a diver who names a file in Japanese should see it back. But the
download routes used to interpolate that stored name straight into the header:

```python
"Content-Disposition": f'attachment; filename="{file.original_filename}"',
```

Starlette encodes every header value as latin-1 while building the response, so anything
above U+00FF raises `UnicodeEncodeError` *before* a single byte is sent. That is not a
garbled filename, it is a 500 - and a permanent one, on every subsequent
`GET /certification/{uuid}/file/{side}` or `GET /dive/{uuid}/file` for that row, because
the name causing it is stored. The upload, the metadata reads and the card thumbnail all
keep working; only the download breaks.

Three things conspire to hide it:

- latin-1 covers the *accented Latin* range, so "café.jpg" encodes fine and merely arrives
  as mojibake. Only CJK, Cyrillic, Greek, Hebrew and emoji actually raise. Testing with
  European names finds nothing.
- The `If-None-Match` branch returns before the header is built, so a client that already
  has the file keeps re-validating happily while a fresh one 500s.
- The web client never reads the header; `downloadBlob` names the saved file from
  `original_filename` in the JSON metadata. So once the 500 is fixed, the header's
  contents only show up in a bare `curl` - which is precisely why the ASCII fallback
  below is worth getting right rather than leaving as a placeholder.

`content_disposition_attachment` (`core/utils/uploads.py`) now builds both parameters RFC
6266 defines:

```
attachment; filename="card.jpg"; filename*=UTF-8''%E6%BD%9C%E6%B0%B4.jpg
```

`filename*` carries the real name percent-encoded as UTF-8 for anything that understands
it, which is every current browser; the plain `filename` carries an ASCII folding for
anything that doesn't, `curl -OJ` being the one that matters. `quote(name, safe="")`
escapes everything outside the unreserved set, so its output is always ASCII and always
inside RFC 5987's `attr-char`.

The alternative - ASCII-folding inside `safe_filename` at upload time - was rejected
twice over. It would show every non-Latin diver a mangled version of their own filename in
the list rows, the source-file card and the download dialog, and it would do nothing for
the rows already stored, which are exactly the ones that 500. Only the header has to be
narrow, so only the header is narrowed.

Two details in `_ascii_fallback` that look like padding and aren't:

- The folded name goes back through `safe_filename`. NFKD maps fullwidth punctuation onto
  its ASCII twin (`＂` → `"`, `／` → `/`), so folding *re-introduces* the characters
  `safe_filename` had already stripped - and a `"` there would close the quoted string
  early.
- A name with no ASCII in it at all folds to a bare extension, so `default` supplies the
  stem: "潜水.jpg" downloads as "card.jpg" rather than as the dotfile ".jpg".

## Dives-per-month is counted in Python, off two columns, not by `date_trunc`

`GET /user/dive-activity` returns one `{year, month, dives}` per month a user actually
dived in, oldest first. The obvious implementation is a `GROUP BY date_trunc('month',
start_time + make_interval(mins => utc_offset_minutes))`, which would return the same rows
without pulling any across the wire. It's deliberately not that.

**The offset arithmetic has exactly one home.** `core/utils/datetime_offset.py` says so in
its module docstring, and `combine_start_time` is what `_to_public_dive` and
`gas_use_history` already reconstruct a dive's local time with. A `date_trunc` over
`start_time + utc_offset_minutes` is a second copy of that rule in another language, and
the failure mode when the two drift isn't an error - it's a chart that quietly disagrees
by one month with the dive pages it was built from, for the divers whose trips cross a
date boundary. The same trade `gas_use_history` makes with `compute_gas_use`, for the same
reason, at the same cost: two small columns per dive on a cached endpoint.

**The month is the dive's own local one.** A dive that began at 00:30 on the 1st of May in
Bangkok (+07:00) is 17:30 on the 30th of April as an instant, and counting the stored
instant files it under April. That is the rule "a dive displays in the timezone it was
logged in" extended from formatting to bucketing - the server-side twin of the note the
web app's `diveWallClockTime` carries.

**Months with no diving are absent, not zeroed.** The client draws a fixed grid - twelve
months, or every year between the first dive and the last - and has to fill its own gaps
regardless, so sending empty buckets would be padding one shape into a different one. It
also keeps the response proportional to the diving rather than to the calendar: a diver
who logged one dive in 2014 and came back in 2026 gets two rows, not 145.

**The result is sorted on the buckets, not left in query order.** `ORDER BY start_time` is
chronological by *instant*, and the two facts above mean that isn't the same as
chronological by month: the Bangkok dive above is an earlier instant than a London dive at
20:00 on the 30th of April, and they belong to different months. Sorting the counted
buckets is the only place that can be fixed.

**Cached under `user_{id}_dives:dive_activity`**, the same prefix as the gas series and for
the same reason: `invalidate_dive_caches()` already sweeps `user_{id}_dives:*` after every
dive create, update and delete, so this series drops with them and needed no invalidation
change at all. A key of its own would be a third pattern to remember to add there, and the
bug from forgetting is a chart that silently keeps showing last week's diving.

## FIT is one parser for both vendors, and its one real trap is developer fields

`FitParser` (`services/dive_parsers/fit.py`) reads ANT/Garmin FIT activity files - what a
Garmin Descent produces and what a Suunto Ocean or D5 exports natively - and it is
deliberately **one** parser rather than one per manufacturer. FIT is self-describing: the
global profile fixes field numbers, units and scale factors, so `session.max_depth` is
`uint32` scaled by 1000 whoever wrote it. `fitdecode` applies that profile and hands back
meters, Celsius, bar and whole percent, which is the opposite of the situation the two
Suunto parsers are in (Pascal here, millibar there, Kelvin over there). Vendor differences
are additive, not contradictory: Garmin also writes `dive_summary` and
`tank_update`/`tank_summary`, Suunto writes neither. Both are read where present.

`fitdecode` was chosen over `fitparse` and Garmin's own SDK because it is MIT, pure Python
with no dependencies of its own, and - unlike `fitparse` - decodes developer fields, which
the next paragraph makes non-negotiable. `export/parse-fit.py` had already settled on it.

**Never build a `{field.name: field.value}` dict from `frame.fields`.** Suunto's exporter
declares developer fields whose names collide with native profile fields, so a Suunto
`session` carries **two** `max_depth` values: the native `uint32`/scale-1000 one (exactly
32.41) and a `float32` developer duplicate (32.40999984741211). A dict comprehension keeps
whichever came last, which is the lossy one - and that is precisely what the
`export/parse-fit.py` prototype did, so the noise was visible in its output from the
start. `_native_value()` walks the fields and skips anything that is a `fitdecode.types.
DevField`.

`fitdecode`'s own `get_value()` returns the right one *when both are present*, but only as
a side effect of taking the first positional match: a definition record carries native
field definitions ahead of developer ones, so a valid FIT file cannot order them the other
way round, and no test can construct one that does. Where the two genuinely differ is a
message carrying **only** the developer duplicate - `get_value` then hands back a vendor's
`float32` as though it were the profile's scaled `uint32`, units and semantics included,
while `_native_value` answers `None` and lets the caller fall back to a field that means
what it says. That is the case worth pinning, and
`test_a_developer_field_never_stands_in_for_a_missing_native_one` is the test that
distinguishes the two implementations;
`test_prefers_the_native_field_over_a_developer_field_of_the_same_name` covers the
dict-comprehension bug the prototype had but would pass against `get_value` too.

**`start_time` carries the dive's real local offset, reconstructed from
`activity.local_timestamp`.** Every timestamp in a FIT file is UTC, and `local_timestamp`
on the `activity` message is that same instant written as local wall-clock time - so the
gap between the two *is* the UTC offset at the dive site, and nothing else in the file
records it. This matters more than it looks: `DiveStartTime` rejects a naive datetime, and
the frontend's `normalizeParsedStartTime` keeps whatever offset a parse supplies. Handing
back plain UTC would therefore be *accepted* and would silently file an 11:16 Red Sea dive
as 09:16. The offset is rounded to whole minutes and rejected past ±14 h - `timezone()`
raises beyond ±24 h, which a corrupt file would otherwise turn into a 500.

**`bottom_temperature` never reads `max_temperature`, though that is the field Suunto
fills in.** Both Ocean exports in the corpus hold 22 °C in `session.max_temperature` while
their samples run 22-25 °C: Suunto writes the *coldest* reading into a field named for the
warmest. Reading it as a maximum would be wrong; reading it as a minimum would bake one
vendor's bug into a shared parser. So `session.min_temperature` is used when present (a
Garmin populates it), and otherwise the coldest `record` sample - ground truth, and it
returns the same 22 °C on those files.

**`dive_number` is not imported, same as both Suunto parsers.** `session.dive_number`
counts dives on *that device* and restarts at 1 after a factory reset or a new computer.
The corpus settles it outright: a D5 export reporting `dive_number` 5 carries the diver's
own label for the same dive in `session.description` - "#28: Elphinstone Reef". The number
comes from `GET /dives/next-number` instead.

**A non-diving FIT file is a `DiveParseError`, not an `UnsupportedDiveFileError`** - a
deliberate departure from the Suunto parsers. `UnsupportedDiveFileError` means "not my
format, let the next parser try", and with no other FIT parser registered it surfaces as a
415 "no parser available for this file" about a format that is very much supported. A bike
ride *is* a FIT file this parser read successfully; it just holds no dive, and a 422 that
says so is the more useful answer. A session with no `sport` is still accepted when the
file carried depth samples, which is the stronger evidence anyway.

**Tank pressures come from `tank_summary` if the device wrote one, otherwise off the ends
of the `tank_update` telemetry.** A Descent streams `tank_update` throughout the dive
whether or not it also emits a summary, so taking the first and last reading per pod is
better than dropping the transmitter data. Readings are ordered by their own timestamps,
not by arrival, since separate pods interleave.

The two sources are joined **per cylinder and per field**, not one branch or the other. An
earlier version fell through per branch - "if there are any summaries at all, ignore the
telemetry" - which keyed off a frame existing rather than that frame carrying numbers. The
realistic failure is the partial one: a pod that drops out near the end writes a summary
with `start_pressure` set and `end_pressure` null, and the last real reading, the one the
whole SAC/RMV turns on, was discarded in favour of that null. Transmitter dropout is
routine rather than hypothetical - see the 224-of-441 figure in the DM5 section above. The
join is exact rather than positional because both messages carry the pod's ANT `sensor`
id, so unlike tanks-to-gases below there is a real key to join on.

Summaries are deduped by `sensor` first, keeping the last. A device that writes the summary
twice for one pod would otherwise count as two cylinders, and the exact-count rule below
then discards every pressure in the file - one repeated frame losing a real 207 -> 62 bar
and the dive's RMV with it. A summary with no `sensor` cannot be joined to anything and
stands as its own cylinder, unless it carries no pressures either - one that describes
nothing is dropped rather than inflating the count past the gas list.

**A file with tank telemetry and no `dive_gas` at all still yields cylinders.** Mixtures
were built only from `dive_gas`, so a Descent dive logged in gauge mode - which writes no
gas list, while a paired pod reports throughout - had nothing to hang its pressures on and
discarded every reading. That contradicted the rule the Ocean JSON path follows in the same
breath: evidence of a tank is evidence of a tank, whichever way round it arrived. Such a
mixture carries pressures and nothing else, `oxygen`/`helium`/`volume` all `None`.

They are then **paired to gases by position, and only when the counts match.** Nothing in
the format links the two: tank telemetry is keyed by the transmitter's ANT id and a
`dive_gas` by its `message_index`. Position is the only available signal, so two gases and
one pod leaves every pressure null rather than guessing - these feed `compute_gas_use`,
and a confidently wrong start pressure yields a plausible, wrong RMV, which is worse than
an empty field the diver can fill in. For the same "a serial is not a label" reason the
XML parser refuses `<TransmitterId>`, profile pressure series are labelled 1, 2, … in
first-seen order rather than by `sensor`.

**Suunto's FIT export contains no transmitter data at all**, which is a vendor limitation
and not something the parser can work around. Dive `69e21526bf486d396e2786b5` exists in
the corpus as *both* a `.fit` and a `.json`: the JSON carries 419 `Cylinders[].Pressure`
readings, and the FIT has zero pressure-shaped fields anywhere - no `tank_update`, no
`tank_summary`, and a `dive_gas` holding only oxygen/helium/status. The two exports are
complementary rather than redundant, and it is worth knowing which way round: **the FIT
has the gas mixes** (that dive's 21 % and 54 %, which its JSON twin omits entirely) **and
the JSON has the pressures.** So the `tank_update`/`tank_summary` paths above are written
from the FIT profile's own unit definitions and remain the one part of this parser not
confirmed against a real file; a Descent Mk2i/Mk3i export would close that.

## Cylinder reconstruction is best-effort, and never fails an import

`_mixtures_from_cylinders` runs for **every** Suunto JSON export with no `Gases` block,
including ones that have no cylinder data and never did, and it walks a sample stream whose
shape is barely documented. Written unguarded, any structural surprise in there turned a
previously fine import into a 422: an export pairing a naive `Header.DateTime` with
offset-aware sample timestamps failed on `moment > dive_end` *before a single cylinder was
inspected*, and a `Cylinders[]` entry missing `GasNumber` failed on the key lookup.

Two changes, and the order matters. Both causes are fixed at source - timestamps are
compared only when both sides agree on tz-awareness, and a reading with no gas number is
skipped rather than indexed - and the call is *additionally* wrapped so the whole thing
degrades to "no mixtures" and logs. The wrapper is not the fix; it is the acknowledgement
that this is enrichment layered onto a format we do not control, while the header fields
are what the diver actually came for.

## Uploaded files are parsed in a thread, not on the event loop

`POST /dive/parse` and `PUT /dive/{uuid}/file` both hand their bytes to
`run_in_threadpool`. Parsing is pure CPU with nothing awaited inside it, and the FIT
decoder is pure Python: ~2 s per MB of densely-encoded FIT, against ~0.07 s for a 2.8 MB
Suunto JSON export through the C-accelerated `json` module - two orders of magnitude more
CPU per byte. Inline in an `async def`, a single large upload would stall every other
request on that worker. The XML and JSON parsers went the same way rather than being
special-cased: they are the same shape of work, just faster today.

**A thread is not a bound, though, and the file size cap wasn't one either.** This section
originally sized the worst case from a 500 KB file at ~0.6 s, extrapolating to ~6 s at
`MAX_DIVE_FILE_SIZE`. That was measured on a sparsely-encoded file and under-counted:
a device writes *one* definition record followed by a long run of bare 10-byte `record`
messages, so a 5 MB file holds ~524 000 of them and takes **~10 s** to decode - and the
two-step import pays it twice, once at `/dive/parse` and once at `PUT /dive/{uuid}/file`.
`run_in_threadpool` keeps the event loop free but AnyIO's default limiter is 40 threads,
so 40 such uploads saturate the pool and everything else queues behind them. It needs
authentication, so it is not an open DoS - but one diver with a long, high-rate log could
do it by accident.

`_MAX_FRAMES` (100 000) is the actual bound, and it is on **frames decoded**, not samples
collected. That distinction is the whole fix: of the ~10 s, bare decoding is ~8 s and
collecting the samples is under 1 s, so capping what `_collect_record` keeps would have
saved about a fifth of the cost and left the rest unbounded. Stopping the decode holds the
worst case to ~1.7 s regardless of what the file contains.

It **raises** rather than truncating. A FIT file's `session` is written after the samples
it summarizes, so keeping the first 100 000 frames and stopping would discard the start
time, duration and depths, and import a confidently empty dive. The cap is ~23x the largest
real file in the corpus - a 72-minute multi-channel Suunto Ocean dive at 4 339 frames,
about one per second - or roughly 28 hours of continuous logging.

## The 2026 Suunto Ocean JSON is a third header shape, with gas data only in the samples

`SuuntoJsonParser` was built against two header shapes - a "clean"/header-only one and a
D5-style one nesting gas mixtures under `Header.Diving.Gases`. The 2026 Suunto Ocean
export is a third: it has **no `Header.Diving` block at all**, so the `Gases` path found
nothing and every dive imported from one came back with `mixtures: []` - despite the file
carrying several hundred transmitter readings. Across the whole 2026 corpus,
`hasDiving` is false on every file and the string `Oxygen` does not appear in any of them.

What the Ocean does record is `Samples[].Cylinders[]`: `GasNumber`, `Pressure` (Pascal),
`GasTime` and `Ventilation`. Those readings are the entire point of owning a transmitter
and are what `compute_gas_use` needs, so `_mixtures_from_cylinders` reconstructs a mixture
per cylinder that actually reported, taking `start_pressure`/`end_pressure` from its first
and last reading. On a real file that turns "no mixtures" into 205.11 → 91.55 bar.

Three details this has to get right:

- **Ordered by the sample's own timestamp, not by array position.** The union of an
  Ocean's sample timestamps is not monotonic (adjacent entries go backwards by up to
  0.7 s, because separate sensor streams are appended out of order), so the last entry in
  the array is not reliably the last reading of the dive. Same fact that forces
  `_parse_samples` to sort each channel independently.
- **A `null` `Pressure` is skipped, never treated as a reading or as the end of one.** An
  Ocean reports five cylinder slots on every sample with only one paired, and its final
  samples null out even the live slot - reading those as the end pressure would report a
  dive that finished on an empty tank.
- **Only the pressures are real, and nothing else is invented.** The export records no gas
  fraction and no tank size anywhere, so `oxygen`/`helium`/`volume` come back `None` - see
  the next section.

`Gases` still wins wherever an export has one: it carries the gas fraction and the tank
size that telemetry alone cannot, so the sample-derived path is a fallback for an empty
list, not a merge. The D5 exports are unaffected.

**The cylinder list comes from `DiveEvents.GasSwitch`, not from which tanks
transmitted** - and getting that round the right way is what makes this safe on a
multi-gas dive. A `Samples[].DiveEvents` entry of `{"GasSwitch": {"GasNumber": 1}}` is
the only record the export keeps of *which* cylinders were on the dive, and it is keyed
by the same gas number as `Cylinders[]`, so a transmitter reading is attributable to a
named cylinder rather than to "whichever tank this was".

Listing only the tanks that transmitted would have been actively dangerous. A two-tank
dive would produce exactly one mixture carrying both pressures - which is precisely the
shape `compute_gas_use` derives an RMV from ("exactly one mixture, an average depth, both
pressures") - so a stage bottle's pressure drop would have been silently charged to the
whole dive. Reading the switches instead means a two-gas dive yields two cylinders, the
pressures land on the one that reported them, and the RMV correctly declines to compute.
Across the 19-dive Ocean corpus this finds 4 multi-gas dives, and on the one that also has
a FIT twin the two formats now agree on the cylinder count.

**Readings from after the dive ended are dropped, bounded by `Header.DiveTime`.** The
transmitter keeps reporting while the computer logs on the surface, so the file's last
reading is whatever the tank read once the diver purged the regulator to break down their
kit - one dive records `DiveTime` 3 888 s against `Duration` 4 231 s, and that gap is the
boat. Two dives in the corpus end on a purge, and taking the final reading gave them an end
pressure of **0.14 bar** instead of 53 and 76: a diver who breathed their cylinder dry, and
an RMV to match. The bound moves every other dive by under 2 bar (surface breathing before
derigging). Deliberately not falling back to `Duration` when `DiveTime` is absent -
bounding a window by its own full length is not a bound.

The *profile* pressure series is deliberately left unbounded and still shows the purge as a
cliff at the end. That is what the sensor reported, and the same reasoning keeps the XML
parser on raw `Temperature` rather than `AveragedTemperature`: trimming is a chart decision
that shouldn't be baked into storage. Only the mixture pressures are bounded, because only
they feed an RMV.

**What still cannot be recovered is the gas *mix*.** A cylinder's presence and pressures
survive; what was in it does not. `Header.Settings` holds no gas configuration and the
string `Oxygen` appears nowhere in any 2026 file, so the two Suunto Ocean exports remain
complementary. The same dive, `69e21526bf486d396e2786b5`, imported both ways:

| | cylinders | gas mixes | tank pressures |
| --- | --- | --- | --- |
| Ocean **FIT** | 2 | 21 % and 54 % | none - Suunto's FIT export carries no transmitter data |
| Ocean **JSON** | 2 | none recorded anywhere | 211.62 → 127.16 bar on the transmitting one |

A multi-gas diver still has to type the mixes in after a JSON import, or the pressures in
after a FIT one. The real fix is letting a dive keep more than one source export and merging
what each format knows, which `ux_dive_file_dive_id` (one file per dive) and the single-file
parse-token flow both currently rule out - a deliberate design to revisit rather than an
oversight.

## Parsers report what a file recorded, and `None` for what it didn't

`DiveMixtureSchema`'s `oxygen`, `helium` and `volume` are nullable, and no parser
substitutes a value for gas data a file doesn't carry.

All three used to coerce a missing reading to `0.0` (`_float(mix, "Size") or 0.0` and
friends), and the FIT parser hardcoded `volume=0.0` because the format cannot express
cylinder size at all. The result was a 0 % oxygen mix in a 0 L cylinder presented as if it
had been read off the device - a hypoxic gas nobody dives and a volume
`ck_dive_mixture_volume_positive` rejects outright. It was also actively destructive on the
web form: `DEFAULT_MIXTURE` starts a hand-added cylinder at 11.1 L, and an imported
`volume: 0.0` overwrote that with something the diver then had to notice and undo.

The fix considered first was the opposite one - keep the defaults and add a `notices` array
to `ParsedDiveResponse` explaining which values had been substituted. That is a worse
design: it makes the API assert something untrue and then ships a second mechanism to walk
it back. Not inventing is simpler, and it puts the fact in the data rather than in prose,
so any client can act on it without parsing a message.

Two consequences worth stating:

- **The guess moved to the form, which is where it belongs.** `toMixtureFormValue`
  (`dive-file-import.tsx`) fills a `null` from `DEFAULT_MIXTURE`, so the diver gets the
  identical starting point they would from "add a mixture" - now with whatever the file
  *did* record already filled in. The form has to pick something (its Zod schema requires
  all three); the parser does not.
- **The rule is "don't invent", not "treat zero as missing".** A nitrox export recording
  `Helium: 0` has genuinely recorded 0 % helium, and that survives - hence `??` rather than
  `||` on the frontend, and dropping the `or 0.0` rather than adding an `is None` guard on
  the backend. `TestParsersInventNothing` pins both halves for all three parsers.

`DiveMixtureCreate` (`schemas/dive_mixture.py`) and the DB constraints are untouched: that
schema describes a dive being *saved*, where a cylinder really must have a volume. This one
describes a *file*.

**`volume` is `None` on every FIT mixture.** The format has nowhere to record cylinder
size - not on `dive_gas`, and `tank_summary` carries only the volume *consumed* - so this
is the clearest case of the rule above: a value the file cannot express is not reported.
The form fills it from `DEFAULT_MIXTURE`.

**`fitdecode`'s `CrcCheck.WARN`/`ErrorHandling.WARN` defaults are kept on purpose.** The
CRC guards against transfer corruption, not tampering, and nothing downstream trusts it -
refusing an otherwise readable dive log over a bad checksum would lose real data for no
gain.

**`_scan` catches `Exception`, not `fitdecode.FitError`.** This is the only place in the
codebase where a third-party binary decoder walks bytes a stranger uploaded, and a corrupt
file does not reliably present as the library's own error type. Fuzzing a valid FIT with
1-4 byte flips past the header found three escapes within 400 mutations: `AssertionError`
from `reader.py`, `ValueError: size` from a bad field definition, and `TypeError: '>=' not
supported between instances of 'tuple' and 'int'` from `processors.py`. `POST /dive/parse`
handles only `UnsupportedDiveFileError` and `DiveParseError`, so each of those was a 500.

The truncated-file test gave false confidence here: truncation happens to raise
`FitEOFError`, which *is* a `FitError`, so the one malformed-input case in the suite was
the one case the narrow catch covered.

Extraction (as opposed to decoding) keeps a named tuple of exception types,
`_EXTRACTION_ERRORS`, which includes `ArithmeticError` for the `decimal.InvalidOperation`
that `channels.scaled_int` raises when a corrupt float32 reading arrives as NaN and `quantize`
refuses it.

`parse_dive_file_with_parser` additionally converts anything unexpected out of *any*
parser into a `DiveParseError`, and logs it. Each parser still guards its own failure modes
and produces a better message; the backstop exists because "the parsers are careful" is a
weaker guarantee than "the endpoint cannot 500", and a new parser shouldn't have to
rediscover that.

Both entry points decode the file in full. A FIT file is a stream whose `session` summary
comes *after* the samples it summarizes, so there is no cheap header-only read to be had -
`parse()` pays for the whole pass either way, which is also what makes the coldest-sample
temperature fallback free.

## FIT fixtures are written, not committed as blobs

FIT is the first supported export that is binary, which would otherwise force a choice
between committing opaque `.fit` files and not testing the interesting cases. Neither is
good: a blob cannot be edited to express "a session whose developer field shadows a native
one" or "a dive with no `activity` message", which is exactly what needs pinning down.

`tests/helpers/fit.py` is therefore a minimal FIT *writer* - definition and data records,
developer-field declarations, and the spec's nibble-table CRC-16 - driven by `fitdecode`'s
own copy of the global profile. Field numbers, base types, scale factors and enum members
are looked up rather than hardcoded, so a fixture reads `message("session",
sport="diving", max_depth=32.41)` and cannot drift out of step with the profile the parser
decodes through. The files it produces pass `CrcCheck.RAISE`.

This keeps the inline-fixture, no-database style the rest of `test_dive_parsers.py` and
`test_dive_profiles.py` are written in. Only what those tests need is implemented:
little-endian, one definition per data message, no compressed timestamp headers, no
accumulators.
