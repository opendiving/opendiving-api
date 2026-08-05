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
2. Restart the `web` container (`docker compose restart web`) - this creates any
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
- `GET /dives`, `/trips`, `/dive-sites`, `/dive-stats`: take `user_id` as a query
  param instead of a path segment. The handler checks `current_user["id"] ==
  user_id` and raises `403` on mismatch. These endpoints are no longer public -
  they previously had no auth dependency at all (readable by anyone who knew a
  username).
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

## All `/user*`/`/users`/`/dive/parse-xml` endpoints require auth, except signup

`GET /users`, `GET /user/{id}`, and `GET /user/{id}/tier` used to have no auth
dependency at all - readable by anyone, unauthenticated. `POST /dive/parse-xml`
was the same. These now all require `Depends(get_current_user)` (added via the
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
