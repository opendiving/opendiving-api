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
key pattern (note: even `dives.py`'s own `write_dive` doesn't currently invalidate
the dives list cache - this is a known, currently-accepted gap, not a model to copy).

## Date-only vs datetime fields

`Dive.start_time` is a full `DateTime(timezone=True)` (ISO8601 with time).
`Trip.start_date`/`end_date` are plain `Date` (no time component, `YYYY-MM-DD`).
Don't mix these up when adding new date fields - decide up front whether a field
is a point in time (`datetime`) or a calendar date (`date`), since the frontend
handles each very differently (see the web app's `DECISIONS.md`).
