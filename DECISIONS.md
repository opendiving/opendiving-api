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

## Caching is deliberately skipped for trips/dive sites

`dives.py` list/read endpoints use the `@cache` decorator (Redis-backed), but
`trips.py`/`dive_sites.py` do not. This is intentional: the dive form's trip/dive
site combobox creates a new record and expects to immediately see/select it in the
list. Caching the list endpoint (even with a short TTL) would let the combobox
show stale data right after creating a new trip/site. If caching is added back for
these endpoints, the create/update/delete handlers must invalidate the list cache
key pattern - `dives.py`'s `write_dive`/`patch_dive`/`erase_dive` now do this via
`delete_keys_by_pattern(f"user_{user_id}_dives:*")` after they learn the owning
user's id, which is the model to copy.

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

## Date-only vs datetime fields

`Dive.start_time` is a full `DateTime(timezone=True)` (ISO8601 with time).
`Trip.start_date`/`end_date` are plain `Date` (no time component, `YYYY-MM-DD`).
Don't mix these up when adding new date fields - decide up front whether a field
is a point in time (`datetime`) or a calendar date (`date`), since the frontend
handles each very differently (see the web app's `DECISIONS.md`).
