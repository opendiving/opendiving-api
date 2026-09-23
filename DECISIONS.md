# Backend Decisions & Gotchas

Non-obvious choices in the API and the pitfalls behind them, one short section each. Grep for the
symbol you are touching. The bar for a new entry is in `AGENTS.md`.

## Schema changes have no migration tool

Alembic migrations run on startup (see *Migrations run on startup, and every schema change ships
one*); a database created before they existed needs `alembic stamp head` followed by
`alembic check`, not a stamp alone, because hand-applied `ALTER TABLE`s may be missing or extra —
`CONTRIBUTING.md` has the commands.

## Migrations run on startup, and every schema change ships one

`apply_migrations` (`core/setup.py`) runs `alembic upgrade head` in the API lifespan so a
self-hoster's upgrade is `docker compose pull && docker compose up -d`; `MIGRATE_ON_START` (default
`true`) is the opt-out. The lifespan runs once per gunicorn worker, so the upgrade sits behind
`pg_advisory_xact_lock(_SCHEMA_BOOTSTRAP_LOCK_KEY)` — Alembic takes no cross-process lock.
`core/db/migrations.py` builds the `Config` in code with `prepend_sys_path`, because `env.py`'s
`fileConfig` resets the root logger and undoes `configure_logging(LOG_LEVEL)`. `env.py` imports
`token_blacklist` explicitly (it lives under `core/db/`, and a table absent from `target_metadata`
is `op.drop_table`'d) and filters CRUDAdmin's `admin_` tables with `include_name`. It hands
`DATABASE_URL` straight to `create_async_engine`; `alembic.ini` carries no `sqlalchemy.url`, since
Alembic's `ConfigParser` rejects a percent-encoded password with
`ValueError: invalid interpolation syntax`. `tests/test_migrations.py` renders `--sql` with such a
password. CI runs `alembic upgrade head` then `alembic check`. Primary keys never carry
`unique=True`: the DDL compiler drops it, but autogenerate reports it missing forever.

## Domain `CheckConstraint`s need a manual `ALTER TABLE` on existing DBs

`DiveMixture` (`oxygen`/`helium` 0–100, `oxygen + helium <= 100`, `volume > 0`,
`end_pressure <= start_pressure`) and `Dive` (`duration > 0`, `visibility >= 0`, `max_depth > 0`,
`avg_depth > 0`) declare `CheckConstraint`s in `__table_args__` as DB-level backstops for validation
that otherwise lives only in the web app's Zod schemas (`lib/validations/dive.ts`). Postgres treats
a `CHECK` evaluating to `NULL` as satisfied, so the explicit `<col> IS NULL OR ...` on nullable
columns is for clarity. Autogenerate picks a `CheckConstraint` up like any other change. A database
stamped before migrations existed may lack them; `alembic check` names the missing ones
(`ck_dive_mixture_volume_positive`, `ck_dive_mixture_oxygen_range`, `ck_dive_mixture_helium_range`,
`ck_dive_mixture_oxygen_helium_sum`, `ck_dive_mixture_pressure_order`, `ck_dive_duration_positive`,
`ck_dive_visibility_non_negative`, `ck_dive_max_depth_positive`, `ck_dive_avg_depth_positive`) — add
them by hand and let `alembic upgrade head` bring their text forward; revision `d3b1700eb489` drops
and recreates the `dive_mixture` ones by name. A violation surfaces as
`IntegrityError`/`asyncpg.CheckViolationError`, not a Pydantic error; callers or a shared handler
must turn it into a 4xx.

## Dive sites are many-to-many with dives via a join table

A dive can span several named sites (a drift dive), so `dive` has no `dive_site_id` column.
`dive_dive_site` (`DiveDiveSite`) joins `dive`/`dive_site` with a `position` column preserving visit
order; position 0 is the primary site, shown wherever only one fits ("Site Name +2"). Both FKs are
`ON DELETE CASCADE`: deleting a dive removes its join rows, not the sites; deleting a site drops it
from each dive's list. `crud_dive_dive_sites.replace_dive_sites_for_dive()` deletes and reinserts
the full list on every create and update, never diffs, mirroring `crud_dive_mixtures.py`. The
`dive_site_id` filter on `GET /dives` matches any dive that includes the site, via a subquery over
the join table. `dive_id` has no standalone index: `UniqueConstraint("dive_id", "dive_site_id")`
already leads on it, and `Index("ix_dive_dive_site_dive_id_position", "dive_id", "position")` serves
`get_dive_sites_for_dive`/`get_dive_sites_for_dives` including their `ORDER BY position`.
`dive_site_id` keeps its own index.

## Hot list queries have composite indexes, not independent single-column ones

The list endpoints (`_cached_read_dives`, `read_dive_sites`) run
`WHERE user_id = ... ORDER BY <col> LIMIT ... OFFSET ...`; Postgres uses one index per scan and
sorts separately, so single-column indexes on `user_id` and `is_deleted` cannot serve it. Each has
one composite index matching its filter and sort, so the scan needs no sort step:
`ix_dive_user_id_start_time` on `(user_id, start_time DESC) WHERE is_deleted = false`,
`ix_dive_site_user_id_name` on `(user_id, name)`. Only `dive` is soft-deleted, so only its index
carries the partial predicate; dive sites are hard-deleted. `read_trips` is no longer one of these:
a trip stores no dates, so it sorts by an aggregate over `trip_part` that no index on `trip` can
serve — see *A trip's list order is an aggregate, so the query is hand-written*.
`ix_dive_site_user_id_name` is separate from `ux_dive_site_user_id_name_location_lower`, which is
keyed on `lower(name)` and cannot satisfy a case-sensitive `ORDER BY name`. No table keeps a
standalone `is_deleted` index; other lookups filter by `id` or use a `user_id`-leading unique index.
`DROP COLUMN is_deleted` silently takes a partial index with it — see *The row goes, and so does
everything pointing at it*.

## `Mapped[X]` vs `Mapped[X | None]` on `MappedAsDataclass`

`Base` extends `DeclarativeBase` and `MappedAsDataclass`. `Mapped[X]` without `| None` compiles to
`NOT NULL` in Postgres even with `mapped_column(default=None)`; `default=` only sets the dataclass
`__init__` default. Declare every nullable column as `Mapped[X | None]`
(`Dive.max_depth/avg_depth/bottom_temperature/visibility`, `DiveMixture.start_pressure/end_pressure`
are the pattern).

## Two-pass Pydantic validation needs `X | None` on *both* type and default

Endpoints like `write_dive` do `SomeCreate(...).model_dump()` and re-validate the dict against an
internal schema (`SomeCreateInternal(**dict)`). The second pass validates every key, including those
whose value is `None`, so a field declared `Field(default=None)` without `X | None` in its
annotation passes the first pass and fails the second. Always pair `Field(default=None)` with an
`X | None` annotation.

## `DiveMixture` records `helium`, not a `po2` set-point

A mixture records a `helium` percentage (for trimix), not a PO₂ set-point; there is no `po2` column.
`helium` is handled identically to `oxygen` on the model, `DiveMixtureBase` and the web app's
`diveMixtureSchema` (0–100 range): both nullable, neither carrying a default on the wire (*A
cylinder may record a mix without a vessel*). A set-point and a helium fraction measure different
things, so nothing derives one from the other.

## Mixtures are always replaced wholesale, never upserted by id

`crud_dive_mixtures.replace_mixtures_for_dive()` deletes all of a dive's mixtures and re-inserts the
given list on every save, create and update alike. The mixture `id` returned by `GET` is
informational (a React key); never send it back. `DiveMixtureCreate` has `extra="forbid"` and no
`id` field, so echoing it is a `422 extra_forbidden`. The web app's `normalizeMixtures()` strips it
before every submit.

## Case-insensitive per-user uniqueness (trips, dive sites)

`Trip.name` and `DiveSite.name` are unique per user, case-insensitively, enforced two ways: a
functional unique index on `(user_id, lower(name))` — `Index(..., func.lower(name))` in
`__table_args__` reproduces that DDL exactly — and an application-level check (`trip_name_exists` /
`dive_site_name_exists`) before insert or update, so a duplicate returns
`422 DuplicateValueException` instead of a raw integrity error. Neither half carries a
`WHERE is_deleted = false` predicate: trips and dive sites are hard-deleted, so a deleted name frees
its slot because the row is gone. The index and the check must agree — a predicate on one and not
the other refuses names the other accepts. A new named, user-owned entity mirrors this pattern; add
the predicate to both halves only if it is genuinely soft-deletable.

## `trips.py`/`dive_sites.py` caching mirrors `dives.py`

`trips.py` and `dive_sites.py` each instantiate one `OwnedResourceCache`
(`core/utils/owned_resource_cache.py`), built on the same Redis-backed `@cache` decorator as
`dives.py`. `read_list` (`GET /trips`, `GET /dive-sites`) wraps `get_multi` with
`@cache(key_prefix="user_{user_id}_...", resource_id_name="user_id", expiration=60)`; `read_item`
(`GET /trip/{id}`, `GET /dive-site/{id}`) wraps `get` with
`key_prefix="trip_cache"`/`"dive_site_cache"`. Both are called only after the route's ownership
check; the factory exposes the cached reads and never the check, so authorization cannot land inside
a cached function. Every mutation invalidates: `write_trip`/`write_dive_site` call
`invalidate_list(user_id)` (`delete_keys_by_pattern(f"user_{user_id}_trips:*")`);
`patch_*`/`erase_*` carry `@cache("trip_cache"/"dive_site_cache", resource_id_name="id")`, which
invalidates the item key on any non-GET call, and call `invalidate_list(owner_id)` by hand because
the owner is known only after the fetch and cannot be expressed via `to_invalidate_extra`. `patch_*`
skips the list when `update_data` is empty. The factory covers only read/cache/invalidate; route
bodies stay per resource. `dives.py` keeps `_cached_read_dives`/`_cached_read_dive`, since its reads
add filters, related uuids and mixtures. New simple owned resources use `OwnedResourceCache`.

## The dive form's pickers search server-side via `search=`, never fetching whole tables

`GET /dive-sites`, `GET /trips` and `GET /gear-items` take `search=`, a case-insensitive substring
match, with `items_per_page` capped at 100 (`MAX_DIVE_SITES_PER_PAGE`, `MAX_TRIPS_PER_PAGE`,
`MAX_GEAR_ITEMS_PER_PAGE`). Sites match `name` and both of the locality's text columns
(`DIVE_SITE_SEARCH_COLUMNS`), trips their own name and both of their parts'; gear matches `name` and
`brand`, not `type` — `type` is a closed vocabulary with its own filter, and "reg" would match every
regulator. `core/utils/search.py` builds the `select()` by hand: FastCRUD's `__ilike` filters AND
together and `__or` groups operators on one column, not columns. It selects
`model.__table__.columns`, so rows are dicts like the unsearched path. `escape_like()` escapes `\`,
`%`, `_` in that order, paired with `.ilike(pattern, escape="\\")`. `search` is appended to the list
cache key after `user_{id}_{resource}:page_{n}:items_per_page:{n}`, so the `user_{id}_{resource}:*`
wildcard still purges it; routes lowercase and strip the term first. A resource without
`search_columns` passes no `search` kwarg, or `@cache` would `KeyError`. Gear calls
`search_clause`/`search_multi` directly, not through `OwnedResourceCache`.

## Resource routes are flat, `/...` + explicit ids, never `/{username}/...`

Resource routes are flat and take ids, never a username. Nothing in a request names the account a
row belongs to: `POST /dive`, `/trip`, `/dive-site` take the owner from the session, and
`GET /dives`, `/trips`, `/dive-sites` list the caller's own rows with no owner parameter to check.
`GET/PATCH/DELETE /dive/{uuid}`, `/trip/{uuid}`, `/dive-site/{uuid}` fetch by id alone, then compare
the row's `user_id` with the caller's; a missing row and someone else's row are both a 404 (*Someone
else's row is a 404, not a 403*). FastAPI matches routes in registration order, so two handlers on
one path and method leave the second unreachable. No route hard-deletes a model with
`PersistentDeletion`: `crud.delete()` (soft delete, flipping `is_deleted`/`deleted_at`) is the only
delete path. A GDPR purge runs against the database or the admin panel, not a REST endpoint.

## All `/user*`/`/users`/`/dive/parse` endpoints require auth, except signup

Every `/user*` route and `POST /dive/parse` require authentication. Where the handler does not need
the caller's identity, `Depends(get_current_user)` goes in the route's `dependencies=[...]`;
`GET /user/{id}/rate_limits` and `PATCH /user/{id}/tier` require `get_current_superuser`. There is
no unauthenticated signup exception: `POST /user` does not exist, and a `User` row is created only
by `POST /auth/complete` (*Unified auth flow*). `@cache` and per-request authorization do not mix:
the decorator (`core/utils/cache.py`) returns a cached GET response before the wrapped body runs, so
an ownership check inside a cached function never executes on a hit, and `GET /dive/{uuid}` would
serve another account's data from Redis. `dives.py` splits each cached GET into a private
`_cached_read_*` helper (pure fetch, no auth) and a public route that checks ownership first. Never
put authorization inside a `@cache`-decorated function; gate access in the uncached caller.

## Single-resource path params are a bare `{uuid}`, not `{resource}_uuid`

Single-resource path parameters are a bare `{uuid}`: `/trip/{uuid}`, `/dive-site/{uuid}`,
`/dive/{uuid}`, `/user/{uuid}` — the resource name is already the path segment, so `{trip_uuid}`
would repeat it. A body field or query param that names a *different* resource keeps its prefix —
`trip_uuid` and `course_uuid` on a dive payload, `dive_site_uuid` and `gear_item_uuid` on `/dives` —
because there the prefix disambiguates rather than repeating the path segment. Each module's
`_cached_read_*` helper and its `@cache(..., resource_id_name="uuid")` match, because FastAPI
requires the handler parameter to match the placeholder. The stdlib `uuid` module is imported as
`uuid_pkg` in these files so a parameter can be named `uuid` without shadowing it.

## `GET /user/dive-stats` lives in `users.py`; there is no `/dive-stats` router

`read_dive_stats` lives in `api/v1/users.py` under the `"user"` tag, one more current-user route at
`GET /user/dive-stats` (see *Current-user routes live at a bare `/user`, not `/user/me` or
`/user/{uuid}`*). There is no separate `dive_stats.py` router module and no `"dive-stats"` tag;
`api/v1/__init__.py` registers none. `schemas/user_dive_stats.py`, `crud/crud_user_dive_stats.py`
and `services/dive_stats.py` (the recalculation invoked from `dives.py`) are internals and keep
their names. Unlike `/dives`, `/trips`, `/dive-sites`, the route takes no other id that would need
disambiguating from the path.

## Date-only vs datetime fields

`Dive.start_time` is a `DateTime(timezone=True)` (ISO 8601 with time).
`TripPart.start_date`/`end_date` and `Course.start_date`/`end_date` are plain `Date` (`YYYY-MM-DD`).
Decide up front whether a new field is a point in time (`datetime`) or a calendar date (`date`); the
web app handles each differently (see its `DECISIONS.md`).

## `start_time`'s UTC offset is stored separately, but the API only ever sees one field

A dive logged at 09:00 in Bangkok (+07:00) displays as 09:00 for every viewer, but `timestamptz`
stores only the instant. `Dive` therefore has `start_time` (UTC instant) and `utc_offset_minutes`
(`120` for `+02:00`; default `0` so `Dive(...)` constructors in tests and the admin panel still
work). The API never exposes `utc_offset_minutes`: every `start_time` on the wire is one ISO 8601
string such as `"2021-04-04T10:04:47.910+02:00"`. `DiveCreate` rejects a naive datetime
(`require_utc_offset` in `core/utils/datetime_offset.py`); reads and updates may carry an
offset-less value for an import that lost it — see *A dive's UTC offset may be unknown, and only
import can make it so* and *An offset-unknown dive keeps its wall clock editable*. `write_dive`
(`api/v1/dives.py`) calls `split_start_time()`; `patch_dive` calls `split_updated_start_time()`, the
same split plus the one update-only rule; `_to_public_start_time()` calls `combine_start_time()` on
read. Only `DiveCreateInternal`/`DiveUpdateInternal`/`DiveReadInternal` carry `utc_offset_minutes`
as a field, never `DiveCreate`/`DiveUpdate`/`DiveRead`.

## `recalculate_dive_stats`'s aggregate is backed by a covering index, not incremental counters

`services/dive_stats.py`'s `recalculate_dive_stats` reruns `COUNT`/`MAX`/`SUM` over all of a user's
non-deleted dives on every create, update and delete, synchronously — O(n) per write. Incremental
counters (O(1) `total_dives`/`total_time`) are rejected: more complexity than per-user counts in the
thousands justify, and read-modify-write counters need row locking, where a full recompute converges
under concurrent writers by construction. Instead `ix_dive_user_id_stats` —
`(user_id) INCLUDE (max_depth, duration) WHERE is_deleted = false` — makes the aggregate an
index-only scan. `max_depth`/`duration` are `INCLUDE`d, not key columns; they are never filtered or
sorted on. The aggregate uses `func.count()` (`COUNT(*)`), not `func.count(Dive.id)`: `id` is not in
the index, so counting it forces a heap fetch per row and defeats the covering index; only then does
`EXPLAIN (ANALYZE, BUFFERS)` show `Index Only Scan using ix_dive_user_id_stats` with
`Heap Fetches: 0`. Revisit if a bulk-import endpoint lands (recompute once per batch).

## The Arq worker runs one job: purging expired `token_blacklist` rows

The worker (`core/worker/`) runs one job: `purge_expired_tokens` (`core/worker/functions.py`), an
hourly cron (`core/worker/settings.py`, `arq.cron.cron(..., minute=0, run_at_startup=True)`)
deleting `token_blacklist` rows with `expires_at < now()`. `blacklist_token(s)` in
`core/security.py` adds a row per logout or account deletion, and a row is only needed until its JWT
would expire on its own. The job calls `count()` before `crud_token_blacklist.delete()` because
fastcrud's `delete()` raises `NoResultFound` on zero matches, which most hourly runs produce.
Nothing in the API enqueues jobs, so there is no API-side queue plumbing (`core/utils/queue.py`,
`create_redis_queue_pool`, `api/v1/tasks.py` and `schemas/job.py` do not exist);
`RedisQueueSettings`/`REDIS_QUEUE_HOST`/`REDIS_QUEUE_PORT` stay in `core/config.py` because the
worker builds its own `RedisSettings` from them. `TokenBlacklist.expires_at`
(`core/db/token_blacklist.py`) is indexed (`ix_token_blacklist_expires_at`) so the cron's
`WHERE expires_at < ...` is not a sequential scan.

## Unified auth flow: no passwords, no separate sign up, one `User` row per identity

Authentication is separated from account creation: `api/v1/auth.py` verifies an email (magic link)
or Google identity, then `services.auth_service.resolve_identity` returns `AuthenticatedUser` or
`OnboardingRequired`, so no unverified user reaches the `user` table. `AuthenticationRequest`
(`models/authentication_request.py`) stores a SHA-256 hash of the magic-link token
(`core.security.hash_token`), `expires_at` (`MAGIC_LINK_TOKEN_EXPIRE_MINUTES`) and a single-use
`used_at`, and references no `User`. `AuthenticationProvider` (`models/authentication_provider.py`)
holds one row per linked provider (`provider="email"`; `"google"` with `provider_user_id` = `sub`),
with `UniqueConstraint`s on `(provider, provider_user_id)` and `(user_id, provider)`. Onboarding
tokens (`create_onboarding_token`/`verify_onboarding_token`, `TokenType.ONBOARDING`,
`ONBOARDING_TOKEN_EXPIRE_MINUTES`) carry the identity to `POST /auth/complete` and are blacklisted
once used. `complete_profile` creates `User` and its provider row with `create(..., commit=False)`
and one `db.commit()`; `IntegrityError` rolls back into `DuplicateValueException`.
`POST /auth/email/request` counts before `update(..., allow_multiple=True)` (it raises
`NoResultFound` on zero rows) and returns the same `EmailAuthRequestResponse` regardless.
`enforce_rate_limit` (`core.utils.rate_limit`) is a Redis fixed window, a no-op when `cache.client`
is `None`. Only `POST /auth/refresh` is cookie-authenticated, via an httpOnly, `samesite="lax"`
`refresh_token` cookie.

## `/refresh` and `/logout` live under `/auth` in `auth.py`, not in `login.py`/`logout.py` modules

`POST /auth/refresh` and `POST /auth/logout` live in `api/v1/auth.py` on its
`APIRouter(prefix="/auth", tags=["auth"])`, beside `/auth/email/request` and `/auth/google`; there
are no `login.py`/`logout.py` modules and no bare `/refresh`/`/logout` paths — the callers are the
web app's `client.ts`/`auth.ts`, not a public integration surface, so no redirect exists. A tag
names the URL segment its routes hang off, dash-separated: `dive_sites.py` is tagged `"dive-sites"`,
and `users.py` is tagged `"user"` because every route in it lives under `/user`
(`GET/PATCH/DELETE /user`, `/user/email-change/...`, `/user/dive-stats`, `/user/gas-use-history`,
`/user/dive-activity`) and no `/users` collection exists. The module keeps its plural `users.py`
name, matching `dives.py`/`trips.py`; the tag is independent of the file name.

## Changing an account's email requires confirming the new address first

`PATCH /user/{uuid}` (`api/v1/users.py`) does not accept `email`; `UserUpdate` has no such field, so
sending it is a 422 (`extra="forbid"`). An email change reuses the sign-in magic link's
`AuthenticationRequest` mechanics with `purpose="email_change"` and a `user_id`, which `"sign_in"`
rows never carry.

- `POST /user/email-change/request` (authenticated) acts on the caller's own account, with no
  `{uuid}` path parameter: nothing changes someone else's email. It mails the link to the **new**
  address and answers the same generic message either way, as `/auth/email/request` does.
- `POST /user/email-change/verify` (no auth; the token, tied to a `user_id`, authorises it) runs
  `crud_users.update`, marks the request used, and sends a best-effort
  `send_email_changed_notification` to the **old** address; `IntegrityError` -> rollback ->
  `DuplicateValueException`, as in `/auth/complete`.

`admin/views.py` uses `UserAdminUpdate` (`UserUpdate` plus `email`) as its `update_schema` for
direct superuser edits.

`create_all()` does not add `purpose`/`user_id` to an existing `authentication_request` table
(`asyncpg.exceptions.UndefinedColumnError: column authentication_request.user_id does not exist`):

```sql
ALTER TABLE authentication_request ADD COLUMN purpose VARCHAR(20) NOT NULL DEFAULT 'sign_in';
ALTER TABLE authentication_request ADD COLUMN user_id INTEGER REFERENCES "user"(id) ON DELETE CASCADE;
CREATE INDEX ix_authentication_request_user_id ON authentication_request (user_id);
```

## A confirmed change can still show "invalid or expired" - because something already used the link

Mail scanners and link previews open links in a real browser before a human clicks, so a page that
fires the verify call on load has its token consumed first. The web app's `/auth/verify` and
`/settings/confirm-email` pages therefore require an explicit "Sign in"/"Confirm email change" click
before calling `POST /auth/email/verify`/`POST /user/email-change/verify` (see the web app's
`DECISIONS.md`); a scan can load a page but cannot click. Rejected: a same-browser pairing cookie
(cross-device use is routine and would read as a scanner) and unconditional auto-verify with lenient
replay (automation still triggers the real action).

`AuthenticationRequest` separates `used_at` (first verification) from `invalidated_at` (superseded:
`request_email_link`/`request_email_change` invalidate any earlier live request for the same
email/user) so the caller can be told which; the next steps differ. Sign-in rejects both (*"A spent
sign-in link is a spent sign-in link"*); `verify_email_change` keeps a narrowed replay tolerance.

On a dev DB predating `invalidated_at`:

```sql
ALTER TABLE authentication_request ADD COLUMN invalidated_at TIMESTAMPTZ;
```

## The confirm-email button shouldn't even be shown for a link that's already been used

`GET /auth/email/verify/check` and `GET /user/email-change/verify/check`
(`check_email_link`/`check_email_change_link`) are side-effect-free prechecks: is the token live
(not found, invalidated, already used and expired are all not live) and, if so, for which email. The
web app calls one on page load before rendering the confirm button, so a revisited link shows an
error instead of a button that replay tolerance would let succeed again. The response carries the
target address; the email-change POST echoes it on success.

`verify_email_change`'s replay branch (`used_at is not None`) reports success only while the
account's current email equals the token's `new_email`; otherwise it rejects like an invalidated
token.

`admin/views.py`'s `User` view has no `password_transformer`/`PasswordTransformer`; admin-created
users sign in by `email`. `scripts/create_first_superuser.py` sets no `hashed_password` and inserts
an `authentication_provider` row (`provider="email"`) so that account can sign in. `create_all()`
leaves an existing `user` table untouched:

```sql
ALTER TABLE "user" DROP COLUMN hashed_password;
ALTER TABLE "user" DROP COLUMN google_id;
```

## A spent sign-in link is a spent sign-in link

`verify_email_link` rejects `used_at is not None` with `"This sign-in link has already been used."`,
after the invalidated check and before expiry: superseded beats used beats expired, as in
`check_email_link`.

A replay is a second session: `_start_onboarding_or_sign_in` → `issue_tokens` mints an access token
and a refresh cookie lasting `REFRESH_TOKEN_EXPIRE_DAYS`, so anyone reading the mailbox afterwards
had `MAGIC_LINK_TOKEN_EXPIRE_MINUTES` to gain a week-long session. `services/email_service.py`
already promises `"This link expires in N minutes and can only be used once"`.

No grace window: the precheck-then-button flow defeats scanners, and `/auth/verify` unmounts the
button as it fires the request and never retries, so no double click reaches the server. Being wrong
costs one click on "Request a new sign-in link".

`verify_email_change` still tolerates a replay while `current_email == new_email`, which re-reports
an applied change; sign-in would mint a second credential. `POST /auth/email/verify` answers 401 for
a used link; the web precheck never sends one.

## Current-user routes live at a bare `/user`, not `/user/me` or `/user/{uuid}`

Current-user routes resolve the account from the access token at a bare `/user`: `GET /user`
(`read_current_user`), `PATCH /user`, `DELETE /user`, `GET /user/dive-stats`. There is no `{uuid}`
to mismatch, so no `403` ownership check, and `DELETE /user` does no
`crud_users.get`/`NotFoundException` re-fetch — `get_current_user` already loaded a fresh,
non-deleted row. "My account" and "someone else's public profile" are meant to be distinct,
differently shaped endpoints — full data including `email` from the token versus a limited public
subset keyed by `{uuid}` — rather than one `/user/{uuid}` route gated by a runtime
`if current_user["uuid"] != uuid` check a new route can forget. There is no `GET /user/{uuid}`, no
`GET /users` and no `read_users`: nothing fetches another user's data through this API.
Public-profile-shaped replacements are separate work, and public dive stats would be a new route
such as `GET /profile/{uuid}/dive-stats`. `opendiving-web` (`authAPI.getCurrentUser`/`updateProfile`
in `lib/api/auth.ts`) and `opendiving-ios` (`AuthAPI.currentUser()`) match.

## `CORSMiddleware` is gated on `FrontendSettings`; without it every preflight is a 405

`create_application` (`core/setup.py`) adds `CORSMiddleware` gated on
`isinstance(settings, FrontendSettings)`, like the `ClientSideCacheSettings`/`EnvironmentSettings`
checks: `allow_origins=[settings.FRONTEND_URL]` (the one trusted origin, already used for magic-link
URLs), `allow_credentials=True` for the refresh-token cookie, and `allow_methods`/`allow_headers`
`["*"]` — the Fetch spec forbids only a wildcard origin with credentials. Without it every browser
preflight is a bare `405 Method Not Allowed`, since FastAPI has no `OPTIONS` handler: `apiClient`
(`lib/api/client.ts`) sends `withCredentials: true`, an `Authorization` header and JSON bodies, all
of which preflight. `lifespan_factory` and `create_application` both accept `FrontendSettings` in
their `settings` union. `tests/test_cors.py` builds its own app with
`create_application(..., apply_migrations_on_start=False)` instead of `conftest.py`'s session-scoped
`client` fixture, whose lifespan runs `alembic upgrade head` against `POSTGRES_URI`; checking
middleware headers on an `OPTIONS` request needs no Postgres.

## `/dive/parse` accepts Suunto XML and Suunto JSON; there is no `/dive/parse-xml`

`POST /dive/parse` (`parse_dive` in `dives.py`) accepts Suunto DM5-style XML (`SuuntoXmlParser`,
`services/dive_parsers/suunto_xml.py`) and Suunto app/Ocean JSON (`SuuntoJsonParser`,
`suunto_json.py`, `DeviceLog.Header`). `parse_dive_file()` tries each class in
`dive_parsers/__init__.py`'s `_PARSERS` list via `can_parse()`. `DiveParser.can_parse`
(`dive_parsers/base.py`) prefers a filename check but may inspect `content` when the extension is
ambiguous, turning every failure into `False`, never an exception: `.json` alone is not distinctive,
so the JSON parser checks for `DeviceLog.Header`, and the XML parser checks the root is a namespaced
`<Dive>`. `parse()` reports unrecognised data as `DiveParseError` (generic "malformed"); the XML
parser also keeps `root.tag != _tag("Dive")` → `UnsupportedDiveFileError` for direct calls, because
element lookups return `None` silently. Both `parse()`s wrap field extraction (`_int`/`_float`) in
`try/except (TypeError, ValueError)` re-raised as `DiveParseError`, so
`<MaxDepth>not-a-number</MaxDepth>` is a `422`, not a 500. The JSON header reports temperature in
Kelvin; the parser subtracts `273.15`. Extend `SuuntoJsonParser` for new JSON shapes rather than
adding a second JSON class.

## `SuuntoJsonParser` gas mixtures come from `Header.Diving.Gases`, in SI units

Suunto's JSON export has two header shapes: header-only, with everything under `Header` and no
gases, and D5-style, with stats under `Header.Diving`. `SuuntoJsonParser` reads
`Header.Diving.Gases` (default `[]`, so the header-only shape still parses) into
`DiveMixtureSchema`s. `Diving.Gases` is raw SI: pressure in Pascal, tank size in cubic metres,
oxygen/helium as 0–1 fractions. `_pascals_to_bar`/`_cubic_meters_to_liters`/`_fraction_to_percent`
(on `_decimal_multiply`/`_decimal_divide`) use `Decimal`, like the Kelvin conversion, because float
math on round factors leaves noise (`0.21 * 100` misses `21.0`); `Decimal` works on the JSON
literal's exact digits. Fields with no source: `State` ("Primary", "Deco") stands in for the XML
`<Name>`; type is hardcoded `0` (no `<Type>` equivalent); `gas_changes` is always `[]`.
`StartPressure`/`EndPressure`/`TransmitterID` are optional per gas and come back `None` when absent.
`duration` falls back like `avg_depth` (`header.get("DepthAverage", depth.get("Avg"))`):
`header.get("DiveTime", header.get("Duration"))`, because D5-style exports carry only a top-level
`Duration`. `ascent_time` has no fallback — see *`ParsedDiveSchema`/`DiveMixtureSchema` carry only
fields the backend models support*.

## `ParsedDiveSchema`/`DiveMixtureSchema` carry only fields the backend models support

`ParsedDiveSchema` (`schemas/parsed_dive.py`) carries only what `Dive` (`models/dive.py`) persists —
`avg_depth`, `bottom_temperature`, `dive_number`, `duration`, `max_depth`, `start_time`, `mixtures`
— plus `device` (`serial`, `firmware`, `model`), an identity for the recording device with no column
behind it; see *"A parser reports what recorded the file"*. `DiveMixtureSchema` carries
`end_pressure`, `helium`, `oxygen`, `start_pressure` and `volume`, named after
`DiveMixtureBase.volume`. The rest of the export formats' field sets (algorithm, tissue loading,
CNS/OTU, PO2 set points, gas-change events, per-sample profiles, `transmitter_id`, `type`) is not
mirrored: nothing downstream persists or renders it, so `suunto_xml.py` and `suunto_json.py` never
extract it. There is no `DiveSampleSchema` or `DiveGasChangeSchema`; profiles go through
`parse_profile`.

`start_pressure`/`end_pressure`/`oxygen`/`helium` are rounded to two decimals by `_round2_or_none`,
duplicated per parser, because `_pascals_to_bar` and fraction-to-percent produce more digits than a
gauge is precise to (`20714062 Pa -> 207.14062 bar`). It quantizes via
`Decimal(str(value)).quantize(Decimal("0.01"))`, not `round(value, 2)`, which can land `2.675` on
`2.67`. Depths and temperatures keep source precision.

## Gear is `GearItem` + `GearSet`, not a single `Gear` table

"Gear" is what divers call their equipment, so it beats "Equipment" as the domain name — but it is a
mass noun, and "a gear" reads wrong for one regulator. The tables are `gear_item` (one physical
piece: brand, name, notes, `rented`) and `gear_set` (a named grouping); the API exposes
`/gear-item(s)` and `/gear-set(s)`, and the frontend labels them "Gear" and "Gear Sets".

Both join to their dependents the way dive sites do (see "Dive sites are many-to-many with dives via
a join table"): `dive_gear_item` links a dive to the items used on it, `gear_set_item` links a set
to its members, each with a `position` column preserving list order and `ON DELETE CASCADE` on both
FKs. `crud_dive_gear_items.py`/`crud_gear_set_items.py` mirror `crud_dive_dive_sites.py`:
`replace_*_for_*()` deletes and re-inserts the whole list on every write, de-duplicating ids (first
occurrence wins) rather than diffing or upserting.

## A dive references gear items, never the gear set they came from

Gear sets exist to save typing in the dive form: selecting one replaces the form's gear list with
the set's items, after which the diver edits that dive's list without touching the stored set.
`Dive` has no `gear_set_id` and `DiveRead` no `gear_set` field — only `gear_items`.

If a dive pointed at a set, renaming, re-scoping or deleting the set would silently rewrite or
orphan history for every dive that used it, and "the gear I actually dived with" would stop being
answerable from the dive alone. Keeping the set out of the dive keeps sets freely editable and
disposable, and makes `DELETE /gear-set/{uuid}` cheap: it touches neither gear nor dives.

## Archiving gear is separate from soft-deleting it

`gear_item` carries `is_archived`/`archived_at` and no `SoftDeleteMixin`. Archiving is the only
non-destructive way to retire kit: `erase_gear_item` is a real `DELETE FROM gear_item`, and four FK
cascades take the item's `dive_gear_item` and `gear_set_item` rows, its service schedules and its
service records with it — see *"The row goes, and so does everything pointing at it"*.

An archived item (retired, sold, returned to the rental shop) is hidden from `GET /gear-items`
unless `include_archived=true`, so the dive form's picker stops offering it for a *new* dive, but it
stays on every dive and in every set that references it and keeps its `dive_count`.
`resolve_gear_item_ids_for_user()` resolves archived items normally: archiving must not make an
existing dive or set unsavable. Only the listing filters them.

`archived_at` is derived server-side in `patch_gear_item` from the request's `is_archived` flag (and
cleared on unarchive), never accepted from the caller, so the two cannot drift.

## `gear_item.dive_count` is denormalized, and recalculated exactly like `user_dive_stats`

Every gear list row shows how many dives the item was used on; computing that per request would mean
a join plus `GROUP BY` on every cache miss. It is stored on `gear_item` and refreshed by
`services.gear_stats.recalculate_gear_dive_counts()` after every dive create, update and delete —
the same "recompute from scratch, never increment" approach as `recalculate_dive_stats` (see
"recalculate_dive_stats's aggregate is backed by a covering index").

It is a single `UPDATE ... SET dive_count = (correlated COUNT subquery)` over the user's items,
guarded by `WHERE dive_count IS DISTINCT FROM (...)` so the common case — editing a dive without
touching its gear — does not rewrite every row the user owns. Only non-deleted dives count, so
soft-deleting a dive decrements its gear's counts on the next recalculation.

The admin panel's `GearItem` view can edit `dive_count`, but the value only survives until the
owner's next dive mutation.

## Every gear cache key is user-scoped under one `user_{id}_gear_` prefix

The single-item gear caches are keyed `user_{user_id}_gear_item:{uuid}` /
`user_{user_id}_gear_set:{uuid}`, beside the list caches `user_{user_id}_gear_items:page_...` and
`user_{user_id}_gear_sets:page_...` — unlike `dive_site_cache:{uuid}`/`trip_cache:{uuid}`.
`invalidate_gear_caches()` (`api/v1/gear_items.py`) is therefore one
`delete_keys_by_pattern("user_{id}_gear_*")` covering all four.

The user scoping is what makes that possible. A gear set read embeds its items' names/brands, so
editing an *item* must invalidate *set* reads. A gear item read carries `dive_count`, so any
**dive** mutation must invalidate gear reads (the `invalidate_gear_caches()` calls in `dives.py`); a
dive mutation knows the owner's `user_id` but not which gear uuids changed, and without the prefix
the only pattern would be `gear_item_cache:*` — every user's gear.

The gear-item list key also includes `archived_{include_archived}`, so the picker's active-only view
and the management page's full view never serve each other — the same reason `_cached_read_dives`
keys on its `trip_id`/`dive_site_id`/`gear_item_id` filters. See "Renaming a dive site or gear item
invalidates that user's dive caches".

## `GET /dives` takes a `gear_item_uuid` filter alongside `dive_site_uuid`

`crud_dives`' `custom_filters` has a `with_gear_item` entry mirroring `at_dive_site`: one
`id IN (SELECT dive_id FROM dive_gear_item WHERE gear_item_id = ...)` condition rather than a
separate round trip to resolve matching dive ids. It backs the gear detail page's "Dives with this
Gear" list, which makes the `dive_count` statistic clickable rather than a bare number.

## `GearItem.type` is a closed vocabulary, but has no DB `CHECK` constraint

`GearType` (`schemas/gear_item.py`) is a `StrEnum`, not free text, since free text spells one kit
three ways ("Fins"/"fins"/"Fin"). `OTHER` is the escape hatch. Members are declared in listing
order, which the frontend's picker reuses.

The vocabulary is [DiveJSON](https://github.com/divejson/divejson) 1.0's `gear_item.type` enum,
value for value and in order, so an export writes values through unchanged. A new category goes into
the format first; the check is a diff against `schema/1.0/divejson.schema.json`'s
`#/$defs/gear_item/properties/type/enum`. `opendiving-web` hand-keeps `GEAR_TYPES`; the API widens
first.

No `CheckConstraint`, unlike `dive`/`dive_mixture`'s ranges: `GearType` is a Pydantic field
rejecting every API write, and a DB copy would need `DROP`/`ADD CONSTRAINT` per category. The column
is `VARCHAR(32)`, so a direct SQLAlchemy write reaches it untouched and a **read** schema may not
type the enum — see *"A stored vocabulary is read back as a string"*.

`type` is nullable; the UI shows "No type".

## Renaming a dive site or gear item invalidates that user's dive caches

A dive's cached representation embeds summaries of other resources — its dive sites' names,
locations and positions (`DiveSiteInfo`) and its gear items' names/brands/types (`GearItemInfo`) —
in both `_cached_read_dives` and the single-dive read. Renaming a site, dragging its marker or
renaming a gear item therefore stales every cached dive referencing it.

The single-dive key is user-scoped, `user_{user_id}_dive:{uuid}`, so
`services/cache_invalidation.py` can sweep by pattern; `dive_sites.py`/`gear_items.py` call
`invalidate_dive_caches(owner_id)` after a patch or delete. A flat `dive_cache:{uuid}` would leave
`dive_cache:*`, every user's dives, as the only match.

The helpers live in `services/cache_invalidation.py`, not the routers: `dives.py` invalidates gear
caches and gear invalidates dive caches, so router-level helpers would import circularly.
`invalidate_dive_caches` deletes `user_{id}_dives:*` and `user_{id}_dive:*` separately, because
`user_{id}_dive*` would also sweep `user_{id}_dive_sites:page_...`. `patch_dive`/`erase_dive`
invalidate explicitly rather than through `@cache("dive_cache", ...)`, which cannot reach `user_id`
from route kwargs.

Renaming a trip needs none of this: `DiveRead` carries `trip_uuid` only.

## Weight is a `dive` column, not a gear item

`dive.weight` is a nullable `Float`: total ballast carried on the dive, in kilograms, shaped like
`max_depth`/`visibility`.

A `GearType.WEIGHT` gear item and a `quantity` column on `dive_gear_item` are both rejected. A
`gear_item` is an identity — `ux_gear_item_user_id_brand_name_lower` enforces one row per (brand,
name), and `dive_count` is per item — and "4 kg" is not a thing the diver owns; the list would fill
with "2kg"/"4kg"/"4.5kg" rows whose counts mean nothing. Weight is logged to compare numerically
across dives, which a name cannot answer. A weight belt is a gear item; the lead in it is a
different fact. `dive_gear_item.quantity` would generalize to nothing else.

`ck_dive_weight_non_negative` is `>= 0`, not `> 0` like the depth constraints: diving with no lead
is a real entry (a freedive), distinct from `NULL`. `Float` because half-kilo increments are normal
and pounds do not convert to whole kilos.

## `gear_set.weight` is a default, `dive.weight` is the record

`gear_set` carries its own nullable `weight` (kg): the ballast normally used with it. Loading a set
into the dive form fills in the dive's weight like the item list: a starting point. The dive stores
its own copy and never reads back from the set, so re-weighting or deleting a set cannot rewrite
what a past dive says the diver carried (the gear-items rule above).

`NULL` on a set means "no claim about weight"; loading it leaves the dive alone. A default of 0
would let a fins-and-mask set zero out the dive's weight.

The bound lives in Pydantic (`ge=0` on `GearSetBase`), not a DB `CheckConstraint`, per the
`GearItem.type` rule. `dive.weight` goes the other way because `DiveBase` declares no numeric bounds
— all are DB-enforced and mirrored in the frontend's Zod schemas — and one exception would return a
different 422 shape from its neighbours.

## Gear service is a schedule (the rule) plus records (the history), never one table

A cylinder needs an annual visual inspection **and** a five-year hydrostatic test, which rules out
every "one interval per item" design — columns on `gear_item`, or a single
`service_interval_months`. Servicing is two tables:

- `gear_service_schedule` — the rule. One row per (item, kind, label), enforced by
  `ux_gear_service_schedule_item_kind_label`, which COALESCEs a NULL label to `''` as
  `ux_gear_item_user_id_brand_name_lower` does for a NULL brand.
- `gear_service_record` — the history: what was done, when, by whom.

A diver can set a reminder on never-serviced kit (the baseline is the schedule's `starts_on`) and
change an interval without rewriting history. A record can exist with no rule — "hydro done" on a
cylinder with no reminder — which is why `kind`/`label` are **copied** onto the record at write time
rather than read through `gear_service_schedule_id`, and why that FK is `ON DELETE SET NULL` rather
than `CASCADE`: deleting a reminder must never throw away the receipts.

## A service interval is months OR dives, whichever trips first

`interval_months` and `interval_dives` are two independent thresholds on one rule, not alternatives:
regulator servicing is specified as "annually or every 100 dives, whichever comes first".
`ck_gear_service_schedule_has_an_interval` requires at least one — a rule with neither could never
come due.

Unlike `GearItem.type`, these get DB-level `CheckConstraint`s, alongside `interval_months > 0` /
`interval_dives > 0`: they are domain invariants rather than a duplicated list, mirrored in
`GearServiceScheduleBase`'s `require_an_interval` validator and the frontend's Zod refine.

`services.gear_service.recalculate_service_schedule` derives both due fields from one baseline — the
latest non-deleted record for the schedule, ordered `(serviced_on DESC, id DESC)`, falling back to
`starts_on`/`dive_count_at_start` when the item has never been serviced. The `id` tie-break keeps
two services entered for the same day from resolving arbitrarily.

`add_months` is hand-rolled with `calendar.monthrange` clamping rather than `dateutil`: "serviced 31
August, again in 6 months" must land on 28 February, not overflow into March.

## `dive_count_at_service` is a snapshot, so back-filling old dives inflates it

A dive-based threshold needs a baseline dive count; `dive_count_at_start` (schedule) and
`dive_count_at_service` (record) are snapshots of `gear_item.dive_count` taken server-side at write
time. Neither is in the `Create` schema — both are `extra="forbid"` — because a caller able to set
them could move their own due threshold.

As snapshots of a lifetime counter they carry two accepted inaccuracies:

- Back-filling a 2019 dive inflates "dives since this service". The exact alternative — `COUNT(*)`
  over `dive_gear_item JOIN dive WHERE start_time::date > serviced_on` — cannot be compared against
  a stored threshold and needs a per-item aggregate on every read.
- Deleting dives drives `dive_count` back down, so a naive subtraction can go negative.
  `services.gear_service.dives_since` clamps at 0 (as does `divesSince` in the web app's
  `lib/gear-service.ts`); "-3 dives since service" is nonsense to display and worse to compare.

## `next_due_on`/`next_due_at_dive_count` are stored; `ServiceStatus` deliberately is not

The two due fields are denormalized onto the schedule and recalculated on write — "recompute from
scratch, never increment", as `recalculate_dive_stats` and `recalculate_gear_dive_counts` do.
Storing `next_due_on` makes the digest job's `WHERE next_due_on <= today + 30` an indexed scan
(`ix_gear_service_schedule_next_due_on`) rather than a Python filter over every schedule.

`ServiceStatus` (ok / due_soon / overdue) is **never** stored and never computed inside a
`@cache`-decorated read. It is a function of today's date; the single-gear-item cache uses the
decorator's 3600s default and the list 60s, and either would serve yesterday's countdown. The API
returns only clock-stable facts (`next_due_on`, `next_due_at_dive_count`, `last_service_on`, the
item's `dive_count`), each a pure function of stored data.

The same reasoning gives `GET /gear-service-due` **no `within_days` parameter**: a server-side
horizon would bake today's date into the cached response. It returns every active schedule (capped
at `DUE_OVERVIEW_LIMIT`) and the client buckets.

## Service status is computed twice on purpose: in the browser, and in the digest job

Because status cannot be an API field, whoever displays it derives it.
`services.gear_service.service_status` and `serviceStatus` in the web app's
`src/lib/gear-service.ts` are near-line-for-line twins — the feature's only duplication, and
deliberate: the browser needs it for every badge, and the digest job needs it with no browser to
ask.

The constants are named identically on both sides (`SERVICE_DUE_SOON_DAYS = 30`,
`SERVICE_DUE_SOON_DIVES = 10`) so one `grep SERVICE_DUE_SOON` finds the pair. Change one and you
must change the other; both have the same truth-table tests.

## A dive moves `gear_item.dive_count`, but never `next_due_at_dive_count`

`next_due_at_dive_count` is an **absolute threshold** (`baseline + interval_dives`), not a remaining
count. It depends only on snapshots, so logging, editing or deleting a dive changes what the status
displays as without any write to `gear_service_schedule`: the comparison
`dive_count >= next_due_at_dive_count` happens at read time (browser) and query time (cron).
`services/gear_stats.py` is therefore untouched by servicing.

Storing "dives remaining" instead would force a write to every one of a user's schedules on every
dive mutation and couple two recalculation services together, for no gain.

What this has to preserve: a dive that trips a dive-based interval must still fire an email though
nothing wrote to the schedule. It does, because `should_notify` is evaluated fresh each run against
the item's live `dive_count`.

## Service routes are flat, and every one checks ownership before touching the cache

`/gear-service-schedule(s)`, `/gear-service-record(s)` and `/gear-service-due` are top-level, not
nested under `/gear-item/{uuid}/...`, matching "`/{username}/...` resource routes were flattened"
and "`/user/{username}/...` routes were changed to `/user/{id}/...`": each resource has its own
`uuid`, and nesting would give it a second identity. Filtering by item is a query parameter, like
`GET /dives?gear_item_uuid=`.

Create bodies carry `gear_item_uuid`, and ownership is derived from it: the caller cannot name an
item that isn't theirs, which is the same rule every other create body follows from the session.
`_owned_gear_item` answers identically (422, "Gear item not found.") for "doesn't exist" and "isn't
yours", mirroring `_resolve_item_ids` in `gear_sets.py`, so other users' gear uuids stay
unprobeable.

Every read route resolves the row and checks ownership *uncached* first, then calls a private
`_cached_read_*` helper — see the `@cache`/authorization gotcha under "All
`/user*`/`/users`/`/dive/parse` endpoints require auth".

## Every service cache key starts with `user_{id}_gear_`, so `invalidate_gear_caches` sweeps them

The five service cache keys (`..._gear_service_schedules:page_...`,
`..._gear_service_schedule:{uuid}`, `..._gear_service_records:page_...`,
`..._gear_service_record:{uuid}`, `..._gear_service_due`) sit under the existing `user_{id}_gear_`
prefix, so `invalidate_gear_caches`' single `delete_keys_by_pattern(f"user_{user_id}_gear_*")`
sweeps them. A sixth key must stay under that prefix.

`invalidate_dive_caches` is deliberately **not** called from the service routes: dive reads embed
`GearItemInfo`, which carries no service fields. If service data is ever added to `GearItemInfo`,
these routes must start calling it.

`GearItemRead` carries `service: list[GearServiceScheduleInfo]`, resolved by
`get_schedules_for_gear_items` — one batched query per page, mirroring `get_gear_items_for_dives` —
so the gear list badges every row without an N+1. `GearItemInfo` stays lean: it is embedded in every
dive and gear set.

## The service digest fires once per threshold, tracked in four columns on the schedule

`send_gear_service_digests` runs daily but sends only when `should_notify` finds the schedule's
current `(status, next_due_on, next_due_at_dive_count)` differs from the stored
`(notified_stage, notified_for_due_on, notified_for_due_at_dive_count)`: one email per stage change,
per logged service, per moved due date. `recalculate_service_schedule` clears all four notify
columns **in the same statement** that moves `next_due_*`, so a reminder is always armed for the
stored due date.

A permanently overdue schedule would go silent; `SERVICE_OVERDUE_RENAG_DAYS` (90) re-sends
quarterly, overdue-only.

One email per user, never per item. Send first, mark second: a delivery failure propagates before
the mark, so the worst case is a duplicate, not a lost reminder. No `run_at_startup=True`, unlike
`purge_expired_tokens`: a worker restart must never blast out reminders.

The query's `OR` is half indexable (the date arm uses `ix_gear_service_schedule_next_due_on`; the
dive arm compares `gear_item.dive_count` against a schedule column), fine at a handful of schedules
per user since `should_notify` decides.

## The digest's mark is an ORM bulk UPDATE by primary key, and must carry no `WHERE`

`send_gear_service_digests` marks a user's schedules in one executemany as an ORM bulk UPDATE by
primary key: `update(GearServiceSchedule)` with **no** `.where()`, and `id` in each parameter dict.

Any additional `WHERE` — `.where(GearServiceSchedule.id == bindparam("schedule_id"))` — raises
`InvalidRequestError: bulk synchronize of persistent objects not supported when using bulk update with additional WHERE criteria`;
`synchronize_session=None` then hits
`No primary key value supplied for column(s) gear_service_schedule.id`. Drop the `WHERE`.

A fake session returning `MagicMock` cannot fail on a statement Postgres never runs, so
`TestSendGearServiceDigestsAgainstPostgres` in `tests/test_worker.py` runs the job against a real
database and reads the notify columns back. Any bulk write in a job needs a test on that side.

Those tests skip `async_db`: the job opens its own session from `local_session` on the long-lived
`async_engine`, and a pooled asyncpg connection belongs to the loop that opened it ("attached to a
different loop"), so an autouse fixture disposes `async_engine` per test.

## The digest's "today" is UTC, and that's fine at date granularity

`User` has no timezone column — `Dive.utc_offset_minutes` is per-dive — so
`send_gear_service_digests` treats "today" as UTC and runs at `GEAR_SERVICE_DIGEST_HOUR` (default
07:00 UTC, mid-morning across Europe). With a 30-day lead time and date-granular thresholds, a few
hours either way changes nothing. If it ever matters, the upgrade is to run hourly and gate on the
offset of the user's most recent dive.

## Archived or soft-deleted gear never generates a reminder

The digest query and `get_due_overview_for_user` exclude on three flags, each for its own reason:

- `gear_item.is_archived` — retiring kit has to silence it without the diver pausing every rule on
  it, consistent with archiving hiding an item from the dive form's picker.
- `user.is_deleted` — `User` is still soft-deleted. `gear_item` and `gear_service_schedule` are not:
  a deleted item takes its schedules with it through the FK cascade, so there is no row for the
  digest to skip.
- `user.gear_service_emails` — the opt-out, below.

`gear_service_schedule.is_active` is a fourth, different thing: pausing one rule without deleting it
or touching the item. Each flag answers a distinct question — is this *item* retired, is this *rule*
paused, is this *user* opted out.

## `user.gear_service_emails` is an opt-out with `server_default="true"`

Opt-*out*, not opt-in: a reminder nobody switched on never arrives, and the feature exists to reach
a diver who isn't in the app.

The field is on **both** `UserRead` (so `GET /user` feeds the settings toggle) and `UserUpdate`;
`UserUpdate` is `extra="forbid"`, so missing it there makes the toggle 422 instead of save.

`UserRead`'s `= True` is not a fallback for a database lacking the column: `get_current_user` calls
`crud_users.get` with no `schema_to_select` (`api/dependencies.py`), so FastCRUD selects every
mapped column and Postgres raises `UndefinedColumn` before any response model is reached. The
default exists for `/openapi.json`.

The column carries `server_default="true"` because it is `NOT NULL`: whatever adds it to a table
with rows must supply a value, and only a server-side default is part of the DDL. `default=True` is
applied by SQLAlchemy on INSERT, so autogenerate cannot see it and a revision generated from
`default=` alone fails against any non-empty table.

## Card files are a separate table, hard-deleted, with a deferred `data` column

`certification` carries **no binary columns**, so a certification list never loads megabytes.
Deferred columns on the main table load by accident from a `get_multi` or an admin view; a separate
table cannot. `data` is *also* `deferred()`, so even a direct file-row query returns metadata unless
`undefer` asks — which only `load_certification_file` does.

`CertificationFile` has no `SoftDeleteMixin`: a soft-deleted blob holds bytes nothing can read.
Files are hard-deleted, and `erase_certification` deletes them explicitly — `is_deleted` is
application-level, so no `DELETE FROM certification` runs and the FK's `ON DELETE CASCADE` never
fires. Same trap as `delete_files_for_dive`.

`PublicUUIDMixin`'s `uuid` has a **dataclass**-level `default_factory`, applied on ORM construction.
`store_certification_file`'s Core-level `pg_insert(...)` upsert constructs nothing, so `uuid` must
be passed explicitly. It is set on insert only, outside `DO UPDATE`, so a replaced photo keeps its
identity. And `deferred()` hides the `Mapped[bytes]` annotation from nullability inference, so
`data` needs an explicit `nullable=False`.

## Uploaded content types are sniffed, never taken from the client

`sniff_content_type` identifies an upload from its leading bytes and rejects anything but JPEG, PNG,
WEBP or PDF. The sniffed value is stored *and* served back by the download route, so a trusted
`Content-Type` would let anyone serve arbitrary bytes as any type. A `.jpg` containing HTML is a
415, not a stored `text/html`.

HEIC is detected separately, with its own message naming the fix: it is what an iPhone stores
natively, so a bare "unsupported file type" would read as a bug. Supporting it needs `pillow-heif`.

The download response carries `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`
and `Content-Security-Policy: default-src 'none'; sandbox`. `attachment` rather than `inline`
because the web app renders from a blob URL and never navigates to the URL, so a malicious PDF
cannot execute in the same-origin viewer. The `filename` is built by
`content_disposition_attachment`, never interpolated (see "Non-ASCII filenames need RFC 6266").

## The card download endpoint is never Redis-cached

`@cache` stores serialized API responses; parking multi-megabyte binaries in it would evict
everything else the cache exists for. The endpoint uses `ETag`/`If-None-Match` instead, doing the
equivalent job in the browser where the bytes are wanted. The `ETag` is the stored `sha256`, and
`get_certification_file_sha256` fetches just that column, so a conditional request costs one narrow
query rather than a full read thrown away.

The metadata reads *are* cached, hand-written rather than via `OwnedResourceCache`: that factory's
docstring rules out resources whose list does more than a `get_multi` plus a shape conversion, and
`CertificationRead` embeds each row's file metadata (batched by `get_file_infos_for_certifications`,
the `get_schedules_for_gear_items` pattern). `invalidate_certification_caches` runs after file
uploads and deletes too, not just metadata writes: photographing a card changes what a cached list
page should say even though no `certification` column moved.

## `agency_other` is validated against `agency` in two places

`CertificationBase` rejects `agency_other` unless `agency` is `other`, and requires it when it is —
both directions, so a stored row never carries a second agency name some future read path might
display. `CertificationUpdate` cannot do this: a PATCH may carry either field alone, so the pairing
is only checkable once merged over the stored row, which `patch_certification` does with
`_validate_agency_pairing`.

The asymmetry for clients: switching a certification away from `other` must send
`agency_other: null` explicitly, or the merged result still has both set and 422s.

## No manual DDL for this feature

`certification` and `certification_file` are their own tables; no column was added to any existing
one.

`CertificationFile` is deliberately **not** registered in `admin/views.py`: its rows are mostly one
multi-megabyte `bytea` the generic list/detail views would try to render as text, and no admin form
could meaningfully accept a file upload.

## Dive source files are stored only once a dive exists

`POST /dive/parse` reads the upload, parses it, and drops the bytes. Storage is a second request,
`POST /dive/{uuid}/recordings`, sent by the web app after `POST /dive` (or `PATCH /dive/{uuid}`)
succeeds.

Folding it into the create call is rejected: `DiveCreateRequest` is `extra="forbid"` JSON, so a file
cannot ride along, and turning the one resource-creating route into a multipart endpoint for an
optional attachment is a poor trade. A separate `PUT` is also idempotent, so re-sending the same
file is harmless.

A file that never becomes a dive is never kept, including every file that failed to parse: the
corpus exists to *backfill the dives new extractions would improve*, and a file with no dive cannot
be backfilled into anything.

A failed attach is non-fatal in the web app — a toast, not a rollback. Undoing a saved dive to
preserve a nicety would be worse than losing the file.

## A signed parse token, not `can_parse`, decides what may be stored

`POST /dive/{uuid}/recordings` requires a `file_token`: a short-lived JWT (`create_dive_file_token`,
`TokenType.DIVE_FILE`) minted by `/dive/parse` binding
`(user uuid, sha256 of the bytes, the parser that succeeded)`. The store path re-hashes the body.

Accepting whatever `can_parse` recognizes on upload is rejected: it would store any export-shaped
blob, and `parse_dive_file` falls through to the next parser on `UnsupportedDiveFileError`, so the
matching parser is not always the reading one. `parse_dive_file_with_parser` returns the one that
succeeded.

The token proves only that this server parsed these bytes for this user recently. The route still
checks dive ownership; the token is **not** blacklisted after use, unlike `create_onboarding_token`,
since re-upload is an idempotent no-op. `DIVE_FILE_TOKEN_EXPIRE_MINUTES` defaults to 24 h: it bounds
staleness, not credential lifetime.

`content_type` is resolved from `parser_key` via `PARSER_BY_KEY` at store time, never taken from the
token, since it reaches a response header. `sniff_content_type` still governs certification uploads.

## A dive has at most one source file, and identical bytes are stored once per diver

`ux_dive_file_user_id_sha256` reduces every upload to three cases, a `Literal` from `reconcile()` in
`services/dive_files.py`: same bytes, same dive → **no-op**; same bytes, *another* dive → **409**;
unseen bytes → **insert**. A dive holds one export per recording.

Re-pointing the row would silently strip the file off the dive that has it; a second copy would
defeat the dedupe. The realistic cause is one file logged as two dives, which the diver wants told
about.

`dive_id` is NOT NULL: no fourth "unlinked row you could re-claim" case. That needs a state column,
a partial unique index and a fourth branch, for rows no route could reach. If ever needed, a
nullable `superseded_at` plus `UNIQUE (dive_id) WHERE superseded_at IS NULL` is the additive way.

The write wraps `IntegrityError` into a 409 rather than taking a lock: with one user per dive, a
retry beats `SELECT ... FOR UPDATE`.

## Deleting a dive hard-deletes its source file

`erase_dive` calls `delete_files_for_dive` before `crud_dives.delete`, for the same reason
`erase_certification` does: dive deletion is application-level (`is_deleted`), so no
`DELETE FROM dive` runs and the FK's `ON DELETE CASCADE` never fires. `delete_files_for_dive`
removes the dive's *recordings*, and the cascade from `dive_recording` takes their files and
profiles with them — see *"A dive has recordings, and a file belongs to one of them"*.

Leaving the rows would do more than strand bytes: a file keeps its slot in
`ux_dive_file_user_id_sha256`, so re-importing the same export into a fresh dive would 409 against a
dive the diver can no longer see. `DELETE /dive/{uuid}/file/{fid}` likewise hard-deletes rather than
unlinking — a "delete" button that only hides the file is a worse trade than losing it from the
corpus, and a raw device export is more identifying than a card scan.

## `source_file` is on the dive detail response only

`recordings` — the files and profile summaries — hangs off `DiveReadWithMixtures`, not `DiveRead`;
see *"A dive has recordings, and a file belongs to one of them"*. The inheritance trap:
`DiveReadWithMixtures` *extends* `DiveRead`, so a field on the parent lands on the paginated list
response too and forces extra queries into `_cached_read_dives` — the hottest path in the app — for
something only the detail page renders.

`get_file_infos_for_dives` is written batched (explicit columns, `dive_id.in_()`) though it is only
called with one id: it is the `get_file_infos_for_certifications` shape, it makes never selecting
the `bytea` the default, and it is what an attachment marker in the dive list would need without a
rewrite.

## No manual DDL for the dive-file feature

`dive_file` is its own table with the FK on it; no column was added to `dive`.
`DIVE_FILE_TOKEN_EXPIRE_MINUTES` has a default in `config.py`, so no `.env` change is needed.

`DiveFile` is deliberately **not** registered in `admin/views.py`, for the same reason as
`CertificationFile`.

## Gas use is computed on read, never stored, and deliberately refuses multi-tank dives

`compute_gas_use()` in `services/dive_gas.py` derives `gas_used`, `rmv` and `sac_bar_per_min` from
`duration`, `avg_depth` and the one mixture's pressures; `_to_public_dive_with_mixtures()` attaches
it as `gas_use`, the formula's one call site.

Computed, not denormalized like `user_dive_stats` or `gear_item.dive_count`: it is arithmetic on
loaded columns, and storing it would need a recalculation hook on every mixture replace. Safe to
cache, since it depends on nothing but the row.

`None`, never a best guess, unless exactly one mixture, a usable `avg_depth`, both pressures and a
pressure drop are present. Returned whole or not at all.

Multi-tank is refused because a dive records nothing about *how* its cylinders were breathed: a deco
bottle at 6 m and back gas at 30 m would share one average depth. Staged cylinders need a
`parallel`/`staged` discriminator on `dive_mixture`.

`METERS_PER_BAR = 10.0` assumes salt water, a 1 bar surface and ideal gas; the error is uniform
across a user's dives.

## `GET /user/gas-use-history` is one unpaginated series, cached under the dive prefix

The dashboard graph needs a career, so this returns every dive that yields a figure, oldest first,
unpaginated. `GET /dives` cannot serve it: that response carries no mixtures.

The cache key is `user_{id}_dives:gas_use_history`, under the *dives* prefix despite the `/user/...`
route, because `invalidate_dive_caches()` already sweeps `user_{id}_dives:*` on every dive mutation.
Its own prefix would be a third pattern to forget, leaving a graph silently plotting deleted dives.

`gas_use_history()` runs two queries — dives, then mixtures batched — and lets `compute_gas_use`
decide per dive in Python. SQL conditions (`HAVING count(*) = 1`, `avg_depth IS NOT NULL`, both
pressures present) would skip the un-derivable third of rows but duplicate the rules, and drift
shows as a dive quietly missing from a graph.

`_cached_gas_use_history` carries the `_cached_read_dives` caveat: `@cache` serves hits without
re-running the function, so call it only with the calling user's own id.

## A dive profile is stored per channel, not per sample and not on a shared axis

`dive_profile` holds one row per dive, each channel's series in JSONB `data`:

```json
{"depth":       {"t": [0, 10, 20], "v": [139, 372, 632]},
 "temperature": {"t": [0, 1, 2],   "v": [219, 219, 218]},
 "pressure":    [{"gas_number": 1, "t": [0, 10], "v": [2052, 2041]}]}
```

Per channel because channels are independently sampled, so a shared axis is mostly nulls. Each
channel's timestamps are monotonic, the export's union is not; parsers sort per channel.

Not row-per-sample: profiles are only ever fetched whole. JSONB, not packed `bytea`:
`SELECT data->'depth' FROM dive_profile` beats a 3x saving on a TOASTed column.

Values are integers (centimetres, tenths of a degree, tenths of a bar) via `Decimal(str(value))`
with `ROUND_HALF_UP`; the scale lives in `schemas/dive_profile.py` and the web app's
`PROFILE_CHANNELS`. `t` is integer seconds from the first sample of any channel; the later reading
wins a shared second. A dropout is a gap in `t`, never a null. Pressure is a list keyed by
`gas_number`, a label not an index.

Summary columns (`duration`, `depth_sample_count`, five extremes) sit outside `data`; `channels`
derives from them, never stored.

## Profiles are capped at 1 200 points per channel by min/max bucketing, never LTTB

`MAX_POINTS_PER_CHANNEL = 1200`, applied per channel, server-side at extraction.

Min/max bucketing guarantees both extremes of every bucket survive — a test asserts
`max(downsampled.v) == max(original.v)` — so a profile never loses its maximum depth or its minimum
temperature (`bottom_temperature`). LTTB can drop a one-sample spike. Buckets are chosen on
**time**, not index, so an irregular cadence is weighted evenly; each bucket emits min and max in
time order.

The guarantee cuts the other way on purpose: a transmitter glitch reporting 0.6 bar mid-dive
survives; a plausibility filter would silently discard readings.

First and last samples are pinned too, since `min()` returns the first of equal values, so a channel
ending in identical readings (a diver at the surface) would drop the true final sample and stop
before the dive did. `buckets` drops to `(max_points - 2) // 2` so the cap holds; endpoints are
deduped against the picks.

## `parse_profile` is separate from `parse`, and never runs on the `/dive/parse` path

`DiveParser` has a **non-abstract** `parse_profile(content) -> ParsedProfileSchema | None` rather
than a samples field on `ParsedDiveSchema`, so a format can ship header-only. `None` means "no
samples"; malformed samples raise `DiveParseError`.

It never runs from `POST /dive/parse`: returning readings the form has no use for, to be posted
back, would make stored samples client-supplied, the trust problem the parse token closes. It runs
from `POST /dive/{uuid}/recordings`, which has bytes and provenance. `POST /import/logbook` is the
one path storing client-supplied samples.

`SuuntoXmlParser.parse_profile` re-parses the XML via `defusedxml`, and the XXE and billion-laughs
fixtures re-run against it in `tests/test_dive_profiles.py`.

Not read: `AveragedTemperature` (smoothing is a chart decision) and `DeviceInternalAbsPressure`,
which sits beside `Cylinders[].Pressure` but is the device's *ambient* sensor.

A DM5 `<Pressure>` sample has no cylinder identity, so it becomes a single-entry list labelled gas
number 1, as the JSON export does; `<TransmitterId>` is a device serial.

## DM5 XML expresses every pressure in millibar, and `_parse_mixture` reads them as millibar, not bar

DM5 XML carries every pressure in millibar — `DiveMixture/StartPressure` `205203`, sample
`<Pressure>` `205200`, `CylinderWorkPressure = 200000`, `SurfacePressure = 105500` — cross-checked
against the same dive's JSON export (`20520312` Pa). `_parse_mixture` reads them as millibar; the
JSON parser converts through `_pascals_to_bar`. Pre-2025 exports write `0`, having no transmitter.
Reading them as bar would put 205 203 bar in the mixtures table beside 205.2 bar on the profile
chart.

For stored `dive_mixture` rows in the millibar range, dividing by 1000 is the unambiguous
correction. "Above ~500 bar" is a heuristic for spotting a factor-of-1000 error, not a validity
bound: `ck_dive_mixture_start_pressure_range` and `ck_dive_mixture_end_pressure_range` band both
fields at **350 bar**, so a database cleaned to ~500 alone still holds 351–500 bar rows that fail
the `ALTER`. See *"A cylinder pressure is a bounded field, and every layer that writes one says
so"*.

## Profile extraction is idempotent on (file digest, extractor version), and never fails an upload

`should_extract(existing, sha256=, version=)` is the whole test, split out so the case table is
testable without a database. A profile is a function of bytes and extractor, so
`dive_profile.source_sha256` + `extractor_version` is both the idempotency key and the `ETag` the
read route serves.

In `store_recording_file`:

- The `insert` branch deletes any existing profile with the `DiveFile`, same transaction,
  unconditionally, so a sample-less replacement clears the old curves.
- The `noop` branch (same bytes) calls `should_extract`, so a `PUT` after a
  `PROFILE_EXTRACTOR_VERSION` bump upgrades the profile.
- Extraction runs before the `try` block, so a parse failure is not reported as an `IntegrityError`
  conflict.

`extract_profile` catches `DiveParseError` and anything unexpected, logs the parser key, returns
`None`; the file stays for re-extraction.

Extraction never writes `dive` columns: `dive.max_depth` is the diver's record, possibly
hand-edited; profile `duration` is the sample span, not `dive.duration`.

## The profile's `ON DELETE CASCADE` never fires, so two explicit deletes do the work

`dive_profile` carries two cascades. The one to `dive_recording` fires: recordings are hard-deleted,
so removing one takes its profile and files with it. The one on `dive_profile.dive_id` is
decoration: dive deletion is application-level (`is_deleted`), so no `DELETE FROM dive` ever runs —
the trap `delete_files_for_dive` and `delete_files_for_certification` work around. The model
docstring says so.

`delete_profile_for_recording` on the file path and `delete_profiles_for_dive` on the dive path do
the work: deleting a file re-derives what is left of its recording, deleting a dive takes every one.
A profile never outlives its file, since one whose source is gone cannot be re-derived or checked.

`gear_item` is hard-deleted, so its cascades fire. Dives are not, being the record the app exists to
hold. See *"The row goes, and so does everything pointing at it"* and *"A dive has recordings, and a
file belongs to one of them"*.

## `GET /dive/{uuid}/recording/{rid}/profile` uses an ETag, not `@cache`

Modelled on `read_dive_file`. A profile is immutable for a given (source digest, extractor version),
the ideal `ETag` case and the worst Redis case: every dive cache key lives under `user_{id}_dive*`,
and `invalidate_dive_caches` sweeps the lot on every dive edit and every dive-site or gear rename,
none of which can change a profile.

`get_profile_version` (a two-column query) is checked before the payload loads, so a conditional
request costs one narrow query rather than decoding JSONB to discard it. The 304 is a bare
`Response`, bypassing `response_model` validation. The endpoint sets
`Cache-Control: private, max-age=300`; `ClientCacheMiddleware` never overrides one an endpoint set.
`v` is declared and ignored so the contract is visible: the client varies it with the profile's
`updated_at`.

`DiveProfileInfo` goes on `DiveReadWithMixtures`, never `DiveRead` — the inheritance trap
`source_file` documents; on the parent it would cost `_cached_read_dives` a query per page.
`get_profile_infos_for_dives` is batched like `get_file_infos_for_dives`.

## The profile backfill is a script, not an arq job

The worker runs crons only (*"The Arq worker now does one real thing"*); a backfill finishes once
per extractor version, so a cron would rescan the corpus forever.

`src/scripts/backfill_dive_profiles.py`:

```bash
docker compose exec api python -m src.scripts.backfill_dive_profiles --parser-key suunto_xml
```

It selects `dive_recording` LEFT JOIN `dive_profile` where no profile exists, the version is behind,
or `source_sha256` differs from the files' hash — explicit columns, never `select(DiveRecording)` (a
`bytea` would ride along). One recording at a time via `load_recording_files`, committing every 50,
always reporting `examined / extracted / skipped / no_samples / failed`, since success-only output
hides a broken parser.

It calls `create_redis_cache_pool()` first: `delete_keys_by_pattern` silently returns when
`cache.client is None`, which only the API lifespan sets; cached dive details would otherwise claim
no profile for an hour.

`src/scripts/` is bind-mounted into `api` (`./src:/code/src`) so the host copy runs, though the
image's virtualenv carries an installed `src` package regardless.

## No manual DDL for the dive-profile feature

`dive_profile` is a new table with one unique index, so both arrive via `create_all()` on restart.
No column was added to `dive` or any other existing table — the FK lives on `dive_profile` — so
there is nothing to `ALTER` and no `.env` change.

`DiveProfile` is deliberately not registered in `admin/views.py`, for the same reason as `DiveFile`
and `CertificationFile`.

## `DiveMixture.po2_limit` is not the `po2` column that was removed

The removed `po2` was the gas description: a PO₂ set-point held instead of a helium fraction, a
rebreather's loop description that cannot express open-circuit trimix; `helium` replaced it so 21/35
can be written down.

`po2_limit` is orthogonal and coexists with `helium`: the ppO₂ the diver planned this gas to, which
turns a fraction into a maximum operating depth. `oxygen`/`helium` are the gas; this is the plan.

Both Suunto exports record it per mixture; `Dive_2025-06-03-1215.xml` carries `<PO2>1.4</PO2>` on
its 21/0 back gas and `<PO2>1.6</PO2>` on its 49/0 deco bottle, the only field distinguishing their
purpose. The frontend's `mod()` takes it, so `po2_limit ?? PPO2_WORKING` makes an imported dive's
MODs the device's own.

Units are converted at the parser: XML writes bar (`1.4`), JSON Pascal (`140000`).
`ck_dive_mixture_po2_limit_range` (0.4-2.0 bar) catches the second read as the first.

## `DiveMixture.role` is a structured column, not the gas-name synthesis that was rejected

Suunto's `Gases[].State` ("Primary") describes a gas's role, not a label a diver recognizes, so it
never feeds a name (`DiveMixture.name` is not a field: the label is a pure function of the
fractions). Role gets its own nullable `VARCHAR(20)` column typed by `GasRole`
(`schemas/dive_mixture.py`), a `StrEnum` like `GearType` with no mirroring DB `CHECK`. Shown beside
`gasName()`'s "EAN50" as a "deco" badge; editable on the form, since the file usually doesn't say.

- Suunto JSON: `Header.Diving.Gases[].State` through `_GAS_ROLE_BY_STATE`, not a `GasRole(state)`
  cast, so an unseen value comes out `None`; "Primary" maps to `bottom`. Not the `State: "OC"` under
  `DiveHeader`/`DiveFooter`, a loop type.
- FIT: `dive_gas.mode` (`{0: open_circuit, 1: closed_circuit_diluent}`); `open_circuit` covers back
  gas and stage alike, so maps to nothing. Not `dive_gas.status`, which is whether the gas was
  breathed (`_breathed_gases` reads it); `backup_only` would relabel a pony bottle.
- Suunto XML: nothing. `<Type>` is `1` for every mixture in the corpus, even both cylinders of the
  two-gas dive.

## `DiveMixture.gas_number` is a label, and only the JSON export really has one

The join key to the profile's per-cylinder pressure channels
(`dive_profile.data.pressure[].gas_number`) for per-tank gas accounting.

Only `SuuntoJsonParser`'s sample-reconstruction path reads a file-stated number
(`DiveEvents.GasSwitch.GasNumber` / `Cylinders[].GasNumber`); elsewhere it is synthesized from
position. The XML `<TransmitterId>` and FIT `tank_update.sensor` are ANT device serials, not
indices.

So it is a label, not a trustworthy index. In `FitParser._mixtures`, two gases and one pod leave
`_tanks_for` nulling both pressures while the mixtures are numbered 1 and 2 and the only pressure
channel 1; per-tank attribution must refuse that partial result rather than take the coincidence.

The constraint is `ck_dive_mixture_gas_number_non_negative` (`>= 0`), not `>= 1`: the Suunto Ocean
numbers cylinders from 0 (`Cylinders[].GasNumber: 0`, `GasSwitch` to 0 and 1) and `_parse_samples`
keeps the file's number in the pressure channels, so a 1-based floor would break the one join this
column exists for; zero still catches a negative.

## CNS, OTU and surface pressure are written by the import, never by the form

`dive.cns_start`/`cns_end`/`otu_start`/`otu_end`/`surface_pressure_bar` are filled server-side in
`services/dive_files.py::store_recording_file`, in the same `run_in_threadpool` hop and transaction
as profile extraction. They are not on `DiveCreate`/`DiveUpdate`: `DiveTechScalars`
(`schemas/dive.py`) is mixed into the read shapes only, so `DiveCreate`'s `extra="forbid"` turns an
attempt into a 422.

Mixture fields round-trip through the form; these don't, since CNS and OTU depend on the device's
algorithm and prior exposure, which nothing in a logged dive reconstructs.

- The write is unconditional: replacing an export that recorded exposure with one that doesn't
  clears the old readings. Only a failed extraction (logged, returns `None`) leaves them alone.
- `delete_dive_file` clears them too, alongside the profile. Mixtures are not cleared; the diver may
  have edited them.
- `extract_tech_scalars` never raises: it catches `DiveParseError`/`UnsupportedDiveFileError` then
  bare `Exception` — not `EXTRACTION_ERRORS`, which parsers catch internally and contains neither.
- `surface_pressure_bar` is display-only; `services/dive_gas.py` assumes 1 bar at the surface.

## The same three readings are in three different units across the two Suunto exports

Visible only by matching dives that exist as both a DM5 XML and a JSON export; each format is
internally consistent on its own.

| Reading          | XML           | JSON         | Same dive                                       |
| ---------------- | ------------- | ------------ | ----------------------------------------------- |
| CNS              | whole percent | 0-1 fraction | `<CnsEnd>7</CnsEnd>` vs `EndTissue.CNS: 0.069`  |
| OTU              | absolute      | absolute     | `<OtuEnd>18</OtuEnd>` vs `EndTissue.OTU: 17.89` |
| Surface pressure | Pascal        | Pascal       | `105700` in both, 1.057 bar                     |

JSON CNS is a fraction: the parser multiplies by 100, or a 69 % clock reads 0.069 %.

XML `SurfacePressure` is Pascal, not the millibar `_MILLIBAR_PER_BAR` covers. Cylinder pressures are
millibar (`StartPressure: 207141` is 207.141 bar), but read as millibar 105700 is 105.7 bar; every
XML export lands in 103100-106700, plausible only as Pascal, and the JSON twin writes the identical
integer. `ck_dive_surface_pressure_range` is the backstop.

FIT: `session`/`dive_summary` carry `start_cns`/`end_cns` (whole percent) and `o2_toxicity`, the
ending OTU total rather than a delta — a dive held as both FIT and XML shows
`OtuStart 22 -> OtuEnd 23` against `o2_toxicity = 23`. There is no start OTU and no surface
pressure, so `otu_start` and `surface_pressure_bar` stay null; `record.absolute_pressure` is ambient
per sample.

## The tech-field backfill is a second script, not a flag on the profile one

`src/scripts/backfill_dive_tech_fields.py`:

```bash
docker compose exec api python -m src.scripts.backfill_dive_tech_fields
```

Separate from `backfill_dive_profiles` because the selection differs: `PROFILE_EXTRACTOR_VERSION`
lets that one skip a current profile; these columns have no version, so every primary recording with
a file is a candidate each run (a cheap header parse).

It walks primary recordings (a second computer's CNS clock is its own), fills the device columns
too, and never clears a reading the file lacks: a logbook-import match can fill one without bytes
(*"A profile has one of three provenances, and a recording need not have a file"*).

Dive scalars are overwritten outright. Mixture fields are best-effort via `merge_mixture_fields`
(pure). Saves replace mixtures wholesale, so a stored `id` cannot name its parsed cylinder and
position alone is too weak; counts must match and every pair must agree on `(oxygen, helium)`,
all-or-nothing per dive, else `mixtures_skipped`. A parsed `None` fraction is not compared: the form
filled `DEFAULT_MIXTURE`.

## Manual DDL for the Phase 2 tech fields

Eight columns on two existing tables, applied by hand with their `CHECK`s per *"Schema changes have
no migration tool"*: `cns_start`, `cns_end`, `otu_start`, `otu_end`, `surface_pressure_bar`
(`DOUBLE PRECISION`) on `dive`; `po2_limit DOUBLE PRECISION`, `gas_number INTEGER`,
`role VARCHAR(20)` on `dive_mixture`. Constraints `ck_dive_cns_start_non_negative`,
`ck_dive_cns_end_non_negative`, `ck_dive_otu_start_non_negative`, `ck_dive_otu_end_non_negative`,
`ck_dive_surface_pressure_range` (0.5-1.2), `ck_dive_mixture_po2_limit_range` (0.4-2.0),
`ck_dive_mixture_gas_number_non_negative`.

CNS is `DOUBLE PRECISION`, not integer: XML rounds to whole percent, JSON records 0.069. All
nullable: each is absent from some format.

The `CHECK`s follow the `<col> IS NULL OR ...` shape (*"Domain `CheckConstraint`s need a manual
`ALTER TABLE`"*). CNS and OTU are `>= 0`, since no loading is a real 0, with no upper bound: CNS
above 100 % is the reading a diver most needs. Surface pressure is bounded both sides, catching the
Pascal/millibar error.

`_DIVE_CONSTRAINT_MESSAGES` (`api/v1/dives.py`) and `_MIXTURE_CONSTRAINT_MESSAGES` carry an entry
each, so a violation names the field, not a 500.

## The deco ceiling is a fourth channel, on depth's axis and depth's scale

`dive_profile.data` carries `"ceiling": {t, v}`, and `CEILING_SCALE` is defined as `DEPTH_SCALE`: a
ceiling is a depth drawn as shading against the depth curve.

A zero ceiling is not a ceiling; `ceiling_cm` in `dive_parsers/channels.py` is the one place that
judgement is made. Zero means "you may surface"; stored, it draws a surface line on every no-deco
dive. DM5 XML writes `<Ceiling i:nil="true"/>` where the JSON export of the same dive writes
`"Ceiling": 0`, so the imported file would otherwise decide whether the dive owed decompression.

Distinct from *"A zero cylinder pressure is not a reading"* (measured nothing) and a kept zero
helium (measured zero): here zero is the absence of the thing measured. `scaled_int_or_none` stays
faithful to zero; only `ceiling_cm` doesn't.

FIT's ceiling is `record.next_stop_depth`; neighbours `next_stop_time`, `time_to_surface` and
`ndl_time` are durations (tested via `tests/helpers/fit.py`).

`max_ceiling_cm` is the summary column; `channels` derives `ceiling` from it being non-NULL.

## Profile events are a closed vocabulary, and Suunto's `<Marks>` is deliberately not read into it

`dive_profile.data` carries `"events": [{t, type, gas_number?, label?}]`,
`type ∈ {gas_switch, deep_stop, safety_stop, bookmark, other}` (`ProfileEventType`).
`_validate_events` rejects an `other` without `label`. `normalize` sorts them; validation is
separate from `_validate_series`. Rebasing clamps at zero, so the opening
`<GasChangeTime>0</GasChangeTime>` survives.

- Suunto XML: gas switches only, from `<DiveGasChanges>` inside each `<DiveMixture>`, so
  `gas_number` is `_parse_mixture`'s position.
- Suunto JSON: `GasSwitch` (`Events` and `DiveEvents`), `Notify` `Deep Stop`/`Safety Stop`,
  `Alarm`/`Warning` as `other`; `Active: true` edges only. `State` entries and
  `Deep Stop Ahead`/`Stop done` are dropped.
- FIT: `dive_gas_switched`; `user_marker` → `bookmark`; `dive_alert` → `other`; `timer` omitted. A
  switch's `data` is a `message_index`, resolved via `_breathed_gases`.

`<Marks>` is refused: its `<Type>` is an undocumented numeric code.

`MAX_EVENTS = 200` and `FitParser._MAX_EVENTS = 100` cap the list, logging truncation.
`MAX_LABEL_CHARS = 120` bounds `label`, truncated in `_rebase_events` rather than a raising
`Field(max_length=...)`, since `extract_profile` never fails the upload; `label` is file-controlled
text.

## A `PROFILE_EXTRACTOR_VERSION` bump re-extracts the corpus, and the two summary columns are nullable

`PROFILE_EXTRACTOR_VERSION` is 2 because the same bytes now yield different samples;
`should_extract` re-extracts anything behind it, so the existing script picks the corpus up
unchanged:

```bash
docker compose exec api python -m src.scripts.backfill_dive_profiles
```

Every ETag changes (`{source_sha256}:{extractor_version}`) and the backfill flushes every touched
user's dive caches, so run it off-peak.

Two summary columns on `dive_profile`, applied by hand per *"Schema changes have no migration
tool"*:

```sql
ALTER TABLE dive_profile ADD COLUMN max_ceiling_cm INTEGER;
ALTER TABLE dive_profile ADD COLUMN event_count INTEGER;
```

No `CHECK`s: only `store_profile` writes them, never a request body.

Both nullable, meaning different things. `max_ceiling_cm IS NULL` is "no decompression owed", which
`channels` derives the ceiling curve from. `event_count IS NULL` is a row the backfill has not
reached; `0` is "looked and found none". `store_profile` always writes a count; `to_read_schema`
reads both optional payload keys with `.get`, so a version-1 row reads back without a `KeyError`.

`DiveProfileInfo` carries `event_count` and `max_ceiling`; `event_count` stays out of `channels`,
events not being a curve.

## The contact form is an API endpoint, not a `mailto:`

`POST /api/v1/contact` (`api/v1/contact.py`) mails a human, not a user, from anonymous input; the
SMTP credentials live here, not in the frontend.

- Unauthenticated, so rate-limited by submitted email and by client IP (`CONTACT_FORM_RATE_LIMIT_*`,
  one-hour window); the address is unverified, so `From:` is a claim.
- Nothing is stored; there is no inbox here.
- `reply_to`, never a spoofed `from`: `EMAIL_FROM_ADDRESS` is the only address SPF/DKIM covers.
- `send_contact_form_email` runs `html.escape` over name, subject and body, being the only sender in
  `services/email_service.py` carrying a stranger's prose; any new one must too.

`CONTACT_FORM_EMAIL` is the recipient, with no default (*"The contact form has no default recipient,
and no recipient means 503"*), and is not `AppSettings.CONTACT_EMAIL`, the OpenAPI metadata for
`/docs`, so a maintainer address there cannot reroute support mail.

With `SMTP_HOST` unset the send is a logged no-op, submission included; the endpoint still reports
acceptance, never delivery.

## `dive_number` is a label, not an identity or an ordering key

Nothing reads `dive_number` except to print it; chronology is `start_time` (`_cached_read_dives`,
`gas_use_history`, `ix_dive_user_id_start_time`). Joining, sorting or paginating on `dive_number`
makes everything below unsafe.

Free-form because gaps are real data (paper logbooks) yet a backfilled log should read 1..N, so
nothing renumbers unasked. `services/dive_numbering.py`: `suggest_dive_number` proposes
(`GET /dives/next-number`), `summarize_numbering` reports (`GET /dives/numbering`), `renumber_dives`
rewrites on request (`POST /dives/renumber`).

- No unique constraint on `(user_id, dive_number)`: duplicates are normal while back-filling and
  mid-renumber; they are reported, not blocked.
- The suggestion is positional, not `MAX(dive_number) + 1`: the chronologically preceding dive's
  number plus 1, so back-filling a 2019 dive suggests #12 not #213, collision or not.
- `renumber_dives` writes one `UPDATE ... FROM` over a CTE; `dry_run` computes the same change list
  through the same code, both ordered by `(start_time, id)`.

Renumbering invalidates only the dive caches; it moved no `recalculate_dive_stats` figure or gear
`dive_count`.

## A dive computer's own counter is not the diver's dive number

`<DiveNumberInSerie>`, `Header.Diving.NumberInSeries` and `session.dive_number` are the device's
counter: it starts at 1 on a new or factory-reset computer and restarts on the next one, so
importing it as `dive_number` stamps a #5 onto someone's 300th dive. All three parsers leave
`ParsedDiveSchema.dive_number` null, and the number comes from `GET /dives/next-number`, derived
from the dive's date.

The counter is kept under its own name: `ParsedDiveSchema.device` carries a `ParsedDevice` whose
`dive_number` is that count — see *"A parser reports what recorded the file"*. The two are different
quantities that once shared a field.

`dive_number` stays on `ParsedDiveSchema` rather than being trimmed under the "fields the backend
models actually support" rule: it has a direct `Dive` column, and a format carrying a real lifetime
number (Subsurface's XML) can populate it.

## `GET /dive/{uuid}/neighbors` is two one-row queries, ordered by `(start_time, id)`

Returns the two dives either side — `uuid`, `dive_number`, `start_time` — not a `DiveRead` per side
(which costs `_cached_read_dives`' lookups), since after a deep link the client holds no list.
`next` means later in time, the opposite end of the newest-first `GET /dives` list.

Ordering is `(start_time, id)` as a row comparison: on `start_time` alone a strict `<`/`>` skips a
tied dive and `<=`/`>=` returns the dive itself.
`tests/test_dive_neighbors.py::TestSharedStartTimes` pins it. Each query is an index scan over
`ix_dive_user_id_start_time` — measured, not guaranteed, since the index carries no `id`. The pivot
is bound as `literal(start_time, Dive.start_time.type)`; an inferred aware `datetime` binds as
`TIMESTAMP` against `timestamptz`.

`GET /dives`' `trip_uuid`/`dive_site_uuid`/`gear_item_uuid` filters have no counterpart: prev/next
is the whole log.

The cache key `user_{id}_dives:neighbors:{uuid}` is under the list prefix like `gas_use_history` and
`dive_activity`, going stale when any dive moves; `invalidate_dive_caches()` sweeps
`user_{id}_dives:*`. `_get_owned_dive` runs before the `@cache` body.

## The admin panel is off by default, and refuses to boot insecurely in production

The panel bypasses every ownership check in `api/v1` by talking to the models directly, so it is
something an operator turns on, not something a fresh deploy inherits — unlike `/docs`, which
`core/setup.py` disables in production and gates behind a superuser in staging, it has no
`ENVIRONMENT` gate of its own.

1. `ADMIN_PASSWORD` has no default (`str | None = None`). Unset means "no admin account", which
   `admin/initialize.py` handles.
2. `CRUD_ADMIN_ENABLED` defaults to `False`. `src/.env.example` enables it for local development.
3. `Settings._reject_insecure_admin_config` raises at startup when the panel is enabled in
   production with a missing or boilerplate password (`"!Ch4ng3Th1sP4ssW0rd!"`). Chosen over
   silently disabling it: an operator who meant to have it wants to know, and boot is the loudest,
   cheapest place. A missing IP allowlist warns rather than raises, because deployments fronting the
   panel with a VPN would otherwise refuse to start for no gain.

## `/auth/refresh` rotates the refresh token instead of reusing it

The endpoint blacklists the presented refresh token and issues a fresh pair via `issue_tokens`, as
every sign-in path does; a reused cookie would stay usable for its full `REFRESH_TOKEN_EXPIRE_DAYS`
once leaked. `purge_expired_tokens` already cleans the row up.

The cost: two tabs refreshing at the same instant race, and the loser gets a 401. Inherent to
rotation without a grace window; accepted because the access token lives 30 minutes, so collisions
are rare and the failure is a re-login. If it bites, add a short reuse-detection window rather than
reverting.

The token is spent before its replacement is minted, so a crash between leaves the caller signed
out, not holding two live refresh tokens.

Rotation also creates a signal (*"A reused refresh token is a `WARNING`, and `revoked_at` is what
makes it legible"*). The race is distinct from the same-second collision a `jti` prevents.

## Every revocable token carries a `jti`

`core.security._new_jti` puts a `uuid4().hex` in every access, refresh and onboarding token. Without
it the claims are `sub`, `token_type` and `exp`, and `exp` has one-second resolution, so two tokens
for one subject in the same second encode identically. Revocation stores the token string
(`token_blacklist.token`, unique), so identical tokens share one entry: `refresh_access_token`
blacklists the presented cookie before minting its replacement, so a collision hands back a revoked
token and the next refresh is a 401, and `/auth/logout` on one session kills a same-second sibling.

Anything revocable minted here needs the claim; dive-file tokens deliberately don't, since nothing
revokes them by value and two identical parse receipts are one receipt.

Nothing reads `jti` back (`verify_token` ignores it), so tokens minted without it verify until
expiry. The blacklist is not keyed on a `jti` column: tidier, but worthless once issuances are
unique.

## A reused refresh token is a `WARNING`, and `revoked_at` is what makes it legible

`verify_token` returns one `None` for revoked and garbage, and `refresh_access_token` raises
`UnauthorizedException("Invalid refresh token.")` for both. Its signature stays (hottest path,
`api.dependencies.get_current_user`); `core.security.revocation_time` asks again on the failure path
only.

`WARNING`, not `info`, to survive the default level; `tests/test_auth_refresh.py` asserts it.

Elapsed time since the token was spent tells theft from the two-tab race. `expires_at` is the
token's `exp`, hence `token_blacklist.revoked_at`, stamped by `core.security._blacklist_one`, NOT
NULL with `server_default now()`; without it a warning cannot say which case fired.

Sign-out on reuse sits past `_REFRESH_REPLAY_THRESHOLD` (five seconds), never on the branch, so the
race stays a 401 (*"A replayed refresh token takes its session with it"*). The 401 `detail` is
identical for revoked token and non-token: no oracle.

`purge_expired_tokens` (`core/worker/functions.py`) ends the detection window at the token's `exp`;
between `exp` and the purge, `token_subject` reports an unknown subject rather than decoding with
`verify_exp` off.

## Blacklist expiries are UTC-aware, and so is the purge that reads them

`core/security` writes blacklist rows with UTC-aware datetimes, `purge_expired_tokens` compares
against an aware now, and `token_blacklist.expires_at` is `DateTime(timezone=True)` like every other
timestamp. All three have to agree. A naive `datetime.fromtimestamp(exp)` renders the JWT's UTC
`exp` in the host's zone, and a naive `datetime.now()` in the purge makes the same error in the same
direction, so the two cancel on any host; fixing either alone stops them cancelling and purges
revoked tokens while still valid. The column is the third piece: asyncpg does not coerce, so binding
an aware datetime to `TIMESTAMP WITHOUT TIME ZONE` raises
`DataError: can't subtract offset-naive and offset-aware datetimes` on every logout insert and every
purge.

## Pagination bounds live in `core/utils/pagination`, not in each route

`page`/`items_per_page` come off the query string, and every list endpoint calls `clamp_pagination`;
otherwise `GET /dives?items_per_page=999999999` is a request for the whole log and a negative value
reaches the database as a negative LIMIT. It clamps rather than rejects, so a client asking for too
much gets the ceiling instead of a 422. One `DEFAULT_MAX_ITEMS_PER_PAGE` replaces per-module
`MAX_*_PER_PAGE` constants, with a per-call override.

The guard is `TestPageSizeCaps` in `tests/test_picker_search.py`. `PAGINATED_LIST_ROUTES` maps a
filename to a tuple of handler names, and the check is an `ast` parse asking whether that handler's
own body assigns from `clamp_pagination`, since a file-wide substring passes every route sharing a
module with one that clamps (`gear_service.py` has two).
`test_the_inventory_names_every_paginated_route` discovers every
`response_model=PaginatedListResponse` handler in `api/v1` and fails when inventory and source
disagree in either direction, so a route added to an already-named file cannot inherit its
neighbour's pass.

## Ownership checks go through one `fetch_owned_or_raise`

`api/dependencies.fetch_owned_or_raise` is the only implementation; route files keep thin wrappers
(`_get_owned_trip`, `_get_owned_gear_set`, ...) for wording and return type. The check runs before
any `@cache`-wrapped read, since a hit skips authorization; `tests/test_ownership.py` guards the
inline form.

Not converted: `gear_service._owned_gear_item` and `gear_sets._resolve_item_ids` answer 422 for
missing and not-yours alike, the uuid being a body reference, not the addressed resource;
`gear_service`'s schedule/record routes scope by `user_id` in `resolve_schedule_for_user`.

Someone else's row is a 404, not a 403: `fetch_owned_or_raise` raises `NotFoundException` with one
message for both, since a 403 is an oracle, matching the empty page `GET /dives`'
`trip_uuid`/`dive_site_uuid`/`gear_item_uuid` filters return. Nothing answers 403 over identity at
all — see *A request never names its owner; the session does*.

The log keeps the distinction: wrong owner names the owning `user_id` at `warning`, surviving a
raised `LOG_LEVEL`; absent is `debug`, since a client looping over random uuids would otherwise emit
unbounded warnings.

## `mypy` runs over `tests/`, with `call-arg` disabled there

Every schema in `app/schemas` declares optional fields as
`Annotated[T | None, Field(default=None)]`. Pydantic's mypy plugin reads defaults only from the
`x: T = Field(default=...)` form, so it synthesizes an `__init__` with those fields required, and
every test that omits an optional field reports a false `[call-arg]`. The plugin loads; this is a
gap in what it handles.

`[[tool.mypy.overrides]] module = "tests.*"` disables `call-arg` there and only there; `arg-type`,
`attr-defined`, `no-any-return` and the rest still apply, and production code is untouched. Passing
explicit `None`s at every call site would make the tests worse to read to satisfy a tooling gap.
Drop the override once the plugin understands `Annotated` defaults.

Two invocations, not one: the app is reachable as `app.*` (via `mypy_path`) and `src.app.*` (how the
tests import it), and `mypy src tests` refuses with "source file found twice under different module
names".

## The runtime image contains the app, and ships gunicorn

The final stage `COPY`s the application package into `/code` alongside `/app/.venv`, so
`app.main:app` imports without a bind mount. `docker-compose.yml` still bind-mounts `./src/app` over
`/code/app` for live editing, a convenience rather than a requirement.

`--reload` is not in the image: it runs a single worker plus a filesystem watcher and restarts on
any write. `docker-compose.yml` overrides `command:` with the uvicorn `--reload` form, so local
development is unchanged.

## The lifespan runs once per gunicorn worker, so nothing in it may race a sibling

`gunicorn -w 4` runs the FastAPI lifespan once per worker: deployment steps do not belong in it, and
a startup check that can fail spuriously is worse than none.

- `admin.initialize()` is a one-shot, `app.admin.initialize`'s `main()`, run as the `admin_init`
  service `api` waits on via `service_completed_successfully`; raced, losers die with
  `table admin_user already exists`. Constructing `CRUDAdmin` registers routes (`__init__` calls
  `setup()`) without touching the database.
- `Base.metadata.create_all()` stays in the lifespan so `docker compose restart api` picks up new
  tables, serialized behind a transaction-scoped advisory lock in `core/setup.create_tables`;
  `checkfirst=True` alone lets two cold-start workers both see nothing and one die with
  `duplicate key value violates unique constraint "pg_type_typname_nsp_index"`.
- The volume probe (`LocalBackend.ensure_ready`, via `ensure_storage_ready`) uses a per-pid file and
  `missing_ok=True`; a shared name turns one worker's `FileNotFoundError` into "the files volume is
  not writable". `tests/test_blob_store.py::TestEnsureRootWritable` pins it.

## Running the admin panel on more than one worker

Two pieces of the panel's state are per-process and must be shared:

- **Its tables** (`admin_user`, `admin_session`, ...): `CRUD_ADMIN_DB_URL` must point at the app's
  Postgres. Unset means a SQLite file inside one container's filesystem, raced by sibling workers
  and invisible to every other container, including the `admin_init` one-shot. **Required** for
  multi-worker.
- **Its sessions**: `CRUD_ADMIN_TRACK_SESSIONS` defaults to `true` and persists them to
  `admin_session`, so there is nothing to configure, only something not to switch off.
  `CRUD_ADMIN_REDIS_ENABLED=true` is an equivalent alternative that keeps session lookups off
  Postgres, not required. With both off, only the request that lands on the worker holding the
  in-memory session succeeds; the rest bounce to `/admin/login?error=Session+expired`.

Probing by hand: `GET /admin` redirects to `/admin/` signed in or not, so probe `/admin/` without
following redirects; and `SESSION_SECURE_COOKIES` defaults to `true`, so over plain HTTP a
well-behaved client stores the session cookie and never sends it.

## Rate limiting fails open on a Redis *outage*, not just a missing client

`enforce_rate_limit` degrades rather than hard-fails when Redis is unavailable: throttling is
defense-in-depth, and an outage must not take sign-in down.

`if cache.client is None` is not that check: `create_redis_cache_pool` uses `redis.Redis.from_pool`,
which connects lazily, so `cache.client` is an ordinary object whether or not Redis is up, and
`None` only means the pool was never created (unit tests). A real outage surfaces as
`ConnectionError` from `incr`, which unhandled turns the whole auth surface into 500s.

The handler wraps only the `incr`/`expire` pair. `RateLimitException` is raised outside it on
purpose: it is the function's normal signal, and a `try` one line wider would swallow it and disable
rate limiting entirely.

The warning fires on entering the degraded state and re-arms on recovery: per-call logging buries
the signal, and a latch that never resets lets a second outage pass silently.

`tests/test_rate_limit.py::TestRedisIsDown` simulates the connection failure.

## Per-IP rate limits need to know which proxy to believe

`request.client.host` is the socket peer; behind the bundled nginx or any load balancer that is the
proxy's address for every caller, so all per-IP buckets collapse into one and a single bot locks
sign-in for everyone.

Trusting `X-Forwarded-For` unconditionally is worse: the header is caller-supplied, so anyone
reaching the API directly forges a fresh identity per request. `core.utils.client_ip` consults it
only when the socket peer is listed in `TRUSTED_PROXY_IPS`, and takes the right-most entry that is
not itself a trusted proxy, since only hops appended by trusted infrastructure mean anything; unset
(the default) is plain socket-peer behaviour.

`TRUSTED_PROXY_IPS` is a comma-separated string, not `list[str]`: pydantic-settings parses a complex
field's environment variable itself and expects JSON, so `TRUSTED_PROXY_IPS=172.16.0.0/12` fails at
startup regardless of the `cast=` passed to `config()`. `CRUD_ADMIN_ALLOWED_IPS` and
`CRUD_ADMIN_ALLOWED_NETWORKS` are strings for the same reason. Tests patching the attribute cannot
catch this; `TestTheSettingParsesFromTheEnvironment` goes through `Settings`.

## `ClientCacheMiddleware` marks `public` only on a safe method, and never overwrites an explicit header

Whether the request carries an `Authorization` header is the wrong signal for public cacheability.
The endpoints that mint credentials — `POST /auth/email/verify`, `/auth/google`, `/auth/complete`
and `/auth/refresh` — are unauthenticated by nature and return a token in the body, and that rule
labels all four `public, max-age=60`.

`public` additionally requires a safe method (GET/HEAD). That covers the four POSTs but is not
sufficient on its own: `GET /auth/email/verify/check` and `GET /user/email-change/verify/check` are
anonymous, side-effect-free GETs that take a single-use token in the query string and return the
account's email address. Those set `Cache-Control: private, no-store` themselves, which the
middleware's "never overwrite an explicit header" rule preserves. `GET /api/v1/health` still returns
`public, max-age=60`; the fix is targeted, not a blanket disable of client caching.

## `.dockerignore`, or: `.gitignore` does not apply to Docker

The builder stage does `COPY . /app`, so without a `.dockerignore` the entire working tree lands in
an image layer, including the developer's real `src/.env` with `SECRET_KEY`, `POSTGRES_PASSWORD` and
`ADMIN_PASSWORD` — and the `test` stage inherits from `builder`, which makes them part of a
distributable artifact. A file correctly untracked by git is still baked into every image. The
runtime stage copies the app package in, so `src/app/logs/app.log` would travel too; with
`SMTP_HOST` unset, the documented local setup, that file holds magic-link URLs with live sign-in
tokens.

`.dockerignore` excludes `.env` (but not `.env.example`), logs, `__pycache__`, the local venv and
the tool caches.

## `GET /certifications-expiring` mirrors `/gear-service-due`, deliberately

The dashboard's renewal card needs the few certifications with expiry dates, and
`GET /certifications` sorts by neither date; `/gear-service-due` already solves this for gear, and
this is its twin.

It takes no `within_days` parameter: a server-side horizon bakes today's date into a 60-second
cached response, wrong at midnight. With no date input the response is a pure function of stored
rows, and the client buckets expiring-soon/expired itself — the trade `GearServiceDueResponse`
documents.

Two deliberate differences from gear: undated cards are excluded by the query, not sorted last (most
recreational certifications never expire, and none belong on a renewals card); and it carries no
card-file metadata, since embedding `files` would mean the batched lookup
`_cached_read_certifications` does.

The cache key `user_{id}_certifications_expiring` starts with `user_{id}_certification`, so
`invalidate_certification_caches` already sweeps it; that prefix overlap is load-bearing, and a key
outside it leaves a stale list after every card edit.

## The dashboard overviews say when they are truncated

`GearServiceDueResponse` and `CertificationExpiringResponse` return every matching row rather than a
date-filtered slice, so both carry a row cap (`DUE_OVERVIEW_LIMIT` / `EXPIRING_OVERVIEW_LIMIT`, both
200\) and a `truncated` flag, and the web client says the list is partial. A cap without the flag
shows a diver past it a card that looks complete while overdue kit is missing — the wrong direction
to fail in for a safety-adjacent card.

Both queries select `limit + 1` rows and drop the extra, so `truncated` is exact;
`len(rows) == limit` would report a list that happens to end on the boundary as truncated, the kind
of false alarm that gets ignored.

`truncated` defaults to `False` so an older client, or a cached response written before the field
existed, does not read as "the list is partial".

## Update schemas refuse an explicit null for a `NOT NULL` column

Update-schema fields are `T | None` because omission means unchanged; an explicit `null` on a
`NOT NULL` column is rejected by a `model_validator` naming the field, not by Postgres, whose
`IntegrityError` `_fk_error_detail` renders as "Invalid reference: a related record does not exist."
`patch_dive`'s `start_time` branch keys off `model_fields_set`, so a null cannot skip
`split_start_time` and stale `utc_offset_minutes`. `trip_uuid: null` stays the only way to detach a
dive.

The rule is `RejectsExplicitNulls` in `core/schemas.py`; each guarded update schema hand-lists its
columns in `NON_NULLABLE_FIELDS`, since a field is not always a column.
`tests/test_update_explicit_nulls.py` reads each list off the table metadata, so a new `NOT NULL`
column cannot reopen it. `DiveSiteUpdate`'s `latitude`/`longitude` clear only as a pair under
`WholeCoordinatePair`, named in `CLEARED_ONLY_IN_PAIRS`.

`UNGUARDED_UPDATE_SCHEMAS` names the exemptions (admin-panel-only schemas, since CRUDAdmin's form
never sends `None`, and the server-constructed
`AuthenticationProviderUpdate`/`AuthenticationRequestUpdate`); a companion test fails on any
`*Update` class in neither list.

## The `trip_uuid` detach path is pinned by `tests/test_dive_update.py`

`PATCH /dive/{uuid}` detaches a dive from its trip only through

```
if "trip_uuid" in values.model_fields_set:
    if values.trip_uuid is None:
        update_data["trip_id"] = None
```

The web client's "remove from trip" is built on it, and without the branch the request succeeds,
reports "Dive updated" and changes nothing — a silent no-op. `tests/test_dive_update.py` pins both
halves: an explicit null detaches, an omitted key leaves the trip alone. It needs no database;
`patch_dive`'s collaborators are stubbed and the assertions are on the `update_data` handed to
`crud_dives.update`.

## Test users get uuid-derived names, because nothing cleans them up

`create_user` writes a real row wherever `POSTGRES_SERVER` points and nothing deletes it, so the
`user` table accumulates. `fake.user_name()`/`fake.email()` draw from a small vocabulary against
`unique=True` columns, so a few hundred rows in the birthday problem raises `IntegrityError`
intermittently; `fake.unique` de-duplicates within one process, not against stored rows.

`unique_username()`/`unique_email()` in `conftest.py` use `uuid7().hex[-12:]`: the low 48 bits,
which `uuid6` fills from `secrets`, so the names are neither time-ordered nor unique by construction
— CSPRNG entropy with an even chance of collision past sixteen million names, not "cannot collide".
Twelve characters fits the username rule (`^[a-z0-9]+$`, 2-20 characters).

Addresses are `@example.com`, not `.test`: `email-validator`, behind Pydantic's `EmailStr`, rejects
`.test` as special-use, so a schema round-trip fails.

Stored rows stay until `docker compose down -v` or the drop statement in `CONTRIBUTING.md`; fixtures
that roll back are the real fix.

## A session's subject is the user's `uuid`, because a username can change hands

The token subject is `str(user.uuid)`, the immutable `uuid7` from `PublicUUIDMixin`;
`get_current_user` is one `crud_users.get(uuid=..., is_deleted=False)`, and `TokenData.user_uuid` is
typed `uuid.UUID`. `create_access_token`/`create_refresh_token` add only `exp` and `token_type`, so
the subject roots `fetch_owned_or_raise`'s ownership model.

A username is not stable: `PATCH /user` frees it instantly, so a token naming it locks the renamer
out, authenticates the name's next holder, and lets an attacker keep a token parked on a handle via
`/auth/refresh` until a victim claims it; blacklisting in `patch_user` cannot close that, since the
orphaned refresh token is never presented.

`verify_token` catches `ValueError` beside `JWTError` (`uuid.UUID("someuser")` raises it) so a
forged `sub` is a 401. A deleted account is the other half; see *"A refresh token is only as alive
as its account"*.

Only `PATCH /user`'s username branch is rate-limited per user: "Username not available" is the
oracle `/auth/complete` throttles (`AUTH_COMPLETE_RATE_LIMIT_PER_IP`), and a `gear_service_emails`
toggle must never 429.

## Non-ASCII filenames need RFC 6266, because Starlette encodes headers as latin-1

`safe_filename` keeps printable non-ASCII because `original_filename` is what clients display.
Starlette encodes every header value as latin-1, so interpolating that name into
`Content-Disposition` raises `UnicodeEncodeError` above U+00FF — a permanent 500 on
`GET /certification/{uuid}/file/{side}` and `GET /dive/{uuid}/file/{fid}`; accented Latin merely
arrives as mojibake.

`content_disposition_attachment` (`core/utils/uploads.py`) builds both parameters RFC 6266 defines:

```
attachment; filename="card.jpg"; filename*=UTF-8''%E6%BD%9C%E6%B0%B4.jpg
```

`filename*` carries the real name percent-encoded as UTF-8 for every current browser; plain
`filename` carries an ASCII folding for `curl -OJ`, kept inside RFC 5987's `attr-char` by
`quote(name, safe="")`.

Folding to ASCII in `safe_filename` at upload is rejected: it mangles non-Latin divers' filenames
everywhere and leaves stored rows broken. Only the header has to be narrow.

In `_ascii_fallback` the folded name goes back through `safe_filename`, since NFKD maps fullwidth
punctuation to ASCII (`＂` → `"`) and a `"` would close the quoted string; a name with no ASCII folds
to a bare extension, so `default` supplies the stem.

## Dives-per-day is counted in Python, off two columns, not by `date_trunc`

`GET /user/dive-activity` returns one `{year, month, day, dives}` per day dived, counted in Python
off `start_time` and `utc_offset_minutes`, not by `GROUP BY date_trunc`.

- `combine_start_time` (`core/utils/datetime_offset.py`) is the offset arithmetic's only home; a SQL
  copy drifts a day off from the dive pages.
- The day is the dive's local one (the `diveWallClockTime` rule).
- Days, not months: `DiveActivityCard` sums upward itself; a `granularity` parameter would cost a
  second cache entry.
- Empty days are absent; the client draws its own grid.
- Sorted on the buckets: `ORDER BY start_time` orders by instant, not local day.
- Cached under `user_{id}_dives:dive_activity`, within `invalidate_dive_caches()`'s
  `user_{id}_dives:*` sweep.

Changing the shape of a cached response outlives the restart that ships it: a hit skips the route
body, so a previous build's entry fails `response_model` validation (`ResponseValidationError`)
until its TTL expires; see *"A deploy cannot serve the previous build's response cache"*.

## FIT is one parser for both vendors, and its one real trap is developer fields

`FitParser` (`services/dive_parsers/fit.py`) is one parser for both vendors: the FIT profile fixes
fields, units and scales, `fitdecode` (unlike `fitparse`, it decodes developer fields) applies it;
vendor differences are additive.

The trap: never build `{field.name: field.value}` from `frame.fields`. Suunto's developer fields
shadow native names (`max_depth` twice), so `_native_value()` skips `fitdecode.types.DevField`;
`get_value()` is unsafe when only the duplicate exists.

Also: samples stop at the first `session`, `dive_summary`/`tank_summary` being collected above the
cut; `message_index` and `sensor` go through `_native_raw`, the index masked `0x0FFF`;
`start_time`'s offset comes from `activity.local_timestamp`; `bottom_temperature` is
`session.min_temperature` else the coldest `record`, never `max_temperature`; a non-diving file is a
`DiveParseError`, not `UnsupportedDiveFileError`; pressures come from `tank_summary` else
`tank_update`, joined on `sensor`, paired to gases by `_tanks_for` only when counts match;
`_MAX_CYLINDERS` caps the total.

Gaps: telemetry is unbounded past the in-water part, and Suunto FIT has no transmitter data, leaving
the tank paths unconfirmed.

## Cylinder reconstruction is best-effort, and never fails an import

`_mixtures_from_cylinders` runs for every Suunto JSON export with no `Gases` block, including ones
with no cylinder data, and walks a sample stream whose shape is barely documented; a structural
surprise there must not turn a fine import into a 422.

Two layers, in order. The known causes are fixed at source: timestamps are compared only when both
sides agree on tz-awareness (a naive `Header.DateTime` against offset-aware samples otherwise fails
on `moment > dive_end`), and a `Cylinders[]` reading with no `GasNumber` is skipped rather than
indexed. The call is additionally wrapped so anything else degrades to "no mixtures" and logs. The
wrapper is not the fix; it acknowledges that this is enrichment layered on a format we do not
control, while the header fields are what the diver came for.

## Uploaded files are parsed in a thread, not on the event loop

`POST /dive/parse` and `POST /dive/{uuid}/recordings` hand their bytes to `run_in_threadpool`:
parsing is pure CPU, and the pure-Python FIT decoder is two orders of magnitude slower per byte than
the C `json` module, so inline in an `async def` one upload stalls the worker; XML and JSON go the
same way.

The read transaction is released first: `run_in_threadpool` frees the event loop, not the
connection, and `_find_by_digest`/`get_existing_profile` have already opened one, so without
`_release_read_transaction` it sits idle-in-transaction. Safe because nothing is written yet and
both lookups return frozen dataclasses; `TestProfileExtractionReleasesTheTransaction` pins the
ordering.

A thread is not a bound: a 5 MB file of bare `record` messages takes ~10 s to decode against AnyIO's
40-thread limiter. `_MAX_FRAMES` (100 000) bounds frames decoded, not samples collected, since
decoding is most of the cost, and raises rather than truncates: `session` follows the samples it
summarizes, so truncation imports a confidently empty dive.

## The XML pressure channel is labelled from `<TransmitterId>`, not from counting to one

`SuuntoXmlParser._parse_samples` labels its single pressure series by `_transmitted_gas_number`, the
position of the one mixture whose `<TransmitterId>` is not nil, falling back to `_XML_GAS_NUMBER`
when none or several are set. A hardcoded `gas_number=1` assumes the transmitted cylinder is also
the first; a transmitted deco bottle behind an untransmitted back gas attaches its curve to the back
gas, whose `<StartPressure>0</...>` `_drop_unpressurized` nulls, so nothing downstream contradicts
it.

The corpus supports "exactly one": 98 exports have one transmitted cylinder, 244 none, none two, and
pressure samples appear in precisely those 98. With several, one `<Pressure>` per sample and no key
means the format cannot say which is which, so the fallback is no worse than the hardcode.
`test_the_xml_pressure_channel_is_labelled_from_the_transmitter_not_from_position` constructs the
dive the corpus lacks.

Two numberings that both start at 1 agree on every example; that is a corpus property, not a
guarantee.

## The 2026 Suunto Ocean JSON is a third header shape, with gas data only in the samples

The 2026 Suunto Ocean export has no `Header.Diving` block; gas data lives only in
`Samples[].Cylinders[]`, so `_mixtures_from_cylinders` builds a mixture per reporting cylinder, with
pressures from its first and last reading. `Header.Diving.Gases` still wins where present; the
sample path is a fallback, not a merge.

- Ordered by sample timestamp, not array position; sensor streams interleave.
- A `null` `Pressure` is skipped; final samples null even the live slot.
- Cylinders come from `DiveEvents.GasSwitch`, not from which tanks transmitted, or a two-tank dive
  collapses into one mixture holding both pressures, which `compute_gas_use` turns into an RMV.
- Readings after `Header.DiveTime` are dropped, with no fallback to `Duration`; the profile series
  stays unbounded.
- `oxygen`/`helium`/`volume` are `None`.

FIT and JSON exports of one Ocean dive are complementary (FIT has the mixes, JSON the pressures);
merging needs more than one source file per dive, which `ux_dive_file_dive_id` rules out.

## Parsers report what a file recorded, and `None` for what it didn't

`DiveMixtureSchema`'s `oxygen`, `helium` and `volume` are nullable, and no parser invents a value
for data a file lacks: a coerced `0.0` presents a hypoxic mix in a cylinder
`ck_dive_mixture_volume_positive` rejects and overwrites `DEFAULT_MIXTURE` on the form. A `notices`
array on `ParsedDiveResponse` is rejected as a second mechanism walking back an untrue assertion;
`toMixtureFormValue` (`dive-file-import.tsx`) fills a `null` from `DEFAULT_MIXTURE` instead.
`Helium: 0` survives — `??`, not `||`. `TestParsersInventNothing` pins all three parsers;
`DiveMixtureCreate` and the DB constraints, describing a saved dive, stay. `volume` is always `None`
from FIT.

`fitdecode`'s `CrcCheck.WARN`/`ErrorHandling.WARN` defaults stay; the CRC guards transfer corruption
only. `_scan` catches `Exception`, not `fitdecode.FitError`, because corrupt uploads escape as
`AssertionError`, `ValueError` or `TypeError`. Extraction catches `EXTRACTION_ERRORS`
(`dive_parsers/exceptions.py`), one tuple shared by all three parsers, including `ArithmeticError`
and `OverflowError` for NaN and `Infinity` inputs. `parse_dive_file_with_parser` converts anything
else into a `DiveParseError`. Both entry points decode in full: `session` follows the samples.

## A parser reports what recorded the file

`ParsedDiveSchema.device` is a `ParsedDevice` (`schemas/parsed_dive.py`): `brand`, `model`,
`serial`, `firmware`, `name` and `dive_number`, all nullable, backed by
`dive_recording.device_brand` through `device_dive_number`. `POST /dive/parse` returns it;
`DiveCreate` is `extra="forbid"`. Only the serial tells two records of one dive apart.

FIT: `file_id.manufacturer` is `brand`; `file_id.product_name` is the model, never
`file_id.product`, a vendor id `fitdecode` resolves only for `garmin_product`/`favero_product`; the
serial is the `device_info` whose `device_index` is 0 (raw via `_native_raw`; `fitdecode` renders it
`creator`), else `file_id.serial_number`; firmware from the first `device_info` naming the file's
manufacturer (tank pods write one too). Suunto JSON: `Header.Device` for `SerialNumber`, `Info.SW`
and `Name` (a name, not a model), plus `Header.Diving.NumberInSeries`. Suunto XML: `<Source>`,
`<SerialNumber>`, `<Software>`, `<DiveNumberInSerie>`. Both Suunto parsers fix `_BRAND` as `Suunto`;
brand case is kept as read.

`ParsedDevice` reads a trimmed `""` as `None`; `_drop_empty_device` nulls an all-empty device.
Device `dive_number` is guarded `< 0`, like `_drop_negative_gas_number`; the dive's own stays null.

## A zero cylinder pressure is not a reading, and that does not contradict the rule above

`DiveMixtureSchema` nulls a parsed `start_pressure`/`end_pressure` of 0. This invents nothing:
`Helium: 0` survives (`TestParsersInventNothing`) because 0 is inside a gas fraction's range; 0 bar
is outside any breathed cylinder's.

DM5 means "no transmitter". Its XML writes
`<StartPressure>0</StartPressure>`/`<EndPressure>0</EndPressure>` where `<TransmitterId>` is
`xsi:nil`, and its JSON export of the same dive omits both while keeping `Helium: 0`. Read
literally, the zero makes `SuuntoXmlParser` assert what `SuuntoJsonParser` declines;
`_mixtures_from_cylinders` likewise skips a null Ocean `Pressure`.

The zero breaks the form: `mergeMixture` (`lib/dive-import.ts`) overwrites typed pressures because 0
is not nullish, and `diveMixtureSchema` requires a positive `start_pressure`, so the edit form can
never submit. Zod `min(0)` is rejected: it stores a cylinder breathed from 0 bar as fact.
`end_pressure` allows 0 (next section).

The guard is on the schema, not in `SuuntoXmlParser`: the fact is about the field, not the format.
`<= 0` also covers a negative gauge.

## A cylinder pressure is a bounded field, and every layer that writes one says so

A `start_pressure` of 0 must never reach the database. `DiveMixtureSchema` nulls a parsed 0, but
without the rest the API accepts any float, `dive_mixture` carries only the ordering constraint, and
a client can store a 0 that the dive's own edit form then refuses to submit. Every layer that writes
a pressure bounds it:

| Layer                                  | `start_pressure`             | `end_pressure`               |
| -------------------------------------- | ---------------------------- | ---------------------------- |
| Parse (`DiveMixtureSchema`) - a *file* | outside `(0, 350]` -> `null` | outside `(0, 350]` -> `null` |
| Request (`DiveMixtureCreate`/`Update`) | `gt=0, le=350` -> 422        | `ge=0, le=350` -> 422        |
| Read (`DiveMixtureBase`/`Read`)        | **unbounded**                | **unbounded**                |
| DB (`CHECK`)                           | `> 0 AND <= 350`             | `>= 0 AND <= 350`            |
| Form (`diveMixtureSchema`, web)        | `positive().max(350)`        | `min(0).max(350)`            |

## Cylinder pressure bounds: The asymmetry is physical, and that is the whole shape

You cannot start a dive on an empty cylinder; you can finish one on it. A cylinder at 0 bar gauge
delivers nothing (the first stage needs supply above ambient), so no dive's first breath came from a
cylinder reading 0 and no diver types one meaning it. An out-of-gas ascent or a drained stage with
the SPG pegged at zero is rare, real, and exactly the dive worth logging honestly. So
`start_pressure` ∈ `(0, 350]` ∪ `NULL` and `end_pressure` ∈ `[0, start_pressure]` ∪ `NULL`, with
`ck_dive_mixture_pressure_order` unchanged. The rule is "a cylinder that was never filled", not "a
zero is suspicious": a 0 helium fraction and a 0 CNS reading are untouched. An empty box already
normalises to `null`, so a typed 0 is never a sentinel.
`TestCreateAndUpdateAllowAnEmptyCylinderAtTheEnd` and `test_zero_end_pressure_is_allowed` pin the
asymmetry from both sides against a later tidy-up.

## Reject at the request layer, coerce at the parse layer - not a contradiction

The two schemas answer different questions. `DiveMixtureSchema` describes a file (its docstring:
*"this schema describes a file, that one describes a dive being saved."*). A 0 there is a known
dialect, DM5's "no transmitter", so translating it to `null` is interpretation; a 422 would reject a
value the diver never typed and fail the attach of an importable export. `DiveMixtureCreate`
describes a dive a client asserts: the wire format has `null`, every client can send it, and a 0
here is a bug or typo coercion would swallow.

The file already runs this split twice. `_drop_negative_gas_number` exists because
`DiveMixtureCreate`'s `ge=0` would otherwise 422 a field the diver never chose, and
`_drop_implausible_po2_limit` states the rule: no parsed value reaches a bounded column without
passing the bound the column applies. `po2_limit` and `gas_number` each pair a bounded request field
with a parse-side coercion; `start_pressure` had the coercion and no bound.

## The bound goes on `Create`/`Update`, never on `DiveMixtureBase`

`DiveMixtureRead` inherits `DiveMixtureBase`, and `crud_dive_mixtures` runs
`DiveMixtureRead.model_validate(row)` over every mixture on every read, so a bound on `Base`
validates stored rows on the way out: one violating row turns `GET /dives` and `GET /dive/{uuid}`
into a 500, and `services/export/envelope.py` rebuilds a `DiveMixtureBase` per mixture, so the full
export breaks on the same row. `_ParserOutput`'s docstring records a stored `NaN` doing exactly
that.

So `start_pressure` and `end_pressure` are redeclared on `DiveMixtureCreate` (which
`DiveMixtureCreateInternal` inherits, covering the admin create path) and on `DiveMixtureUpdate`.
`TestTheReadSchemaStaysUnbounded` fails the moment someone hoists them to `Base`.

The web side's `toDiveMixtureInput` keeps its `??` rather than coercing a stored 0 to `""`: a 0 from
a database the `ALTER` never reached must surface as a legible form error, not a 500 and not a
silent repair.

## Why a Pydantic bound *and* a `CHECK`, when `volume`/`oxygen`/`helium` have only a `CHECK`

This picks the newer convention. `volume`, `oxygen` and `helium` are `CHECK`-only, so
`_MIXTURE_CONSTRAINT_MESSAGES` exists and its entries fire; `po2_limit` and `gas_number` are bounded
in Pydantic and mirrored by a `CHECK`. Pydantic answers first, so a diver sees
`mixtures.0.start_pressure: Input should be greater than 0` and the curated sentence never fires via
the API; worth it, because a Pydantic 422 names the field and the index, which a `CHECK` cannot, so
the form can focus the box, before any transaction. The messages stay as the backstop for writes
that bypass Pydantic (`backfill_tech_fields`-style Core `UPDATE`s); without an entry
`_mixture_error_detail` falls through to a bare "Invalid gas mixture."

`"Start pressure must be above 0 and at most 350 bar."`, not "between 0 and 350", which tells a
diver who typed 0 the rejected value is legal; `"End pressure must be between 0 and 350 bar."` is
right, there 0 is legal.

## 350 bar, and why the band is two-sided

The upper half catches two things. A unit error: the DM5 XML parser once stored millibar as bar,
`start_pressure ~ 205203` (*"DM5 XML expresses every pressure in millibar"*). And `NaN`: Postgres
sorts it above every number, so `'NaN'::float8 > 0` is true and only `<= 350` rejects the value that
turns `GET /dives` into a 500 (`test_nan_pressures_are_rejected`).

350, not 500: 500 is this file's factor-of-1000 correction heuristic, not a validity threshold, and
as a bound it admits a sidemount pair summed into one cylinder (`415`/`230`), which no other
constraint rejects. 350 clears any real 300 bar DIN fill (`test_a_300_bar_din_fill_is_allowed`); the
band catches unit errors, not filling habits.

The parse layer follows: `_drop_unpressurized` nulls anything outside `(0, 350]`, like
`_drop_implausible_surface_pressure`, or `/dive/parse` would prefill a 205203 that 422s on Save.
`TestEveryParsedPressureSatisfiesTheRequestSchema` drives a real parser with out-of-range files,
since no corpus fixture produces one.

## Cylinder pressure bounds: No backfill, and an out-of-band pressure is a 422 for clients

No stored row violates either bound, so the constraints validate as added. Do not re-run the earlier
import fix's `UPDATE ... WHERE start_pressure <= 0 AND end_pressure <= 0`.

```sql
ALTER TABLE dive_mixture ADD CONSTRAINT ck_dive_mixture_start_pressure_range CHECK (start_pressure IS NULL OR (start_pressure > 0 AND start_pressure <= 350));
ALTER TABLE dive_mixture ADD CONSTRAINT ck_dive_mixture_end_pressure_range CHECK (end_pressure IS NULL OR (end_pressure >= 0 AND end_pressure <= 350));
```

API contract: an out-of-band pressure is a 422 on `POST /dive` and `PATCH /dive/{uuid}`
(`DiveMixtureCreate`) and the admin panel (`DiveMixtureUpdate`); imports pass `_drop_unpressurized`
first. Nothing downstream changes: `compute_gas_use` and `_pressure_used` return `None` on a
non-positive drop, UDDF export skips a null start, and `merge_mixture_fields` writes only
`po2_limit`/`gas_number`/`role`.

Sidemount and independent doubles, which the band surfaced as having no first-class representation,
are handled by `DiveMixture.usage` and `compute_parallel_gas_use` (*"`DiveMixture.usage` is how a
cylinder was breathed, and it is what makes a sidemount pair summable"*). There is no backfill
flagging existing pairs `parallel`: no file says which two-cylinder dives were parallel.

## The mixture backfill joins on position, so the read it joins against must be ordered

`merge_mixture_fields` joins parsed cylinders to stored ones by position: saves replace mixtures
wholesale, so a stored `id` names no parsed cylinder. Postgres does not owe insertion order after
`backfill_tech_fields`'s `UPDATE` moves a tuple, so `crud_dive_mixtures.get_mixtures_for_dive` and
`get_mixtures_for_dives` carry `.order_by(DiveMixture.id)`, which `replace_mixtures_for_dive` makes
the import's position. `TestStoredMixturesAreReadInSavedOrder` pins it; `fill_mixture_fields` shares
it.

The `(oxygen, helium)` check is no backstop: a parsed `None` is not a mismatch, and the Suunto Ocean
shape (`_mixtures_from_cylinders`, no `Gases`) leaves every fraction `None`. Its `gas_number` is the
file's label, joined to `dive_profile.data.pressure[].gas_number`, so a swap silently crosses the
tanks' pressure curves.

The mixture half is fill-only — `po2_limit`, `gas_number` and `role` are written only where parsed —
because all three are client-writable (`DiveMixtureCreate`, `PATCH /dive/{uuid}`), and a parser's
`None` means the file did not record it. The dive's own scalars are overwritten outright, `None`s
included, because only the import writes them (`DiveCreate`/`DiveUpdate` are `extra="forbid"`).

## A parsed value the database refuses must not take the upload — or the backfill run — with it

`store_tech_scalars` writes inside `store_recording_file`'s transaction, where `IntegrityError`
becomes
`DiveFileConflictError("This dive's recordings changed while this upload was in flight. Please try again.")`
— wrong for a `CHECK` violation (a 500 on the `noop` branch). Guards:

- `ParsedDiveSchema` validators mirror each `CHECK`'s numbers, nulling, not rejecting:
  `surface_pressure_bar` and `po2_limit` two-sided; `cns_start`, `cns_end`, `otu_start`, `otu_end`,
  `gas_number` at `< 0`; `avg_depth`, `max_depth` at `<= 0`. Scope: bounded and parser-reachable.
  `ck_dive_mixture_oxygen_helium_sum`, `ck_dive_mixture_pressure_order`, `duration`, `volume`,
  `oxygen` and `helium` stay unguarded.
- The `noop` branch's writes and commit share one `try`/`rollback`, since a `CHECK` raises from
  `execute()` (`TestReExtractionFailureDoesNotFailTheRequest`).
- `begin_nested()` per dive in `backfill_tech_fields` wraps the statements; a rejected dive counts
  into `failed`.

A one-sided comparison is not a bound (`nan < 0` and `'NaN'::float8 >= 0` both pass), and
`JSONResponse` (`allow_nan=False`) 500s `GET /dives`. `_ParserOutput`, base of `DiveMixtureSchema`,
`ParsedDiveSchema` and `ParsedDiveResponse`, nulls non-finite floats, typed `object` so other fields
pass untouched (`test_the_finite_guard_does_not_touch_anything_else`):

```python
@field_validator("*")
@classmethod
def _drop_non_finite(cls, value: object) -> object:
    return None if isinstance(value, float) and not math.isfinite(value) else value
```

## Replacing an export clears its readings even when the new one can't be read

Attaching appends rather than replaces, and the tech-scalar write is outright for a recording's
*first* file and filling for every later one, which of the two being a parameter of the write rather
than a condition inside it — see *"A second file of one recording fills, and never overwrites"*.
Outright means unconditional, `scalars is None` included: a failed extraction must not leave a
previous file's CNS and OTU standing where nothing can re-derive or check them, the state
`delete_dive_file` clears them to avoid and the rule `delete_profile_for_dive` already applies to
the profile. Nothing is lost, because `backfill_tech_fields` re-reads every candidate on every run.
The `noop` branch differs: the file is unchanged and still attached, so "couldn't read" leaves the
dive alone there. `test_an_unreadable_header_still_clears_the_previous_export` and
`test_an_unreadable_header_leaves_the_dive_alone_on_this_branch` pin both sides.

## The backfill's batch boundary counts writes, not positions

`backfill_tech_fields` commits every `_BACKFILL_BATCH_SIZE` dives *written* (`pending`), not every
`_BACKFILL_BATCH_SIZE` positions: an `index % _BACKFILL_BATCH_SIZE == 0` check below the loop's
`continue`s skips the commit whenever the dive on the boundary fails, so an interrupted run loses
two batches. `mixtures_skipped` counts `max(len(stored), len(parsed.mixtures))` rather than
`len(stored)`, because a diver who deleted every cylinder is a count mismatch with an empty `stored`
and would report 0 — and that counter is the run's one interesting signal, a diver having edited
their cylinders.

## `load_dive_file` detaches the row it read, because the blob outlives the need for it

`local_session` is built `expire_on_commit=False` (`core/db/database.py`), right for a request: a
committed instance stays usable without a `SELECT` per attribute. A loader that `undefer`s a payload
column pays for it: the instance stays in the identity map with the blob materialized, nothing
expires it, and a backfill walking every row in one session holds every payload it has read. The
loader `db.expunge()`s the row before returning, having copied out what callers want — there rather
than in the backfill loop, so every caller gets it from the one place that knows a blob was loaded.
File payloads live on the files volume (see *"File payloads live on the files volume, not in
Postgres"*), so the two file loaders read explicit columns and have nothing to detach;
`load_profile` (`services/dive_profiles.py`) is the one loader that still undefers a real column,
`dive_profile.data` being JSONB, deliberately.

## The tech scalars re-extract on the profile's version gate, not one of their own

`store_recording_file`'s `noop` branch re-reads a recording's files when `PROFILE_EXTRACTOR_VERSION`
says the stored profile is stale, and the tech scalars ride that condition rather than a version of
their own — two columns of extra state to bump and get wrong, for a re-upload path that is already
opportunistic. The consequence: a scalar-only parser fix does not reach existing dives through a
re-upload, because `should_extract` still answers "current". `backfill_tech_fields` re-reads every
candidate on every run so that it needs no version to bump; a fix of that shape ships with a
backfill run, not with instructions to re-upload.

## `parse_all` exists so FIT decodes the file once, and `_extract_all` falls back when it can't

`_extract_all` pairs the profile and the tech scalars into one `run_in_threadpool` hop, and on FIT
both `parse` and `parse_profile` reach `FitParser._scan`, so decoding twice roughly doubles
worst-case attach latency. `DiveParser.parse_all` is the seam: its default is `parse()` +
`parse_profile()`, which both Suunto parsers keep (2–11 ms; sharing would be machinery for nothing);
`FitParser` overrides it to scan once.

Independence survives at both levels. `_scan` is a pure function of the bytes, so a file failing it
fails both entry points anyway; `_parse_dive` raising where `_parse_samples` would not is guarded
separately inside the override. An override can still fail both together, so `_extract_all` falls
back to the two independent extractions on any exception — the bare `except` is deliberate, an
override being third-party code to that function — and a file with malformed samples still yields
its header scalars. `TestExtractAllSharesOneDecode` asserts a scan *count*, not a wall-clock time.

## FIT fixtures are written, not committed as blobs

FIT is binary, and a committed `.fit` blob cannot be edited to express "a session whose developer
field shadows a native one" or "a dive with no `activity` message", which is what needs pinning.
`tests/helpers/fit.py` is a minimal FIT writer — definition and data records, developer-field
declarations, the spec's nibble-table CRC-16 — driven by `fitdecode`'s own copy of the global
profile. Field numbers, base types, scale factors and enum members are looked up rather than
hardcoded, so a fixture reads `message("session", sport="diving", max_depth=32.41)` and cannot drift
from the profile the parser decodes through; its output passes `CrcCheck.RAISE`. This keeps the
inline-fixture, no-database style of `test_dive_parsers.py` and `test_dive_profiles.py`. Only what
those tests need exists: little-endian, one definition per data message, no compressed timestamp
headers, no accumulators.

## The markdown docs are formatted by mdformat, not Prettier

`uv run mdformat *.md docs .github tests` formats the markdown; `.github/workflows/linting.yml`
checks it beside `ruff format --check`. Prettier (opendiving-web's choice) would add `package.json`,
a lockfile and `setup-node` to a Python repo's CI; mdformat comes with `uv sync --extra dev` like
ruff and mypy. `.mdformat.toml` sets `wrap = 100`, matching web's Prettier `printWidth`, and
`number = true`, so ordered lists keep counting rather than every item becoming `1.`.

`mdformat-gfm` is required: core mdformat is CommonMark only and reflows a GFM table as a paragraph.
Paths are spelled out because 1.0 has no `--exclude` and `.` walks `.venv/`, `.pytest_cache/` and
`.claude/` worktrees; the list must cover every file `git ls-files '*.md'` prints, and lives in five
places — the workflow, `CONTRIBUTING.md`, `AGENTS.md`, `.mdformat.toml`'s header comment and this
section. A wrapped line starting with `-` comes back as `\-`; it renders as a dash.

## Per-tank gas use is attributed from the device's gas switches, and from nothing else

`dive_profile.gas_attribution` holds one entry per gas number — seconds on that gas and the mean
depth over them — which `derive_gas_attribution` derives at extraction from the gas-switch events.
`compute_multi_tank_gas_use` joins it to the mixtures on `gas_number` and normalizes each cylinder
against its own time and depth; the dive average would understate a deep back gas's rate by a third.

Rejected: per-cylinder pressure activity, since one transmitter on the back gas gives no second
curve and reads down as the cylinder cools after a switch; and a single-gas fallback, dead code
beside `compute_gas_use`.

Seconds sum and mean depth spans every stretch of a gas. The same gas selected twice running is one
stretch. A switch before the first sample is the starting gas, clipped forward like
`_rebase_events`; time before the first switch stays unattributed. Attribution runs before
`downsample` in `finalize_profile`, since min/max bucketing keeps only extremes.

## The cylinder pressures come from the mixtures, not from the profile's pressure curve

`gas_attribution` carries no pressures; consumption comes from `DiveMixture.start_pressure` and
`end_pressure`, as the single-cylinder path does. Two reasons. The header and the sample stream
disagree: the header records a back gas ending at 122.44 bar while the same file's samples end at
117.8, because the transmitter keeps reading while the cylinder cools after the diver has switched
away — the header is the device's account of the cylinder, the series a thermometer with a gauge
attached. And the mixtures are editable through the dive form, while a profile is immutable until
the export is re-imported, so a profile-derived pressure would silently outrank a correction. That
split is why the column is `gas_attribution` rather than `gas_usage`: it holds which cylinder, for
how long, how deep, and nothing another table already knows. The per-tank wire field stays
`seconds_on_gas`, because beside `seconds`-less figures a bare `seconds` would not say what it
counted.

## A multi-cylinder figure covers the cylinders it can account for, and says so

`DiveGasUse` carries `tanks`, `attributed_seconds` and `duration`; `gas_used` and `rmv` cover the
cylinders accounted for. All-or-nothing would report nothing on the common shape: one back-gas
transmitter plus a pressureless deco bottle. The coverage fraction states the scope; `duration` is
the profile's span, not the dive's editable one.

Refused outright: a duplicate `gas_number` (a device's label, ambiguous to join); a cylinder with a
pressure drop but no attribution entry, its time inflating another tank's; a tank over
`MAX_PLAUSIBLE_RMV = 100`, a late-recorded switch, not applied to `compute_gas_use`. A cylinder
failing the per-tank checks is left out, its seconds a reported shortfall; `derive_gas_attribution`
drops zero-length stretches so such cylinders reach the refusal.

`sac_bar_per_min` is null here, bar/min being a rate per cylinder volume, except a flagged
`parallel` set of equal volumes (see *"The pooled `sac_bar_per_min` exists only for a flagged
parallel set of equal volumes"*). `resolve_gas_use` is the only entry point.

## A stored profile is a pure function of `PROFILE_EXTRACTOR_VERSION`, and `gas_attribution` is nullable

Version 3 covers the new column and `_downsample_series` pinning each channel's first and last
sample (see *"Profiles are capped at 1 200 points"*); a stored profile is a pure function of its
version. Every profile ETag changes;
`docker compose exec api python -m src.scripts.backfill_dive_profiles` flushes the dive caches it
touches — run it off-peak. `create_all()` never alters an existing table (see *"Schema changes have
no migration tool"*):

```sql
ALTER TABLE dive_profile ADD COLUMN gas_attribution JSONB;
```

Nullable, like `event_count`: NULL is "extracted before attribution existed", `[]` is "found
nothing". `store_profile` always writes a list; `get_gas_attribution_for_dives` returns `[]` for
either. No `CHECK`s, since nothing comes from a request body; `GasAttribution.model_validate` on the
way out degrades an older payload to "no per-tank figure" with a warning, not a 500. Not `deferred`,
unlike `data`: a few integers read on every dive detail; fetched by its own narrow query, not
`get_profile_infos_for_dives`, because `gas_use_history` needs only this column.

## `DiveMixture.name` is not a field: the label is a pure function of the fractions

`DiveMixture` carries no free-text cylinder label: not in `DiveMixtureBase`/`DiveMixtureUpdate`, not
in `DiveMixtureSchema`, and no parser passes `name=`. The label a diver recognizes is a pure
function of the fractions — the web client derives it with `gasName(oxygen, helium)` — so a stored
string beside `oxygen`/`helium` is a second source of truth free to disagree with the numbers
("EAN32" at 21 % O₂). What the cylinder is *for* is `role`.

`DiveMixtureCreate` and `DiveMixtureUpdate` are `extra="forbid"`, so a client still sending `name`
gets a 422 naming the field. A silent drop would let a form go on collecting a label that lands
nowhere.

`create_all()` does not drop columns, so an existing database keeps the nullable column and every
insert leaves it `NULL`; nothing breaks. Drop it to reclaim the strings:

```sql
ALTER TABLE dive_mixture DROP COLUMN name;
```

No backfill and no `PROFILE_EXTRACTOR_VERSION` bump: nothing derives from the column.

## The export endpoints are never cached, and say `no-store`

Every `GET /api/v1/export/…` route carries no `@cache` decorator and answers
`Cache-Control: no-store`, set in one place: `_download` in `api/v1/export.py`. The rule is by route
prefix, so anything `_download` answers gets it.

Redis holds serialized API responses, and an archive of a real logbook is megabytes; parking one
there evicts everything the cache exists for — the argument of *"Deliberately not `@cache`d"* on
`read_dive_file`. There is nothing to invalidate it on: an export goes stale on every write, so
`invalidate_dive_caches` would need a sibling per table for a hit rate near zero. And a cached whole
account is *"`@cache` and per-request authorization don't mix directly"* at maximum blast radius —
one key, one user's entire logbook, certification-card scans included.

`no-store` rather than `private` because `private` lets the browser keep a copy on disk, and a zip
of ID-like card images should not sit in a cache directory.

## The export spools to a temp file rather than streaming live

Each export endpoint spools its response into a `tempfile.SpooledTemporaryFile` (memory to 32 MB,
disk past it) and streams from that, not from a live generator. FastAPI closes the request's
database session when the endpoint returns, and a `StreamingResponse` body is consumed after that,
so a live generator fails on its first query. A chunked-zip library (`stream-zip`) is rejected: a
dependency, and no `Content-Length`, which the spool provides.

Memory is bounded, disk is not: nothing caps the archive and the rate limit allows ten an hour per
user. No hard ceiling, since refusing a legitimate export is worse; size `/tmp` for the largest
account hosted.

The writers stay generators: the spool bounds what is resident, the generators what is constructed.
`uddf.py` writes one `<dive>` at a time, `envelope.py` one record, and `loader.py` fetches
`dive_profile.data`, `dive_file.data` and `certification_file.data` one row at a time — the
package's one deliberate N+1.

## What UDDF 3.2.2 has no slot for, and what it forces

Settled against the vendored `tests/fixtures/uddf/uddf_3.2.2.xsd`.

No slot, so `logbook.divejson`/CSV only: the deco ceiling (`<decostop>` requires a `duration` a
ceiling cannot supply); the `cns_start`/`cns_end`/`otu_start`/`otu_end` scalars
(`informationafterdiveType` has no oxygen-exposure element; `<cns>`/`<otu>` are per-waypoint, so
only the per-sample `cns` channel goes out); gas `role`, gear sets, service records, c-cards,
courses (no elements; `<divetrip>` is not a course); species (`<observations>` demands UDDF's
mixed-rank taxonomy mapped from the WoRMS `phylum`/`class_name` strings `models/species.py` passes
through — a mapping this repo would get quietly wrong; `species.csv` carries them); the emergency
contact and the policy number (no elements; `<membership memberid>` is not a policy).

Allowed: `po2_limit` maps to `<mix><maximumpo2>`, so `_MixKey` includes it;
`informationbeforedive/link` is `maxOccurs="unbounded"`, so every site goes out in visit order.

Forced: `<greatestdepth>` is mandatory and `Dive.max_depth` is not — deepest profile sample, then
`0`; `<tankpressurebegin>` is mandatory in `tankdataType`, so a cylinder without one is skipped, its
gas still in `<gasdefinitions>`.

The owner's email stays out of `contactType`, though the phone goes in; a UDDF file gets handed to
shops.

## Gas mixes dedupe on a rounded key, because the corpus carries float noise

`collect_mixes` rounds `oxygen`/`helium`/`po2_limit` to three decimal places before using them as a
dictionary key — a thousandth of a percent, a hundred times finer than any analyzer reads. Stored
fractions carry float noise (`28.000000000000004` beside `28`, `28.999999999999996` beside `29`),
and the writer's `_num` already rounds to six decimals on the way out, so an unrounded key yields
two `<mix>` entries whose `<o2>` print the same number. Duplicate mixes are valid, importable UDDF;
they just make an importer show a diver two cylinders of the same gas, which validating against the
XSD cannot catch. Rounding the key makes key and rendered value agree.

## Archive member paths are planned for the whole zip at once

`plan_archive_paths` assigns every stored file its path in one pass over the bundle before anything
is written, so `envelope.py` can record it in `logbook.divejson` and every name is unique. Nothing
upstream guarantees that: dive numbers repeat (`DiveNumberingSummary.duplicate_count`), two
certifications can share a name, and `original_filename` is whatever the dive computer wrote. A zip
with two entries of one name is valid, and most extractors silently keep one file. Collisions get a
`-2`, `-3` suffix, compared case-insensitively because `DIVE.XML` overwriting `dive.xml` on macOS or
Windows loses a file.

`archive_member_name` is path-traversal defence, unlike `core/utils/uploads.py::safe_filename`,
which only protects an HTTP header: an extractor writes this name to disk, and `../../.bashrc` is a
real archive. It also folds non-ASCII away, since zip's UTF-8 flag is not universally honoured.

Member timestamps come from `exported_at` rather than `datetime.now()`, so two exports of an
unchanged logbook are byte-identical and diffable.

## The archive includes certification card images, and that is worth saying out loud

`certifications/` in the export zip holds both sides of every stored c-card: scans of ID-like
documents carrying the diver's name, certification number and often a photograph. Nothing new leaves
the owner's hands — the endpoint is owner-only over a bearer token, has no user parameter, and
answers `no-store` — but a saved export is a folder of identity documents in a downloads directory.
That changes what "export" implies for a user, so the endpoint's docstring, which `/openapi.json`
publishes, says so as well as this section. The CSV and UDDF downloads carry no images for the same
reason: the two files a diver is likely to share contain none of this, and the one that does is the
one labelled "everything".

## `dives.csv` is written with a byte-order mark

The flat CSV starts with U+FEFF and uses RFC 4180 `\r\n` line endings. Excel reads a BOM-less UTF-8
CSV as the local ANSI codepage, turning every accented site name and non-Latin note into mojibake;
it is the file's most likely destination, and the BOM is the only in-band way to tell it otherwise.
Programmatic readers strip it (`encoding="utf-8-sig"`) or tolerate it.

All nine CSVs carry it, including the normalized set inside the archive: `dive-sites.csv`,
`trips.csv` and `certifications.csv` hold the same free text as `dives.csv`, a diver double-clicking
one is ordinary, and a mangled site name is worse than a `utf-8-sig` a script author passes once.

`tests/fixtures/export/dives.csv` pins the exact bytes, and `.gitattributes` marks it `-text` so
`core.autocrlf` cannot normalize the line endings under the test. `-text` rather than `binary`,
because `binary` expands to `-diff -merge -text` and a golden file whose diff cannot be read is
useless.

## An export holds every record the caller can still see, not every record still live

`loader._owned` applies two filters: one user's rows, and live rows where the table has a notion of
liveness (`Dive`, `GearServiceRecord`, `Certification`). The tables an export joins across are
hard-deleted, so a join row cannot outlive the row it points at and no dangling reference needs
repair — see *"The row goes, and so does everything pointing at it"*. An unresolved id fails two
ways: an unguarded lookup is a `KeyError`, a 500 on every export endpoint; a guarded one leaves a
uuid in `logbook.divejson` that nothing defines, and in UDDF an `xs:IDREF` that stops the document
validating.

Where a reference still cannot be resolved — hand-edited data — the export skips the row rather than
raising. `sites_for`/`gear_for`, `_schedule_uuid`, the gear-service CSV and the two `_collections`
comprehensions all agree, because a 500 on the diver's exit door is the worst answer to a row nobody
can see.

## `Content-Disposition` has to be named in `expose_headers` or the browser hides it

`CORSMiddleware` in `core/setup.py` names `Content-Disposition` in `expose_headers`.
`allow_methods`/`allow_headers` govern what a request may carry; `expose_headers` governs what JS
may read off the response, and without it a cross-origin response exposes only the CORS-safelisted
headers, so `fetch` never sees the filename the server sends. The server's name from
`services/export/naming.py` is authoritative; `opendiving-web`'s `exportFilename` mirror is the
fallback.

`expose_headers=["*"]` does not work: the Fetch spec ignores the wildcard when
`allow_credentials=True`. The `["*"]` on `allow_methods`/`allow_headers` is no counter-example —
Starlette expands both before the wire, while `expose_headers` is emitted verbatim.

`GET /dive/{uuid}/file/{fid}` and `GET /certification/{uuid}/file/{side}` build the header through
`content_disposition_attachment` too.

`tests/test_cors.py` asserts on a real 401 `GET /export/csv`, not a preflight, since
`Access-Control-Expose-Headers` is only sent on actual responses — and asserts the 401 itself, since
an allowed origin gets the header on a 404 too. `ServerErrorMiddleware` sits outside
`CORSMiddleware`, so an unhandled 500 carries no CORS headers.

## A real download is checked in as a fixture, and it is not a golden file

`tests/fixtures/uddf/demo-account.uddf` is a real `GET /export/uddf` response for the demo account,
bytes unchanged, kept for two consumers outside this suite: the UDDF importer, and manual
round-trips through Subsurface and divelogs.de.

It follows the opposite rule to `tests/fixtures/export/dives.csv`. The CSV is a golden file,
compared byte for byte. This is a snapshot CI cannot reproduce, and the writer is free to move away
from it, so `TestCheckedInCorpus` validates it against the XSD and asserts nothing else — enough to
catch a truncated file, not enough to fail an ordinary writer change.

`.gitattributes` marks it `-text`: XML 1.0 §2.11 normalizes CRLF before the parser sees it, so a
`core.autocrlf=true` rewrite still validates and nothing reports that the bytes changed.

The demo account is seeded fiction, safe on a bug report; its coverage is single-tank air and nitrox
with one profile, so trimix, gas-switch and multi-tank paths live only in `tests/helpers/export.py`.

## What Subsurface does with our UDDF, and what its own file does not

Subsurface keeps dive numbers, notes, site links, date and local time, cylinder size and pressures,
O₂ fraction, depths and samples; our `<generator><name>` becomes the dive-computer label.
`<lowesttemperature>` survives the import but not Subsurface's own UDDF export, so a re-export alone
misreports what imported. Lost: trips, gear other than cylinders, weights, the site's free-text
location, the `NoDecoTime` `<setmarker>`, and UTC offsets, dropped rather than applied. Every
profile-less dive gains a fabricated six-point depth profile stored in the `.ssrf`,
indistinguishable from recorded samples.

Subsurface's own UDDF fails the vendored 3.2.2 XSD repeatedly — empty `<latitude/>`, ids that are
not NCNames — so the importer must not gate on schema validation: parse leniently, strip whitespace
from ids and refs, treat `<latitude/>` as absent. `sac`, `otu` and `cns` in a `.ssrf` are
Subsurface's own computations.

No capture is checked in; a fresh round-trip takes ten minutes.

## Every UDDF waypoint carries a depth, because the alternative broke both importers

The depth channel sets the time axis, every waypoint carries a `<depth>`, and every other reading
snaps to the nearest depth sample, ties to the earlier one. `<depth>` is optional in `waypointType`,
but Subsurface discards depth-less waypoints and divelogs.de reads a missing depth as zero.

Only a timestamp moves, by at most half the depth channel's median gap, capped at 30 seconds; a
reading further away is dropped, not clamped. The tolerance is per channel, not per bracketing pair:
`suunto_xml` appends depth only where `<Depth>` is non-nil, so mid-dive holes are real.

A dropped gas switch misreports the gas for the rest of the dive, so a switch lands on the first
waypoint at or after it, and where two coincide the later wins.

Interpolating depth is rejected as fabricating a measurement; unsnapped channels stay lossless in
`logbook.divejson`. No depth channel means no `<samples>`.
`tests/helpers/export.py::OFF_GRID_PROFILE` pins the tie-break.

## What divelogs.de does with our UDDF

divelogs.de keeps dives, dates, times, cylinder volume and pressures, O₂ fractions, `<diveduration>`
unrounded, and the dive site's free-text location; it fabricates no profile. It rounds
`<lowesttemperature>` to whole °C, depths to 0.1 m and start times to the minute, and drops the UTC
offset. It does not import dive numbers, weights, gear, visibility, events or trips — trips it
derives from gaps between dive dates, so the loss hides.

It never reads `<divetime>`: it assumes a uniform interval computed as
`round(duration / sample count)`, so playback runs about 3% short. Resampling our profiles to a
consumer's guess is their bug to fix.

Its exporter, not its importer, blanks every string containing an ampersand while the data stays
intact on its dive page, so anything concluded from a re-export alone is a hypothesis. It also emits
`<latitude>0.000000</latitude>` (and longitude) for every site: treat 0/0 from divelogs.de as
unknown.

## Trips and gear cannot survive a UDDF round-trip, and it is not our encoding

Subsurface imports UDDF through `xslt/uddf.xslt`. Trips: `tripmembership`, `divetrip` and
`relateddives` appear nowhere, so our `<tripmembership ref>` plus `<divetrip><trip>` means nothing
to it; the reverse `<trippart><relateddives><link ref>` encoding is not built, since neither
importer reads it. Gear: the only equipment read is `owner/equipment/divecomputer` — the recording
computer, not a kit list; `<equipmentused><link>` is never read. Weights: it reads
`u:informationafterdive/u:equipmentused/u:leadquantity`, but the 3.2.2 XSD defines `equipmentused`
only inside `informationbeforediveType`, so no valid file satisfies it; an illegal second
`<equipmentused>` would trade validity for one consumer's bug. Dives arrive as
`<divecomputer model='Open Diving'>` because `GearItem` has no model designation and the stylesheet
falls back to `<generator><name>`; inventing one is rejected.

divelogs.de imports dive data but not diver data: `<diver><owner><equipment>`, per-dive links and
weights are ignored, and its export carries no `<equipment>`, `<equipmentused>`, `<leadquantity>` or
`<buoyancycontroldevice>`. A file from divelogs.de never carries gear or weights; worth stating in
the importer's migration guide.

## Dive site coordinates are two `Float` columns, and half a pair is not a position

`dive_site.latitude`/`longitude` are plain `Float` columns, `Mapped[float | None]`, not PostGIS
`geography(Point)`, which costs every self-hoster an extension install. Revisit if "sites near me"
ships. They stay out of `ux_dive_site_user_id_name_location_lower`: same name and locality name is a
duplicate whatever the coordinates.

`WholeCoordinatePair`, inherited by `DiveSiteCreate` and `DiveSiteUpdate`, requires a body to name
both coordinates or neither: `len({"latitude", "longitude"} & model_fields_set) == 1` is a 422, and
so is `(latitude is None) != (longitude is None)`. `{"latitude": null, "longitude": null}` clears a
position. Validating the *effective* pair over the stored row is rejected: it needs gating, races
between PATCHes, and splits one invariant in two. It is not on `DiveSiteBase`: a half pair from raw
SQL must not 500 every read.

UDDF emits `<geography>` for a locality or a position, writing the locality's `name` into the
mandatory `<location>` and repeating the site's own name there when only coordinates exist.
`dive-sites.csv` writes `latitude`/`longitude` as stored, empty where absent, never `0`.

## Geocoding is a server-side proxy, and Nominatim's terms are three concrete obligations

`GET /api/v1/geocode/reverse` and `/search` proxy the provider so `GEOCODER_API_KEY` never reaches a
client and the web CSP needs no new `connect-src`. The default `https://nominatim.openstreetmap.org`
is keyless, honouring its terms: an identifying `GEOCODER_USER_AGENT`, every answer cached, and
outbound calls throttled through `enforce_rate_limit` at one per second. A miss sends the coordinate
or search string, never account or token; `GEOCODER_URL=""` disables the feature.

Keys are `geocode:v1:…` — global, outside the `user_{id}_*` sweep, versioned because the normalized
`GeocodeResult` is cached; `GEOCODER_LANGUAGE` is instance-wide since it is keyed.
`geocode:user:{id}` rejects with 429; `geocode:provider` is shared, so exceeding it skips the call
and returns "no result".

`_request` returns `[]` for "nothing there" (cached) and `None` for "could not ask" (uncached);
`unable to geocode` is an answer, any other `{"error": ...}` a failure. Everything else degrades to
`null`/`[]`; the log names the path, never the URL. `location` is place plus country from `address`.

## A pin in open water is named from polygons in the repo, not from a second provider

Nominatim's `/reverse` answers `{"error": "Unable to geocode"}` in open ocean, so
`services/marine_areas.py` names the water from vendored Natural Earth
`ne_10m_geography_marine_polys` (`marine_areas.geojson`). Marine Regions' API (second host, second
licence) and `shapely` (GEOS for some ray casting) are rejected. Only a successful read is memoised;
a failed one answers "no sea". `scripts/build_marine_areas.py` stages and `os.replace`s its output.

`water_name` runs only when the provider answered `[]`, never when `_request` returns `None`, and
`GEOCODER_URL=""` disables it. It runs after the cache, never inside it; the `[]` keeps its hourly
TTL because the file holds Chesapeake Bay and the Amazon River. Smallest bounding box wins; a hole
in that match means `None`. Parts are indexed, not features, and the build script refuses other
meridian-crossing edges. `GeocodeResult` echoes the asked position, names the water in
`location`/`display_name`/`name`, and attributes "Water body names from Natural Earth". Coverage is
coarse: `28.57, 34.54` answers `None`.

## "No name here" is a 204, and "we could not ask" stays a successful `null`

`GET /api/v1/geocode/reverse` answers `200` with a result, `204` with no body when the provider
answered and had no row, and `200` with `null` when we could not ask. The web app writes the answer
into a dive site's `location`, so a nameless position must clear it while "could not ask" must not.

`reverse_geocode` returns `ReverseGeocode(result, asked)`; `_request` separates `[]` from `None`,
and a cached `[]` counts as asked, or clearing would depend on Redis being warm. "Could not ask"
stays a success, not 503, 429 or 502: the diver can type it in.

A `{"result": …, "available": true|false}` envelope is rejected for changing a shape every client
parses. FastAPI will not emit a bare 204 from `response_model=GeocodeResult | None`, so the handler
returns `Response(status_code=204)` and declares `responses={204: …}`; `tests/test_geocoding.py`
asserts the empty body, since axios turns a 204 into `""`. `search_places` still conflates the two.

## `except ValueError, TypeError:` is valid, and `ruff format` writes it that way

PEP 758 (Python 3.14) allows an unparenthesized tuple in an `except` clause with no `as` binding,
and `ruff format` with `target-version = "py314"` (which `requires-python = "~=3.14.0"` earns)
removes the parentheses; putting them back fails `ruff format --check` in CI. It is not a
`SyntaxError`, and the check that settles it:

```bash
uv run python -c "import ast; ast.parse(open('src/app/services/geocoding_service.py').read())"
```

`src/app/core/security.py`, `services/dive_files.py` and `dive_parsers/suunto_xml.py` carry the same
form. The parentheses return when the clause binds — `except (A, B) as exc:` — so
`dive_parsers/suunto_xml.py` spells it both ways, and `services/marine_areas.py` and the `httpx`
handler in `geocoding_service.py` are parenthesized for that reason alone. Unifying breaks whichever
half is rewritten: parentheses added to the unbound form fail `ruff format --check`, removed from
the bound form they are a `SyntaxError` ("multiple exception types must be parenthesized when using
'as'"). Check for an `as` before concluding anything about a clause.

## A trip's parts are a value-object child table, and the trip itself stores no dates

`trip_part` holds a trip's stretches: an optional date range and an optional place. `trip` has no
dates — its span is the earliest start and latest end across its parts, and a trip whose parts carry
none has no span. Rows stay value objects (no `uuid`, no unique constraint) because nothing
references a part; dives point at trips. `name` says whether a part has a place, which is why it is
nullable and why `get_parts_for_trip` reads `location=None` off it. Order is the diver's, not date
order: an undated part has no place in one, and parts may overlap or leave gaps.

On PATCH an absent `parts` leaves them, `[]` clears, a list replaces. Trips keep an
`OwnedResourceCache` for its key shapes alone — `search_columns` must stay non-empty and the
hand-rolled helpers' kwarg names fill the placeholders.

## A bounding box is optional twice over, and west > east is a real box

`GeocodeResult` and `LocationInput` carry `bbox_south/north/west/east` as four named floats, not
nested, because a client writes a picked search result straight onto a place. Nominatim sends
`boundingbox` as four strings ordered south, north, west, east; `_bounding_box` checks each
assumption. A missing, short, unparseable or impossible box leaves all four `None` and keeps the
result; the box is a nicety. The bounds check is the chain `-90 <= south <= north <= 90`, so a `nan`
corner is rejected.

`bbox_west > bbox_east` is valid and never "corrected": it crosses the antimeridian, and swapping
the pair frames the whole planet. South > north has no such reading and is refused. Only forward
search gets a box (`_normalize(..., with_bounding_box=True)`), and `LocationInput` refuses a box
with no coordinates. Cache entries hold normalized `GeocodeResult`s for a month, so adding a field
means bumping `_CACHE_VERSION`; nothing fails if skipped.

## The project instructions live in AGENTS.md, and CLAUDE.md is an import

`AGENTS.md` holds the repo's instructions; `CLAUDE.md` is a `@AGENTS.md` import plus a
`## Running the tests` section. `AGENTS.md` is the cross-tool convention, so one file serves every
coding agent; Claude Code reads it only through the import.

`CLAUDE.md` says nothing about the parent `opendiving/CLAUDE.md`, because it can act on neither
reader: the parent loads by directory ancestry regardless, and a standalone clone has no parent. The
maintainer-facing reason for the bare import lives in the HTML comment atop the file.

`## Running the tests` is not Claude-specific — its mypy and `POSTGRES_SERVER` caveats restate
`CONTRIBUTING.md` for anyone running the suite — and an agent reading only `AGENTS.md` never sees
it; moving it is a separate change. The web repo's extra reason (`next dev` writes a managed block)
does not apply: nothing generates into these files, and `mdformat` treats `AGENTS.md` like any doc.

## GPS from an import lands on the dive, not on the dive site

`dive` carries `entry_latitude`/`entry_longitude`/`exit_latitude`/`exit_longitude`, four `Float`
columns like `dive_site.latitude`/`longitude`. Pre-filling a new site's coordinates from the import
response is rejected: a fix pair is not a site — entry and exit are two places one dive passed
through, and folding either onto the shared site discards the other — and a prefill is not storage,
since most imports go into an existing site.

The fields sit on `DiveTechScalars` (`schemas/dive.py`), so `TECH_SCALAR_FIELDS`,
`store_tech_scalars`, `DiveRead`, the attach path and `backfill_tech_fields` pick them up. That
write sets every mixin field, `None` included, on every attach, so a position that must survive
re-attach comes off the mixin.

The pair rule is a `CHECK` here, not a `WholeCoordinatePair` validator as on `dive_site`: nobody
types these, so a half pair is a parser bug the database refuses.
`ParsedDiveSchema._drop_half_positions` keeps an import from reaching that refusal when
`_drop_non_finite` nulls one ordinate. `DiveSiteInfo` is untouched.

```sql
ALTER TABLE dive ADD COLUMN entry_latitude DOUBLE PRECISION, ADD COLUMN entry_longitude DOUBLE PRECISION, ADD COLUMN exit_latitude DOUBLE PRECISION, ADD COLUMN exit_longitude DOUBLE PRECISION;
ALTER TABLE dive ADD CONSTRAINT ck_dive_entry_latitude_range CHECK (entry_latitude IS NULL OR (entry_latitude >= -90 AND entry_latitude <= 90));
ALTER TABLE dive ADD CONSTRAINT ck_dive_entry_longitude_range CHECK (entry_longitude IS NULL OR (entry_longitude >= -180 AND entry_longitude <= 180));
ALTER TABLE dive ADD CONSTRAINT ck_dive_exit_latitude_range CHECK (exit_latitude IS NULL OR (exit_latitude >= -90 AND exit_latitude <= 90));
ALTER TABLE dive ADD CONSTRAINT ck_dive_exit_longitude_range CHECK (exit_longitude IS NULL OR (exit_longitude >= -180 AND exit_longitude <= 180));
ALTER TABLE dive ADD CONSTRAINT ck_dive_entry_position_pair CHECK ((entry_latitude IS NULL) = (entry_longitude IS NULL));
ALTER TABLE dive ADD CONSTRAINT ck_dive_exit_position_pair CHECK ((exit_latitude IS NULL) = (exit_longitude IS NULL));
```

## Every GPS fix in the corpus is an exit fix, which is why the split is on the deepest sample

`services/dive_parsers/positions.py` splits on the deepest sample: last fix at or before it is the
entry, first strictly after it the exit. GPS does not penetrate seawater; every corpus fix is logged
after surfacing, past `Header.DiveTime`, so first/last silently writes the exit as entry. The entry
comes from `DiveRouteOrigin` instead.

Deepest sample, not an in-water window: a window needs a depth threshold to invent
(`FitParser._tank_pressures` declines to), and FIT has no in-water time (`total_elapsed_time`,
`total_timer_time`, `session.timestamp - start_time` coincide). No depth channel, no positions.

`entry_and_exit` drops non-finite depths first: `inf` wins `max` anywhere and `NaN` wins when first,
landing the split anywhere. `json.loads` accepts bare `NaN` and overflows to `inf`, the
`_ParserOutput._drop_non_finite` hazard with no schema in between.

Last-before and first-after, not first and last: the boat logs fixes motoring out, the diver
drifting after. Suunto sample timestamps are not monotonic across channels, so both ends are scanned
for.

## GPS fixes: The two conversions, and the cross-format check that pins them

Each format states a coordinate in its own unit:

| Format      | Field                         | Unit                                  |
| ----------- | ----------------------------- | ------------------------------------- |
| FIT         | `record.position_lat/_long`   | semicircles (180/2³¹ degrees)         |
| Suunto JSON | sample `Latitude`/`Longitude` | **radians**                           |
| Suunto JSON | `DiveRouteOrigin.Latitude`    | **degrees** — same file, other unit   |
| Suunto XML  | —                             | no coordinate anywhere in 384 exports |

The radians are the trap: `0.496, 0.601` is a plausible pair of degrees. Two corpus dives exported
as both FIT and JSON converge on `28.437455, 34.458997` (Gulf of Aqaba); as degrees the JSON lands
in the Atlantic. Coordinates are rounded to six decimals (~11 cm), where the two paths agree
exactly, so a dive imported from both exports yields one position.

Two junk-fix rules live in `geo_fix`, not the column: `entry_and_exit` picks the fix closest to the
dive, so a bad fix beside a good one would be chosen, then nulled. Exactly `0.0, 0.0` is not a
position (no lock reports the origin). Out of range is a unit error; only radians can overshoot,
since ±2³¹ semicircles *is* ±180°. FIT's `0x7FFFFFFF` sentinel for `sint32` `position_lat` needs no
handling: `fitdecode` returns `None`, where arithmetic would give 180.000000°, a valid longitude.

## The Suunto entry position is a `DiveRouteOrigin`, not a fix

Ocean exports with GPS write one `DiveRouteOrigin` (degrees) on the first sample, timestamped
`Header.DateTime`. It is fed to `entry_and_exit` as an ordinary fix, so a real pre-descent fix still
wins.

The fixes beside it are radians; through `degrees_from_radians`, `28.567251` becomes 1 636°,
`geo_fix` drops it, and the file reverts to no entry position, tests green. Hence
`degrees_verbatim`, a no-op naming the unit, and
`test_the_dive_route_origin_is_degrees_where_the_sample_fixes_are_radians`, asserting the wrong
conversion is out of range.

`DiveRouteQuality` is not read (`1` on good and `0, 0` origins alike); junk hits `geo_fix`'s Null
Island guard. `_positions`'s early-out (no sample `Latitude`, sparing `fromisoformat`) checks both
keys, or an origin-only file is skipped.

The pivot tie is `<=` / `>`: an origin at t=0 ties a flat depth channel (`max` keeps the first), and
`<` / `>=` would write the start to `exit_latitude`/`exit_longitude`. Two positions off one sample:
the fix, appended first in `_positions`, wins.

## GPS fixes: UDDF has no slot for either position

Checked against the vendored XSD: `geographyType` is referenced from `siteType` and `trippartType`
only, neither per-dive, and `informationbeforediveType`, `informationafterdiveType` and
`waypointType` (which carries `heading`) have no coordinate element. Entry and exit positions
therefore survive only in `logbook.divejson` and `dives.csv`, alongside the deco ceiling and the
CNS/OTU scalars (*"What UDDF 3.2.2 has no slot for"*). `dives.csv` gets four columns beside the
other import-owned readings, empty where there was no fix rather than `0`: the Null Island trap from
the writing side.

## The dive page is the map view, so `DiveSiteInfo` carries coordinates

`DiveSiteInfo` carries `latitude`/`longitude`, so every dive read embeds the pins its location map
needs; the alternative is a per-site fetch waterfall. The cost is two floats per linked site on rows
the query already selects.

`patch_dive_site` computes two flags. `touches_name_or_location` gates `dive_site_name_exists`:
uniqueness is a rule about a name at a location, and a position is no part of it.
`touches_dive_summary` adds `"latitude" in values.model_fields_set` and gates
`invalidate_dive_caches`; only `latitude` is tested because `WholeCoordinatePair` already refuses a
body naming one coordinate without the other. Widening a single flag costs a marker drag a
uniqueness query that cannot fail and, worse, leaves a site whose name is already duplicated at its
location impossible to reposition, since the re-check 422s on a field the caller never sent. A flag
gating two collaborators stops meaning one thing the moment the summary grows.

## Deleting a trip or a dive site can move its dives first, in one transaction

`DELETE /trip/{uuid}` and `DELETE /dive-site/{uuid}` take an optional `move_dives_to={uuid}`. Given
it, every live dive of the caller's that references the doomed resource is re-pointed at the
replacement and then the resource is deleted, in one transaction; omitted, both routes are plain
deletes.

Doing it client-side (`GET /dives?trip_uuid=X`, `PATCH /dive/{uuid}` per dive, then `DELETE`) is not
atomic, is N+1 (forty dives, forty-one requests, each invalidating the dive caches) and is racy (a
dive added after the last page fetch is missed); none of that is fixable from the client.

Atomicity is the session: neither reassignment commits; both write through the request's session and
`crud_*.delete` commits last, as `erase_dive` does with `delete_files_for_dive(commit=False)`.
Anything raising in between makes `async_get_db` close the session and roll back, which is why
`tests/test_move_dives_on_delete.py` asserts call order. No schema change: two query parameters and
three statements over existing columns.

## Move dives on delete: The response carries no moved-dives count

Both routes answer `{"message": "Trip deleted"}` / `{"message": "Dive site deleted"}`, the bare
`dict[str, str]` every other delete returns, with no moved-dives count: nothing reads one. The web
dialog states the consequence ("Deleting removes this trip from every dive logged on it"), true at
any N, and the toast names no number. A response field is only as justified as its reader; unread,
it is still a contract everything downstream must type. Widening to `dict[str, str | int]` would
give a generated client `Record<string, string | number>`; two fields of different types need a
model, not a dict.

The crud functions still return their counts (`reassign_dives_to_trip`,
`replace_dive_site_on_dives`): the natural affected-row count of a set-based statement, and what the
database-backed tests assert, including that a dive that merely lost the doomed site (it already
held the replacement) counts as moved.

## Move dives on delete: A bad replacement is a 422, not a 404

`move_dives_to` is a reference inside a request, not the resource addressed, so an unresolvable one
answers 422 with "Trip not found." / "Dive site not found.", as `gear_service`'s and `gear_sets`'
body references do and as `PATCH /dive` does for a `trip_uuid` or `dive_site_uuids` entry it cannot
resolve (*"Ownership checks go through one `fetch_owned_or_raise`"* lists the exception). A 404
would name the wrong thing as missing, and `PATCH /dive` is the per-dive call this parameter
replaces, so a client should not learn a second answer for the same mistake.

`move_dives_to == uuid` is a 422 too: it resolves, and would move every dive onto the resource about
to be deleted.

The addressed resource keeps its 404 and gets it first: ownership is settled before the replacement
is looked at, so a hand-crafted call cannot turn someone else's 404 into a 422 that confirms the
uuid is real.

## Move dives on delete: Soft-deleted dives stay where they are

Both reassignments are scoped to `is_deleted = False`, and the scope protects nothing: both
resources are hard-deleted, `dive.trip_id` is `ON DELETE SET NULL` and `dive_dive_site.dive_site_id`
is `ON DELETE CASCADE`, and both fire on exactly the rows these statements decline to move, so a
soft-deleted dive loses the association silently either way.

Recorded rather than fixed, on the same grounds the loss is affordable: no surface renders a
soft-deleted dive. Widening the scope to every dive is the obvious fix and not obviously right (it
moves rows the diver cannot see onto a trip they did not choose), and the question disappears if
dives ever go hard-delete. `reassign_dives_to_trip` and `replace_dive_site_on_dives` say so at the
site. See *"The row goes, and so does everything pointing at it"*.

## Move dives on delete: The dive-site case is three set-based statements, not a loop

The trip case is one `UPDATE dive SET trip_id`. Sites use an ordered join table, so
`replace_dive_site_on_dives` preserves three things in three statements:

1. The replacement inherits the doomed site's slot (position 0 is primary), so the swap is
   `UPDATE dive_dive_site SET dive_site_id`, not delete-and-insert.
2. A dive logged at both sites keeps one row (`ux_dive_dive_site_dive_id_dive_site_id` would 500):
   the first statement deletes whichever sits later, as `replace_dive_sites_for_dive` gets from
   `dict.fromkeys`.
3. Positions stay contiguous: the third statement renumbers only dives that lost a row.

The count unions both statements' dive ids; a dive that only lost the doomed site counts.

The self-join's `doomed.id != already_there.id` is always true and load-bearing for `from == to`:
without it every row joins itself, the `CASE` falls to `else_`, and the `DELETE` wipes the site from
every live dive, reporting success. `erase_dive_site` 422s that call, but elsewhere;
`test_a_site_moved_onto_itself_destroys_nothing` pins the statement's own correctness.

## Move dives on delete: `updated_at` is stamped by hand, and only on the trip half

`TimestampMixin` gives `updated_at` no `onupdate`, so every writer sets it explicitly: FastCRUD
through `DiveUpdateInternal`, `dive_numbering`'s bulk renumber in its `.values()`, and
`reassign_dives_to_trip`. Skipping it would leave one logical edit ("this dive is on that trip now")
in two row states depending on whether it arrived here or through `PATCH /dive`, which puts
`trip_id` in `update_data` and bumps it. Nothing reads `dive.updated_at` today (no response schema,
no ETag); this avoids seeding a discrepancy for whatever reads it first.

`replace_dive_site_on_dives` has no equivalent: `dive_dive_site` carries no timestamps, and
`PATCH /dive` with only `dive_site_uuids` leaves `update_data` empty and skips `crud_dives.update`,
so not touching `dive.updated_at` matches the per-dive call.

## Move dives on delete: Both routes drop the dive caches unconditionally

`erase_trip` and `erase_dive_site` both drop the caller's dive caches unconditionally, plain delete
included. A skip justified by "nothing changed" is a claim about a read path (here
`get_trip_uuids_by_ids`), not about the write; if that read is wrong, the skip borrows against the
wrongness and the debt comes due wherever the read is fixed. Record such a claim beside the lookup
it depends on, not only beside the invalidation, and name the consequence concretely enough to
check: `trip_uuid: null` from a fresh read, the old value from cache, for an hour.

## The row goes, and so does everything pointing at it

`Trip`, `DiveSite`, `GearItem`, `GearSet` and `GearServiceSchedule` are hard-deleted:
`DELETE FROM dive_site WHERE id = :id` is the whole implementation, and the `ON DELETE` rule on
every referencing FK fires — `SET NULL` on `dive.trip_id` and
`gear_service_record.gear_service_schedule_id`, `CASCADE` on `trip_part.trip_id`,
`dive_dive_site.dive_site_id`, `dive_gear_item.gear_item_id`, `gear_set_item.gear_item_id`,
`gear_set_item.gear_set_id`, `gear_service_record.gear_item_id` and
`gear_service_schedule.gear_item_id`.

Rejected: soft delete with `is_deleted` filters in `get_dive_sites_for_dive`,
`get_gear_items_for_dives` and `get_gear_items_for_sets`, `_owned`'s resurrection in
`services/export/loader.py`, and `soft_delete_schedules_for_gear_item` hand-rolling a cascade. A
read filter is not a local change: a lossy read plus a wholesale-replace write lets a client destroy
what the read hid, in another repo, on an unrelated action; no client can preserve a reference it
was never handed. `resolve_trip_id_for_user`, `resolve_dive_site_ids_for_user` and
`resolve_gear_item_ids_for_user` already refuse a deleted row, so filtering reads to match them is
the narrower fix; not having a row the writes refuse is the wider. With nothing hidden, echoing a
read back destroys nothing.

## The three objections to "clear the link", and why none holds

Three objections stand against clearing the association at delete time; none holds, and the cascade
is that option as one `DELETE`.

IDREF: `_owned` read deleted-but-referenced rows back so every uuid in `logbook.divejson` and UDDF's
`xs:IDREF` named something the file defined. A cascade leaves nothing dangling, so
`still_referenced` is gone. Join rows remain the record of which kit a dive used — why deleting is
destructive and archiving exists, not a reason to keep orphans.

Backfill: a delete-time clear leaves rows already holding a dead item wrong; the migration's purge
step, one `DELETE FROM ... WHERE is_deleted` per table, is that backfill.

Renumbering: only `replace_dive_sites_for_dive`, `replace_gear_items_for_dive` and
`replace_gear_items_for_set` write `position`, each wiping and reinserting `0..n-1`. Nothing appends
at `max+1`, nothing reads `position` except as an `ORDER BY` key, neither join table constrains it,
so a gap sorts identically. `replace_dive_site_on_dives` keeps its renumbering as its own invariant.

## `DELETE` is not idempotent, and a second call 404s

`DELETE /trip/{uuid}` and `DELETE /dive-site/{uuid}` 404 on a second call rather than answering 200
and honouring `move_dives_to` on an already-deleted row. Two grounds for idempotency, both gone: it
was the only after-the-fact recovery — a diver who deleted first could still re-point the dives
because the association survived — and there is no association to recover now; and it let a client
that lost the response to `delete?move_dives_to=X` repeat the call for a definitive answer, where a
404 covers both "dives moved" and "dives stranded". A single `DELETE` in one transaction cannot
half-fail, so there is no half state to disambiguate. `move_dives_to` runs in the same transaction,
before the delete. Client-visible: a client treating 404 as "already gone" behaves correctly.

## Accepted: a soft-deleted dive loses its site links on a `move_dives_to` delete

`replace_dive_site_on_dives` scopes its `UPDATE` to live dives (`crud_dive_dive_sites.py`), so a
soft-deleted dive's join rows are not moved and the `CASCADE` removes them. Its docstring's "either
the whole log moved and this site is gone, or nothing happened" holds for the log a diver can see,
not for the rows underneath. No surface renders a soft-deleted dive, so there is no visible
consequence; recorded rather than fixed. It disappears if dives ever hard-delete too.

## What the cascade destroys: a gear item's service history, not a schedule's receipts

Deleting a gear item destroys its service history: `gear_service_record.gear_item_id` is
`ON DELETE CASCADE`, so the records go with the item. The gear delete dialog promises exactly this —
"To keep it in your log and its service history, archive it instead" — and offers Archive in the
same dialog; *"A deleted gear item's service history has no view, and archiving is the surface that
does"* is why that is affordable. Deleting a schedule is gentler: its `ON DELETE SET NULL` leaves
the receipts in the item's history with `gear_service_schedule_id: null`, the state
`GearServiceRecordRead` documents.

## The ten `is_deleted` indexes are recreated plain, and a partial index cannot serve a cascade

`DROP COLUMN is_deleted` silently drops every index whose predicate references it — ten across the
five tables — and `create_all` never touches an existing table, so the migration's `CREATE INDEX`
statements are load-bearing: the five `ux_*` are the only backstop for name uniqueness
(`*_name_exists` races), and `ix_gear_service_schedule_next_due_on` drives the digest scan. All ten
come back plain; a gone row gives name reuse for free. Every `is_archived`/`is_active` predicate
stays.

A partial index cannot serve a referential-integrity lookup: the cascade's
`SELECT 1 FROM ONLY gear_service_record x WHERE gear_item_id = $1 FOR KEY SHARE OF x` carries no
`is_deleted` clause, so Postgres seq-scans past its partial indexes. Hence the plain
`ix_gear_service_record_gear_item_id` and `ix_gear_service_record_schedule_id`, not redundant, and
`ux_gear_service_schedule_item_kind_label` unpartitioned as the only index on
`gear_service_schedule.gear_item_id`, a cascade target. `tests/test_foreign_key_indexes.py` counts a
partial index as none; see *"Every foreign key column leads an index, and a test says so"*.

## Hard-deleting the five tables: The DDL

The order matters: purge while `is_deleted` still exists, drop the columns, then rebuild the
indexes.

```sql
DELETE FROM gear_service_schedule WHERE is_deleted;  -- then gear_set, gear_item, dive_site, trip. ONE-WAY: dump first
-- orphan counts over dive_dive_site, dive_gear_item, gear_set_item and dive.trip_id must all be 0
ALTER TABLE trip DROP COLUMN is_deleted, DROP COLUMN deleted_at;  -- and the other four; silently drops ten indexes
-- recreate those ten plain, matching the rewritten __table_args__
CREATE INDEX ix_gear_service_record_gear_item_id ON gear_service_record (gear_item_id);
CREATE INDEX ix_gear_service_record_schedule_id  ON gear_service_record (gear_service_schedule_id);
-- pg_indexes over the six tables must list 32 rows, diffable against create_all on a fresh DB
```

A nonzero orphan count means a constraint is missing, not that a sweep is needed. A name missing
from the final listing is an index that will not come back on its own.

## What is soft-deleted, and why

`Dive`, `User`, `GearServiceRecord` and `Certification` keep `SoftDeleteMixin`. A dive is the
irreplaceable record; `dive_profile` rows cascade, but `dive_file` keys files outside Postgres, read
out before the row goes (*"The cascade cannot reach the files, and nothing warns you"*). `User`'s
flag is a grace period: `is_deleted` means a purge is scheduled, the worker runs
`DELETE FROM "user"` a fortnight later, and `POST /auth/restore` clears it meanwhile (*"Deleting an
account is two changes with a fortnight between them"*). `GearServiceRecord` and `Certification` are
leaves.

`fetch_owned_or_raise` and `_owned` in `services/export/loader.py` filter on `is_deleted` only when
the model carries the column: FastCRUD's `get_model_column` raises `ValueError` otherwise, so an
unconditional `is_deleted=False` would 500 every `GET`/`PATCH`/`DELETE` through it. The check is on
the model, not a per-call flag, so a soft-deleting table added later is filtered by default; the
accident to avoid is a deleted dive in an export.

## The resolve is a second statement, and a hard delete can race it

Every gear-service read collects `gear_item_id`s off a page of schedules or records and resolves
them to uuids in a separate statement. READ COMMITTED takes a snapshot per statement, so a
`DELETE /gear-item/{uuid}` committing between them leaves an id that resolves to nothing; indexing
the mapping directly is a `KeyError` 500. `get_gear_item_uuids_by_id`'s `NOT NULL` plus
`ON DELETE CASCADE` reasoning holds within one snapshot, not across statements: a hard delete turns
every "the row is still there" assumption into a race.

List routes `.get()` and skip, as the export writers do for an unresolvable reference.
Single-resource routes 404: the addressed schedule or record is itself gone, the answer
`resolve_schedule_for_user` gives a moment later. `gear_item_uuid` is required on both read schemas,
so `None` is a validation error; those are the only two shapes.
`TestAVanishedGearItemDoesNotFiveHundred` in `tests/test_gear_service.py` stubs the two statements
to disagree, at all four call sites.

## The admin panel has no delete on the five hard-deleted models

`admin/views.py` registers `Trip`, `DiveSite`, `GearItem`, `GearSet` and `GearServiceSchedule`
without `"delete"`; `view`/`create`/`update` stay. FastCRUD's `delete` branches on the column's
presence, so a registered delete would turn "flag one row" into "destroy the item, its schedules,
its service records and every join row" with no cache invalidation — that lives only in the API
routes (`services/cache_invalidation.py`), leaving Redis stale for the TTL. The panel is off by
default.

## No trash bin, and the decision is deferred

There is no trash bin: nothing restores a deleted row and no endpoint names one. Deciding the real
thing — `deleted_at`, a restore endpoint, a TTL purge job, UI — or adopting "deletes are final" as a
stated stance is deferred, and stops being optional once there are users who are not the developer.

## `get_gear_item_uuids_by_id` filters nothing, and no call site indexes its mapping directly

`get_gear_item_uuids_by_id` filters nothing, and no call site indexes its mapping directly.
`gear_item_uuid` is required on `GearServiceRecordRead` and `GearServiceScheduleRead`, so a miss is
a validation error rather than a null, and no write accepts a `gear_item_uuid` on a record, so
nothing on the write side votes for a filter. The resolve is a second statement under READ
COMMITTED, so a `DELETE /gear-item/{uuid}` committing between the two cascades the schedule or
record away and leaves an unresolvable id. List routes `.get()` and skip the row; single-resource
routes 404, the answer `resolve_schedule_for_user` gives a moment later.
`TestAVanishedGearItemDoesNotFiveHundred` in `tests/test_gear_service.py` stubs the two statements
to disagree at all four call sites. A record's `gear_service_schedule_uuid` is `UUID | None`, nulled
by `gear_service_schedule_id`'s `ON DELETE SET NULL`; `GearServiceRecordUpdate` carries no reference
fields. `tests/test_hard_delete.py::TestTheRowIsActuallyRemoved` pins the `DELETE` on all five
resources, and `invalidate_gear_caches`'s `user_{id}_gear_*` pattern covers the
`user_{id}_gear_service_*` keys.

## The Postgres test fixtures are shared, and a local copy silently wins

Database-backed test modules (`grep -rl 'skipif(not db_available' tests/`) take `_ensure_tables`,
`async_db`, `diver` and `other_diver` from `tests/conftest.py`. A same-named module-level fixture
shadows conftest's silently (pytest resolves from the nearest scope outwards), so never redefine one
you still depend on. Substitution is fine: `test_client_cache_middleware.py`, `test_geocoding.py`
and `test_export_endpoints.py` define their own `client` over a purpose-built `FastAPI()`.
`async_db` builds and disposes an engine per test: pytest-asyncio gives each test its own event
loop, and a pooled asyncpg connection is bound to its opening loop. `db_available()` is a plain
function because `pytest.mark.skipif` calls it at import. `POSTGRES_SERVER=localhost` runs them, and
the skip is silent: a worktree lacks the gitignored `src/.env`, so credentials fall back to
`postgres`/`postgres` and the tests skip identically; copy it in and check the skip count. Rows
written are permanent (`unique_username`); the traps in that are under *"Species are a global
catalog, filled one pick at a time"*.

## The suite has its own database, and builds it with the migrations

The suite owns a database named `POSTGRES_DB` plus `_test` (`f"{name}_test"`, never a literal, so it
cannot equal the app's). Sharing the dev database `opendive` is rejected: `create_all` there leaves
`alembic_version` behind, and the api container's startup `alembic upgrade head` dies with
`DuplicateTableError`; the remedy there is `alembic stamp head`, then `alembic check`. The redirect
sits atop `tests/conftest.py` before any `src.app` import, since `settings.POSTGRES_URI` is fixed
when `core.config` imports. It is created at import, not in a fixture (`db_available()` runs at
collection via `skipif`), by one `CREATE DATABASE` issued from the configured database. Schema comes
from `alembic upgrade head`, not `create_all`, so added columns arrive and the `client` fixture's
real startup upgrade is a no-op. A model change without a revision fails locally; a test database
stranded at a revision outside the branch makes `_ensure_tables` re-raise `CommandError` naming the
`DROP DATABASE` that fixes it. CI's `POSTGRES_DB=postgres` derives `postgres_test` unaided.

## A deleted gear item's service history has no view, and archiving is the surface that does

Deleting a gear item destroys its service records (`gear_service_record.gear_item_id` is
`ON DELETE CASCADE`); archiving is the only surface for retired kit whose history still reads.
`_owned_gear_item` and `_get_owned_gear_item` do not filter `is_archived`, so an archived item
resolves: its detail page loads, `?gear_item_uuid=` works on both listings, and the digest skips it.
The gear page has a *Show archived* toggle, and the delete route's docstring, published in
`/openapi.json`, steers to archiving. `/export/*` carries a live or archived item's history; a
deleted item has nothing to export.

Rejected, and moot now the rows are gone: an `include_deleted_items` flag on
`GET /gear-service-records`, or relaxing the 422 for a deleted item: both make deleting a lossy
archive, `is_archived` being the reversible flag. `is_archived=False` on `_owned_gear_item`,
matching the listing filter, would silently take the history off archived items;
`tests/test_hard_delete.py::TestArchivingIsTheNonDestructivePath` pins that an archived item
resolves with its schedules and records.

## Water type and altitude are dive columns, and the gas math deliberately ignores them

`dive.water_type` and `dive.altitude` are nullable, form-writable and feed no arithmetic.
`WaterType` (`schemas/dive.py`) is a `StrEnum` of `salt`, `fresh`, `brackish`, `en13319`, ordered
for the picker; `en13319` is a calibration kept for import fidelity (Shearwater, Garmin FIT) under
*"Parsers report what a file recorded"*. No `OTHER` (`NULL` means not recorded) and no `CHECK`, per
`GearItem.type`. `altitude` is `Integer` metres under `ck_dive_altitude_range` (`-450..6500`), which
needs a `_DIVE_CONSTRAINT_MESSAGES` entry (`api/v1/dives.py`) or the 422 misdescribes itself.
Neither is a `DiveTechScalars` field, since `store_tech_scalars` overwrites every field on
re-attach. FIT seeds it from `_FitScan`'s `dive_settings` via `_water_type` (`custom` nulled) and
`ParsedDiveSchema`, whose `water_type = None` default the Suunto parsers rely on. `METERS_PER_BAR`
stays 10.0: a column `NULL` on most rows would step one diver's trend by 3%. UDDF carries `altitude`
(`informationbeforediveType`) but no `water_type`, 3.2.2 having no per-dive salinity; `dives.csv`
gains `water_type` and `altitude_m`. Any future `GET /dives` filter must join `_cached_read_dives`'
`key_prefix`.

## Measurements are metric in the database and on the wire; `units` is who's looking

`user.units` (`metric`/`imperial`) changes nothing the API serves. Every stored measurement is
metric (`max_depth` metres, `bottom_temperature` Celsius, `weight` kilograms, pressures bar,
`sac_bar_per_min`); the client converts at display and entry. The API never converts: every dive
read is `@cache`d under a user-scoped key, so a viewer-dependent response would need the preference
in the key, and `?units=` adds a forked contract. `src/` holds no conversion code. Exports do not
bend: `logbook.divejson` carries values as served (the preference rides on `ExportUser`),
`dives.csv` keeps `_m`/`_kg`/`_bar` suffixes, UDDF is SI. One toggle, not per dimension (Shearwater
Cloud, Garmin); per-dimension stays a later additive change. `UnitSystem` is a `StrEnum` over
`VARCHAR(16)`, no `CHECK`. It sits on `UserRead` and `UserUpdate` (`extra="forbid"`), and `"units"`
is in `UserUpdate.NON_NULLABLE_FIELDS`, pinned by
`test_update_explicit_nulls.py::test_the_declared_fields_match_the_table`. `server_default="metric"`
is load-bearing: a `NOT NULL` column on a populated table needs a server-side default, and
`default=` is invisible to autogenerate.

## The attribution string is a wire format, so its shape is part of the API contract

`GeocodeResult.attribution` is a wire format: `parseAttribution`
(`opendiving-web/src/lib/map-tiles.ts`) reads `[label](href)` and degrades to plain text, so a shape
change ships client-first. The rendered string is the provider's `licence`; `_normalize` falls back
to `_DEFAULT_ATTRIBUTION` only when it is absent. `_linked_attribution` folds
`<text> <trailing-url>` into `[<text>](<url>)` and nothing else: a rule about shape, not
OpenStreetMap, because `GEOCODER_URL` is an operator setting and a hardcoded credit or
`GEOCODER_ATTRIBUTION` knob could credit the wrong party. The text is required (`\s+`, never `\s*`):
LocationIQ's `licence` is a bare URL that `\s*` folds to `[](…)`, and month-cached results re-read
via `GeocodeResult(**row)` would become `[[text](](url))`. `_ATTRIBUTION_MAX_LENGTH` (255) is
checked after the fold (up to four added characters), so `_normalize` cannot raise
`ValidationError`. `_CACHE_VERSION` bumps with every shape change. `_MARINE_ATTRIBUTION` and
`_DEFAULT_ATTRIBUTION` are written folded; `test_the_built_in_credits_are_already_folded` asserts
`_linked_attribution(credit) == credit`. The string must always name the origin, the licence and a
link to it (OSM Foundation attribution guidelines).

## Species are a global catalog, filled one pick at a time

`species` has no `user_id`: a species is a fact about the ocean, not about one logbook, so one row
serves every diver who saw it. That sharing is what makes sighting counts, a life list and photos
possible. Consequences:

- No `OwnedResourceCache`, no `fetch_owned_or_raise`; both require `model.user_id`
  (`core/utils/owned_resource_cache.py`, `api/dependencies.py`).
- No endpoint checks ownership: `GET /species/{uuid}` 404 means "not in this catalog", and a species
  uuid guards nothing private. `GET /species/{uuid}/photo` serves bytes with no token, because an
  `<img>` tag cannot send one.
- `crud_species.resolve_species_ids` checks existence only; `write_dive` still answers 422 "Species
  not found." before writing join rows.
- The export's `species` list is scoped through dives
  (`services/export/loader.py::_referenced_species`), not through `_owned`.
- `species:` cache keys join `geocode:` outside the `user_{id}_*` namespace
  `services/cache_invalidation.py` sweeps: one answer serves everyone, and per-user keys would only
  multiply register calls. They expire and are never invalidated.

## Species: Why the catalog cannot be bulk-imported

WoRMS's full-database download is proprietary: institutional registration, a vetted application, a
non-transferable licence and a quarterly refresh obligation. The WoRMS copy on GBIF is CC BY 4.0,
but its DwC-A endpoint is marked "Restricted to GBIF". Paging GBIF's own API is the one lawful bulk
route; it is a real pipeline, a gigabyte or two and a re-sync story, and it is the recorded
escalation, not the design. The WoRMS REST webservice is free with citation, publishes no rate
limit, and asks only that it not be harvested completely. Resolving one taxon the first time a diver
picks it is the opposite of harvesting, which makes the on-demand catalog the licensed option rather
than merely the cheap one.

## What this project has told WoRMS it does

These claims were made to `info@marinespecies.org` from `contact@opendiving.app`; a change
falsifying one owes WoRMS a second message.

- Six endpoints only: `AphiaRecordsByName` (`like=true`, `marine_only=false`),
  `AphiaRecordsByVernacular` (`like=true`), `AjaxAphiaRecordsByNamePart`, `AphiaRecordByAphiaID`,
  `AphiaVernacularsByAphiaID`, `AphiaSynonymsByAphiaID`. The live set is the `_worms()` call sites
  in `services/species_service.py`; `AphiaTaxonRanksByID` is never fetched.
- A search costs at most three requests; `…ByAphiaID` calls fire only on adding a species to a dive.
- Nothing is harvested; an unreachable register falls back to the local catalog.
- Two TTLs: `_HIT_TTL_SECONDS` (thirty days) and `_MISS_TTL_SECONDS` (one hour, miss or partial).
- A self-imposed 120 requests a minute per instance (`SPECIES_WORMS_RATE_LIMIT_REQUESTS` /
  `_WINDOW_SECONDS`).
- Credit to `marinespecies.org` beside the CC BY licence on every result, served by the API; the
  citation is in `README.md`.
- Self-hosted copies call WoRMS directly under `SPECIES_USER_AGENT` (default
  `OpenDiving (+https://github.com/opendiving/opendiving-api)`).

The WoRMS logo stays unused until WoRMS permits it.

## Two registers, because WoRMS alone cannot answer "clownfish"

WoRMS's only vernacular for *Amphiprion ocellaris* is Japanese, and GBIF's `species/suggest` matches
scientific names only. Wikidata is CC0 and P850 is the WoRMS AphiaID, so
`clownfish haswbstatement:P850` returns taxa keyed by the identifier both registers share.
`wikidata_qid` is stored for P18 (image).

WoRMS owns taxonomy: scientific names, synonyms, the accepted-taxon fold. Wikidata owns common
names, breadth and order: asked first, never overwritten, fifty candidates, since WoRMS has no sort
parameter and truncates alphabetically. A candidate Wikidata discards still arrives via WoRMS
by-name; the cut costs a row's Wikidata name, never the row.

P225 lets an entity become a row: an AphiaID item with no taxon name is dropped from search.
`resolve_species` never reads an entity for a binomial and needs no gate.

Claims are read statement-rank first: `deprecated` skipped, `preferred` first, then serialization
order; pinned by a test on `resolve_species`.

## The timeouts are measured, and the geocoder's must not be copied

WoRMS `like=true` searches take 6–9 s and `AphiaRecordByAphiaID` about 11 s, Nominatim milliseconds,
so `services/geocoding_service.py`'s 5 s read and 10 s deadline must not be copied: under them
`POST /species/resolve` 503s and search quietly goes Wikidata-only. Three budgets:

- `_SEARCH_BUDGET_SECONDS` (6 s) bounds the search fan-out with `anyio.move_on_after`; whatever
  arrived is the answer.
- `_RESOLVE_BUDGET_SECONDS` (25 s) covers the one call resolve cannot do without: a click with a
  spinner.
- `_ENRICHMENT_BUDGET_SECONDS` (10 s) covers the non-load-bearing synonym/vernacular/Wikidata
  fan-out.

A partial answer is cached for an hour, not a month (`_store_search(..., complete=...)`), so a
fan-out that lost WoRMS is not pinned for a month. `complete` is `answered and all(ok)` over each
source's `_SourceAnswer`, since a fast failure looks exactly like an empty answer; `[]` from a
provider means nothing until it is known to be an answer rather than a failure.

## `wbgetentities` is asked in chunks of four, because a taxon entity is enormous

`props=claims` returns every statement on an entity, and a taxon carries dozens of external
identifiers, so one entity runs around 50 KB and ten hits in one call exceed the 512 KB
`_MAX_RESPONSE_BYTES`. Tripping the cap makes `_request` return `None`, emptying the whole Wikidata
contribution for popular queries. The cap stays, because it bounds what a hostile host can make the
process buffer; the ids are asked for in chunks of four, run concurrently under the provider
throttle.

`_WIKIDATA_ENRICH_LIMIT` cuts the fifty search candidates before any entity is fetched, so
`wbgetentities` sees sixteen ids in four chunks however wide the candidate list; hence it is written
as a product of the chunk size. A chunk over the cap loses only its own four candidates and drops
`ok`, putting the merged page on the hour TTL, while the other chunks land.

## Wikidata's search asks for names, and its shapes are measured

Search asks `generator=search` with `prop=entityterms` (fifty candidates, terms only); resolve asks
`list=search` by AphiaID. Shapes:

- `query.pages` keys are pageids in arbitrary order; only each page's `index` restores the ranking.
- `entityterms` omits `label` or `alias` rather than emptying them; fall back label → alias →
  binomial.
- `entityterms` aliases equal `wbgetentities` aliases, order included, so a matched term is
  `matched_name`.
- No `totalhits`; `has_more` is `continue` present or more candidates than the cut enriches.
- The empty answer is exactly `{"batchcomplete": ""}`, no `query` key, where `list=search` returns
  `query.search: []`; hence two readers.
- Errors are HTTP 200 with a top-level `error` object.

Wikimedia's anonymous throttle 429s `wbgetentities&props=claims` after roughly 28 heavy calls in 90
s, below the app's own cap; a 429 logs as "request failed (HTTPStatusError)" and drops `ok`.
`_WIKIDATA_ENRICH_LIMIT` is the lever: lower it, or authenticate.

## The WoRMS search term goes in the URL *path*, so it must be percent-encoded

WoRMS's routes are `/AphiaRecordsByName/{name}`, with the diver's typed term as a path segment,
where httpx does not encode it, unlike the geocoder's query parameters. Interpolated raw,
`../../../etc/passwd` normalizes to `marinespecies.org/etc/passwd`, letting any authenticated user
point the outbound request at an arbitrary path on that host and cache the result for a month under
their own string; a `#` or `?` truncates the search silently. `_worms` takes the endpoint and the
segment as separate arguments and quotes the segment with `quote(segment, safe="")`, so nothing
reaches `_request` unencoded; `safe=""` because `/` in a taxon name would otherwise make a new path
segment. Check whether user input lands in the path or the query string: the client only encodes the
second.

## The read transaction is released before either endpoint goes outbound

`search_species` and `resolve_species` read the catalog, then spend seconds outbound. `AsyncSession`
autobegins on the first `execute()`, so the connection sits idle-in-transaction; under
`create_async_engine`'s default pool fifteen concurrent resolves park every connection and unrelated
endpoints fail with `QueuePool limit of size 5 overflow 10 reached, connection timed out`.

`release_read_transaction` (`core/db/database.py`) is a plain `rollback()` before each outbound
step, shared with `services/dive_files.py` and `store_certification_file`'s `blob_store.put`.
`rollback` expires live ORM objects regardless of `expire_on_commit=False`, so the caller must hold
only detached data; `resolve_species` returns a live `Species` before the release.

`TestTheReadTransactionIsReleasedBeforeGoingOutbound`, `TestProfileExtractionReleasesTheTransaction`
and `TestTheReadTransactionIsReleasedBeforeTheBlobWrite` assert positionally (no query between the
last release and the outbound call), because `index` comparisons pass with a query reinserted.
`TestConcurrentResolvesDoNotExhaustThePool` uses a real engine: the mock register holds fifteen
resolves until all are outbound, then `pool.checkedout()` must be zero, with fresh uuid7 AphiaIDs so
persisted rows cannot short-circuit it.

## The common-name rule: a prefix test, a reject list, and one capital letter

`species.common_name` is chosen in `_choose_common_name`, English throughout: the Wikidata label
unless it starts with the scientific name (labels often append the authority), else the first alias,
else the first WoRMS vernacular (listed alphabetically, no preference), else NULL.

A junior synonym under another genus passes that test, so resolve passes the taxon's superseded
names as a reject list and skips casefold-equal candidates. `_worms_synonyms` returns `None` for any
failure and `[]` only for WoRMS's 204; resolve holding `None` raises 503, a guessed name being
written forever. `AphiaSynonymsByAphiaID` pages at 50; the fetch walks to a short page, bounded only
by the enrichment budget.

The first character is capitalised inside `_choose_common_name`. The reject list is resolve-only
(fifty extra calls per keystroke), so search shows an unvetted name until the first resolve;
`test_search_shows_the_unvetted_name_until_a_resolve_fixes_it` pins that. `_name_rows` keeps raw
source strings: `species_name` is an `ILIKE` find-index, and disowned synonyms stay findable.

## Identity is the accepted AphiaID; synonyms are names, not rows

`species.aphia_id` is unique and always accepted: `resolve_species` follows `valid_AphiaID`, keeping
the superseded name as a `species_name` row; search folds inline and sets `matched_name` only when
no visible name answers the query. The unique key turns concurrent resolves into a recovered
`IntegrityError`.

Genus and family rows are legal. `rank` and `status` are `VARCHAR`, not `StrEnum`s: WoRMS's
vocabularies grow. P105 fills `rank` on Wikidata-only rows via `_WIKIDATA_RANK_BY_QID`, spelled
WoRMS's way (`"Phylum (Division)"`), enumerated against `AphiaTaxonRanksByID`'s closed vocabulary
plus Parvorder and Clade since unmapped items fall to the sentinel.

`"unknown"` is ours: `_wikidata_result` and `_worms_taxon` write it where a source is silent, and a
persisted row can carry it for `rank`. `_merge_result` lets a real rank displace it. `rank` can
differ between identical searches: registers disagree (*Mysticeti*: `Superfamily` vs `Parvorder`)
and `_merge_result` keeps the first writer. NULL is rejected: both columns are `NOT NULL` once
resolved.

## Species: Search ranks on what the diver can read

`_match_bucket` (exact, prefix, word boundary, substring, none) is the one helper that ranks,
chooses and cuts. Prefix outranks word boundary, so "Whale shark" opens `?q=whale`. `_ordered`'s
key:

1. Visible before hidden: a row placed by a visible name beats one placed only by its `matched_name`
   hint.
2. The bucket of the name that placed the row, not the best over all names.
3. Rank tier via `_rank_tier`: species and below, `"unknown"`, then the rest; the top tier is
   enumerated; everything else defaults downward.
4. Named before bare.
5. Displayed name, casefolded.
6. `aphia_id`, for a total order.

`status` (the fold normalises it to `accepted`) and `source` (which register beat the budget) stay
out. `_local_search` keeps its own three-way `match_rank` and `LIMIT _MAX_RESULTS`, so a catalog
with more matches than that can cut a row the Python key would rank first; fix with the `pg_trgm`
escalation.

## The ajax endpoint annotates rows; it never makes them

`AphiaRecordsByVernacular` cannot say which vernacular matched.
`AjaxAphiaRecordsByNamePart?combine_vernaculars=true` knows and chains inside `_worms_by_vernacular`
to fill `matched_name` and, where English, `common_name`. It only annotates: its taxa are a subset
of by-vernacular's and its rows are unfolded ids without `valid_AphiaID`, so `has_more` ignores it.

`max_matches=50` is mandatory: the default is twenty, the ceiling fifty, and a value above the
ceiling is discarded. `marine_only=false` is sent explicitly and is not inert. No `languages[]`
filter: a hit explains itself in whatever language matched. Nothing relies on ajax ordering
(alphabetical by `displayname`) or coverage; the vername kept per taxon is chosen by best bucket,
English, then casefolded order.

`_AJAX_ANNOTATION_BUDGET_SECONDS` bounds the annotation, since `_request`'s bounds exceed
`_SEARCH_BUDGET_SECONDS` and a hung call would lose fetched records; expiry ships rows unannotated
on the hour TTL, and an empty answer (204 → `[]`) is complete. `_claim_provider_slot` is first-come
across both legs.

## An English vername names a row Wikidata left bare

Search names rows from Wikidata only, so a taxon Wikidata leaves bare shows a `matched` hint until
resolve reaches `AphiaVernacularsByAphiaID`. The ajax annotation's English half fills `common_name`:

- After the merge, into an empty `common_name`, before `_drop_redundant_hints`
  (`_fill_vernacular_names`), so a vernacular never beats a Wikidata name.
- Through `_choose_common_name`, so the prefix refusal and one-capital rule hold on both paths; the
  reject list stays resolve-only, a WoRMS vernacular being a common name already.
- Never across a fold: ajax rows carry no `valid_AphiaID`, so `_worms_page` fills only when the id
  it sent equals the id returned.

Two maps: `matched` takes the best vername in any language, `english` the best English one. Ajax
returns only vernames that matched, so a binomial hit gets none; fifty vernacular calls per
keystroke would close that. Fewer hints result: a name that answers the query makes its hint
redundant.

## Test fixtures in a global table are visible to real accounts

Postgres-backed tests write real rows and nothing cleans them up (`unique_username`'s docstring
records the trade), which is harmless for tables hanging off a fixture `user_id`. A `species`
fixture row is in everyone's picker, and the suite's own database only shrinks the blast radius. The
fix is naming, not cleanup: `create_species` writes `zzfixture-species-<hex>` and the `species_name`
fixtures write `zzfixture-name-<hex>`, names no query a diver would type can reach. Uniquifying is
not the same as making unmatchable: `Amphiprion <hex>` is a real genus, and `clownfish <hex>` still
matches `%clownfish%`. Cleanup is rejected as disproportionate and divergent from every other
generator. Fixtures for a global table need names that no real query can return.

## Species: Persist at pick time, not at dive-save time

`POST /species/resolve` is called when the diver picks a species in the form, so every species uuid
exists locally before the dive is saved and saving never blocks on a third party. The cost is
catalog rows for picks never saved, which is harmless in a global catalog: they are real species and
the next search benefits. The picker owns the latency with an explicit pending row rather than
leaving synthetic ids in form state for the submit path to trip on.

## Species: Rows are immutable in v1, which defers the hard cache problem

A global rename cannot be expressed as one user's invalidation pattern;
`services/cache_invalidation.py` speaks only `user_{id}_*`. So: no PATCH endpoint, no refresh job,
and the admin panel registers `Species`/`SpeciesName` without delete (deleting cascades through
`dive_species` into other divers' dives, uninvalidated). A psql rename goes stale in cached dive
reads for at most the 3600 s single-dive TTL. Taxonomy refresh (re-resolving when WoRMS moves a
species) waits on cross-user invalidation.

The `photo_*` columns are the one mutation, exempt because the argument is about invalidation: the
only long-lived cached photo field is the single-dive response's `SpeciesInfo.photo_sha256`,
self-healing within the same 3600 s; `SpeciesRead` is uncached and the life list's digest sits under
a 60 s key. The taxonomy is written once, not the row; an immediate photo correction would need the
sweep, so `--force` on the backfill is an operator action, not an endpoint.

## Species embed on `DiveReadWithMixtures`, not `DiveRead`

On `DiveRead` the field would land on the paginated list and cost `_cached_read_dives`, the hottest
path in the app, a query per page for something only the detail page renders.
`get_species_for_dives` is batched for the day a list surface needs species chips: move it with the
batched loader, never fetch per row. `DiveReadWithMixtures.species` has `default_factory=list`
because `user_{id}_dive:{uuid}` entries live an hour and replay through this schema, so an entry
written without the key must still validate.

## ILIKE with no `pg_trgm`, and no manual DDL anywhere

Local search is a plain escaped `ILIKE` through `core/utils/search.py::escape_like`. A
leading-wildcard pattern cannot use a btree index (see *Substring search over a user's own rows*),
and the catalog is small by construction, growing only when a diver picks something new. The
recorded escalation:

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX ix_species_name_name_trgm ON species_name USING gin (name gin_trgm_ops);
```

Deferring it keeps the feature at zero manual DDL: `create_all` cannot emit `CREATE EXTENSION`, and
all three tables are new, so their constraints and indexes arrive with them.

## The local search query is tested by executing it, not by building it

`_local_search` is the feature's one non-trivial statement: an outer join to `species_name`,
aggregates, a `GROUP BY` leaning on functional-dependency inference, an `ORDER BY` on a string
label. `search_species` tests mock the session, which suits the merge logic but never sends the
statement to Postgres, and the catalog is the half that must answer when both registers are down.
`TestLocalSearchAgainstPostgres` executes it against seeded rows, and `TestReadSpeciesRoute` covers
`GET /species/{uuid}`. A mocked session tests the code around a query, never the query: any
statement whose correctness lives in the SQL needs one test that runs it.

Seed what the writer writes. A wildcard test needs a decoy that matches only without `escape_like`,
and a `matched_name` test needs the `scientific`-kind name row `resolve_species` always writes; a
fixture simpler than the real row lets absence of input satisfy the assertion.

## `species_seen` is computed in `recalculate_dive_stats`

`species_seen` is `COUNT(DISTINCT dive_species.species_id)` over the diver's live dives, computed in
`recalculate_dive_stats`, which already runs on every dive write, so it adds no invalidation
surface. It is set on both branches of that function, and the update branch is the one nearly every
write takes; `test_updates_existing_stats_row` asserts a stale value being overwritten. It is a
second query rather than a fourth column on the aggregate, because that aggregate is served by the
covering index `ix_dive_user_id_stats` and joining `dive_species` into it would cost every dive
write the index-only scan.

## No custom species, and what that costs

Free text would put user-authored rows in a global table (wrong ownership) or demand a per-user
overlay (a second model). The dive's `notes` field carries "weird translucent blob, 10 cm" until a
real identification exists. The accepted consequence: with both registers unreachable and the
species not yet in the catalog, the picker comes up dry and the diver adds the species later. That
is the one resilience gap against the trip picker's free-text hatch, and it is deliberate: a trip
location is whatever the diver says it is; a species is what the register says it is.

## SMTP is the only email transport, and the from-address has no default

`services/email_service.py` speaks plain SMTP only. Every provider and relay speaks it; Resend works
as `SMTP_HOST=smtp.resend.com`, username `resend`, password the API key. `smtplib` runs through
`anyio.to_thread.run_sync`, not `aiosmtplib`; connections are per-send. `_send` passes
`ssl.create_default_context()` in both TLS modes (otherwise `smtplib` negotiates unverified) and
sets `timeout=` from `SMTP_TIMEOUT_SECONDS`. `SMTP_TLS_MODE=none` is for loopback or compose-network
relays only; `SMTP_USERNAME`/`SMTP_PASSWORD` are independently optional.

`EMAIL_FROM_ADDRESS` has no default: `Settings._require_from_address_with_smtp` refuses to boot when
`SMTP_HOST` is set without it, and `.env.example` ships it commented out, since an active template
value would pass the check. `_build_message` flattens CR/LF in the subject (`EmailMessage` raises;
the contact `subject` is unrestricted input) but not in addresses.

Mailpit is the opt-in `mail` compose profile (`docker compose --profile mail up`, inbox
`127.0.0.1:8025`) so the logged magic link stays the default sign-in. `docker compose restart` does
not re-read `env_file`; `docker compose up -d --force-recreate api` does.

## Postgres is pinned to 18, and the volume mounts one level above `PGDATA`

`docker-compose.yml` and `.github/workflows/tests.yml` both pin `postgres:18` and move together, so
CI tests what ships. 18 because a major upgrade is a self-hoster's most painful operation and 18
defers it longest. Nothing anchors a version: no PostGIS (marine polygons are GeoJSON in
`services/marine_areas.py`), no pgvector, no extensions; `LargeBinary` columns are plain `bytea`;
`asyncpg 0.31.0` tests against 18; PG18's incompatibilities (COPY `\.`, VACUUM inheritance,
AFTER-trigger roles, FTS collation) touch nothing here.

The mount is `postgres-data:/var/lib/postgresql`: the 18 image's `VOLUME` is one level up and
`PGDATA` is `/var/lib/postgresql/18/docker`, so `pg_upgrade --link` can hold both clusters in one
volume. A wrong mount fails loudly: the entrypoint scans for stray `PG_VERSION` files and exits 1
naming the paths. For an old-layout volume, `docker compose down` and remove `postgres-data`
(`pg_dumpall` first to keep it). PG18's `initdb` enables data checksums; `pg_upgrade` requires that
setting to match on both sides.

## The `worker` service overrides `api`'s inherited HTTP healthcheck with `arq --check`

`worker` and `api` build from one `Dockerfile`, whose `HEALTHCHECK` GETs
`http://127.0.0.1:8000/api/v1/health/ready`. `worker` runs
`arq app.core.worker.settings.WorkerSettings` and serves no HTTP, so the inherited check can never
pass. A permanent false red teaches people to stop reading the status column.

The `worker` service carries a `healthcheck:` override running
`arq --check app.core.worker.settings.WorkerSettings`. Preferred over `test: ["NONE"]` because arq
writes a sentinel key in Redis and `--check` exits 0 only on finding it, so a pass means alive *and*
talking to Redis. The default `health_check_interval` is an hour (TTL interval + 1);
`WorkerSettings.health_check_interval = 15` bounds the lie at 16 seconds, and compose probes every
30s with `retries: 3`, so a real death shows within a minute without flapping.

Any further non-HTTP service from this image needs its own override; `admin_init` disables it (see
*Dev compose restarts, and its third-party tags are pinned*).

## The config template is not a working configuration

`cp src/.env.example src/.env` must not boot: a setting nobody has to touch is one nobody touches.

- `SECRET_KEY` is startup-fatal on a placeholder in every environment
  (`Settings._reject_placeholder_secret_key`). The rejected set is `PLACEHOLDER_SECRET_KEYS`,
  case-insensitive after stripping, not an entropy heuristic that would refuse genuine keys. CI's
  `test-secret-key-for-testing-only` is outside it; `tests/test_config_safety.py` reads the shipped
  value from `.env.example`.
- The admin block (`CRUD_ADMIN_ENABLED`, `ADMIN_PASSWORD`) ships commented out, so enabling the
  panel is an edit.
- An `ENVIRONMENT` other than `local` without `SMTP_HOST` fails at startup
  (`_require_smtp_outside_local`), since passwordless sign-in then admits nobody.
  `_refuse_to_log_credential_outside_local` stays, guarding the code path.

`POSTGRES_PASSWORD` is the exception: compose feeds `src/.env` to `db` too, so a rejection would
stop the stack, not protect it. Survivable because `docker-compose.yml` binds Postgres to
`127.0.0.1`.

`APP_VERSION` defaults from `importlib.metadata.version("opendiving-api")`; the env var overrides,
and `PackageNotFoundError` falls back to `None`. Consumers: `/api/v1/health`, the export's
`generator.version`, the UDDF `<version>`.

## The contact form has no default recipient, and no recipient means 503

`CONTACT_FORM_EMAIL` has no default. No address is right for somebody else's install, and an
upstream inbox gets reports the operator never sees. Unset, `POST /api/v1/contact` answers 503 — a
raw `HTTPException`, since `core/exceptions/http_exceptions.py` has no class for it, as
`species.resolve` does. The check runs before the two rate limiters, so a switched-off form cannot
spend the buckets.

`send_contact_form_email` raises on a missing recipient, unlike its missing-`SMTP_HOST` branch,
which logs the whole submission and returns: that log line lets a developer read what would have
been sent, and there is no equivalent consolation for mail with nowhere to go.

The route tests build a minimal app and mock the sender, never settings, so the guard would
otherwise read whatever `CONTACT_FORM_EMAIL` the developer's own `src/.env` holds.
`tests/test_contact.py` has an autouse fixture pinning it, and the 503 cases override that fixture.

## Comma-separated strings are how this app takes a list

For a complex field type, pydantic-settings parses the environment variable itself and expects JSON,
so `TRUSTED_PROXY_IPS=172.16.0.0/12` fails validation at startup whatever `cast=` says at the field
(see *Per-IP rate limits need to know which proxy to believe*). A bare `list[str] | None` annotation
with no `config()` call has the mirror problem: settable only as JSON, and only through an exported
variable rather than the `src/.env` file every other setting and every doc uses — an allowlist that
silently isn't there.

So lists are comma-separated strings. `CRUD_ADMIN_ALLOWED_IPS` and `CRUD_ADMIN_ALLOWED_NETWORKS` are
parsed by `split_csv` in `core/config.py`, the one function `TRUSTED_PROXY_IPS` also uses, so the
three agree about surrounding whitespace and empty entries. `admin/initialize.py` passes
`split_csv(...) or None`, because CRUDAdmin reads `None` as "no restriction" while an empty list is
an allowlist matching nobody.

## `POSTGRES_URI` is built by percent-encoding, not by f-string

An f-string `f"{user}:{password}@{server}:{port}/{db}"` breaks the moment a password contains URL
syntax, and `@` is the character password generators reach for first: `p@ss` yields
`postgres:p@ss@db:5432/opendive`, which parses with `ss` as the host, so the failure is a connection
error naming a host nobody configured while the operator can see the password is right. `/`, `:` and
`#` break it in their own ways.

`postgres_uri()` percent-encodes the user and password with `quote(..., safe="")` and leaves host,
port and database alone. `redis_url()` does the same for `REDIS_PASSWORD` and treats an empty
password as no password: `redis://:@host` is not the same URL as `redis://host`.

There is no `POSTGRES_URL` — `db/database.py` builds only from `POSTGRES_URI` — and no
`SQLiteSettings` or `MySQLSettings` mixins, which no code path read. `DatabaseSettings` stays as the
base class because `core/setup.py` dispatches on it.

## There is no `core/logger.py`; `configure_logging` in `core/config.py` reads `LOG_LEVEL`

There is no `core/logger.py`; `configure_logging(level)` in `core/config.py` is the one setup,
called by both entrypoints: `core/setup.py` for the API and `core/worker/functions.py` for the
worker. It lives in `core/config.py` because that module owns `LOG_LEVEL` and is the one both
entrypoints already import; `core/setup.py` would drag FastAPI, the routers and the models into the
worker's import graph for two lines of logging.

The one-shot scripts in `src/scripts/` keep their own `basicConfig(level=INFO)`: a person runs them
to watch something happen, and `LOG_LEVEL=WARNING` should not turn that into a silent command.

`logging.basicConfig` does nothing when the root logger already has a handler, so
`configure_logging` also sets the root level explicitly. The httpx pin to `WARNING` in
`core/setup.py` stays after the call and independent of `LOG_LEVEL`, so `DEBUG` does not write
`GEOCODER_API_KEY` into the logs. `Settings._normalize_log_level` rejects an unrecognised level,
naming the typo, and uppercases, so `LOG_LEVEL=debug` works.

## `AUTH_COOKIE_SECURE` exists for the LAN instance with no certificate

The refresh cookie's `secure` flag comes from `AUTH_COOKIE_SECURE`, default `true`. A literal
`secure=True` is right over HTTPS and wrong for the shape a self-hoster reaches for first: the app
on a LAN address over plain HTTP, where the browser accepts the `Set-Cookie` and silently declines
to store it. Sign-in appears to work and the next page load is signed out, with nothing in any log
on either side. The default keeps the safe posture without a decision; turning it off is a
deliberate edit with the reason written beside it in `.env.example`. `SESSION_SECURE_COOKIES` is the
same escape hatch for the admin panel's own cookie.

## There are two health endpoints, and the container check runs the strict one

`GET /api/v1/health` answers for the process; `GET /api/v1/health/ready` round-trips Postgres
(`SELECT 1`) and Redis (`PING`), answering 503 naming whichever failed. Collapsed, one answer is
wrong: a datastore check has an orchestrator restart a healthy process during a Postgres outage; no
check reports "up" while every request 500s.

The image's `HEALTHCHECK` runs `/health/ready`, because `depends_on: condition: service_healthy`
asks "can serve"; Docker has no restart-on-unhealthy policy, so it cannot loop.

Both send `Cache-Control: no-store`; `ClientCacheMiddleware` otherwise labels anonymous GETs
`public, max-age=60`. The 503 needs `HTTPException(..., headers={"Cache-Control": "no-store"})`,
since raising discards the injected `Response`.

The detail is `{"detail": "Not ready: redis unreachable"}`, never the exception with its connection
string; `logger.warning` at `%r` gets that. Both checks run even if the first fails, each under
`asyncio.timeout(3)`, via `asyncio.gather` so 3s bounds the request (`HEALTHCHECK`:
`urlopen(timeout=4)` inside `--timeout=5s`); a third check joins that `gather`.
`cache.client is None` reports `redis is not configured`, like `enforce_rate_limit`.

## Dev compose restarts, and its third-party tags are pinned

`restart: unless-stopped` on every long-runner (`api`, `worker`, `db`, `redis`, `mailpit`), not
`always`, so `docker compose stop` stays stopped across a daemon restart. `admin_init` keeps
`restart: "no"`: on a one-shot, a restart policy is a boot loop. `docker-compose.test.yml`'s `api`
overrides `command` to `pytest tests/ -v`, and compose inherits any scalar an `-f` overlay omits, so
the overlay says `restart: "no"` explicitly or the daemon restarts the container the instant pytest
exits, racing `--abort-on-container-exit`.
`docker compose -f docker-compose.yml -f docker-compose.test.yml config` is the check; CI never runs
it.

`redis:8-alpine` and `axllent/mailpit:v1.30`, not bare `alpine`/`latest` tags, which float to
whatever major is current the day someone pulls. CI's `redis` service moves with the compose file.
Mailpit publishes no bare-major tag, so `v1.30` is the widest pin.

`admin_init` sets `healthcheck: disable: true`: it inherits `api`'s HTTP check and only avoids
showing unhealthy by exiting inside the 20-second start period, by luck.

## A release is a tag push, and the image is built twice on two architectures

`publish-image.yml`: `push: tags: ["v*"]` is a release: `X.Y.Z`, `X.Y`, `latest`, a bare major only
from 1.0.0. `workflow_dispatch` covers scratch builds, `staging` tags, CVE rebuilds, recomputing
every alias; `latest` is a checkbox so an old tag cannot move it backwards. `push: branches: [main]`
publishes `:edge` and `:sha-<12>` (*The edge channel publishes on every merge, and deploys what it
publishes*).

It mirrors `opendiving-web`'s workflow of the same name, tags assembled by hand, not by
`docker/metadata-action`.

A `v*` ref must be `vX.Y.Z` and match `pyproject.toml`, before any layer builds. A dispatch ref is
stripped of `refs/tags/`/`refs/heads/` first; a version-shaped extra tag is refused. The matrix
checks out `needs.prepare.outputs.sha`.

`matrix.include` pairs `linux/amd64`/`ubuntu-latest` and `linux/arm64`/`ubuntu-24.04-arm`. Each
pushes by digest (`push-by-digest=true,name-canonical=true`) and `docker buildx imagetools create`
tags the manifest list, so nothing is tagged until both land. `gh release create --generate-notes`
follows, categorised by `.github/release.yml`, no assets; that job alone holds `contents: write`.

## The PR title becomes a label, in a job of its own

Release-note labels derive from the PR title's conventional-commit type: `feat`, `fix`, `breaking`
for the `!` variant, one per remaining type. A retitle moves a PR between sections: the step removes
the owned labels the new title no longer implies, leaving others alone.

It is a separate job from the title check, gated to same-repo PRs: a fork's `GITHUB_TOKEN` is
read-only, so labelling fails on external PRs. The required title check keeps `permissions: {}`; the
label job carries `pull-requests: write` and `issues: write` and skips forks.

Labels are created on demand, since `gh pr edit --add-label` fails on a name the repository has
never seen; only created, never updated, so a colour tuned in the UI stays tuned.

The type list lives once, in the workflow's top-level `env`, feeding both the enforcing regex and
the owned-labels array; `CONTRIBUTING.md`'s prose copy is one a person reads and notices.

## The install bundle lives next door, and this compose file is the development one

The root `docker-compose.yml` builds from `./src`, bind-mounts it, runs `uvicorn --reload` and
publishes the API on every interface: right for development, wrong for an install.

The install bundle (a compose file of pulled images, a `Caddyfile` and an `example.env`) lives in
[opendiving/opendiving](https://github.com/opendiving/opendiving): it pins
`ghcr.io/opendiving/opendiving-web` beside the api image and sets `SITE_URL`, `MAP_TILE_*` and the
web container's environment. Its reasoning lives in that file's comments and that repository's
`DECISIONS.md`; a copy here goes stale.

The obligation stays: any change to how this app is configured (a new required setting, a rename, a
new service) is a two-repository change. `AGENTS.md` carries the rule.

`publish-release` in `publish-image.yml` creates a release with generated notes, no assets;
attaching install files and checking both images exist at that version is the product repository's.
Renovate here does not watch the bundle's digests. Operator issues route next door; issue templates
invite rather than gate.

## `admin_init` runs as `app.admin.initialize`, and `src.scripts.*` marks nothing

`admin_init` runs `python -m app.admin.initialize` in both compose files; `main()` sits in
`app/admin/initialize.py` beside `create_admin_interface()`.

`src.scripts.*` marks nothing: the wheel installs the `src` package, so `src`, `src.app` and
`src.scripts` are importable in the image without a bind mount; `opendiving/opendiving`'s
`DECISIONS.md` (*The operator commands are `python -m src.scripts.…`, and they do run in the shipped
image*) explains it. What breaks is filesystem: `/code` holds only `app`, `migrations` and
`alembic.ini`, so relative paths and source-tree file opens fail.

A compose `command:` must be reachable from the installed package and name the same package as the
image's `CMD`: the image carries two importable copies, `/code/app` and `site-packages/src/app`,
with separate `settings` singletons, so a one-shot under the other name configures a second copy.

`src/scripts/` stays put and runs from `site-packages` either way; which scripts an operator gets is
the front door's business, answered by `git grep -oE 'python -m src\.scripts\.[a-z_]+' -- docs/`
there.

## The panel needs forwarded headers, and `TRUSTED_PROXY_IPS` is the one knob for them

The install bundle's compose file sets `FORWARDED_ALLOW_IPS` from `TRUSTED_PROXY_IPS`; gunicorn
hands it to uvicorn's `ProxyHeadersMiddleware`, which rewrites `scheme` from `X-Forwarded-Proto` and
`client` from `X-Forwarded-For` for listed peers. Without it, every request reads as `http` from
Caddy, and the admin panel breaks two ways: `create_admin_interface()` passes `enforce_https=True`
on `ENVIRONMENT=production`, so CRUDAdmin's `HTTPSRedirectMiddleware` 301s to `https://` and Caddy
proxies it back as `http`, forever; and `IPRestrictionMiddleware` compares `request.client.host`
without reading `X-Forwarded-For` (unlike `core/utils/client_ip.py`), so `CRUD_ADMIN_ALLOWED_IPS`
matches only the proxy.

One setting, because "who is in front?" is one fact; uvicorn's
`_TrustedHosts.get_trusted_client_address` and `client_ip` both take the right-most unvouched entry
and accept CIDR. They differ on shape: `client_ip._trusted_networks` uses
`ip_network(entry, strict=False)`, gunicorn's `validate_string_to_addr_list` is strict at config
load, so `TRUSTED_PROXY_IPS=172.29.0.1/16` exits `api` with
`Error: 172.29.0.1/16 has host bits set`. Write `172.29.0.0/16`; `example.env` and the docs name the
shape and the error. Per-IP rate limits still key on the true peer.

## Staging is a deployment, so the relay guards fire on any `ENVIRONMENT` other than `local`

`core.config.Settings._require_smtp_outside_local` and
`services.email_service._refuse_to_log_credential_outside_local` fire for any `ENVIRONMENT` other
than `local`. With `SMTP_HOST` unset, `send_magic_link_email` and
`send_email_change_confirmation_email` log the whole URL at WARNING, and the URL is the credential.
That is the documented sign-in flow on `local` (`README.md` and the troubleshooting guide:
`docker compose logs api | grep`) and a leak anywhere else: a staging box with more than one reader
has logs with an audience. A throwaway instance runs `ENVIRONMENT=local`, giving up only `/docs`
behind a superuser.

The line is `local`, stated positively, so a new `EnvironmentOption` inherits the safe side. The
error interpolates the environment (`ENVIRONMENT is staging but SMTP_HOST is not set`). A staging
instance without a relay does not boot; configure `SMTP_*` or set `ENVIRONMENT=local`.
`_reject_placeholder_secret_key` and `_require_from_address_with_smtp` are ungated on the same
argument.

`tests/test_config_safety.py` and `tests/test_email_service.py` parametrise both guards over
`production` and `staging`; the email side pins that the raise precedes the warning.

## `ADMIN_EMAIL` has no default, because `admin.com` belongs to somebody else

`ADMIN_EMAIL` is `str | None` with no default, `src/.env.example` ships it commented out, and
`create_first_user` exits early naming the setting. Unset means no admin account.
`scripts/create_first_superuser.py` creates an `is_superuser=True` row keyed on the address, and
passwordless sign-in delivers that account's magic link to whoever controls the domain;
`admin@admin.com` has a real owner. The same rule as `EMAIL_FROM_ADDRESS` and `CONTACT_FORM_EMAIL`.

`is_superuser` gates `/docs`, `/redoc` and `/openapi.json` on non-local non-production environments
(`core/setup.py`) and the three `/api/v1/admin/*` routes (`api.v1.admin`); it grants no access to
dives.

`ADMIN_NAME` keeps `"admin"`: a display name nothing is keyed on or delivered to.

`config()` resolves defaults at import against the developer's own `src/.env`, so
`settings.ADMIN_EMAIL is None` on the imported module proves nothing. `tests/test_config_safety.py`
loads a second copy of `core/config.py` under another name with `starlette.config.Config` pointed at
a nonexistent file; `config.py` imports nothing from this package, so the copy is isolated.

## `linting.yml`, `tests.yml` and `type-checking.yml` run on a read-only token, with nothing left in `.git/config`

All three declare `contents: read` at workflow level; a workflow with no `permissions:` block
inherits the repository default, read *and* write across every scope unless narrowed.

Every checkout sets `persist-credentials: false`. `actions/checkout` otherwise leaves the
`GITHUB_TOKEN` in `.git/config`, and each job installs the dev dependency tree into that workspace
and executes it (pytest, `alembic upgrade head`, and mypy via `plugins = ["pydantic.mypy"]` all
import it), so a compromised dependency finds a credential on disk. No job uses git after checkout.

`tests.yml`'s service containers and the coverage table in `$GITHUB_STEP_SUMMARY` use the runner or
the Actions runtime token, which no `permissions:` entry governs. `astral-sh/setup-uv`'s
`github-token` only downloads a public release.

A job needing more takes a job-level block and re-states `contents: read`, since a job-level
`permissions:` replaces the workflow's; see `pr-title.yml` and `publish-image.yml`'s
`publish-release`. `actions/*` stay on major tags (`@v7`); SHA-pinning them is a separate trade-off.

## A filter on a FastCRUD `update` is a `count()`, not an atomic condition

A filtered FastCRUD write (`crud.update(..., id=X, used_at=None)` catching `NoResultFound`) is not
atomic. FastCRUD 0.22.3's `validate_update_delete_operation` counts in a separate query before the
UPDATE and `update` discards `rowcount`; the filter is a precondition assertion, never a guard.
`request_email_link`'s `pending_count` guard relies on this.

`claim_authentication_request` in `crud/crud_authentication_requests.py` drops to Core and gates on
`rowcount`:

```python
update(AuthenticationRequest)
.where(AuthenticationRequest.id == request_id, AuthenticationRequest.used_at.is_(None))
.values(used_at=datetime.now(UTC))
```

`rowcount == 0` is the race-lost signal under READ COMMITTED: the loser blocks on the winner's lock,
re-evaluates the `WHERE` and matches nothing. The read-based `used_at` check stays as a fast path;
the claim sits before `resolve_identity` or the email UPDATE. Sign-in's loser is a 401;
`verify_email_change` tolerates a replay while that email is current, via
`_replay_result_or_reject`. Its two writes share one `db.commit()` (`commit=False`), claim first, so
`except IntegrityError: await db.rollback()` unspends the token too.

Endpoint tests use `stub_claim` (`tests/helpers/mocks.py`), since a bare `Mock(spec=AsyncSession)`'s
`rowcount` reads as won; `tests/test_authentication_request_claim.py` races two claims under
`asyncio.gather` on real Postgres.

## The disclosure policy names two channels, and naming a channel means making it live

`SECURITY.md` names two channels in a deliberate order. GitHub private vulnerability reporting comes
first: it needs no mail infrastructure, the thread lives on the repository where the fix lands, and
it can become a published advisory with a CVE. It is a per-repository toggle (Settings → Advanced
Security → Private vulnerability reporting) that renders only on public repositories; committing
`SECURITY.md` does not flip it, and the account-level switch in personal settings covers a user's
own repositories, not the `opendiving` org. `security@opendiving.app` is second and is a real
mailbox. A published role address is a claim about infrastructure, so the change that names one
creates the mailbox; `opendiving-web/DECISIONS.md` records the invented `security@`, `community@`
and `docs@` behind that rule. `opendiving-web` carries a copy of the policy; `opendiving/opendiving`
carries a third that routes on the operator/app line instead of restating this one. No org-wide
custom security configuration exists.

## A pin is a promise to renew, and Renovate is what renews them

The install bundle pins `postgres:18`, `redis:8-alpine` and `caddy:2.10-alpine` to digests;
`.github/renovate.json5` renews them. A pin nothing renews is worse than a floating tag: both change
silently, but the tag drifts towards the patched build and the digest away from it while looking
deliberate. Renovate over Dependabot because Dependabot has no uv ecosystem (`uv.lock` feeds
`uv sync --locked`), cannot read `.python-version`, and cannot group across ecosystems. `pinDigests`
is `false` and `helpers:pinGitHubActionDigests` is not extended: first-party actions like
`actions/checkout@v7` ride a major tag, third-party ones like `astral-sh/setup-uv` are SHA-pinned.
The `Dockerfile` bases `ghcr.io/astral-sh/uv:python3.14-bookworm-slim` and
`python:3.14-slim-bookworm` float on purpose: the base-image CVE remedy, a Publish Image dispatch at
the old `v` tag (`CONTRIBUTING.md`), works only because the base resolves at build time.
`requires-python = "~=3.14.0"`, ruff's `target-version = "py314"`, `.python-version` and the
`Dockerfile` tags move together; Renovate reaches only the last two, so they sit behind
`dependencyDashboardApproval`, the rest hand-edited.

## The scan that matters runs on a schedule, not on a pull request

`.github/workflows/vulnerability-scan.yml` has two jobs: the PR job asks whether a change is
vulnerable, the scheduled job whether what people already run is. Trivy, not `pip-audit`:
rebuild-forcing CVEs are Debian packages in `python:3.14-slim-bookworm`, and Trivy labels OS and
Python findings, whose remedies differ — a Publish Image dispatch for OS, a new patch release for
Python, because a dispatch reinstalls that tag's `uv.lock`. Trivy floats on `latest`; a frozen
scanner silently misses new advisories. The PR job fails on fixable HIGH/CRITICAL; the scheduled job
fails only on errors. `ALIASES` starts as `(edge)`, and the job skips only with nothing resolved and
no `v*` tag. Only the newest release plus `edge` is scanned, per `SECURITY.md`'s support table:
`prepare` in `publish-image.yml` rebuilds one version, so an older minor's finding never clears.
Findings are public advisories about shipped bytes and stay on the scanner's surface; an undisclosed
defect belongs in `SECURITY.md`'s private channel.

## The alert arrives where it can be acted on, and the scan replaces the whole set

The scheduled scan uploads SARIF to code scanning via `github/codeql-action/upload-sarif`: an upload
replaces the alert set for its category, so a fixed finding closes itself and a recurrence opens
fresh. Every image's `.sarif` goes to `sarif/`, uploaded once under the constant category
`published-images` — separate uploads under one category overwrite each other, and a category per
image leaves alerts open forever once an `X.Y.Z` alias leaves the scan set. The upload is gated on
targets, not findings; the empty upload closes the last scan's alerts. Each image is scanned twice,
the second with `--ignore-unfixed`, since `trivy convert` cannot filter fixability; the markdown
report stays to say which remedy applies. The footer builds absolute URLs from `github.server_url`
and `github.repository`, because a relative link 404s from a run summary. CodeQL default setup is
unaffected. The `image-cve` label stays; deleting it strips the one closed issue recording the old
surface.

## Security headers are the app's, not the proxy's

`SecurityHeadersMiddleware` fills in `Content-Security-Policy: frame-ancestors 'none'`,
`X-Frame-Options: DENY` and `X-Content-Type-Options: nosniff` where absent. A `header` block in the
bundled `Caddyfile` was rejected: it fixes one deployment shape (the `proxy` profile), and `/admin`
(`main.py`) and `/docs` (`core/setup.py`) are this app's own responses. `frame-ancestors` alone,
since a `default-src` breaks CRUDAdmin's templates; `X-Frame-Options` for parity with
`next.config.js`. No `Strict-Transport-Security`: it is host-scoped, the web app sends it, and its
off-switch `WEB_HSTS` lives there, so an API copy would ignore `WEB_HSTS=off`. Handlers keep their
own headers; the binary downloads in `dives.py`, `certifications.py` and `export.py` spell out
`frame-ancestors 'none'` because it never falls back to `default-src`. Registered last, so
`add_middleware` puts it outside `CORSMiddleware`'s self-answered preflights; only the 500 from
`ServerErrorMiddleware` escapes. The bundled `Caddyfile` still sends `/admin*` to `api:8000` while
the operator surface moves to the web app's `/admin`; `CRUD_ADMIN_MOUNT_PATH` keeps its `/admin`
default so existing panel installs keep working.

## `public` requires the absence of every credential, not just a bearer token

`ClientCacheMiddleware` labels a response `public, max-age=60` only when the request uses a safe
method and carries none of `CREDENTIAL_HEADERS` — `Authorization` *and* `Cookie`. A bearer token is
not the only credential the app accepts: the admin panel authenticates with a session cookie, and
CRUDAdmin's `_should_add_cache_headers` skips the login path, `/static/` and every 3xx, so those
responses are this middleware's to label. CRUDAdmin's own `AdminAuthMiddleware` sets
`no-cache, no-store, must-revalidate, private` on the pages it authenticates and the "never
overwrite an explicit `Cache-Control`" rule preserves it, but a cookie-authenticated GET marked
public relies on a pinned dependency to keep saving it. An *anonymous* `GET /admin/login` stays
`public`: the login template (`templates/auth/login.html`) is a static form, and the CSRF token is
issued as a cookie by the POST handler. Re-read that template when bumping `crudadmin`; a
per-session token in the HTML would make the mount path the right check.

## Keying on the cookie rather than on the mount path

The credential check reads the `Cookie` header, not `settings.CRUD_ADMIN_MOUNT_PATH`. A path check
is an allowlist by omission: the next cookie-authenticated surface mounted on the app is public
again silently, a shipped `public` header rather than a red test. Reading `CRUD_ADMIN_MOUNT_PATH` in
a generic middleware couples it to one feature's config and needs prefix matching with its own edge
(`/admin` must not match `/administrators`). A wrong path fails open, a wrong cookie fails closed,
which is also why `CREDENTIAL_HEADERS` takes the whole `Cookie` header rather than a list of session
cookie names. Accepted costs: the install bundle serves API and web app from one origin and sets
`refresh_token` without a `path`, so a signed-in browser's API reads are `private, no-store` even
for public data (anonymous visitors, whom a shared cache serves, still get `public, max-age=60`),
and the panel's static assets are `no-store` for a signed-in admin.

## The public branch carries a `Vary` built from `CREDENTIAL_HEADERS`

The public branch appends `Vary: Authorization, Cookie`. The label is decided from request headers,
and a cache not told which ones answers the next request for that URL from the entry it stored: an
anonymous `GET /admin/` is a 303 to login, and a signed-in admin behind the same cache is bounced.
The value is `", ".join(sorted(CREDENTIAL_HEADERS))`, never a second literal: a credential header
added to the set but not the `Vary` still gets `private, no-store` itself while a shared cache
serves it the anonymous entry, so two copies drift fail-open.
`tests/test_client_cache_middleware.py` asserts containment, so the set is the only place to edit.
Added with `MutableHeaders.add_vary_header`, not assignment: `add_middleware` inserts at the front,
so `ClientCacheMiddleware` sits outside `CORSMiddleware` and sees its `Vary: Origin`, which
assigning would drop. Anything inserted into the stack must keep that order. The `private, no-store`
branch needs no `Vary`; nothing may store it.

## Two properties are guarded by enumeration, not by review

Two properties are enumerated over the real route table: every `/api/v1` route requires
authentication (`tests/test_route_authentication.py`) and every `/{uuid}` route resolves ownership
(`TestEveryUuidRouteIsAccountedFor` in `tests/test_ownership.py`). Both take an allowlist dict keyed
by route with a sentence of justification as the value, each with a stale-entry test that doubles as
the vacuity check. The ownership guard is behavioural: it drives each route with `get_current_user`
overridden and the crud `get` stubbed to another `user_id`'s row, asserting the 404, and stays
outside the `db_available()`-gated modules that skip without `POSTGRES_SERVER=localhost`. The six
gear-service routes are checked by asserting `resolve_schedule_for_user`/`resolve_record_for_user`
received the caller's `user_id`. `GET /api/v1/species/{uuid}` is the one ownerless route
(`crud/crud_species.py`). The walk uses `iter_route_contexts` via `tests/helpers/routes.py`, not
`app.routes`, because `include_router` stores lazy `_IncludedRouter` wrappers.
`test_the_bearer_scheme_alone_counts` is the canary that `oauth2_scheme` (`auto_error=True`,
`core/security.py`) in a `.dependencies` walk counts as authentication. Scope is `/api/v1`; the docs
routes are `ENVIRONMENT`-gated and mounts are skipped explicitly.

## Onboarding demands a username rather than suggesting one, so nothing generates one

No server-side code derives a username from an email. `POST /auth/complete` (`api/v1/auth.py`) is
the only place a `User` row is created; it takes `username` on `ProfileCompletionRequest` and checks
availability itself, so no path needs a name the person did not type. A prefilled suggestion in the
onboarding form is a plausible product change, but it belongs on the client (prefilling as the user
types, not on submit), and the availability oracle it needs is already an endpoint concern with its
own rate limit — see the docstring on `complete_profile`. `services/auth_service.py` has no
`generate_unique_username`, and a server-side generator would be the wrong half of that feature.

## File payloads live on the files volume, not in Postgres

Uploaded dive-computer exports (`dive_file`), c-card images (`certification_file`) and avatars are
blobs behind `blob_store` — under `FILE_STORAGE_DIR` (`/data/files`, a Docker named volume) by
default — named by a `storage_key` the row carries, never `bytea` columns, which bloat backup size
and restore time. Any further move of stored bytes is a real data migration: the project's own
instance runs `main` and holds data. The trade given up is "one `pg_dump` is the whole logbook".
Backup is two artifacts, in order: dump the database first, copy the files second. Files are written
before their rows commit and never mutated, so a later copy is a superset of what the dump
references, except a file deleted between the two steps, which leaves one dangling row; the
backup-and-restore guide names that window and offers "stop the stack first" as the exact option.
Atomicity between bytes and rows is replaced by that ordering rule and the sweeper.

## A filesystem volume, not an object store

`local` is the default backend and what every compose install gets: no bundled S3 server, no extra
container, credential pair or second data directory to back up. On a single node the S3 API buys
nothing the filesystem lacks — no replication, no durability, no multi-writer this app needs — and
the self-hosted apps this project measures against (Immich, Nextcloud, Paperless-ngx, Forgejo,
Mastodon, Outline, PhotoPrism) all default to local disk; MinIO itself is in maintenance mode with a
proprietary successor. The shape that takes a second backend is what is built: opaque string keys
that are also valid S3 object keys, bytes in and bytes out, no caller holding a `Path`, and the
whole filesystem in `services/blob_store.py`. `FILE_STORAGE_BACKEND` exists because the first named
trigger — a hosted offering — fired; see *"A second backend, because the hosted disk is not
shared"*.

## A second backend, because the hosted disk is not shared

`FILE_STORAGE_BACKEND` selects `local` (default) or `s3`, any S3-compatible store. Topology forces
it: on the hosting platform a persistent disk attaches to one service, and the API and the worker
(`purge_deleted_accounts`) both need the blobs; a shared store also keeps zero-downtime deploys.
S3-compatible rather than a vendor SDK because the six operations used (put, get, delete, batch
delete, head, list) are what every store implements. `boto3` over hand-signed `httpx`: 26 MB of
botocore beats SigV4 bugs indistinguishable from bad credentials. `blob_store.new_s3_client` pins
`request_checksum_calculation` and `response_checksum_validation` to `when_required` (newer botocore
sends CRC32 trailers some stores reject), `signature_version="s3v4"` and standard retries. Keys are
unchanged, so `src/scripts/migrate_blobs.py` is an idempotent copy loop that never deletes the
source. On `s3` the post-commit delete runs in a thread, unawaited, because a `DeleteObjects` round
trip would block the loop; `blob_store._await_pending_removals()` is for tests only.
`tests/test_blob_store_s3.py` drives an in-memory stub raising real `ClientError`, not `moto`.

## Every key carries a per-write nonce, and that is what makes the unlink safe

Keys are `{kind}/{sha256[:2]}/{uuid7}_{sha256}`, minted by `blob_store.new_key`, which is
deliberately impure: the same arguments give a different key every call. Blobs and rows live in two
stores, so the rule is write the file, then commit the row; delete the row, then unlink. With
re-derivable keys that second rule is data loss: one request's post-commit unlink destroys a blob a
concurrent request has since committed a row against. Keying on the owning row's uuid is rejected
because a card's row survives replacement (`ON CONFLICT DO UPDATE` preserves its uuid). The nonce
makes a retired key unrepeatable and closes the sweeper's TOCTOU; re-uploading a card's existing
bytes therefore writes a new file. The content hash keeps blobs immutable and lets `sha256sum`
verify any file; sharding uses its prefix because uuid7's leading hex is a timestamp. No cross-row
sharing: dedupe is `ux_dive_file_user_id_sha256`, and deletion is `DELETE … RETURNING storage_key`
into `delete_after_commit`.

## Orphans are the only failure product, and there is a script for them

A crash between `put()` and the commit, a racing replacement, a restore whose rows were deleted
after the dump, and a hard delete of a `Certification` from the admin panel (its FK cascade removes
file rows with no service layer to unlink) all strand unreferenced files. They are harmless until
swept, and `src/scripts/sweep_orphaned_files.py` sweeps them: dry-run by default, a 24-hour mtime
grace so an online sweep cannot race an upload in flight, and a refusal — overridable only by
`--force` — when the numbers look like a wrong database rather than real orphans. That absolute
sanity check is the lesson of Gitea's `doctor --fix` deleting valid LFS files against a database
that looked empty; an mtime grace does nothing against it. No arq cron: scheduled deletion machinery
is what that warns against automating.

## Startup fails loudly if it cannot write, and shouts if the volume looks unmounted

`ensure_storage_ready` runs in the lifespan before `apply_migrations`, because the revision that
moves payloads writes files itself. The writability probe is per-pid (`{root}/tmp/.writable-{pid}`,
unlinked with `missing_ok=True`; one lifespan per gunicorn worker); on `s3` it is a put-and-delete
under the `tmp/` prefix that `iter_keys` hides. The worker runs it too, since
`purge_deleted_accounts` is a GDPR erasure and credentials that cannot delete must fail at boot. It
does not catch a `local` volume that was never mounted: the image creates `/data/files` owned by uid
1000, so an unmounted container probes its own layer; `docker-compose.yml`'s `files-data` line
prevents that. After migrations, `warn_if_files_volume_looks_empty` logs CRITICAL when file rows
exist but `blob_store.has_any_key()` (a bounded `MaxKeys=1` call, not a walk) is false, without
refusing, because the documented restore order has a legitimate window. `TestClient` enters the real
lifespan, so `tests/conftest.py` pins `FILE_STORAGE_DIR` to a temp directory before importing
`src.app.main`, and `.github/workflows/tests.yml` sets it too.

## File payloads: Download routes keep their contract, and `async_engine` is disposed both ways

The download routes keep their contract: ownership check, narrow sha256 query,
`ETag`/`If-None-Match` 304, and their own CSP with `frame-ancestors 'none'` — load-bearing because a
response that sets its own policy opts out of `SecurityHeadersMiddleware`'s default. `FileResponse`
is rejected: its mtime-derived ETag collides with the sha256 contract, its 304 handling lives in
`StaticFiles`, and it brings Range support this contract never promised. `opendiving-web` fetches
through its API client into object URLs and never sees where bytes live. The lifespan opens the
shared `async_engine` twice at startup, and a pooled asyncpg connection belongs to the loop that
opened it while `TestClient`'s portal loop is no test's loop — so the helpers using that engine from
`pytest-asyncio` tests (`test_export_loader.py`'s `_load`, `test_worker.py`'s
`_dispose_the_app_engine`) dispose it on both sides, not only on the way out. The symptom otherwise
is `asyncpg … another operation is in progress` from a query unrelated to files.

## File payloads: The migration

One hand-written revision, `c3c2c4dd4c27`; autogenerate cannot see a data backfill. It survives
offline rendering — `tests/test_migrations.py` runs `alembic upgrade head --sql` against no database
— by guarding the move with `context.is_offline_mode()`. Every filesystem touch is lazy, per row
written, because CI runs `alembic upgrade head` on a bare runner where `/data/files` is not
creatable. The key layout and atomic-write helper are inlined, not imported: a revision is frozen
history, and importing live layout code would let a refactor rewrite the past. `FILE_STORAGE_DIR` is
the one live setting read, so the volume location matches what the app reads; the revision knows
nothing about `FILE_STORAGE_BACKEND`, which is harmless on an `s3` instance, necessarily new and
holding no `bytea` rows. It is one transaction, so a mid-move failure rolls back and a retry
rewrites byte-identical files. `downgrade` raises `NotImplementedError`. Reclaiming the dropped
columns' pages needs a `VACUUM FULL`, the operator's call.

## The two messages that reach an operator name a URL, not a repo path

`warn_if_files_volume_looks_empty`'s CRITICAL and revision `c3c2c4dd4c27`'s `downgrade` refusal both
point at `https://github.com/opendiving/opendiving/blob/main/docs/backup-restore.md`. Their reader
runs the published image with no checkout, so a repo-relative `docs/` path resolves to nothing for
exactly that audience; comments and the config template in `src/` use absolute URLs too, since the
self-hosting docs live in `opendiving/opendiving`. The URL comes last and carries no trailing
period, because linkifying terminals swallow the punctuation into the href. `blob/main`, not a
release tag: an operator on any version wants the current restore procedure. `blob/main/<path>` is
the shape for a cross-repo file link (it renders a file; `tree/main/<path>` is for a directory, and
GitHub 301s `blob` on a directory rather than failing). `tests/test_operator_messages.py` pins that
the URL is present and no bare `docs/` path survives beside it, not the wording.

## The sign-in email carries a code as well as a link

`POST /auth/email/request` mints two credentials for one `authentication_request` row: the magic
link and a six-digit code printed beside it. Either completes the sign-in and whichever arrives
first consumes the row, since both end at `claim_authentication_request`; the code is redeemed at
`POST /auth/email/verify-code`. A link signs in the device that opens it, and here the split —
address typed on a desktop with a dive computer plugged in, mail read on a phone — is the normal
case; a code crosses that gap because a person carries it. Slack, Notion and Anthropic send the same
link-plus-code email. Three columns, no new table: `code_hash`, `code_attempts`, and a public `uuid`
(the `request_id`). The row's expiry, single-use claim and supersede-on-new-request rule apply to
the code unchanged.

## `request_id`, and the denial-of-service it removes

`POST /auth/email/request` answers with the new row's public uuid, the only handle on the verify
endpoint: there is deliberately no lookup by email. The id is not an oracle; a row is minted for
every address, account or not. An email-keyed verify endpoint lets anyone who knows an address fire
wrong codes at the victim's live request, and with a burnt code burning its link, five garbage
guesses cancel a sign-in for the accounts whose inbox is the recovery path. Keying on `request_id`
makes the row unreachable to anyone who did not request it, and a burnt code voids `code_hash`
alone: the link keeps working, which `tests/test_authentication_request_claim.py` pins. It also
settles the two-tabs race: the invalidate-then-create pair is not atomic, so two live rows can exist
for one address, which an `(email, live)` lookup resolves through FastCRUD's unordered `.first()`;
by uuid each tab names its own.

## The attempt cap is the boundary, and it is one statement

Six digits is ~20 bits; `code_hash` is SHA-256 for hygiene only, and no key-stretching saves a
millionfold space. `SIGN_IN_CODE_ATTEMPTS_MAX` (5) is the defence, so it must hold under
concurrency: `register_failed_code_attempt` is hand-written Core, one `UPDATE` that increments and,
in the same `CASE`, nulls `code_hash` when the count reaches the cap. Read-decide-write lets five
parallel guesses all read `0`, and rate limiting is no substitute: it fails open on a Redis outage.
Residual exposure: only a requester holds a `request_id`, and every request emails the victim.
`MAGIC_LINK_REQUEST_RATE_LIMIT_PER_EMAIL` allows 3 per `MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS` (900
s), each worth 5 guesses in 10⁶: 15 guesses a quarter hour, an expected success in ~694 days of 288
emails a day. The lever, if observation disagrees, is eight digits (×100), not the request cap,
whose 3 → 1 of headroom every diver with a lost first email pays for.

## Sign-in code: Everything else is the shape already there

Every rejection — wrong digits, unknown `request_id`, expired or superseded request, spent code — is
`"This code is invalid or has expired."`, so a caller learns nothing about a row it cannot name; the
link's rejections stay specific because they steer a human on a page. A malformed code is a 422, not
a guess: `EmailCodeVerifyRequest` strips non-digits (the email prints `481 052`) and insists on six,
since `code_attempts` bounds guesses at the secret. `purpose="email_change"` rows never carry a code
— that flow proves the new mailbox by opening the link there — so
`AuthenticationRequestCreate.code_hash` defaults to `None` and only `request_email_link` sets it.
`/auth/email/verify-code` is listed in `ANONYMOUS_BY_DESIGN` (fails closed) and in the POST-only
minting set in `test_client_cache_middleware.py` (opt-in; a forgotten entry passes silently).
`_has_expired` replaces the third copy of the naive-timestamp coercion in `api/v1/auth.py`.

## Sign-in code: The migration

`60ec1a2894ea` is autogenerated and corrected in one respect: both `NOT NULL` adds (`uuid`,
`code_attempts`) are given a server default and then have it dropped, so rows in flight are filled
while the models stay the only place a default is declared — `migrations/env.py` does not set
`compare_server_default`, so a leftover server default drifts silently past `alembic check`. The
backfill uses `gen_random_uuid()` rather than `uuidv7()`: the column is only looked up by equality
and every row it touches expires within the hour. Nothing backfills `code_hash`; a request created
before this ran was emailed no code, and `NULL` is the truth about it.

## Passkeys are a third *first* factor, not a second one

WebAuthn passkeys (`POST /auth/passkey/options`/`verify` in `api/v1/auth.py`, `/user/passkey*` in
`api/v1/passkeys.py`) sit beside the magic link and Google as a third way in, deliberately not a
second factor. A second factor backstops a phishable password, and this system has none: both
existing methods are possession proofs, and the email path is the recovery of last resort, so any
second factor answerable with "use the magic link instead" is theater. Making it real means letting
users switch email off, importing lockout and recovery codes into a dive logbook. A passkey ceremony
with user verification is already two factors in one gesture and origin-bound, strictly stronger
than the link beside it. `resolve_identity` → `_start_onboarding_or_sign_in` → `issue_tokens` is
"verified identity in, session out" with no step-up concept; a third identity proof drops in.
Account security stays bounded by the email account; TOTP and backup codes return only if this
guards something worth more than that inbox.

## The passkey path skips `resolve_identity` entirely

Every other entry point asks which account owns an email and whether the provider is linked. An
assertion carries no email; the `webauthn_credential` row is the link and can only exist because a
signed-in user registered it, so `finish_sign_in` resolves `credential_id` → `user_id` directly and
hands an `AuthenticatedUser` to the shared funnel. No auto-linking decision exists here — Google's
silent link-by-email in `auth_service.resolve_identity` rests on `email_verified`, and a passkey
asserts nothing to link by. This is a third resolve site, outside the `resolve_identity` call sites,
so it answers `AuthenticatedUser | DeletionPending` itself and its user lookup carries no
`is_deleted` filter; see *"A restore is a click, not a side effect of signing in"*.

## No `authentication_provider` row for passkeys, and its own table instead

`uq_authentication_provider_user_provider` allows one row per `(user_id, provider)`; passkeys are
many-per-user and each carries state that table has no column for (public key, signature counter,
transports), so they live in `webauthn_credential`. No `provider="passkey"` marker row beside it
either: nothing reads `authentication_provider` except `resolve_identity`'s provider-link branch,
which a passkey never traverses, and a marker would need create-on-first / delete-on-last
bookkeeping to stay truthful when `webauthn_credential` already answers "does this user have
passkeys".

## Challenges live in Redis, and this is the one place it fails *closed*

`core/utils/rate_limit.py` fails open on a Redis outage because throttling is defense-in-depth;
`services/passkey_challenges.py` returns 503 instead, because the challenge is the anti-replay
guarantee. The failure domains stay independent: a Redis outage stops passkeys and leaves the magic
link (pure Postgres) working, an SMTP outage does the reverse, and Google is untouched by either.
Redis rather than an `authentication_request` row because conditional UI arms on every signed-out
page view, landing hero included, so challenges are minted at page-view frequency — a row per view
is the unbounded-personal-data-table shape that table needed a sweep for, while a TTL key cleans
itself. Consumed with `GETDEL` on the verify attempt, so a challenge is single-use even when
verification then fails. Registration challenges are keyed per user, so two tabs racing both fail:
accepted, since it self-heals on retry and the alternative is unbounded keys for a ceremony nobody
runs twice.

## `FRONTEND_URL` is the passkey domain, and the origin must be *rebuilt* from it

`FrontendSettings.passkey_rp_id` and `passkey_origin` both derive from one `urlparse(FRONTEND_URL)`,
so there is no second place to be wrong — the same reasoning that rejects a `PASSKEYS_ENABLED` knob
(the browser's capability detection is the switch). The origin is `f"{scheme}://{netloc}"`, never
the raw setting: browsers put a bare `scheme://host[:port]` in `clientDataJSON.origin`, so a
trailing slash in `FRONTEND_URL` would 401 every ceremony while magic links (plain concatenation in
`auth.py`) kept working. `TestDerivedRelyingParty` in `tests/test_passkeys.py` pins both halves.
Consequences: changing `FRONTEND_URL`'s hostname orphans every registered passkey (browsers scope
credentials by RP ID; email is the recovery), and an IP-address `FRONTEND_URL` is never a valid RP
ID — the browser offers WebAuthn over HTTPS and then throws `SecurityError` at every ceremony. Plain
HTTP gets no WebAuthn, so a LAN instance hides the UI; `localhost` is exempt, which keeps local
development working.

## `resident_key: "required"`, `user_verification: "preferred"`

Discoverable credentials only: the assertion carries the credential id and the row names the user,
so sign-in is usernameless, one code path, browser autofill falls out of it, and no "does this email
have a passkey" oracle exists. It costs pre-resident-key security keys (old U2F YubiKeys), which
cannot register; the alternative is an email-first `allowCredentials` flow that leaks exactly that.
UV `preferred` rather than `required` keeps a PIN-less key usable: a passkey asserted without UV is
a bare possession proof, the strength of the magic link beside it, so nothing is lost against the
current floor, and the UV flag arrives in every assertion if a stricter policy is ever wanted.

## A counter regression is a `WARNING`, and the app does the comparison itself

Synced passkeys report `0` forever and `0 → 0` passes; a regression (stored > 0, presented ≤ stored)
is the cloned-authenticator signal, and rejecting it blocks the clone while the real device, whose
counter is ahead, keeps working. py_webauthn already raises on that comparison inside
`verify_authentication_response`; `_record_if_counter_regressed` re-does the stored-versus-presented
comparison against the assertion's own `authenticatorData` rather than parsing the library's
exception message, which any release can reword and would silence a security log on a dependency
bump. `WARNING` because the app configures no logging and `uvicorn` only its own loggers, so
anything lower is dropped (same as refresh-token reuse). It writes an audit row as well as the line
— the persist-only trail's criterion for sites that write nothing of their own — and the row commits
before `finish_sign_in` raises its 401, since `async_get_db` does not commit on unwind.

## One 401 for every way an assertion can fail

Unknown credential, tombstoned owner, expired or spent challenge, wrong origin, wrong RP ID and bad
signature are one indistinguishable message; telling them apart answers "does this credential exist"
and "does that account still" for whoever holds a failed assertion.
`test_every_failure_answers_identically` collects the messages into a set and asserts one.
`record_assertion` (`crud/crud_webauthn_credentials.py`) is the same conditional-`UPDATE` trick as
`claim_authentication_request`: verification runs against the counter read before it, so the write
is predicated on that value still being current, and exactly one of two racing submissions of one
assertion advances the row and mints a session. FastCRUD's filtered `update` cannot express it — see
*"A filter on a FastCRUD `update` is a `count()`, not an atomic condition"*.

## `GET /user/passkeys` is not cached, which makes it the sixth `OwnedResourceCache` opt-out

The list is unpaginated, a handful of rows, and embedded by nothing, so there is no page worth
caching and no invalidation obligation; caching it would invent a thing that can go stale. Other
opt-outs have their own reasons (reads enriched by a second query, `courses.py`'s ordering,
`sessions.py` varying by credential, `dive_form_presets.py` for this same one); the class docstring
is the list — count it rather than trusting a number written beside it. The heading's ordinal is
passkeys' position in that list, which does not move when something is appended. `_LIST_LIMIT` is
deliberately above `PASSKEY_MAX_CREDENTIALS_PER_USER`, not equal: the cap is enforced at
registration, so lowering the setting later leaves accounts holding more rows than it allows, and a
limit equal to the setting would hide exactly those — a passkey nobody can see is a passkey nobody
can revoke.

## The test authenticator is vendored, not depended on

`tests/helpers/webauthn.py` (~130 lines) emulates `navigator.credentials` well enough to sign real
ceremonies. `soft-webauthn` is rejected: its last release is from 2022 and it pins `fido2 <1.0.0`,
which would drag a years-old copy of a security library into the dev environment and cap it there.
The vendored version leans only on `cbor2` and `cryptography`, both of which arrive with `webauthn`,
so it costs no dependency. Recorded fixtures are strictly worse: a challenge is generated per
ceremony, so a fixture can only replay against a challenge pinned to match it, and the test could
never exercise the challenge check. Every ceremony test signs whatever it is given and runs through
the real verifier; only Redis, the CRUD singletons and the rate limiter are stood in for.

## There is no "sign up with a passkey"

A passkey can only sign in. Registration lives behind an authenticated session, so a visitor with no
account takes the email or Google path, finishes onboarding, and is offered a passkey afterwards.
The alternative — an onboarding token carrying a pending attestation through `POST /auth/complete`
to land the credential with the new `User` row — is rejected on three counts: it couples account
creation to a ceremony replayed across two requests, a new failure mode in the one flow that must
not have any; it would create the only account shape whose email was never structurally verified
(every account exists only behind a verified onboarding token); and it saves one screen, for
first-time visitors, once. So enrollment has to be offered — the web client owns a nudge after
onboarding and a card in settings. Revisit only with funnel data showing the email round-trip losing
signups.

## The absence of a passkey switch is what puts eligibility in the operator docs

There is no `PASSKEYS_ENABLED`, so an operator asking why their instance shows no passkey option is
asking a question no server-side state can answer: nothing was configured, nothing failed, and the
API never learns that a browser declined the ceremony. Only the deployment's shape explains it. So
the configuration reference
(`https://github.com/opendiving/opendiving/blob/main/docs/configuration.md`) carries the rules as an
eligibility list rather than a setting — HTTPS, a hostname, `localhost` exempt, IP addresses never —
`reverse-proxy.md` repeats it where the plain-HTTP LAN instance is described, and
`troubleshooting.md` answers the symptom. The trap the docs exist for is the IP address:
`https://192.168.1.10` is a secure context, so the browser offers WebAuthn and then throws, which
reads as a certificate problem and is not one.

## Signing is enforced by two local hooks, because GitHub cannot do it yet

No git setting can refuse an unsigned commit (`-c commit.gpgsign=<falsy>` outranks every config
file), so enforcement lives at the push or before the command runs. A GitHub `signatures` ruleset
requires them on every branch except `main`, where required signatures plus squash-only merging
would refuse any pull request you did not author. Outside contributors' commits are exempt.
`.githooks/pre-push` (`git config core.hooksPath .githooks`) refuses a push containing a `%G?` of
`N`; GitHub's web-flow commits on `main` report `E`. It resolves the advertised sha with
`cat-file -e` and refuses when `rev-list` fails, since `revs=$(...)` hides exit 128.
`.claude/hooks/no-unsigned-commits.py`, a `PreToolUse` hook on Bash, rejects a command only when git
reports the key it would disable, strips heredoc bodies first, and catches a single `ValueError`: on
the shell's `python3` a `SyntaxError` exits 1, failing open. The script is tracked via `.claude/*`
plus `!.claude/hooks/`; registration is in untracked `.claude/settings.local.json`.

## A refresh token is only as alive as its account

`POST /auth/refresh` runs `crud_users.exists(db=db, uuid=..., is_deleted=False)` between
`verify_token` and rotation. It is the one path that mints a session on a signature alone:
`verify_token` checks blacklist, expiry, signature and `token_type`, never a row, while
`get_current_user` filters `is_deleted=False` and every other session source resolves an account.
Otherwise `erase_user` blacklists only the two tokens it is shown and every other device keeps
rotating for `REFRESH_TOKEN_EXPIRE_DAYS`. The 401 is the same 401: no oracle for whether a uuid ever
had an account. Nothing is logged: post-deletion refreshes from other devices are routine. The
lookup precedes spending the presented token, so a 401 writes nothing and a reversed soft delete
leaves other sessions working, which `POST /auth/restore` depends on. `tests/test_auth_refresh.py`
pins it with a fake `crud_users` that filters like FastCRUD and a Postgres-backed class for the real
SQL; `Mock(spec=AsyncSession)` makes `exists` read as found, so unit tests state `SignedInAccount`
explicitly.

## `authentication_request` is swept on a cron, a week after each row expires

`core.worker.functions.purge_expired_authentication_requests` runs hourly at `:00` beside
`purge_expired_tokens`, `run_at_startup=True`, and deletes rows older than `expires_at` plus
`AUTHENTICATION_REQUEST_RETENTION` (seven days). Every row stores an email address, sometimes a code
hash; `purpose="sign_in"` rows carry `user_id IS NULL`, beyond the `ON DELETE CASCADE` on
`authentication_request.user_id`, and the account purge's delete-by-email misses rows created under
a previous address, so this sweep is the table's primary bound. Deleting at `expires_at` would be
wrong by a week: `verify_email_change` tolerates a replay of a used link while the address is still
current (`_replay_result_or_reject`), and that leniency lives only in the row. Not a setting,
because tuning it tunes that leniency unknowingly. One Core `DELETE` with `rowcount`, where
`purge_expired_tokens` counts first because FastCRUD's `delete()` raises `NoResultFound` on an empty
match. `expires_at` is indexed by a hand-written revision. Cost: a link past retention reports
"invalid" rather than "expired"; `check_email_link` already collapses both to `valid=false`.

## Every foreign key into `user.id` declares `ondelete="CASCADE"`

Every foreign key into `user.id` declares `ondelete="CASCADE"`: `certification_user_id_fkey`,
`dive_user_id_fkey`, `dive_file_user_id_fkey`, `dive_site_user_id_fkey`, `gear_item_user_id_fkey`,
`gear_service_record_user_id_fkey`, `gear_service_schedule_user_id_fkey`, `gear_set_user_id_fkey`,
`trip_user_id_fkey`, `user_dive_stats_user_id_fkey`, beside `authentication_provider`,
`authentication_request` and `webauthn_credential`. `DELETE /user` soft-deletes (`SoftDeleteMixin`);
the raw `DELETE FROM "user"` is the purge's. Two traps in the revision: autogenerate and
`alembic check` do not detect an `ondelete` change, so it is hand-written; and Postgres cannot
`ALTER` a delete rule, so each is `DROP CONSTRAINT` plus `ADD CONSTRAINT` under Postgres's default
`<table>_<column>_fkey` name, with `downgrade` passing `ondelete=None` (NO ACTION). No index is
added: all ten already carry a plain btree leading with `user_id`, and a cascade on an unindexed or
partial-indexed FK seq-scans the child; `tests/test_foreign_key_indexes.py` checks every FK.
`tests/test_user_cascade.py` is two halves: `TestEveryForeignKeyIntoUserCascades` walks
`Base.metadata` to catch a bare `ForeignKey("user.id")`, and
`TestDeletingAUserTakesEverythingWithIt` seeds one row per table plus second-order rows
(`certification_file`, `dive_dive_site`, `gear_set_item`, `trip_part`) and issues the raw `DELETE`
on Postgres (skipped without `POSTGRES_SERVER=localhost`).

## Deleting an account is two changes with a fortnight between them

`DELETE /user` flags the row and names a date; `purge_deleted_accounts` destroys the account once it
passes. `ACCOUNT_DELETION_GRACE_DAYS` defaults to 14, chosen against the privacy page's "permanently
deleted within 30 days": a 30-day window swept hourly lands at "30 days and change", false by a
rounding error, while 14 leaves the sweep cadence as margin. The account goes dark immediately and
there is no countdown banner. A grace period where the app keeps working fails the case that matters
most — someone who wants to be gone from it: a stalker, a shared laptop, a stolen phone.
`get_current_user` filters `is_deleted=False`, so every read 401s, and `/auth/refresh` re-resolves
the row, which stops other devices' cookies rotating through the window. What a banner would say
goes into the confirmation email and onto the way back in, where someone changing their mind
actually is.

## The double-submit guard is a database predicate, not a rate limit

Two `DELETE /user` calls from one session both clear `get_current_user` before either commits, and
FastCRUD's soft delete rewrites `deleted_at` unconditionally, so the second would move the deletion
clock and send a second email. The write is
`UPDATE ... WHERE is_deleted = false ... RETURNING deleted_at`; zero rows is "already pending" —
same body, same date, no mail. `RETURNING` makes "did this call do it?" one statement rather than a
check-then-act with the same race inside. The endpoint is rate limited because it sends mail, but a
two-request race beats any counter. The email is sent last and non-fatally, after the body is
composed: `_send` propagates, an instance off `local` must have a relay, and a raise there would
leave the user locked out and never told the date. The body carries `purge_after` so a dead relay
costs the copy, not the date.

## The purge's `DELETE` repeats the selection's `WHERE`, and that is the whole safety property

The batch is selected once and deleted one account at a time, each in its own transaction; a restore
committing in that window would be destroyed by a bare `DELETE FROM "user" WHERE id = :id`, along
with every dive behind it. So the statement carries the selection's predicate again:

```sql
DELETE FROM "user" WHERE id = :id AND is_deleted AND deleted_at IS NOT NULL AND deleted_at < :cutoff
```

Zero rows affected is the normal "they came back" outcome, logged at info. An invariant that holds
within a snapshot is not a guarantee across statements; `cutoff` is computed once per run and shared
by the selection and every delete. `deleted_at IS NOT NULL` is not noise: `deleted_at < :cutoff` is
NULL for a row flagged without its clock, which would be dark forever, so the job logs a warning
when it counts any. An admin-initiated suspension, if it ever arrives, gets its own column; that
warning catches someone giving it this one.

## The cascade cannot reach the files, and nothing warns you

`blob_store.delete_after_commit` parks keys on the session's `info` dict for a post-commit listener,
but `DELETE FROM "user"` retires `dive_file` and `certification_file` inside Postgres through FK
cascades: SQLAlchemy never sees those rows, nothing is parked, and the purge would report success
with every export and c-card scan still stored. `src/scripts/sweep_orphaned_files.py` is a manual
script, not an erasure guarantee. So the keys are collected explicitly (two selects, the second
joining `certification` because `certification_file` has no `user_id`) and registered after the
guarded `DELETE` and before the commit; a restored account rolls back, and the rollback listener
drops the unlinks. `authentication_request` rows are deleted by email, since `purpose="sign_in"`
rows carry a `NULL user_id`; partial, because `verify_email_change` rewrites `user.email`, so older
rows are left to the weekly expiry sweep. `token_blacklist` rows stay until natural expiry, keeping
the deleted account's tokens dead. Logs carry the id and request date, never the address.

## No Redis sweep in the worker, and the absence is the decision

`delete_keys_by_pattern(f"user_{id}_*")` after each purge is deliberately absent. `cache.client` is
set only by the API's lifespan, which the arq process never runs, so the call would be a permanent
no-op — the trap *"The profile backfill is a script, not an arq job"* records, in a second process.
It is also unnecessary: `erase_user` sweeps those keys from inside the API when the account is
flagged, and nothing repopulates them because every read for a deleted account 401s.

## The admin panel's own log is a second copy, and the purge leaves it alone

`crudadmin` keeps `admin_event_log` (a row per action with the acting admin, address and user agent)
and `admin_audit_log` (the affected row's JSON before and after every create, update and delete).
`User` is registered for `view`/`create`/`update` in `admin/views.py`, so an operator editing a
diver writes that diver's email into an audit row; `CRUD_ADMIN_TRACK_EVENTS` gates both and defaults
to `True`, and the install bundle's compose file points `CRUD_ADMIN_DB_URL` at the app's own
Postgres. The purge does not touch them: they carry no FK to `user`, and deleting deliberately would
let a data-subject request rewrite the record of what an operator did, the one thing an audit log
exists to prove. The boundary is the app's own data; the second copy is the operator's retention
duty, stated in the configuration reference under both the deletion knob and the panel with the
`DELETE` to run, since `crudadmin`'s `cleanup_old_logs` helper is wired to nothing.

## Hourly at :30, and no `run_at_startup`

Hourly rather than daily makes `ACCOUNT_DELETION_GRACE_DAYS=0` behave as an operator setting it
would expect. At :30 so it does not contend with the two sweeps on the hour. No `run_at_startup`,
unlike both of those: they delete rows already past expiry and a restart loop costs a no-op
`DELETE`, while this one destroys logbooks, and a restart loop must never decide an account's fate
minutes early. The batch is capped at 100 and the cap is logged when hit, because a silent
truncation reads as "purged everything"; one account can carry hundreds of dives and sites, so an
unbounded batch holds locks for minutes.

## The email points at signing in, and carries no restore link of its own

The confirmation mail tells the user to sign in again before the date; the self-service restore is
reached that way. It carries no restore link: a restore token is minted only against a freshly
verified identity and lives minutes, and one sitting in an inbox for a fortnight is a standing key
to an account whose owner has asked for it to be destroyed — the deletion mail is the one message
guaranteed to sit in the inbox of someone who just decided to leave.

## The purge test that would silently pass having done nothing

`TestPurgeDeletedAccountsAgainstPostgres` asserts the files are gone from the volume, not just the
rows: a row count passes while every c-card scan stays on disk. It needs a real database for a
second reason — `delete_after_commit` fires on `Session.after_commit`, and the `AsyncMock` sessions
much of the suite uses never commit. Per CONTRIBUTING.md the class skips silently without
`POSTGRES_SERVER=localhost`, so a green host run proves nothing until checked for skips. The restore
race cannot be arranged with real concurrency, so its test tells the mocked session to answer the
guarded `DELETE` with `rowcount = 0` and asserts the job rolls back, registers no unlinks, and logs
rather than raising — the shape `TestAVanishedGearItemDoesNotFiveHundred` uses.

## A restore is a click, not a side effect of signing in

`DELETE /user` flags the row and names a purge date; signing in must never clear it, or a magic link
opened a week later or a passkey offered unprompted would undo a deliberate deletion. Every entry
point (magic link, six-digit code, Google, passkey) ends in a third outcome:
`AuthOutcome.status = "deletion_pending"`, carrying a restore token and the purge date and no
session. Only `POST /auth/restore` acts on it.

`GET /auth/email/verify/check` looks the user up and answers `valid=true` plus a `deletion_pending`
flag, not `valid=false`, so the landing page can offer *Restore my account* while "ask for a new
one" stays on the `valid=false` branch. Only the link path has that precheck; the other three show
the screen after the POST. `verify_email_code` claims the request before resolving the identity, so
a code spent reaching the restore screen is spent; the link's token stays unused until redeemed.

## Account restore: One funnel, and three resolve sites that all admit a pending-deletion account

Every sign-in path ends in `_start_onboarding_or_sign_in`, so the `deletion_pending` branch lives
there: its `else` reads `outcome.email`, `.provider`, `.name`, and an unhandled third variant dies
on the first attribute while type-checking clean.

Three upstream sites turn a verified identity into a row, and all three admit a pending-deletion
account. `resolve_identity`'s email lookup. Its provider-link lookup: someone who signed up with
Google and later changed their address is reachable only by provider link (`verify_email_change`
rewrites `user.email` in place), and filtered out there they fall through to `OnboardingRequired`
and get a second account. `passkey_service.finish_sign_in`, which never traverses
`resolve_identity`: its branch sits after `verify_authentication_response`, since every earlier
failure shares one 401 reachable without a credential and an earlier branch would be a
credential-existence oracle, and before `record_assertion`, so reaching the restore screen writes
nothing.

Neither lookup links a provider onto a pending-deletion account; the sign-in after the restore does,
against a live row.

## `TokenType.RESTORE`, because the alternative is harmless only by accident

The restore token is its own `TokenType` member with its own helper pair and the same
`jti`-plus-blacklist single-use mechanics as the onboarding token. Reusing the onboarding token is
rejected: `verify_onboarding_token` hard-checks `token_type`, and making the two interchangeable
drops that check, after which each is redeemable at the other's endpoint. That is harmless only
while an onboarding token names no user id and a restore token names no email — a property of
today's payloads, not of the design.

Its subject is the account's `uuid`, not the verified email: the passkey path proves an identity
with no email in it, and `verify_email_change` can rewrite the row's address while the token is in
flight.

## `/auth/restore`, not `/user/restore`

Every `/user/*` route sits behind `get_current_user`, which filters `is_deleted=False` and so 401s
on exactly the accounts a restore serves. The restore token is the credential and names the account
itself, so the route is anonymous by design and listed in `test_route_authentication.py`'s
`ANONYMOUS_BY_DESIGN`.

It clears `is_deleted` and `deleted_at` in one statement. `is_deleted = false` with `deleted_at` set
is a live account carrying a deletion clock nothing reconciles; the mirror state is a row the purge
never reaches. The `UserRestoreDeleted` schema, which carried `is_deleted` alone, is deleted rather
than fixed.

## Account restore: The row lock is what settles the race with the purge

`purge_deleted_accounts` selects a batch and deletes one account at a time, so a restore can commit
between. The purge's `DELETE` repeats the selection's predicate, and the restore takes the row
`FOR UPDATE`. A restore behind a mid-account purge waits, finds no row and answers a 401 saying the
account is gone, not that the link is invalid — the caller holds a token this server signed, and
"invalid link" sends them after a fresh one that cannot exist. A purge behind a restore matches
nothing and logs that the account came back. `crud_token_blacklist.create` commits the session, so
spending the token and restoring the row are one transaction.

`verify_restore_token` reads the blacklist before the lock, so a double-click passes both requests;
the loser inserts a duplicate `token_blacklist.token`, and that `IntegrityError` maps to the same
"already used" 401 — `_RESTORE_REJECTED` is one constant so both checks say one sentence.

## Avatars are the third kind on the files volume, and Pillow re-encodes every one

A diver's picture is uploaded to `PUT /user/avatar`, stored on the files volume under the
`user-avatars/` kind, and served to its owner by `GET /user/avatar`. The `user` row carries two
nullable columns, `avatar_storage_key` and `avatar_sha256`; there is no `profile_image_url`. This is
the "future kind" the key layout in *"File payloads live on the files volume, not in Postgres"* is
built for, and nothing at the `blob_store` layer changes for it.

Gravatar is rejected: off by default, a default install had no pictures; on, it disclosed a SHA-256
of every signed-in user's email plus their IP to Automattic on every page, and changing a picture
meant an account on someone else's website. There is no `GRAVATAR_ENABLED` passthrough in the
install bundle's `docker-compose.yml`, `example.env` or `docs/configuration.md`, and *Third-party
calls* from the browser is map tiles and nothing else.

## Two columns, not a `user_avatar` table

A table mirroring `certification_file` is rejected. That table earns its rows by holding two per
parent (`side`), display metadata (`byte_size`, `original_filename`, `content_type`) and bulk
fetches for list views. An avatar is a strictly 1:1 optional attribute, its served content type is
always WebP, and size and original filename mean nothing once the bytes are re-encoded. The deciding
argument is the read side: `UserRead` needs the avatar's existence and version on every read, and
`get_current_user` already selects every mapped `user` column, so a column rides free where a table
costs a join on the hottest dependency in the app.

`avatar_storage_key` carries the same unique index as the file tables, for the same reason: two rows
naming one key would let either's replacement unlink the other's bytes. Postgres allows any number
of NULLs in a unique index, so accounts without a picture are unaffected.

## Avatars: The bytes that arrive are never the bytes that are stored

Every upload is decoded, EXIF-oriented, centre-cropped square, bounded to 512 px and re-encoded as
WebP at quality 85. Privacy first: re-encoding strips EXIF, and a phone photo's EXIF carries GPS.
The client cannot be trusted with it — the web app is one caller of an API that also serves iOS and
whatever else somebody writes — so the guarantee is server-side. This is the opposite stance to card
files, which are archival documents stored byte for byte; an avatar is a derived display artifact
and the original is not kept.

Bounding is the second reason: without decoding, a 10 MB 8000×8000 upload is served forever on every
header mount, while one ~512 px WebP is 10–40 KB and covers every mount the clients have (36–80 px,
doubled on retina).

`pillow-heif` is rejected as cards reject HEIC: a native-library dependency for a case iOS pickers
already transcode around.

## Pillow parses untrusted bytes, and the two oversized bands are not one check

Three fences: an explicit `formats=["JPEG", "PNG", "WEBP", "GIF"]` allowlist so only four parsers
are reachable from an anonymous byte string; a 50 MP cap read from `Image.size` before any pixel is
decoded; and the 10 MB `read_upload_within_limit` on the read. Decode and encode run in
`anyio.to_thread.run_sync`.

The second band is the trap. Above `Image.MAX_IMAGE_PIXELS * 2` (178,956,970 px) Pillow raises
`DecompressionBombError` from inside `Image.open` itself, before the app's cap runs, so the open
sits inside the same `try` and maps to the same 415; uncaught it is a 500 that a pixel-cap test with
an under-threshold fixture never sees. `tests/test_user_avatars.py` covers the bomb band twice, once
against Pillow's real limit and once with `MAX_IMAGE_PIXELS` lowered under an ordinary image, so the
`except` clause executes. Both fixtures are 74-byte PNGs whose IHDR declares the dimensions; the
checks are header-only.

## Avatars: The pixel cap bounds what is *accepted*, not what is allocated

A 50 MP raster is 150 MB from a ~150 KB PNG; a pixel cap is no memory bound.
`draft(None, (512, 512))`, `exif_transpose(..., in_place=True)` and a mode-guarded `convert` spare
RGB extra copies; `Image.resize` and `Image.reduce` premultiply `LA`/`RGBA` into a full-size copy
before the `box=` crop.

`MAX_AVATAR_PIXELS` (50 MP) judges the header, before `draft` makes a dishonest declaration cheap.
`MAX_AVATAR_DECODE_PIXELS` (1536×1536) judges the post-`draft` size before decoding. Only JPEG
honours `draft`, so phone photos pass and large PNG/WebP/GIF are refused; `draft` halves only while
both edges stay ≥ 512, so a JPEG panorama wider than ~2.25:1 is refused too, so the error gives no
format advice.

`_DECODE_LIMITER`, an `anyio.CapacityLimiter(1)` passed to `run_sync`, makes the worst decode (~90
MB, RGBA WebP) a per-worker ceiling (~360 MB across four workers) instead of
`core/setup.set_threadpool_tokens`' 100. Measure new image paths per format in a fresh process
(`ru_maxrss`).

## The retired key is read from the database, never from `current_user`

`store_user_avatar` takes no `current_user` argument and reads the key it replaces with a fresh
narrow `select`. That dict is resolved once per request by `get_current_user`, so a second upload
overlapping the first would name a key already retired, and unlinking it destroys the other
request's committed blob. The narrow read leaves only two uploads overlapping the function, which
costs an orphan and never a live file because `blob_store.new_key` mints a fresh nonce per write.

`delete_user_avatar` repeats the key it read in its `UPDATE`'s `WHERE`, so a delete racing a
replacement matches no row and answers "nothing to remove" rather than clearing columns it did not
write.

The download route reads key and digest in one narrow select: serving from the `current_user`
snapshot lets a concurrent replace unlink the old blob mid-request, turning a race into
`BlobMissingError`, deliberately a loud 500. The 304 path stops at that query.

## The Google picture is imported once, at account creation, and never again

`GoogleUserInfo.avatar` rides the onboarding JWT to `POST /auth/complete`, where the bytes are
fetched and normalized so the row is born with its avatar columns. `put()` precedes the route's
single commit, so a rollback strands only a nonce-keyed orphan for the sweeper;
`release_read_transaction` runs first, since the duplicate checks autobegan a transaction that must
not idle across the fetch.

Server-side fetch rules: the URL comes from a verified token, must be `https` on a host that is or
ends in `.googleusercontent.com`, no redirects, 5 s timeout, read capped at the upload limit. Every
failure is non-fatal: the account is created without a picture. Inline, not an arq job: one bounded
fetch, once per account.

Later sign-ins never re-import; silently replacing an uploaded picture would be Gravatar again.
`AuthOutcome` has no `avatar` field; `OnboardingTokenData` keeps it, the URL being an input to
account creation no client renders.

## Avatars: Erase, purge, sweep, export

`DELETE /user` leaves the avatar alone: it flags the row for the grace period, and
`POST /auth/restore` brings back a whole account rather than a faceless one, as it does dives and
cards. The purge's `_collect_stored_file_keys` reads the key off the `user` row before the `DELETE`,
with no join, or a purged account leaves its portrait on the volume inside an erasure feature. The
sweeper's referenced set has a third source, the only one on a nullable column, so that select
filters the NULLs out. The export archive carries a root `avatar.webp`, `ZIP_STORED` like the other
already-compressed blobs, with the usual log-and-skip on `BlobMissingError`.

## The admin bootstrap's `Table` copy is drift-checked in both directions

`src/scripts/create_first_superuser.py` hand-builds a Core `Table` mirroring `user`, and a column
with a client-side `default=` goes into the INSERT whether or not the script's `data` dict names it.
A column dropped from the model makes that INSERT an `UndefinedColumn`, which the bare
`except Exception` in `create_first_user` logs and swallows, so a fresh install comes up with no
superuser. Nothing else covers the script: `--cov` is scoped to `src/app` and it runs from a
one-shot compose service.

The two `Table` definitions are therefore module-level constants so
`tests/test_create_first_superuser.py` asserts both directions of drift: every column the copy names
exists on the real table, and every `NOT NULL` column without a server default is named by the copy.
The next drop fails in CI rather than on somebody's first install.

## Species photos are the fourth kind on the files volume, and Commons is never hotlinked

A species carries one Wikimedia Commons photograph, fetched once at resolve time, stored on the
files volume under the `species-photos/` kind, and served by `GET /api/v1/species/{uuid}/photo`.
Eight nullable columns on `species` hold where it is, which version it is, and the parts of its
credit. It is the "future kind" the key layout in *"File payloads live on the files volume, not in
Postgres"* anticipates; nothing at the `blob_store` layer changes for it.

## Species photos: Fetched once and served from here, rather than hotlinked

`<img src="https://upload.wikimedia.org/…">` is rejected on the grounds web `DECISIONS.md`'s
*"Avatars are this instance's own, and there is no Gravatar fallback"* records — a third-party host
in the CSP and a privacy-page disclosure — plus a third cost: every viewer's browser would tell
Wikimedia which species they look at.

Serving from the API needs no CSP change in either topology: `DEFAULT_API_BASE_URL` is the relative
`/api/v1` and `img-src` lists `'self'` unconditionally (a split-origin build already adds
`apiOrigin` to `img-src`).

The cost is a third server-side third party, one party on three hosts: `commons.wikimedia.org` for
credit metadata, then `thumb.wikimedia.org` or `upload.wikimedia.org` for the bytes. None is sent
anything a diver typed; Commons is asked for a file title derived from an AphiaID.
`src/.env.example`'s species PRIVACY paragraph says so, and the front door's `docs/` and the web
app's privacy page owe the same statement.

## Species photos: The selection rule is a sequence, and it refuses rather than guesses

P18 is multi-valued, an extra value can be a different animal, and rank rarely settles it.
`services/species_photos.py` reads file titles in this order: drop `deprecated` statements and
non-raster extensions; one candidate, use it; one `preferred`, use it; keep only titles containing
the taxon name or its specific epithet; if any survive, prefer one beginning with the taxon name,
else the first in statement order; none, no photo.

The survivors step is not optional: stopping at "exactly one survives" loses ties where both name
the taxon, and choosing between those cannot pick a different animal. The match uses the P225 of the
item examined, not the stored name; the synonym-retry path makes them differ.

No reliable photo means no photo. Genus fallback is rejected: *Acropora* has hundreds of species a
diver cannot separate, so a genus photo is another animal in a logbook whose claim is "this is what
I saw".

## A synonym's item can hold the photo the accepted item lacks

WoRMS's accepted AphiaID for the zebra shark (313100) reaches a Wikidata item with no image, while
the unaccepted 220032 reaches `Q169468`, which has one. Storing the accepted id is correct and is
what misses the photo, so a retry keyed on the synonym ids exists — one search, not one per synonym:
`haswbstatement` ORs its values inside a single query, which matters when a taxon has 22 or 55
synonym ids. `_worms_synonyms` carries each row's AphiaID alongside its name for this.

The retry fires only when the accepted item offered no candidate at all, never when the rule looked
at candidates and declined them; the reverse would hand *Triaenodon obesus* a synonym's photo right
after the rule refused its own for naming a different shark.

## Species photos: None of it may share `resolve_species`' enrichment budget

`resolve_species` runs its enrichment legs inside one
`anyio.move_on_after(_ENRICHMENT_BUDGET_SECONDS)` task group and 503s when the synonym list is
`None`; a slow Commons call inside that group would cancel the synonym walk and fail
`POST /species/resolve`. So the photo work runs after that group and the row's commit, under its own
timeout; `TestCommonsCannotFailAResolve` pins it: no failure or slowness of Commons changes that
route's status code.

An expired budget is not an attempt. A failed attempt stamps `photo_fetched_at` so the backfill's
second run reports zero, but a cancelled fetch learned nothing, and stamping it is a permanent
no-photo verdict, invisible to `photo_fetched_at IS NULL`, recoverable only by `--force`;
`PhotoAttempt.completed` carries `cancelled_caught` so both callers skip the write.

P18 comes free off the `_WikidataEntity` the group already loaded. The worker is rejected for the
byte fetch: `core/worker/functions.py` is all cron-driven, `enqueue_job` appears nowhere in
`src/app/`, and the picker's pending row absorbs the delay.

## The credit is stored as parts, and `Artist` is HTML

*"The attribution string is a wire format"* gives search results a ready-made `attribution` string;
that is rejected here: a compliant credit needs two hyperlinks, licence and source, and one string
carries at most one. The clients compose.

`Artist` is HTML on most files and the `Attribution` key is rare, so parsing is the common path. A
tag-stripping regex does not leak the `title` tooltip; it leaves `&amp;` and `&#39;` in the name and
glues adjacent elements, so two authors become one run-on string. `_TextExtractor` resolves entities
and treats every tag as a soft boundary. None of it may be interpolated as HTML.

`descriptionurl` is always present, so the source link every licence asks for sits one click away:
CC BY-SA 4.0 §3(a)(2) says so; 3.0 and earlier rest on §4(c)'s "reasonable to the medium or means
You are utilizing". Cite the file's own licence version, not 4.0 by default.

## Species photos: The stored bytes are a scaled copy and nothing else

Never cropped, overlaid or composited, so `services/species_photos.py` does not reuse
`user_avatars._normalize` despite similar decode fencing. A licence property: most sampled files are
ShareAlike, and while displaying and scaling is not adaptation, cropping and compositing move toward
it. Square-card presentation is the browser's business.

One stored width, from Commons' own buckets. Thumbnail widths are bucketed (120/250/330/500/960), an
off-bucket URL answers HTTP 400, and `imageinfo`'s `thumbwidth` reports the width asked for rather
than the one served, so layout arithmetic on it errs by up to ten pixels. The API is asked to name
the URL via `iiurlwidth=500` rather than building one. Storing 330 for lists and 960 for the page
waits for evidence that list bandwidth is a problem.

The byte fetch sends the identifying `User-Agent` the API calls send: an empty one is 403 on
`upload.wikimedia.org` as well as `api.php`. The User-Agent policy lives at
`foundation.wikimedia.org`, not `meta.wikimedia.org`.

## The byte fence admits two hosts, because one `imageinfo` reply names two

`iiurlwidth=500` answers with `thumburl` on `thumb.wikimedia.org` and the full-size `url` on
`upload.wikimedia.org`; `_commons_imageinfo` prefers the thumbnail, so
`species_photos.PHOTO_BYTE_HOSTS` names both — with only the second, `_fetch_photo_bytes` refuses
every thumbnail ("Refusing to fetch species photo bytes from an unexpected host").

Exact hostnames, not a `*.wikimedia.org` suffix match: a subdomain-matching bug in a pattern is an
SSRF hole, while a name that stops resolving visibly stops working. A third name is a deliberate
one-line change.

Preferring `url` is rejected too: sampled originals exceed the 4 MB `MAX_PHOTO_DOWNLOAD_BYTES` cap,
and the rest would be stored at full resolution, the no-resize rule being a licence constraint.

`tests/test_species_photos.py`'s `_THUMB_URL` names the thumbnail host, its fake Commons routes by
hostname, not substring (a substring test would accept
`https://evil.example/upload.wikimedia.org/x.jpg`), and its byte hosts are spelled out, not imported
from `PHOTO_BYTE_HOSTS`, so it can disagree. A fixture naming something upstream does not send
cannot fail, whatever it asserts.

## Species photos: The endpoint that serves them is the first unauthenticated bytes route

Access tokens are Bearer-only (`refresh_token`, the sole cookie, is read only on refresh, logout and
account deletion), so an `<img src>` cannot authenticate; `useAuthedBlobUrl`, the certification
cards' route, re-fetches on every mount, and a life list is a gallery.

It discloses nothing: the catalog is global and ownerless, and the bytes are freely licensed Commons
files. A missing photo and an unknown uuid are the same 404, so it is no catalog-membership oracle.

Two departures from the authenticated binary reads, the bytes being public and immutable:
`Cache-Control` is `public`, not `private`, and the content is `inline`, not an `attachment`, which
would break the `<img>` the route exists for. The rest is copied: a digest-only query before any
bytes, an ETag of the sha256, `If-None-Match` → 304, `nosniff`, the per-response CSP.

It is enrolled by hand in `ANONYMOUS_BY_DESIGN` (`tests/test_route_authentication.py`) and
`UNOWNED_ROUTES` (`tests/test_ownership.py`).

## The sweeper has to learn every new blob kind, and forgetting is destructive

`blob_store.iter_keys()` walks the whole volume while `_referenced_keys()` builds its set from
columns, so a kind missing there is on disk and referenced by nothing, and every file of it past the
24-hour grace window is an orphan for `--delete` to unlink. The suspicious-fraction refusal is the
only brake, `--force` overrides it, and it does not engage below 20 files on the volume, so the
smallest instances have none. Species photos are the worst case, being the only kind on a global
table. `_referenced_keys`' docstring does not count its sources, because any count goes stale at the
next kind.

## Species photos: Backfilling selects on the timestamp, not on the absence of bytes

`src/scripts/backfill_species_photos.py` departs from `backfill_dive_profiles.py`. No Redis pool:
that one needs it because `delete_keys_by_pattern` no-ops silently without; this one invalidates
nothing. Predicate `photo_fetched_at IS NULL`, not "no bytes": most of the catalog permanently has
no photo, so a bytes predicate re-queries Wikidata and Commons forever; a failed attempt is stamped
so the second run reports zero. Candidates are plain `_Candidate` columns, not `Species` entities:
`save_photo_attempt`'s `release_read_transaction` is a `Session.rollback()` that expires every
instance regardless of `expire_on_commit`, so a held entity's next attribute read lazy-refreshes
outside a greenlet. It sleeps between species rather than dropping them; the provider throttle
degrades instance-wide.

`--force` re-attempts regardless of the stamp and is the only remedy for rows a broken fetch
poisoned: a stamped `photo_fetched_at` over a null `photo_storage_key` is byte-for-byte a species
the rule declined, so no `--retry-failed` could tell them apart. `--force --limit` redraws the same
first `limit` ids, pinned by `test_force_with_a_limit_redraws_the_same_slice_rather_than_advancing`.

## GBIF is rejected as a photo fallback, on the evidence

GBIF is rejected as a fallback for species lacking a P18. Run live, its licences come back `null` or
CC BY-NC-ND (NonCommercial and NoDerivatives, so even a thumbnail is unsafe), `/species/match`
silently fuzzy-matches *Stegostoma tigrinum* to *Stegostoma tigrinus* at confidence 96, and its
images are taxonomic monograph plates with distribution maps stitched underneath rather than
photographs. There is no AphiaID→GBIF-key route; it goes through a name and inherits every synonym
problem plus GBIF's own. iNaturalist is rejected for NC licensing. P373 (Commons category) is more
common than P18 across the register and is the recorded escalation if the no-photo rate becomes the
complaint — not built, because a category's first member is not a curated lead image.

## The life list is a hand-written aggregate, and it lives under `/user/`

`GET /api/v1/user/species` lists the caller's logged species with dive count and first/last
sighting, paginated and searchable. It lives under `/user/` rather than `/species/logged?user_uuid=`
because `/species/` is the one router that belongs to nobody. It is a hand-written `select()`
returning `{"data": …, "total_count": …}` for `paginated_response`, and the first paginated `/user/`
route.

Quiet properties: it reaches through `Dive.is_deleted` as `recalculate_dive_stats` does, since
`dive_species` has no liveness and its `dive_id` cascade is dormant; its `total_count` equals
`species_seen`; search is an `EXISTS` over `species_name`, not an alias-multiplying join;
`first_seen`/`last_seen` use `array_agg(utc_offset_minutes ORDER BY start_time)[1]` and
`combine_start_time`, keeping offsets in `core/utils/datetime_offset.py`.

`@cache` keys on `key_prefix` placeholders alone, so `SPECIES_LIFE_LIST_CACHE_KEY_PREFIX` carries
`page` and `search` (`TestTheCacheKeyCarriesTheWholeQuery`) and sits under `user_{id}_dives:` for
`invalidate_dive_caches`. `GET /dives?species_uuid=` is an `IN (subquery)` like `at_dive_site`,
takes no `user_id`, spells `(species_map or {}).get(uuid, -1)` since an unknown uuid resolves to
`None`, and adds `species_{id}` to the dives cache key.

## Signing is a maintainer's setting, and the hook checks before it blocks

Contributors need not sign (`CONTRIBUTING.md`, *Pull requests*);
`git config core.hooksPath .githooks` is a *For maintainers* step. `main`'s provenance is the squash
commit GitHub signs with its web-flow key; rebase-merge would copy branch commits unsigned, so leave
the merge-method toggles alone.

The hook blocks a signing-disabling command only when `git config --type=bool --get <key>` prints
`true`, run in the hook's cwd, not `$CLAUDE_PROJECT_DIR`. Commit patterns (`--no-gpg-sign`) ask
`commit.gpgsign`; tag patterns (`--no-sign`) ask `tag.gpgsign`. Removing the config would disarm the
gate, so each pattern also matches `--unset`, `--unset-all`, the `unset` subcommand, the
section-named `--remove-section` and `--rename-section`, the empty value `commit.gpgsign=` (git
reads it as `false`), quoted values, and `commit.gpgsign ""`. Word end is `(?![^\s;&|()<>])`, not
`(?!\S)`. Every failure allows: unset exits 1, missing git is `OSError`, `bad boolean config value`
exits 128, an escaping exception exits 1, never 2. `sed` on `~/.gitconfig` and
`GIT_CONFIG_GLOBAL=/dev/null` are unmatched by design.

## Issue templates are YAML forms, and the sharp edges are in what a form cannot do

`.github/ISSUE_TEMPLATE/` holds forms, not markdown templates: a required `input` and a `dropdown`
reliably get version and install method. Dropdowns cover the install path (release bundle,
`docker compose up`, host tooling against `src/`) and the container (`api`, `worker`, `admin_init`).

Security has no form: a `contact_links` entry in `config.yml` points at private vulnerability
reporting, since a form produces a public issue. The Discussions link points at
`opendiving/opendiving`, the project's one space; a second would split threads by app half. Links
end in `/issues/new/choose`; bare `/issues/new` bypasses the forms.

Forms apply labels but cannot create them; only `bug` and `enhancement` exist. Blank issues stay
enabled. No file-upload field exists, so the dive-computer form's privacy warning is a `markdown`
block asking for a pool-session export, not a real dive. The logs field says to read before pasting:
with `SMTP_HOST` unset the sign-in magic link and code land in the API log.

## The operator docs carry the consent duty, because the privacy page is part of what ships

Operators publish the web image's `/privacy` page under their own name. It asserts no analytics,
advertising storage or cookie banner, true only until they add a tracker, when the ePrivacy consent
duty becomes theirs;
[the configuration reference](https://github.com/opendiving/opendiving/blob/main/docs/configuration.md)
says so under *Third-party calls*.

The CSP is not a guarantee: `script-src` carries `'strict-dynamic'`, so a bundled analytics package
loads unhindered, and `connect-src` allows `'self'`; it refuses only `fetch`, WebSocket or
`sendBeacon` to a third-party collector; the docs claim no more.

`REFRESH_TOKEN_EXPIRE_DAYS` joins `ACCOUNT_DELETION_GRACE_DAYS` on the "before you change the
number" list: the sign-in note and `/privacy` say "about a week", and it is an inactivity window,
not a session length: the cookie is single-use and each refresh restarts the clock
(`services/auth_service.py`, `core/security.py:create_refresh_token`).

`/privacy` §10.4 points at the docs' *The admin panel*, which names cookies as a category only: they
are `crudadmin`'s (pinned `>=0.4.2`), so a listed name would rot.

## Google sign-in is an authorization code this server redeems, not an ID token the browser hands over

`POST /auth/google` takes `{code, code_verifier, redirect_uri}`, redeems the code at
`https://oauth2.googleapis.com/token` with `GOOGLE_CLIENT_SECRET`, and verifies the returned
`id_token`. Everything from `resolve_identity` down is untouched: `provider_user_id` is Google's
`sub` claim.

Nothing of Google's runs in a signed-out visitor's browser. Loading `accounts.google.com/gsi/client`
at mount discloses every front-page visitor's IP and user agent and lets Google set cookies unasked
(WP29 Opinion 04/2012 §3.7 wants consent; clicking "Continue with Google" is the consent). So the
browser half is a hand-built authorization URL and a top-level navigation;
`google.accounts.oauth2.initCodeClient` ships in the same bundle, and click-deferred facades still
run Google's code.

PKCE `S256` comes free — `CodeClientConfig` has no `code_challenge` field — documented only in
`https://accounts.google.com/.well-known/openid-configuration`'s `code_challenge_methods_supported`.
No `nonce`: OIDC Core §3.1.2.1 makes it OPTIONAL for the code flow, Google enforces it only for
`response_type=id_token`, and the ID token never passes through the browser, so it would guard no
open threat.

## Google sign-in: The boot guard refuses rather than degrading

`Settings._require_google_client_secret` fails startup on a `GOOGLE_CLIENT_ID` without
`GOOGLE_CLIENT_SECRET`. Degrading is rejected: the web app renders the button from its own
`GOOGLE_CLIENT_ID` and cannot learn the API is short a secret; the button would 401. Failing at
startup puts the error where the mistake was made, like `_require_from_address_with_smtp` and
`_reject_placeholder_secret_key`.

The guarantee is only that a running API has both halves; a `FRONTEND_URL` disagreeing with the
visitor's origin (400 from `auth_with_google`, naming it), an unregistered redirect URI (401) and an
unreachable Google (503) each get their own message.

It fires at import: `core/config.py` reads `src/.env` and ends with `settings = Settings()`, so a
`src/.env` with a client id and no secret kills `pytest` at collection and stops `api`, `worker` and
`admin_init`. A placeholder secret suffices; the guard checks presence, not validity. CI has no
`src/.env`, so `TestGoogleSignInNeedsBothHalvesOfItsClient` names both variables.

## The `redirect_uri` in the request body is a diagnostic, not a control

The handler compares `body.redirect_uri` byte-for-byte against `FRONTEND_URL`'s
`/auth/google/callback` and refuses anything else with a 400 naming the setting. It is not a
security control, so do not remove it as a redundant one: Google binds a code to the URI it saw, and
an unregistered URI cannot be used at all, which is why accepting the field from the browser is not
a hole.

What it buys is that a `FRONTEND_URL` disagreeing with the origin the visitor reached fails here, in
this app's error naming this app's setting, instead of at Google as a `redirect_uri_mismatch` that
names neither. Deriving the URI server-side and ignoring the browser's is rejected because the two
derivations then diverge in silence. `google_redirect_uri` is rebuilt from the parse rather than
concatenated, like `passkey_origin`, so a trailing slash on `FRONTEND_URL` does not yield
`//auth/google/callback`.

## "Google said no" and "Google was not there" are different responses

A token endpoint answering 4xx — an expired code, a replayed one, a `redirect_uri_mismatch` — is a
bad credential and stays `UnauthorizedException("Invalid Google credential.")`. A network failure or
5xx from Google is this server failing, and reporting it as a bad credential sends the visitor to
retry something that was never their problem, so `exchange_google_code` raises a 503 for those — a
bare `HTTPException`, following `api/v1/contact.py` and `services/species_service.py`, because
`core/exceptions/http_exceptions.py` has no class for that status.

429 is sorted with the 5xx. The question is not which side of 500 but whether this is the visitor's
credential or the server's problem: a throttled or quota-exhausted client is the second, and "try
signing in again" cannot help there.

## Nothing of the token response is logged, and the secret is a `SecretStr`

`GOOGLE_CLIENT_SECRET` is typed `SecretStr` where `SMTP_PASSWORD` and `GEOCODER_API_KEY` beside it
are bare `str | None`: a value that must not reach a log line is better served by a type whose
`repr` cannot spill it into a traceback. `SecretStr("")` is truthy (no `__bool__`), so the boot
guard checks `.get_secret_value().strip()` rather than falsiness, or an empty variable passes
startup and fails at the first sign-in.

The secret travels only in the request body, and httpx's request log line is URL and status.
Google's response is not logged above DEBUG: a refusal writes its HTTP status at WARNING and the
short OAuth `error` enum at DEBUG, and `error_description` is never read. The enum is what
distinguishes `invalid_client` from `redirect_uri_mismatch` for an operator debugging a fresh
install; `LOG_LEVEL=DEBUG` is the documented way to see it.

## Google sign-in: The outbound call has a seam because the tests need one

`exchange_google_code` is imported into `api/v1/auth.py`'s namespace so endpoint tests patch
`src.app.api.v1.auth.exchange_google_code`, as they patch `verify_google_id_token`. Those tests
patch the helper rather than the library, so an exchange call without the seam reaches the network
during `pytest`. `respx` is not a dependency; the convention for outbound calls is
`patch`/`AsyncMock`, or, where the request itself is worth asserting, a real `httpx.AsyncClient`
over an `httpx.MockTransport`, which is what `TestExchangeGoogleCode` uses to read the form Google
would receive. `httpx` is already a dependency; `google-auth-oauthlib` is rejected as a new
supply-chain surface for a single HTTP call.

## Google sign-in: The client secret must never reach the `web` service

The install bundle's `docker-compose.yml` needs no change for `GOOGLE_CLIENT_SECRET`: `api`,
`worker` and `admin_init` take `env_file: .env`, so a new variable in `.env` reaches all three. The
`web:` service deliberately does not — a compromised Node process must not read the database
credentials out of its environment — and instead names each variable it uses, `GOOGLE_CLIENT_ID`
among them. `GOOGLE_CLIENT_SECRET` must never be added to that block: the browser half never sees
the secret, the Node process has no use for it, and adding it breaks the rule the whole flow rests
on.

## `DiveMixture.usage` is how a cylinder was breathed, and it is what makes a sidemount pair summable

`dive_mixture.usage` is nullable, typed by `TankUsage` (`schemas/dive_mixture.py`): `parallel`,
`staged`, or null. A second column, not more `role` values: `role` is what a cylinder was carried
for, `usage` how it was breathed, and a `bottom` sidemount pair and a staged `deco` bottle share
dives. Only `parallel` changes arithmetic; `staged` tells a deliberately mixed set from a
half-filled one. `manifolded` is rejected: a manifolded twinset is one row at combined capacity and
computes correctly.

No DB `CHECK`, as with `GasRole`, `GearType` and `WaterType`: Pydantic validates the `VARCHAR(20)`
on every write. Plain nullable `ADD COLUMN`.

The parse-layer `DiveMixtureSchema` and `merge_mixture_fields`' fill-only tuple omit it: no parsed
format records the distinction (libdivecomputer's `DC_USAGE_SIDEMOUNT` is Shearwater-binary only,
unparsed here). `DiveMixtureBase` carries it into `DiveMixtureCreate`, `DiveMixtureRead` and
`logbook.divejson`; `DiveMixtureUpdate` stays off `RejectsExplicitNulls` because CRUDAdmin drops
blank fields before building the schema, so only the dive form can un-flag a cylinder.

## `compute_parallel_gas_use`: additive litres for a flagged parallel set

A sidemount pair or independent doubles is the one multi-cylinder shape with additive consumption:
breathed alternately at one depth over one dive, the multi-cylinder refusal's misattribution cannot
arise, and two flagged 11.1 L rows agree with one 22.2 L row to the litre.

Guards: at least two cylinders, all flagged `parallel` (a pair plus an unflagged or `staged` bottle
returns `None`, the short-numerator trap Subsurface's `calculate_airuse()` also refuses); the dive's
own average depth and duration; both pressures on every cylinder; a total drop above zero, summed
rather than per row, since a carried-but-untouched cylinder (`_pressure_used`) adds zero litres to a
still-correct denominator (`ck_dive_mixture_pressure_order` bars negatives).

`tanks` is empty, both seconds fields null: rates need time-on-gas, and litres alone would
half-populate `DiveTankGasUse` against `DiveGasUse`'s whole-object-or-nothing rule.
`MAX_PLAUSIBLE_RMV` is not applied, as in `compute_gas_use`: the ceiling targets segmentation
artefacts, this path has none, and it would break the manifolded equivalence.

## `resolve_gas_use` dispatches on cylinder count first, then on the `parallel` flag

Count picks the first candidate — one cylinder is `compute_gas_use`'s, several are
`compute_multi_tank_gas_use`'s — and where the multi-tank path declines and every mixture is flagged
`parallel`, `compute_parallel_gas_use` gets a turn. Attribution wins over the flag, so the fallback
is second rather than gated ahead: per-cylinder attribution from a profile is a strictly richer
answer, and a diver whose computer recorded the switches must not lose it by flagging the pair
truthfully. The fallback rescues the dives that returned `None`: no profile, no switches, or a
cylinder with real pressures and no attribution entry.

`compute_gas_use` and `compute_multi_tank_gas_use` are untouched, an unflagged multi-cylinder dive
still says nothing, and the fallback is reached only through a flag no import can set, so a figure
changes only after its owner edits. All three callers (dive read, CSV export, `gas_use_history`) get
it. Gas use stays computed on read from row-only inputs, so cache-safe.

## The pooled `sac_bar_per_min` exists only for a flagged parallel set of equal volumes

*"A multi-cylinder figure covers the cylinders it can account for"* leaves `sac_bar_per_min` null
for several cylinders, a combined definition being practically useless. One case reverses it: a
flagged parallel set of equal volumes, the figure being the mean drop per surface-minute.

A combined bar/min is plannable against neither cylinder (10 bar of an 11 L stage is not 10 bar of a
22 L twinset); with equal volumes it reads against the gauge in hand, as Shearwater's sidemount mode
does for same-sized tanks.

Manifolded equivalence decides it: one row of volume `nV` and drop `d` reports
`sac = d/surface_minutes`; `n` flagged rows report `(Σd_i/n)/surface_minutes` and litres
`nV·(Σd_i/n)`, so both encodings agree throughout. Unequal volumes return `None` for SAC while
`gas_used` and `rmv` compute. Equality is exact: volumes come from presets or one diver's hand, and
a tolerance invents a threshold no agency defines.

## The certification list spells out `NULLS LAST`, because `get_multi` cannot

`ix_certification_user_id_certified_on` is `(user_id, certified_on DESC NULLS LAST)`, but
`get_multi(sort_columns=["certified_on", "uuid"], sort_orders=["desc", "desc"])` emits a bare
`desc(column)`, Postgres `NULLS FIRST`: the index cannot serve it and undated cards sort above the
newest. Fixed on the query side, since dateless-last is the list's purpose: `_LIST_ORDER` in
`crud/crud_certifications.py` is `certified_on.desc().nulls_last(), uuid.desc()`, the shape of
`_INFO_ORDER` in `crud_gear_service_schedules`, and `get_certifications_page` is a hand-written
`select()` returning `get_multi`'s `{"data": [...], "total_count": n}` shape. `search_multi` in
`core/utils/search.py` is the other list that outgrew `get_multi`.

`get_multi` cannot express null placement — `SortProcessor` has no such parameter — so avoid
`sort_columns` on a nullable column. `gear_service.py` sorts `next_due_on` ascending, where Postgres
already defaults to `NULLS LAST`; descending would bring the same bug. `uuid DESC` is deliberately
not a third index column; an incremental sort settles the tie. `TestListOrderingSql` and
`TestListOrderingAgainstPostgres` in `tests/test_certifications.py` pin it; the second needs real
Postgres because SQLite sorts `DESC` nulls last.

## The test registrations that fail on nothing, and the inventory to walk when a model is added

Two kinds of registration live in `tests/`. The first derives its set from the code and fails by
name on a model it does not recognise: `test_migrations.py::TestMigrationsCoverEveryModel`,
`test_user_cascade.py::TestEveryForeignKeyIntoUserCascades`,
`test_foreign_key_indexes.py::TestEveryForeignKeyColumnLeadsAnIndex`,
`test_ownership.py::TestEveryUuidRouteIsAccountedFor` and `TestEveryOwnedRouteUsesIt`,
`test_picker_search.py::TestPageSizeCaps`, and `test_hard_delete.py::TestTheRegistryIsComplete`. The
last's predicates live in `tests/helpers/model_metadata.py`, shared with `test_admin_config.py`; it
walks `app/models/` and the mapper registry rather than `models/__init__.py`, and filters on `uuid`
rather than `user_id`, which `DiveProfile` and `CertificationFile` lack.

The second kind is a hand-written list a missing model escapes silently, so adding a model means
walking it by hand: `TestListCacheKeys` and the search-column lists in `test_picker_search.py`
(`Trip` is deliberately absent from `TestSearchClause`, going through
`crud_trips.search_conditions`), the per-helper scoping classes in `test_owned_read_scoping.py`
(plus `tests/test_courses.py::TestCourseUuidLookupScoping`), and the `populated_diver` fixture with
its two model tuples in `test_user_cascade.py`. Each encodes a judgement no predicate supplies.

Docstrings counting a current set rot; prefer "these" to a number. Find candidates with:

```bash
git grep -nwE 'four|five|six|seven|eight|nine|ten|eleven' -- tests/
```

## Courses are the grouping entity trips could not be

A `Course` is a trip without a location: dives point at it the way they point at a trip, and
certifications point at it through `certification.course_id`, the first reference
`crud_certifications` carries. Most of the shape is the trip family copied; the sections below
record only where it deliberately is not.

UDDF gets nothing. `<divetrip>` is the near miss rather than the answer: it has a name and a date
range, but a training course is not a trip, and writing one there makes an importer read "PADI Open
Water" as a holiday beside the real trips in that element. See *"What UDDF 3.2.2 has no slot for"*.

## One certification, at most one course — not a join table

The reference is a nullable `certification.course_id`, the shape of `dive.trip_id`. No agency
construct issues one card from two courses: a PADI referral is one course completed in segments,
ReActivate reissues the same card, and SSI recognition ratings come from no course at all and carry
no link. The one shape a single link cannot express — GUE Fundamentals Part 1 and Part 2 logged as
two rows feeding one card — is credited by GUE to the completing part. A join table buys that edge
case at the cost of replace-wholesale semantics, resolver helpers and multi-select UI on both sides.
The other direction is unconstrained: one course yields many certifications, which is what TDI's
combined Advanced Nitrox + Decompression Procedures is.

## Courses: No per-user unique name, diverging from `Trip`

`ux_trip_user_id_name_lower` has no counterpart on `course`, and no `course_name_exists` helper
stands in front of one: a course failed and retaken later is legitimately the same name twice, the
reasoning that leaves `certification` without a unique index. *"Case-insensitive per-user uniqueness
(trips, dive sites)"* is the pattern this declines, not one it forgot.

`tests/test_hard_delete.py` runs three behavioural classes over one registry. `Course` takes the
first two; `TestADeletedNameFreesItsSlot` has nothing to assert for a name that was never exclusive,
so `Resource.name_exists` is optional and that class runs over the entries supplying one. The
exemption is derived, not declared:
`test_only_a_resource_without_a_natural_key_may_omit_its_name_check` requires the set omitting the
check to equal the set of registered models with no unique index over anything but `uuid`, in both
directions — a `Course` that gains a unique name fails there until the helper and the entry arrive
with it.

## A course may have no agency, and a certification may not

`course.agency` is nullable and OPTIONAL in DiveJSON (§6.17); `certification.agency` is `NOT NULL`
and REQUIRED (§6.16). The asymmetry is about who issues what: a c-card is issued by an agency, so a
card naming none is not a card, while a course can be taught by an independent instructor and a
diver logging one has nothing to put in the field. A default there fabricates the fact the record
exists to hold.

So the two answer an unreadable agency differently. A certification without one is skipped on import
and omitted from an export; a course keeps the record and loses the pair, along with every dive's
link to it. `validate_agency_pairing` is shared and decides neither — requiredness lives in the
field declarations and in each update schema's `NON_NULLABLE_FIELDS`.

Rejected: a vocabulary value meaning "no agency", which would be meaningless on a card and is one
list with §6.16's.

## Courses: Both dates are nullable, and three layers keep them ordered

Both are nullable — a `planned` course has no dates yet. A stored course never has
`end_date < start_date`:

1. `CourseBase`'s both-present check, for 422s on create.
2. `patch_course`'s merged check, the incoming date against the stored one — the layer
   `CourseUpdate` cannot supply, seeing only what was sent.
3. `ck_course_date_range` on the table, because CRUDAdmin writes through `CourseUpdate` and a lone
   date passes layer 1 without reaching layer 2. NULL semantics make it vacuous when either date is
   absent.

`trip_part` gets no such constraint, which is a choice rather than an omission: a part arrives whole
and `TripPartInput` checks it, so layer 2 has nothing to add and layer 3 would only cover the admin
panel — the parity `trip` had, and the panel is being retired. `agency`/`agency_other` takes the
same three layers minus the constraint, for the reason `models/certification.py` gives. Each rule
lives once — `validate_date_range` and `DATE_RANGE_MESSAGE` in `core/schemas.py`,
`validate_agency_pairing` in `schemas/certification.py` — the routes differing only in reporting, a
per-field 422 from a schema and a flat `{"detail": ...}` from a handler.

## Courses: The list read is hand-rolled for an ordering, not for an enrichment

`GET /courses` sorts `start_date DESC NULLS LAST` with a `uuid` tie-break, and no path through
`OwnedResourceCache` produces it: `get_multi`'s `sort_orders` is `'asc'`/`'desc'` with no null
placement, and `core/utils/search.py::search_multi` builds a bare `.desc()`/`.asc()` from a single
`sort_column`. So `crud_courses.get_courses_page` is one hand-written `select()` serving both
branches, searched and unsearched — unlike the resources that leave the factory for an enrichment,
and unlike `get_certifications_page`, which needs it for one branch. `get_trips_page` is the third,
for an ordering `search_multi` cannot reach at all. `OwnedResourceCache`'s docstring records the
distinction.

A `planned` course has no dates; under Postgres's default `NULLS FIRST` a diver with two planned
courses sees only those. `courses.py` keeps an `OwnedResourceCache` purely for
`list_cache_key_prefix` and `invalidate_list`; its non-empty `search_columns` keeps the
`:search:{search}` segment in the key, since every dimension a list read varies on — page, size,
search term, and this list's four filters — appears in its key.

## Deleting a course invalidates three cache families

`invalidate_course_caches` sweeps `user_{id}_course*` — one pattern, since both key shapes share
that prefix and nothing else starts with it, the arrangement certifications use rather than the two
patterns dives need. `erase_course` also calls `invalidate_dive_caches` and
`invalidate_certification_caches`, unconditionally: both reads carry the course's uuid and the FK's
`ON DELETE SET NULL` has just rewritten every row that pointed here, so skipping either leaves
cached reads naming the course for the rest of the TTL. `erase_trip` does the dive half for the same
reason.

No `move_dives_to` equivalent, unlike `DELETE /trip/{uuid}`: a trip groups a holiday's dives and
moving them is a real operation; a deleted course simply unlinks, which the FK does itself.

## `CertificationUpdateRequest`, and where `course_uuid` may not sit

`CertificationUpdate` is CRUDAdmin's registered form schema for `Certification`, and
`CertificationCreateInternal` inherits `CertificationBase`; a non-column `course_uuid` on either
lands in the admin form as a field the panel renders and cannot resolve — the trap
`TripUpdateRequest`'s docstring records. So the PATCH body is
`CertificationUpdateRequest(CertificationUpdate)`, carrying `course_uuid`; on create it sits on
`CertificationCreate` itself, never the base; the internal schemas carry `course_id`, the column, as
`DiveCreateInternal` exposes `trip_id`. The certification admin views are not re-registered on
column-shaped internal schemas the way the dive views are; that is a wider admin refactor.

`CertificationRead` carries `course_uuid` and all three producers fill it: both cached readers run
the batched `get_course_uuids_by_ids`, and `write_certification` passes the request's value.
`_to_public_certification` stays a synchronous pure function taking the resolved value; `course_id`
joins its exclusion set as `trip_id` does in `_to_public_dive`.

## The hook script is repo content; what wires it up is not

`.claude/hooks/no-unsigned-commits.py` is committed; its `PreToolUse` registration is not. It lives
in the untracked `.claude/settings.local.json`; there is no `.claude/settings.json`. The guard is
repository content; switching it on is machine configuration, as with `.githooks/pre-push`.

`api-N-*` worktrees come from a plain `git worktree add`, which checks out tracked files only, so
there the script has nothing wired to it and the guard does not fire. Claude Code's own
randomly-named worktrees get one written at session start; that is not a counterexample.
`.githooks/pre-push`, inherited through `core.hooksPath`, still refuses the push.

`CONTRIBUTING.md`'s *For maintainers* carries the registration JSON verbatim beside
`git config core.hooksPath .githooks`, since `core.hooksPath` is inherited by linked worktrees and a
file in `.claude/` is not. Do not ignore the script too: untracking a tracked file deletes it on the
next `git pull`, and a missing hook command exits non-2, non-blocking.

## A refresh token has a `user_session` behind it, and `sid` is what survives rotation

`user_session` is the state behind a refresh token, and answers "which devices am I signed in on",
"sign my other devices out" and "which of these is this one". The design turns on one distinction.
`jti` identifies an issuance: it makes revoking a token by value per-issuance and changes on every
rotation (*"Every revocable token carries a `jti`"*). `sid` identifies a device: minted once with
the session row and carried unchanged across every rotation, which gives a session the continuity
spend-then-mint destroys. `token_blacklist` stays a deny-list with no `user_id`.

Tier 3 family revocation from *"A reused refresh token is a `WARNING`"* is implemented on this claim
and no new column: a replay past the threshold stamps `revoked_at` on the row the spent token names
— *"A replayed refresh token takes its session with it"*.

## The row is created in `issue_tokens`, which is why "one session per mint" is not a checklist

Every sign-in path funnels through `issue_tokens` — `_start_onboarding_or_sign_in` for all four
providers, `POST /auth/complete`, `POST /auth/restore` and `POST /auth/refresh` — so "every minting
path creates exactly one session" is a property of the function they all reach rather than of four
call sites remembering. `session_uuid=None` starts a row; a value continues one.

The cap statement is `ORDER BY last_used_at DESC ... OFFSET keep`, and it reads backwards: `OFFSET`
keeps what it skips, so the rows skipped are the ones retained. Sorted ascending it evicts the right
number of rows and precisely the wrong ones — every session in daily use. Only a test asserting
which row survived, not how many, can catch that. Eviction is by `last_used_at`, not `created_at`,
departing from GitLab's oldest-deleted: a diver's oldest browser is usually their busiest.

## The refresh path asks three questions and answers every failure with the same 401

`POST /auth/refresh` asks whether the token verifies, whether the account is live, and whether the
token's `sid` resolves to an unrevoked, unexpired row belonging to that subject — which is what
makes "sign out my other devices" mean anything, since revoking a row kills its refresh at the next
rotation. All failures answer the same uniform 401, a property this file pins; the session lookup is
a single statement so the route has nothing to tell apart. Both lookups happen before the presented
token is spent, so a request answering 401 writes nothing.

Access tokens are session-checked as well, in `get_current_user` — *"Revoking a session ends its
access token too, at the cost of a third indexed read"*.

## Sessions: Everyone is signed out once, and that is the whole compatibility story

A refresh cookie carrying no `sid` 401s at its next refresh, and `get_current_user` wants a `sid` as
well, so the access token of such a pair is refused on its next request rather than surviving to
`ACCESS_TOKEN_EXPIRE_MINUTES`. No shim: for a self-hoster upgrading across the change, a one-time
sign-out is a clean event rather than corruption.

## `DELETE /user` revokes its own session and no others

It stamps `revoked_at` on the session it was called with, alongside blacklisting the pair it was
handed, and touches no other row — deliberately. The other devices are already inert
(`get_current_user` filters `is_deleted=False` and `/auth/refresh` re-resolves the account), and
leaving them unrevoked is what lets `POST /auth/restore` bring the account back with its sessions
intact (*"A refresh token is only as alive as its account"*). A diver who wants them gone has
`DELETE /user/sessions`.

## `UserSession` is exempt from the hard-delete registry, with its own reason

It goes in `NOT_A_DIVERS_OWN_RESOURCE`, not `HARD_DELETED_RESOURCES`, because
`DELETE /user/session/{uuid}` revokes — it stamps `revoked_at` and the cron sweep removes the row
later. All three behaviour classes in that file assert the row is gone, and a second revoke succeeds
where they expect a 404. `AuthenticationRequest`'s reason cannot be reused: it turns on "nothing
offers a delete of it", and this resource offers one; nothing tests a reason's truth, so a copied
one would sit there saying something false. The exemption also keeps `UserSession` out of
`PANEL_HARD_DELETED`, which `tests/test_admin_config.py` derives from `divers_own_hard_deleted()` —
otherwise the parametrized case there would require the panel registration to carry `"create"` and
`"update"`, the last thing a table of live credentials should have.

## `GET /user/sessions` is the seventh opt-out, and the first for a correctness reason

Every other uncached list is a staleness trade; this one cannot be cached correctly. The response
carries `current: bool` per row, resolved from the requesting token's `sid`, so it varies by
credential, while every cache key here is user-scoped by design (pattern invalidation depends on
it). A cached entry would serve one device's "This device" marker to another — a wrong answer, not
an old one.

`current` comes from `api.dependencies.current_session_uuid`, a sibling of `get_current_user` rather
than a change to it: that function returns the account dict every ownership check compares against,
and widening it would touch every route to serve four. The sibling decodes rather than verifies —
`get_current_user` runs `verify_token` over the same string on the same request — so it must never
be a route's only auth dependency.

## The auth audit trail is persist-only, and its erasure has two arms

`auth_audit_event` records successful authentications, account creations and provider links, which
nothing else logs durably — `authentication_request.used_at` is swept a week after the row expires.
Persist-only: rows are written and read through the admin panel, and no API route returns one, which
is what keeps the enumeration-oracle discipline untouched — it constrains responses, and no response
exposes these.

## Auth audit: Membership is a rule, and the exclusions are half of it

A site emits an event when it (a) commits a row to an auth table, (b) mints a session, onboarding or
restore token, (c) revokes a credential at its owner's request, (d) ends, revokes or detects the
replay of a session at the caller's request, or (e) logs a `WARNING` for a failure the code deems
rare and meaningful — its own criterion, and how the passkey counter regression qualifies.

Excluded, following the *Nothing is logged* bullet of *"A refresh token is only as alive as its
account"*: ordinary refresh rotations (`last_used_at` records liveness), session-cap evictions,
passkey renames, superseding your own pending auth request, the provider row inside
`POST /auth/complete`'s transaction, failed passkey ceremonies, the passkey-notice delivery failure,
and the generic 401/400s across the auth surface. `tests/test_auth_audit.py` tests the exclusions as
explicitly as the events; a positive-only suite passes against a version that logs every rotation.

## Auth audit: `user_id` is set only where the request already holds the account

Four events are user-less by design: auth request created, sign-in code failed, onboarding started,
invitation requested. `request_email_link` never queries `crud_users` — the enumeration protection
there is structural, the path cannot distinguish an existing account from a new one (*"Unified auth
flow"*) — and an audit-time lookup to fill a `user_id` would reverse that guarantee in a
`crud_users` call no diff reviewer would connect to the section it contradicts.

Everything downstream of a resolved identity carries the id it already has. Two sites work harder,
both bounded: `POST /auth/logout` authenticates on `oauth2_scheme` alone and resolves its subject
from the presented access token as `_handle_revoked_refresh` does, and the refresh-replay event
resolves the token's subject with one indexed read, rare by construction since it is only reached
past the threshold.

## Auth audit: The replay event is conditioned on elapsed time; the log line still is not

The two-tab rotation race lands on the same branch a stolen cookie does, resolves in milliseconds
and is benign; an unconditioned audit row would put the cry-wolf defect `token_blacklist.revoked_at`
exists to fix straight into the audit table. So the row is written only past
`_REFRESH_REPLAY_THRESHOLD` (five seconds — three orders of magnitude above the race, negligible
against a theft replayed minutes later), and a row's existence means replay-not-race. The `WARNING`
still fires for every presentation, gap attached.

The same threshold gates the session revocation (*"A replayed refresh token takes its session with
it"*), and the one row means "a replay was detected and its session was ended" rather than earning a
second `SESSION_REVOKED`: one event per site, and this is one act. Too low a number now means a
diver signed out for their own two tabs, not merely a spurious row.

## Auth audit: Retention is two tiers, and the short one is not independently chosen

Account-tied events are swept after 90 days; events with `user_id IS NULL` after 7, and that number
is `AUTHENTICATION_REQUEST_RETENTION` rather than a second opinion. Storing an address here is
justified because "an auth request for `<email>`" puts in the operator's database exactly what
`authentication_request.email` already does — an equivalence that holds for duration only if the two
sweeps agree. Both are module constants beside the sweep, not `Settings` fields, following
`AUTHENTICATION_REQUEST_RETENTION`'s deliberate non-configurability; no new setting also means
nothing to add to the install bundle's `example.env` or configuration reference.

## Auth audit: The FK cascade reaches only half the rows

Account-tied rows go down the `ON DELETE CASCADE` with the purge's `DELETE FROM "user"`. User-less
rows carry an email and no `user_id`, so no cascade reaches them; `_purge_one_account` deletes those
by email, the second arm `authentication_request` already needs. It is partial the way its precedent
is: `verify_email_change` rewrites `user.email` in place, so rows created under an address the
account has since left carry an email the purge cannot name. The 7-day tier bounds those, every
address that never became an account, and the one row satisfying neither arm — a replay detected for
an account already purged.

*Rejected:* an operator security log that survives deletion. It needs a user reference without a FK
(the cascade suite fails any FK into `user.id` that is not `CASCADE`) plus registry exemptions, and
the operator already keeps `admin_event_log`/`admin_audit_log`, which survive purges by design.

## Auth audit: Failures propagate, and a write on an error path commits first

No swallowed exceptions and no fire-and-forget: a trail that drops rows when the database is unhappy
loses exactly the events worth having. Where the event has a write to join, it commits with it
(`commit=False`) — the account and its provider row, the email change and its claim, the credential
and its registration — so a thing that fails to happen leaves no row saying it did. Everywhere else
the write commits on its own, including every path about to raise: `async_get_db` does not commit on
unwind, so a row left in flight on a 401 is lost. The refresh replay and the passkey counter
regression, which `finish_sign_in` follows with an `UnauthorizedException`, both fire on such a
path; `register_failed_code_attempt` commits before its own 401 for the same reason.

## Auth audit: What a row may never contain

Tokens, token hashes, codes, code hashes: the fact of the artifact, never the artifact (OWASP's
never-log list). Enforcement is structural — `record_auth_event` is the only writer, the schema is
`extra="forbid"`, and the table has no free-text column. `tests/test_auth_audit.py` pins the column
set, so a column that could hold a secret fails there before any call site fills it.

## IP and User-Agent are captured once, and both are attacker-supplied

`RequestContext.from_request` reads both once, for session rows and audit events. Both values are
bounded to the column widths, imported from the same constants: the User-Agent has no length limit,
and `client_ip` behind `TRUSTED_PROXY_IPS` returns the right-most non-trusted `X-Forwarded-For`
element unvalidated (`_is_trusted` answers `False` for anything unparseable). An over-length value
is a `StringDataRightTruncation`, which under the failures-propagate rule turns a sign-in into a
500\.

The User-Agent is stored raw. The web client's `passkeyNameForUserAgent` alone names devices; a
server parser would let a session row and a passkey row disagree, and the raw string keeps labels
re-derivable. Rejected: `uap-python`, `user-agents` (dormant), `ua-parser-js` 2.x (AGPL-3.0).

`TRUSTED_PROXY_IPS` handling is unchanged; a misconfigured instance records the proxy's address in
`ip`, as `docs/authentication.md` says.

## The dive-site catalog is a vendored ODbL database, and the licence is the hard part

`GET /api/v1/dive-sites/suggest` answers from `src/app/data/dive_site_catalog.json` — 3,702 records
extracted from OpenStreetMap and Wikidata by `scripts/build_dive_site_catalog.py` and checked in —
because the geocoder cannot: Nominatim knows where Dahab is, not where the Blue Hole's north entry
is. It is `services/marine_areas.py` one size up and copies that reasoning: a vendored,
licence-stamped extract beats a live dependency, since a self-hoster should not acquire an outbound
host, an account and a second failure mode for a form suggestion.

No schema change, by design. A pick copies values into an ordinary per-user `dive_site` row through
`POST /dive-site`; nothing links to the catalog. That is the opposite of the species catalog because
the entity differs: a species is a universal immutable fact, so a shared row is right; a dive site
is personal and editable, and the whole chain — join table, per-user rows, per-user uniqueness,
user-scoped caches, export — is built on ownership.

## Dive-site catalog: The PostGIS revisit condition is *not* met by these query parameters

*"Dive site coordinates are two `Float` columns"* rejects PostGIS with "Revisit if 'sites near me'
ever ships", and `search_sites` takes a latitude and longitude and ranks by distance. The condition
is not met. There is no table: the catalog is a tuple of frozen value objects parsed from a file in
the image, and the database never sees the request. There is no spatial query: a linear scan over a
few thousand in-memory objects computes a haversine per match after the name has narrowed the
answer, and no index would help. The rejected cost was an install step — a new Postgres image and an
extension for every self-hoster — and nothing here adds one. The condition is about the diver's own
`dive_site` rows becoming searchable by distance in Postgres, and nothing here moves towards it.

## Dive-site catalog: Refresh policy, and why `marine_areas`' excuse is not available

`marine_areas.geojson` is exempt from refresh as "293 public-domain polygons that do not change";
that does not transfer: OSM dive sites are added, renamed and moved. Sites change slowly, though, so
a stale file only fails to suggest a recent addition. So: regenerated by hand in the PR that changes
the file's meaning, like every generated artifact; no CI job, Makefile target or schedule.
`CONTRIBUTING.md` documents both generators, `build_dive_site_catalog.py` and
`build_marine_areas.py`.

*Rejected:* a scheduled Actions job opening a refresh PR (`contents: write` on a schedule, a cron
GitHub disables after 60 quiet days), and release-cadence refreshes, a release being a heuristic,
not a calendar.

The tripwire is the count: `tests/test_dive_site_catalog.py` asserts the exact count, so a moved
number fails loudly — a shrinking catalog is usually a filter deleting real sites. It is pinned in
that test, `services/dive_site_catalog.py`'s module docstring and the 3,702 above; move all three
together.

## Dive-site catalog: Three ODbL obligations, and all three are discharged in the file

The catalog is a Derivative Database under ODbL §4.4(b). §4.4 share-alike: the file is ODbL though
`LICENSE` is the AGPL, a software copyleft, so the carve-out is named in the file's provenance block
and in `README.md`. §4.2 notices travel in the data itself; that is the in-file block. §4.6 access
is met by checking in the JSON, its generator and the exact Overpass and SPARQL queries.

Natural Earth is public domain and never shipped; the block names it anyway, recording where every
field came from. Wikidata is CC0, but the merged file is ODbL as a whole — never mix an NC or ND
source in.

`_attributions` builds per-source credit from the file's `sources` block and drops at load any
record naming an uncredited source: losing the record beats a missing notice. Natural Earth carries
no `source` key there, so it cannot become a record source by accident.

## Dive-site catalog: What the wire carries, and the two things it deliberately does not

Results follow `SpeciesSearchResponse`'s shape, capped at 10, no `clamp_pagination`; each carries
exactly `name`, `name_en`, `latitude`, `longitude`, `country`, `region`, `source`, `source_id` and
`attribution`. `country_code` stays off the wire: the field a client fills is a place's `name`, free
text — display name to the UI, code to the data. No distance field: the web tier has
`haversineMeters` and a `formatDistance` wired to the unit preference. Name fills from `name`, not
`name_en`; both are searched, so a Latin keyboard reaches 砂辺.

No `@cache`, for `api/v1/species.py`'s reason: local, non-user-scoped, immutable until rebuild. No
per-user rate budget: the 600/hour ones bound a shared resource; this is a scan over a resident
file, and `enforce_rate_limit` is one call away.

With a position, distance is the ranking, not a tie-break. Without one: exact name, prefix,
substring, then name; full ties keep file order, since `CatalogSite` is not orderable. Half a
position is a 422, mirroring `WholeCoordinatePair`.

## Dive-site catalog: The selection rules are floors, and scuba attributes decide over `leisure`

The generator takes every feature carrying `sport=scuba_diving` or `scuba_diving:divespot=yes`, then
drops businesses and indoor facilities; both exclusion sets are floors, re-derived from the tag
distributions before a refresh.

`leisure=pitch` and `leisure=water_park` are not noise: `pitch` is the Dutch convention in Zeeland
(35 of 43 named features carry scuba attributes). Scuba attributes decide, not the `leisure` value;
`scuba_diving:divespot=yes` rescues an otherwise-excluded feature, and only `=yes` does.
`amenity=scuba_diving` is not in the business set. OSM's `;` multi-values are split, so
`amenity=restaurant;dive_centre` is a dive centre.

Overpass is queried with `out center;`, never bare `out;`: a way or relation has no position of its
own.

Wikidata is a regional patch, not a second global source: OSM has three named `sport=scuba_diving`
features in South Africa, and 302 of Wikidata's 345 are there. Dedupe: the OSM `wikidata` tag, a
position within 200 m, or within 1 km with matching normalised names.

## Dive-site catalog: Country and region are resolved from Natural Earth, and containment alone is not enough

Neither upstream carries them; the generator downloads Natural Earth admin-0 and admin-1 and
resolves each record itself, as `build_marine_areas.py` does.

Rule 1 is point-in-polygon; rule 2, nearest boundary within 50 km, is not optional: only 44.5% of
records fall inside any admin-0 polygon, because dive sites are in water. A generator written to
rule 1 alone empties the country for over half the catalog while every unit test passes. Rule 3 is
null, and the record ships anyway. Distances are vertex-to-point, which is why
`tests/test_build_dive_site_catalog.py` subdivides its synthetic edges.

`ISO_A2` is `-99` for 22 admin-0 features and `ISO_A2_EH`'s 13 are a strict subset: read `ISO_A2_EH`
alone, `-99` as null. Admin-1: take `name_en`, never `name`. Resolve admin-0 first, then restrict
admin-1 to that country by `iso_a2`, or the layers disagree and ship `Tabuk, Egypt`; an empty
restricted set within 50 km gives a null region, not a neighbour's.

## Dive-site catalog: The OpenStreetMap credit is byte-identical to the geocoder's, on purpose

The catalog's OSM attribution is exactly `geocoding_service._DEFAULT_ATTRIBUTION`: the site form
renders catalog hits and geocoder hits in one list under one credit line that collapses repeats by
exact string, so a second wording for the same licence would show the credit twice. A test asserts
the two are equal. The Wikidata credit is necessarily different — CC0 is not ODbL — which is why
attribution travels per record rather than per response.

## Self-hosting is a capability we offer, not what the product *is*

Rule: an adjective that defines the product may only name something true of every instance. The
reader of the README is not necessarily the operator; a diver whose club runs the instance has no
`psql`. So the pitch leads with the guarantee — open formats, the original dive-computer export kept
beside every import, full export in one request — while the README's lead adjective is
"self-hostable", `SECURITY.md` opens "yours to self-host", and the Postgres sentence is conditioned
on running your own instance. Prose about self-hosters (release notes, upgrade path, operator
security decisions) stays. "Every instance is exposed for as long as it takes to cut a release" in
`SECURITY.md`, `.github/ISSUE_TEMPLATE/config.yml` and `01-bug.yml` carries no "self-hosted"
qualifier: disclosure timing does not turn on who runs the instance. Test: does removing
"self-hosted" change what the sentence asserts?

## An invitation is an allow-list entry, not a bearer token

`invitation` carries no secret; the invitee is admitted because they signed in with the invited
address. Rejected: a shareable redeemable code (Mastodon, Lemmy, Mealie, Bluesky PDS). Sign-in
already proves ownership of the address via magic link, code or Google's `email_verified`, so a
token would add a redemption surface through `/auth/verify` and `/onboarding` while proving nothing
new; Plausible and Ghost bind the same way. Losing forwardability is the feature: a closed beta
keyed on addresses keeps "who was invited" coupled to "who signed up". No per-invitation expiry; the
90-day retention sweep bounds the address. Revocation stamps `revoked_at` rather than deleting, so
the quota stays a bound on emails sent.

## The registration gate sits below `release_read_transaction`, and that is the whole design

`POST /auth/complete` calls `release_read_transaction(db)` between the duplicate checks and the
insert; it rolls back, so anything read or locked above it is discarded. The gate check therefore
lives below the release, inside the `try` that creates the row, in the transaction `db.commit()`
ends: a revocation or mode flip committed after verification is honoured, account creation and
invitation acceptance are one commit, and the bootstrap emptiness check shares the insert's
transaction. `tests/test_registration_gate.py` stages the race on two Postgres connections, hooking
the revocation onto the avatar import; a "was called" assertion would pass above the rollback. The
refusal path's `except ForbiddenException` rolls back explicitly: the gate holds a
transaction-scoped advisory lock and `async_get_db` does not end the transaction on unwind, so a
raise would block every other account creation until the connection returned to the pool.

## The bootstrap exemption is a property of the table being empty, not a flag

While `user` is empty, the next address to complete onboarding is admitted whatever
`REGISTRATION_MODE` says and gets `is_superuser = true`, in `open` mode too. This resolves the
deadlock of a fresh `invite` instance with no invitations and no superuser;
`scripts/create_first_superuser.py` is excluded from the published image. Emptiness rather than a
flag: `_purge_one_account` hard-deletes the `User` row, so an instance whose last account is purged
is fresh again. `admit_or_refuse` takes `pg_advisory_xact_lock` on a fixed key before the count, on
every account creation — transaction-scoped so there is no unlock to forget, unconditional so there
is no race between branch and check. Account creations serialise, no cost for a human-paced act.
Rejected: `SERIALIZABLE`, which moves the guarantee into the isolation level and the failure into a
retryable serialization error. The suite stages the race on two connections and asserts exactly one
superuser.

## The invite-request endpoint stays ignorant, exactly as the sign-in request does

`POST /invite-requests` never queries `user`; it answers the same `202` and frozen message for a
first request, a repeat, a registered address and an invited one.
`tests/test_auth.py::test_never_queries_whether_the_user_exists` covers it: the handler learns
neither whether an address has an account nor whether it is invited, since on an invite-only
instance the membership list is the whole population. Consequences: a row may be stored for an
address with an account (`GET /admin/invite-requests` carries `has_account` for the operator), and a
repeat stores nothing because the insert is `ON CONFLICT DO NOTHING`, though the audit row is
written for its IP and User-Agent. `ON CONFLICT DO NOTHING` rather than select-then-insert:
check-then-insert has a window where two submissions both insert, and translating the
`IntegrityError` is a branch that can leak. It is the app's first Postgres-specific `insert`;
`core.config` builds only `postgresql://` DSNs. Refusal happens at onboarding and account creation,
after the address is proven.

## The invitation quota is counted from the table, not from the rate limiter

`INVITATIONS_PER_USER` per `INVITATIONS_WINDOW_DAYS` (5 per 1 day) counts `invitation` rows the
inviter created in the trailing window, revoked and accepted included; Redis is not consulted. The
rate limiter fails open on a Redis outage and its counters are flushed locally — right for pacing a
sign-in form, wrong for a bound on who enters a closed instance. Revoked rows count so the quota
bounds emails sent rather than live invitations; a revoke cannot refill the bucket. A rate rather
than a lifetime allotment (Lemmy's `max_invites_per_user_allowed` shape rejected): five today, five
more tomorrow. Superusers are exempt structurally — the count is never taken for them.

## Inviting an address that already has an account is a 409, and that is a disclosure

`POST /user/invitations` answers `409 That address already has an account on this instance.`,
telling a signed-in caller that an address is registered. Accepted: a `201` that creates nothing
leaves the inviter hunting for a missing row and reveals the same fact a moment later; Plausible and
Ghost surface it openly. The quota does not bound this — it counts `invitation` rows via
`invitations_created_since`, and the 409 returns before anything is created, so a probe is free and
a 409 stays distinguishable from a 429. So the route carries
`INVITATION_ATTEMPT_RATE_LIMIT_PER_USER` (20 per magic-link window), applied above the existence
check, to superusers too, larger than the quota so a mistyped address does not spend a real
invitation. Same shape as `PATCH /user`'s username check, keyed per user. Rule: an availability
check that answers distinguishably needs its own throttle; a quota counted from successful writes is
not one. The comparison is case-insensitive.

## Every account comparison in the invitation path is on `lower(User.email)`

`POST /auth/complete` inserts `token_data.email` verbatim and the Google path hands
`resolve_identity` its `email` claim un-lowercased, so `User.email` may carry capitals; both
invitation tables store lowercase. The narrow fix is chosen: compare on `lower(User.email)` wherever
an invitation address meets an account — the `409` on `POST /user/invitations`, `has_account` on
`GET /admin/invite-requests`, the skip arm of `POST /admin/invitations` — all through
`crud.crud_invitations.account_exists_for`. Rejected: normalising `User.email` on write, which
touches the account path, the email-change path and every row. `_purge_one_account` is the mirror
image: its `authentication_request` and `auth_audit_event` deletes compare the stored email raw
because those tables hold what sign-in wrote, while the two invitation deletes lowercase first; the
difference is commented at the call site.

## `invite_request.created_at` needs a `server_default`, unlike every other one in this app

The column carries both `default_factory` and `server_default=func.now()`. `default_factory` applies
when SQLAlchemy constructs a model instance; the table's only writer is a Core
`INSERT ... ON CONFLICT DO NOTHING` in `crud.crud_invite_requests`, which constructs none, so
without the server default the statement sends no value for a `NOT NULL` column and every anonymous
invite request is a 500. Only a Postgres-backed test sees it; a mocked session evaluates no
constraint. The Python default stays for ORM inserts, and both are UTC-aware. Rule: a column written
by hand-written Core needs its default in the database, not in the mapper.

## The invitation tables carry their own retention, and it is 90 days for both

`INVITATION_RETENTION` and `INVITE_REQUEST_RETENTION` are module constants beside
`AUTH_AUDIT_RETENTION`, not settings, following `AUTHENTICATION_REQUEST_RETENTION` and adding
nothing to the install bundle's `example.env`. Ninety days for both because each holds a non-user's
email address and the audit row naming that address expires at 90 days; an address surviving far
longer in one table than another is a new retention decision. A revoked invitation is swept on
`created_at`, not `revoked_at`, so a withdrawn address does not outlive a live one. An accepted
invitation is never swept: it is two accounts' shared history and goes with either — the inviter's
via FK cascade, the invitee's via the by-address arm of `_purge_one_account`. Cost: an invitation
ignored for three months stops admitting its address.

## The operator's routes are the first superuser-gated `/api/v1` routes, and the gate is router-level

`api.v1.admin` declares `dependencies=[Depends(get_current_superuser)]` on its `APIRouter`; the
route-walking auth guard reads include-level dependencies through the merged dependant
(`tests/helpers/routes.py`), so a new route cannot arrive unprotected. The operator UI is a
superuser-gated section of the web app and this module is its JSON. Reason: no passwords, the access
token is a bearer header in browser memory, the refresh cookie is single-use `SameSite=lax`, so a
browser navigating to a server-rendered admin page carries no credential — the staging `/docs` route
shows it. A library admin keeps a second identity (CRUDAdmin) and writes past the service layer
(`admin/views.py`: "no cache invalidation: that lives on the API routes"). Cost accepted: each admin
screen costs full review and tests; a table-browser can be bolted on later. The batch route reports
per-address outcomes — `invited`, `already_registered`, `already_invited`, `mail_failed` — with
sequential inline sends, since the worker runs crons only.

## The local suite has no Redis, so a test that reaches the rate limiter is green here and red in CI

`docker-compose.yml` publishes Postgres to the host but not Redis (`6379/tcp` on the compose network
only), so a host run's `enforce_rate_limit` takes its fail-open path and an unpatched test passes.
CI runs Redis on `localhost`, where the module-level client binds to the first event loop while
pytest-asyncio gives each test a fresh one: `got Future attached to a different loop` /
`Event loop is closed` from redis-py, in tests that look like they are about invitations, and only
after another module poisons the loop. Remedy: patch `enforce_rate_limit` in any test that drives a
handler calling it (`test_invitations.py` does so as an autouse fixture, `test_auth.py` per case),
and sweep a handler's tests when it acquires a limit. To reproduce CI locally, give the suite a
throwaway Redis:

```bash
docker run -d --rm --name od-redis-test -p 16379:6379 redis:7-alpine
POSTGRES_SERVER=localhost REDIS_CACHE_HOST=localhost REDIS_CACHE_PORT=16379 \
  ENVIRONMENT=local SECRET_KEY=testsecret uv run pytest -q
docker rm -f od-redis-test
```

## Gear categories land in UDDF's fixed equipment list, and three of them share `<knife>`

UDDF 3.2.2's `equipmentType` is a closed `xs:sequence` and our `GearType` vocabulary is larger, so
the mapping is many-to-one. `services/export/uddf.py` holds `_EQUIPMENT_ELEMENT` and
`_EQUIPMENT_ORDER`; the writer iterates the order tuple so elements follow the schema's sequence,
and both are asserted at import so a category without a home fails in CI rather than as a `KeyError`
in a diver's download. `line_cutter` and `shears` join `knife` in `<knife>`, the only cutting-tool
slot; rejected: `variouspieces` for both (loses the grouping) or splitting by physical form.
`mirror` and `whistle` go to `variouspieces`. `camera` declines `cameraType`, which extends
`ID_TYPE` and has no `<name>`, and goes to `<variouspieces>` with its name. The collapse costs only
the sub-category, and no tested reader imports the kit list under `<diver><owner><equipment>` beyond
Subsurface's `divecomputer`. Never emitted: `compressor`, `scooter`, `rebreather`, `watch`.

## There is no `Course.cost`, and money is a cross-cutting concern this app has not designed yet

`Course.cost`, `CourseBase`'s and `CourseUpdate`'s fields, `ExportCourse`'s, both export writers'
handling and `courses.csv`'s `cost` column are removed; revision `84bee1255635` drops it. It was
free text ("EUR 650"), and "what has diving cost me" needs amount plus currency across gear and
dives too — a feature with its own design, not a column. DiveJSON's `course` object has no cost
member and is `additionalProperties: false`. No backfill into `notes`; `DROP COLUMN` is
content-independent. `CourseCreate` and `CourseUpdate` are `extra="forbid"`, so the web client stops
sending the key before the API drops it. A cached single-course read (`cache(...)` default 3600 s)
may hold `cost` until expiry; `CourseRead` takes Pydantic's default `extra="ignore"`, so it is
dropped on the way out — no flush. `services/export/tabular.py` matches header to row by position
and `_rows_to_csv` never compares them;
`test_every_normalized_file_lines_its_rows_up_with_its_header` asserts alignment for every
normalized file, checking a row count first so an empty fixture cannot pass vacuously.

## The JSON export is DiveJSON, and the app maintains one JSON format

`src/app/schemas/export.py` writes a DiveJSON 1.0 document (`"format": "divejson"`,
`"version": "1.0"` as a string, the published member names) and the archive member is
`logbook.divejson`; `GET /export/divejson` serves the same bytes with
`Content-Type: application/vnd.dive+json`. Versioning is spec §7: minors are additive.

Rejected: an internal `opendiving-export` beside a public DiveJSON — the app would not exercise the
public format, a spec bug its own export would catch goes uncaught, and two shapes drift on every
change.

Conformance runs through the `divejson` runtime dependency: `divejson.validate_document` is the
reference rule set (spec §3's beyond-schema rules included) and `divejson.load_schema()` serves the
schema. Only the UDDF XSD is vendored.

`units`, `gear_service_emails` and a dive file's `parser_key` live under `extensions.opendiving`.

The writer emits no nulls (`exclude_none=True` in `envelope._encode`); absence is the only spelling
of "not recorded" (§5.4). An empty `notes` is written absent: the `NOT NULL` column's `""` cannot
distinguish a blank note from none.

## The profile speaks one vocabulary, storage included

`DiveProfileRead`, `DiveProfileSeries`, `DiveProfilePressureSeries` and `DiveProfileEvent` carry
DiveJSON's member names — `duration`, `times`/`values`, `pressures`, an event's `time` — and serve
both `ExportRecording.profile` and `GET /dive/{uuid}/recording/{rid}/profile`, whose
`RecordingProfileRead` subclasses them (see *"The profile's provenance is published as a closed
enum, not as `parser_key`"*). Rejected: export-local profile models, which leave two profile
vocabularies on two surfaces and every future change made twice.

The column is `dive_profile.duration`. The JSONB `dive_profile.data` keeps its compact
`t`/`v`/`pressure` keys, mapped out by `to_read_schema`: renaming them is a data migration across
every profile row to save one function, on an encoding only `services/dive_profiles.py` reads.

`DiveProfileInfo.duration` and `DiveGasUse.duration` share the name for consistency; neither is on
the wire.

`DIVES_HEADER` in `services/export/tabular.py` and `tests/fixtures/export/dives.csv` keep a
`duration_seconds` column: it writes `dive.duration`, a different quantity.

## A dive's average depth cannot exceed its maximum, and nothing can store one that does

The writer must not emit a dive whose `avg_depth` exceeds `max_depth`; the DiveJSON validator
rejects one (spec §6.2).

Both layers. `ck_dive_avg_depth_within_max` makes it true of the table; `validate_depth_pair` in
`schemas/dive.py` makes the API name the fields instead of raising `IntegrityError`, on `DiveBase`
and `DiveUpdate`; `patch_dive` re-runs it on the merged stored-plus-incoming pair.
`_DIVE_CONSTRAINT_MESSAGES` backstops a concurrent edit. `<=`, not `<`: a square profile is unusual,
not impossible.

No parse-side guard: a pair rule has no single bad value to null, as with
`ck_dive_mixture_oxygen_helium_sum` and `ck_dive_mixture_pressure_order`;
`test_every_single_column_bound_a_parser_can_reach_has_a_parse_side_guard` lists it among the
exclusions.

Revision `c4d81e6b3f57` nulls `avg_depth` on violating rows before adding the constraint, so
startup's `alembic upgrade head` cannot abort. `avg_depth` goes because `max_depth` feeds the dive
list, stats and UDDF's `<greatestdepth>`; `avg_depth` feeds only gas arithmetic, which declines to
compute when absent. Rejected: swapping the pair (invents a reading) and clearing both.

## The importer is a reader, not a validator, and the schema is never a gate

DiveJSON's §3 conformance rules address writers: `divejson.validate_document` holds the export
honest, and the importer never runs it. A reader salvages rather than grades: a dangling `trip_uuid`
imports without the link, a duplicate uuid is remapped, a profile whose `times` go backwards is
dropped with a note and its dive kept.

`divejson.convert` validates its own output and raises `NonConformingOutputError`; the api logs a
traceback and answers 422. A converted document still passes `_validate_envelope`.

`divejson` is a main dependency because the request path imports it; the `Dockerfile` runtime image
carries only the main set, so a dev-only import dies at container start. `tests.yml`'s
`runtime-imports` job builds the `runtime` stage and imports the route module.

Rejected: an importer-local restatement of §3, which drifts.

Spec §9's duplicate-member rule is a parsing rule — `json` keeps the last value — so
`parse_document` and `DuplicateMemberError` in `services/logbook_import/reader.py` reject it.

## A uuid is preserved, matched, restored or remapped, and which one is a property of the instance

Every caller-owned collection takes one of four branches on a document uuid. Unclaimed here: create
under that uuid, preserving identity across instances. The caller's, live: nothing is written,
counted as `linked` — the idempotence invariant. The caller's, soft-deleted: restore (next section).
Another account's: mint a fresh uuid, create under it, remap every reference.

A fifth remap — two records of one collection claiming one uuid — is decided in
`_claim_document_uuid` first and has its own note code; see *The two remaps are two codes, because
they differ in what happened to other records*.

The remap branch cannot be idempotent: `dive` has no unique constraint, so a second import creates a
second copy; the preview reports the creation set first. Rejected: a source-uuid provenance column,
a schema-wide change for one corner case.

`species` is exempt: it belongs to nobody, and import never creates or claims a catalog row.

## A soft-deleted match restores the row — wholesale, under its original uuid

A soft-deleted row of the caller's whose uuid the document carries has `is_deleted` and `deleted_at`
cleared together, every imported column overwritten and its children rebuilt, and is reported under
its own `restored` count.

Create-fresh under a new uuid is rejected: the `uuid` unique index is full, not partial, and no
purge job reclaims a deleted dive, so every re-import would duplicate the restored dives.

Two traps: the dedupe lookup branches on `is_deleted` rather than filtering it — an
`is_deleted=False` query would call the husk's uuid unclaimed and hit that index — and the match is
re-evaluated inside the apply transaction, a row being deletable between the two calls, so apply
re-plans the document rather than carrying a plan in its token.

Derive the tables with `git grep -n "SoftDeleteMixin" -- src/app/models/` (`Dive`, `Certification`,
`GearServiceRecord`, `User`), reading the hits since *not*-soft-deleting models match too; `User`
never imports.

## No uniqueness rule anywhere fails an import

Every user-scoped unique index is a "you already have one of these" rule, so a collision links to
the caller's existing row and remaps every reference, within one document too:
`ux_dive_site_user_id_name_location_lower`, `ux_trip_user_id_name_lower`,
`ux_gear_item_user_id_brand_name_lower`, `ux_gear_set_user_id_name_lower`,
`ux_gear_service_schedule_item_kind_label`. Derive the set with
`git grep -n "unique=True\|UniqueConstraint\|ux_" -- src/app/models/ src/app/core/db/models.py`;
join tables declare `UniqueConstraint(...)`, not `Index(..., unique=True)`.

`course` and `certification` have no name rule; a course failed and retaken is the same name twice.

`gear_service_schedule` is keyed on `gear_item_id`, so its dedupe key is the gear row id where the
caller owns the item and the document's canonical gear uuid where the import creates it;
`_claim_unique` takes both, because missing either spelling is two inserts against
`ux_gear_service_schedule_item_kind_label` and the logbook refused.
`tests/test_logbook_import.py::TestTwoSchedulesOnOneNewGearItem` covers it.

`ux_dive_file_user_id_sha256` has no link available (one `dive_file` row names one `recording_id`,
and `ux_dive_file_storage_key` forbids sharing a key), so it is skip-and-report: the preview names
the missing file.

## Where the format is optional and this app is not, the record goes rather than a value being invented

DiveJSON's REQUIRED set (§5.4) is smaller than what this app writes; one rule covers the gap.

Where a column's default already means "not recorded", absence takes it: `notes` (`NOT NULL`, `""`
meaning the diver wrote nothing), the booleans `rented`, `archived` and `active`, and a schedule's
`dive_count_at_start` (0).

Otherwise the record is skipped and reported: a course with no `status` (§6.17), a service record
with no `dive_count_at_service`, a dive with no `duration` and no profile, a species with no
`aphia_id`, and a record whose REQUIRED closed-vocabulary member (a certification's `agency`, a
service `type`) carries an unknown value, which §5.6 reads as absent. A course's `agency` is
OPTIONAL and so costs the field rather than the record — see *A course may have no agency, and a
certification may not*.

Two derivations are allowed and reported: a dive with no `duration` takes its profile's span; one
with no `number` gets a placeholder, duplicates being legal (`DiveNumberingSummary`).

`visibility` is finer in the format (a number, §6.2) than here (whole metres); a fractional value is
dropped and reported, not rounded.

## A dive's UTC offset may be unknown, and only import can make it so

`dive.utc_offset_minutes` is nullable, and NULL is a third state: wall clock recorded, instant
unknown — DiveJSON's local date-time (spec §5.2), because UDDF pipelines destroy offsets.

The requirement is write-side: `require_utc_offset` guards `DiveCreate`,
`DiveRenumberRequest.from_start_time` and `GET /dives/next-number`. `schemas/dive.py` declares
`DiveStartTime` (strict) and `DiveLocalStartTime` (permissive, for read shapes).
`DiveUpdate.start_time` is `DiveLocalStartTime` and `patch_dive` preserves the state, never removes
it; see *An offset-unknown dive keeps its wall clock editable*. Derive the set with
`git grep -nE "DiveStartTime|DiveLocalStartTime|require_utc_offset" -- src/ tests/ DECISIONS.md` and
sweep the prose in `core/utils/datetime_offset.py` too.

Storage: `start_time` holds the wall clock labelled UTC and `combine_start_time(instant, None)`
returns it naive, so every reader through that converter is correct. `services/dive_activity.py` and
`services/species_life_list.py` (`array_agg(..., type_=ARRAY(Integer))[1]` yields `None`) bypass it
safely.

The column keeps its `0` default and `server_default`, so a `Dive(...)` built without an offset
cannot claim the unknown state; only the importer passes `None`.

## The surface-pressure floor is 0.4 bar, because the altitude ceiling says so

`ck_dive_surface_pressure_range` is `[0.4, 1.2]` because ambient pressure at
`ck_dive_altitude_range`'s 6500 m ceiling is about 0.44 bar; a higher floor refuses a surface
pressure the altitude bound blesses. DiveJSON §6.2 bands the member at 0.4–1.2 for the same reason,
and an importer must not drop a value the format blesses. The band is stated in the constraint, in
`_drop_implausible_surface_pressure` (whose docstring pins itself to the CHECK's numbers), in the
constraint message `api/v1/dives.py` serves, in `suunto_xml.py`'s backstop comment, and in the two
tests asserting the parser's band is the column's; move all of them together.

## Importing a logbook is the one client-supplied profile

`/dive/parse` neither returns nor accepts samples, which would make stored samples client-supplied.
Logbook import accepts them, into the caller's own account only, through the same
`derive_gas_attribution` → `downsample` sequence as a dive-computer file. Authority, not provenance:
the profile lands in the importer's own logbook and is never claimed to be extracted from bytes this
instance holds. `parser_key` is `divejson_import` on a bare import, the restored file's key on the
archive path.

`source_sha256` is the dive's file digest (`should_extract` and the backfill query select on it):
the restored file's on the archive path, so that dive is never a backfill candidate; the payload's
on the bare path.

The profile's `duration` is the document's (§6.4 allows it past the last sample; it is the
gas-coverage denominator) via a `store_profile` override, clamped up if below its samples.

Samples skip `normalize()`, which rebases onto the earliest reading; `times` are already elapsed
(§6.5).

## A bare document creates no file rows, and only the archive restores bytes

A bare document carries `source_file` and certification-file metadata without bytes (`archive_path`
absent, spec §6.7), and a file row here claims bytes exist (`BlobMissingError`, `storage_key`
`NOT NULL`). So the bare path imports the dives and reports the files as not contained, never a 422.

The archive path writes the blob before the transaction that references it, as
`store_recording_file` and `store_certification_file` do, so a failure leaves only an orphan for
`sweep_orphaned_files.py`; no compensating unlink (see *"Orphans are the only failure product, and
there is a script for them"*).

Each restored member is verified against the manifest digest before writing; a mismatch skips and
reports it.

Content types are not the document's: a card image's is sniffed as `store_certification_file` does,
a dive file's comes from this build's parser registry via `parser_key` (see
`create_dive_file_token`).

A stored file's uuid is not preserved; nothing resolves a file by it, and
`ux_dive_file_user_id_sha256` enforces identity.

## Species import by AphiaID, resolved before the transaction opens, and never created from the document

`Species` is a global, ownerless catalog; creating rows from a document would make every caller a
writer to a shared table, and `status` is `NOT NULL` with no format member (§5.4). Import matches on
`aphia_id` (§6.11) and calls the same `resolve_species` as `POST /species/resolve` for an unknown
AphiaID. An unmatched species is skipped and its sighting links dropped and reported — never the
dive.

It runs as a batch before the apply transaction: preview stores nothing, and `resolve_species`
manages its own transaction around the outbound call. The worst case is two
`_RESOLVE_BUDGET_SECONDS` passes plus `_ENRICHMENT_BUDGET_SECONDS`, so
`IMPORT_SPECIES_BUDGET_SECONDS` is sized against that ceiling; unknowns outstanding at expiry are
skipped and reported.

`resolve_species` raises 503 on budget expiry and on a WoRMS outage; the pre-pass converts each to
skip-and-report.

Its rows are the accepted exception to all-or-nothing: global, idempotent, and reused by the next
attempt.

## An import recomputes what is derived, imports what is a snapshot, and drops the caches afterwards

Recomputable aggregates are recomputed, never imported (spec §5.7): `UserDiveStats` through
`recalculate_dive_stats`, `GearItem.dive_count` through `recalculate_gear_dive_counts`, a schedule's
`last_service_on`/`next_due_on`/`next_due_at_dive_count` through `recalculate_service_schedule` —
after the service records, being a function of them — and the profile summary family `store_profile`
derives.

Server-set snapshots import verbatim, since no recomputation procedure exists and recomputing
against the destination's live counters would reset every baseline:
`GearServiceSchedule.dive_count_at_start` and `GearServiceRecord.dive_count_at_service`. Classify a
new column by whether a recomputation procedure exists, not by its comment's wording.

`created_at` imports from the document; `updated_at` is minted by the write.

Six invalidators run after commit: `invalidate_dive_caches`, `invalidate_certification_caches`,
`invalidate_course_caches`, `invalidate_gear_caches`, and two helpers in
`services/cache_invalidation.py` for the dive-site and trip list caches, module-private
`OwnedResourceCache` instances in `api/v1/dive_sites.py` and `api/v1/trips.py`; they share
`OwnedResourceCache.list_cache_pattern` with `invalidate_list` rather than importing a route module.

## The `diver` member's identity and settings are never applied, and its check-in details only as confirmed

A document's owner — name, username, email, `created_at` — and its preferences under this producer's
key are never applied: changing a live account's identity or settings as a side effect of a restore
is a worse surprise than setting them once. The archive's `avatar.webp` is not restored either.

The check-in details — date of birth, phone, emergency contact, insurance — are shown in the preview
beside the account's, and the apply writes exactly those the diver submits (§6.1's SHOULD NOT). The
importer cannot tell a restore from a buddy's file, so writing on the document's say-so would take a
stranger's contact. An object is proposed whole, never merged member by member, which would pair one
insurer's name with another's policy number.

Rejected: restore-means-restore for preferences; filling only empty details.

## The certification agency vocabulary is the format's, value for value, and cannot grow again

`CertificationAgency` equals DiveJSON's §6.16 enum value for value and in order, `andi`, `snsi`,
`acuc`, `pss` and `ida` included. The column is a plain `VARCHAR(32)` with no DB `CHECK`, for the
reason `gear_item.type` has none, so the vocabulary needs no migration.

Rejected: laundering real agencies through `other`/`agency_other` on the way in, which makes a round
trip lossy on a member the format guarantees.

The list cannot grow: a certification's `agency` is a REQUIRED member of a closed set, and §7
freezes those at 1.0 because a reader treats an unrecognized value as absent, which for a REQUIRED
member leaves the record uninterpretable. That is why the spec seeded the list wide, and why a
national CMAS federation is `cmas`. The freeze holds for the whole enum although a course's `agency`
is OPTIONAL: one list is shared by both, so the certification's requiredness is what governs it.

## Logbook import spools its upload and still parses the document whole

The upload is read in bounded chunks into a `SpooledTemporaryFile` (`SPOOL_THRESHOLD`);
`read_upload_within_limit` returns `bytes` and is sized for uploads two orders of magnitude smaller.

Parsing still materializes the whole document, so the document cap is the memory ceiling; a
streaming parser, an arq job or a preview-token-keyed spool are unbuilt.

Zip bombs are refused before a byte inflates: declared sizes must sum under
`MAX_ARCHIVE_EXTRACTED_SIZE`, members are checked by declared size, `zipfile` verifies the CRC, and
`archive_path` is a member name, never a path.

`archive.read(...)` raises `RuntimeError` (password-protected), `BadZipFile` (CRC mismatch) and
`NotImplementedError` (unsupported compression), untranslated by the route, so it is caught as well
as `zipfile.ZipFile(...)`: the `logbook.divejson` member failing is a 422 naming the password case;
a blob member failing has `read_member` return `None` for skip-and-report.

Both endpoints rate-limit per user on their own budget, not the export's: twenty, one import being
two calls.

## Logbook import reads whatever the converter reads, and the routes are `/import/logbook/*`

`POST /import/logbook/preview` and `POST /import/logbook` accept a DiveJSON document, any format in
`divejson.read_formats()`, or a `.zip` of those; labels are `reader.py`'s `_FORMAT_LABELS` (see *"A
version bump can add a reader, and only a test notices"*).

`divejson.sniff` runs once on `divejson.SNIFF_BYTES`. Rewind the spool before `registry.convert`: it
sniffs from the current position and honours `max_members`/`max_member_size` only on its
`format=None` branch. Conversion runs in `run_in_threadpool`; `json.loads` does not.

A zip (`PK\x03\x04`) without `logbook.divejson` goes to the converter whole, and
`_refuse_oversized_source` bounds the members' declared sum by `MAX_DOCUMENT_SIZE`: every converted
member is held until merge.

A head starting `{` keeps the 422 "not valid JSON"; other unclaimed bytes are 415. The token covers
the uploaded bytes and apply converts again, deterministic except `exported_at`, passed in UTC via
`_conversion_moment`.

`ImportReport.conversion` (`Conversion.grouped()`, capped by
`MAX_CONVERSION_GROUPS`/`MAX_CONVERSION_WHERES`) keeps `groups[].kind` an opaque string, never
`StrEnum` or `Literal`: a dependency bump can add a kind.

## Two readings of one Suunto file live here, and they disagree by design

`divejson` reads the Suunto app's JSON export, and so does `services/dive_parsers/suunto_json.py`.
`POST /dive/parse` fills a form from one file and wants this app's keys, prefill shape and rounding;
logbook import wants a whole logbook in a format other applications write. The same file read both
ways differs; none of it is a defect. Known differences: `gas_number` (a position there, the file's
number here); `bottom_temperature` (unmapped there, a minimum over samples here); precision
(`parse_float=Decimal` there, `_round2_or_none` through `float` here); the profile time axis
(`dive_profiles.normalize` rebases onto the earliest reading, ~+1 s); `duration` on a half-second
(`round()` here, `ROUND_HALF_UP` there); `DiveRouteOrigin` rounded to six places by
`positions.geo_fix`; per-second merging keeping first there, `_rebase` last here; bounds, zero
floors, `null`, `ActivityType` and `Header.DateTime` rules the library applies and this parser does
not; `can_parse` requiring `.json`. Only the library records `source_generator`. The list is a
floor; add to it rather than reconciling.

## An `Integer` column's real bound is its width, and no `CheckConstraint` census can see one

Postgres `Integer` is 32 bits and DiveJSON puts no ceiling on any integer member (a dive's `number`
is a bare `{"type": "integer"}`), so a conforming document (a converter with a unit bug, say) can
hold a number a column cannot, and unbounded that is SQLSTATE 22003 mid-transaction, a whole logbook
refused; the `CheckConstraint` census cannot see it.

Every reachable integer is bounded in `_DIVE_BOUNDS`, `_MIXTURE_BOUNDS`, `_plan_schedule`,
`_Planner._count` and `_series`/`_plan_profile` (sample extremes become `Integer` summary columns).
A second census enumerates `Integer` columns on every table an import writes and requires each
bounded in the planner or excluded with a reason.

Derived columns are closed at their inputs, since `recalculate_dive_stats` and
`recalculate_service_schedule` know nothing of import: `next_due_at_dive_count` sums
`dive_count_at_start` and `interval_dives`, both capped at `_MAX_DIVE_COUNT` (a million);
`user_dive_stats.total_time` sums `dive.duration` over every dive the account holds, so a per-dive
cap of a year cannot bound it and the column is `BigInteger`.

## The two remaps are two codes, because they differ in what happened to other records

`ImportNoteCode` carries `record_remapped_references_follow` and `record_remapped_references_stay`
rather than one `record_remapped`, because the two remaps differ in the half a client cares about.
In `_resolve`, an identifier already owned by another account gets a fresh uuid and every reference
to the document's uuid is rewritten to follow it. In `_claim_document_uuid`, two records of one
collection claiming one uuid (non-conforming under §5.3) means the second gets a fresh uuid and
nothing is rewritten, so references stay on the first. The names carry the reference behaviour, not
the cause, because a client meeting one value alone should not need to know which branch a cause
implies. Rejected: one code with clients branching on `message`, which freezes sentences into the
API; keeping `record_remapped` for one cause and adding a member for the other, which reads as a
main case plus an exception. Tests pin each code by the other's absence.

## An offset-unknown dive keeps its wall clock editable

`PATCH /dive/{uuid}` accepts a `start_time` without a UTC offset only against a dive whose
`utc_offset_minutes` is already NULL, and only leaving it NULL: preserving is allowed, removing is
not. An offset-aware update is always accepted. The reason is round-trip closure: the DiveJSON
writer emits an offsetless `started_at` for such a dive, so the write API must accept what the app
exports rather than push clients to fabricate `+00:00`. This narrows *A dive's UTC offset may be
unknown, and only import can make it so*; import stays the only origin of the state. The rule lives
in `split_updated_start_time`, not a schema annotation, because a schema cannot see the dive:
`DiveUpdate.start_time` carries the permissive `DiveLocalStartTime`, the helper raises `ValueError`,
and `patch_dive` answers a flat 422 `{"detail": "<sentence>"}` using
`START_TIME_OFFSET_REQUIRED_MESSAGE`, which names the allowed case. The guard tests
`stored_offset_minutes is not None`, never falsiness: `0` is an offset and the column's
`server_default`.

## A cylinder may record a mix without a vessel

`dive_mixture.volume`, `.oxygen` and `.helium` are nullable; NULL means the source never recorded
one. All three are OPTIONAL in DiveJSON (§6.3: absent means not recorded, not 21), and UDDF's
`<tankvolume>` is `minOccurs="0"`. `DiveMixtureBase` carries no `default=21.0`/`default=0.0` — the
prefill belongs in the form — and admits NULL on read, or `GET /dive/{uuid}` would 500. The four
`CHECK`s are restated `X IS NULL OR …` in revision `d3b1700eb489`, hand-written because autogenerate
cannot see a constraint whose text changed under its name. `services/dive_gas.py` guards are
null-aware: `compute_gas_use` refuses a missing size, and `compute_parallel_gas_use` narrows volumes
before its `len({volumes}) == 1` test, which an all-NULL set satisfies. `gas_name` returns
`Unrecorded gas` or `O2 32% / He unrecorded` for UDDF's mandatory `<name>`; `<o2>`, `<he>` and
`<tankvolume>` are omitted, never `0`. `merge_mixture_fields` checks both sides for absence.
`_plan_cylinders` skips no cylinder; a value outside `_MIXTURE_BOUNDS` drops with a note.

## The dive form's field vocabulary lives in the API, and is mirrored with a two-sided guard

Hidden dive-form fields are a `DiveFormField` `StrEnum` in `schemas/dive_form_preset.py`; a preset's
`hidden_fields` and `user.dive_form_hidden_fields` are `list[DiveFormField]`, so an unknown name is
a 422. Rejected: opaque strings, where a typo silently un-hides a field. The values are the dive
resource's own optional fields, pinned from both ends:
`tests/test_dive_form_presets.py::TestTheVocabularyNamesRealFields` checks a subset (every value
names an optional field of `DiveCreateRequest`, or of `DiveMixtureCreate` under `mixture.`); the web
half checks equality against its form schema minus `NON_HIDEABLE_MIXTURE_SCHEMA_KEYS`, the
authoritative exempt set. API-optional is wider than hideable: `gas_number` has no input,
`volume`/`oxygen` are exempt because they are what a cylinder is, and `mixture.helium` is hideable
because no helium means air. A `mixture.` key hides that input on every tank card; `mixtures` hides
the section. The values are stored data; a rename is a data migration. Storing the hidden set keeps
a new field visible everywhere and lets "Technical" be `[]`.

## `hidden_fields` is canonical on write, so two equal sets are two equal lists

Every write — a preset's `hidden_fields` and `user.dive_form_hidden_fields` on `PATCH /user` — is
rewritten into `DiveFormField` declaration order with duplicates collapsed; a caller may send any
order. This serves the client: the panel marks the preset whose hidden set equals the account's
current state, which with a canonical form is one loop over two lists rather than set-building in
every client, forever. The single definition is `canonical_hidden_fields`, used by both schemas, so
the two write paths cannot drift. The cap `max_length=len(DiveFormField)` sits on the input list,
before duplicates collapse, so a body padded with repeats is a 422; nothing legitimate sends more
entries than the vocabulary has members.

## Dive form presets are seeded at registration, not lazily

Every account gets Basic, Recreational and Technical as ordinary rows, created in
`complete_profile`'s own transaction (`api/v1/auth.py`): registration creates the account with its
three presets or nothing. Only self-service registration passes through it; accounts from the admin
panel's insert or `src/scripts/create_first_superuser.py` reach the same three through
`POST /dive-form-presets/defaults`. Rejected: lazy seeding on the first `GET /dive-form-presets`
behind a `seeded` flag — a GET that writes and a column on the hottest row; and web-side seeding
from its own constant, which gives the vocabulary two owners. Restore adds what is missing by name,
case-insensitively, and never overwrites: idempotent, and an edited or renamed default keeps its
edit. `services/dive_form_presets.py::seed_default_presets` serves both callers. A changed default
set reaches new accounts only; no revision rewrites existing rows, since that overwrites a saved
preference and un-marks the preset as current for every diver whose `user.dive_form_hidden_fields`
still holds the old set.

## The revision that backfills presets carries its own frozen copy of them

It seeds accounts that predate the registration seed. The three default sets are copied into the
revision, not imported from `services/dive_form_presets.py`: a revision is frozen history
(`c3c2c4dd4c27`), and a renamed `DiveFormField` member would make an import raise `ImportError` at
load time, taking every `alembic upgrade head` with it. Two things autogenerate cannot see: a uuid
per row from `uuid7` in Python, since `PublicUUIDMixin`'s `default_factory` runs only on model
construction and `gen_random_uuid()` is uuid4; and `bindparam(..., type_=sa.JSON())` on the
`sa.text()` insert, since a text statement carries no column types. The step skips a name the
account already holds, case-insensitively, so `_backfill_default_presets` can be driven twice:
`tests/test_dive_form_presets.py::TestTheBackfill` reaches it through Alembic's `ScriptDirectory` by
revision id and asserts against the revision's own `_DEFAULT_PRESETS`, never today's
`DEFAULT_PRESETS` — a frozen-versus-living comparison is a drift alarm. Soft-deleted accounts are
backfilled too; `POST /auth/restore` can bring one back.

## `user.dive_form_hidden_fields` follows the `gear_service_emails` template

The account's current hidden set, as opposed to a preset, which is a snapshot. Applying a preset
copies its list into this column in one `PATCH /user`; toggling a field afterwards moves this
column, not the preset. The column follows `gear_service_emails` to the letter: on `UserRead` and
`UserUpdate` (which is `extra="forbid"`, so omitting it 422s the panel); in
`UserUpdate.NON_NULLABLE_FIELDS`, which
`test_update_explicit_nulls.py::test_the_declared_fields_match_the_table` reads off the SQLAlchemy
metadata; inherited by `UserAdminUpdate`. `JSON` with `server_default` `[]`, the
`webauthn_credential.transports` precedent — nothing queries into the list, so `ARRAY` buys nothing.
Server-side rather than per device, unlike the per-field entry-unit switch: it follows the diver
across devices, and because it rides on `get_current_user` the form's first paint already omits
hidden fields. Per-device storage would also owe a `/privacy` row. A fresh account hides nothing
(`[]`); starting everyone on Basic is rejected because it changes the first form a new diver meets.

## Presets are snapshots, and the list read is not cached

Applying a preset copies its hidden set into the account's current state; editing a field afterwards
changes the current state, not the preset, until the diver writes it back. Lightroom's model rather
than VS Code's live profiles: "show me altitude just this once" must not edit a preset, and a diver
who deleted every preset still needs somewhere to toggle. `PATCH /dive-form-preset/{uuid}` therefore
touches no user row. `GET /dive-form-presets` is not Redis-cached and is named in
`OwnedResourceCache`'s opt-out list. Nothing embeds a preset, so there is no second cache to
invalidate; the panel reads the list once when it opens. Caching buys one Redis round trip per form
and costs an invalidation obligation on five mutating routes. `PATCH /user` carries the current
state through the path `units` already takes.

## Presets and the hidden-fields preference travel in the archive

Both ride the `diver` member's `extensions.opendiving` payload beside `units` and
`gear_service_emails`; logbook import reports them and never applies them, per *The `diver` member's
identity and settings are never applied, and its check-in details only as confirmed*. They are UI
configuration, but `/export/archive`'s docstring promises nothing in the account is reachable only
through the app, and leaving them out would make that false. The producer key is the format's
extension mechanism (spec §5.5), so the DiveJSON spec is untouched and an unrecognising reader must
not fail. A preset travels as `{name, hidden_fields}` only: `uuid`, `user_uuid` and `created_at`
identify a row in this instance and mean nothing elsewhere. The empty set is written as `[]` rather
than omitted, so "Technical hides nothing" is distinguishable from a failed export.

## `PROJECT_OPERATED` is the first setting that knows who runs the instance, and it selects copy only

`RegistrationSettings.PROJECT_OPERATED`, default `false`, is published as `project_operated` on
`GET /config`. The web app selects the request-an-invite form's copy on it (waitlist wording versus
"the operator decides"), and `send_invitation_email` selects one sentence on it: invited "to
OpenDiving at `<url>`" rather than "to their OpenDiving log book". The subject is not branched. It
is documented only in `src/.env.example` and kept out of the install bundle's `example.env`, as no
self-hoster needs it. Rejected: operator-configurable free text (four strings move together, and
env-var prose escapes the render tests); a web-side env var or hostname check (`GET /config` exists
for facts the web needs before first paint, and the email template needs it too). It gates no
feature, unlocks no route and reaches no row; `git grep PROJECT_OPERATED` and
`git grep project_operated` list every branch. `tests/test_email_service.py` asserts the unset
message as a whole-message literal. The project's hosted instance answers `project_operated: true`.

## Revoking a session ends its access token too, at the cost of a third indexed read

`DELETE /user/session/{uuid}` stamps `revoked_at`, and `get_current_user` honours it on every
request: after the account lookup it calls the `live_session_for` that `/auth/refresh` calls, and
`None` raises the same `UnauthorizedException` as every other failure there. One error for every
cause, so nobody gets an oracle for which `sid`s exist. The cost is a third indexed read on a path
already making two (`verify_token`'s blacklist `exists`, `crud_users.get`): `user_session.uuid` is
unique and live sessions cap at 100 per account. Reusing `live_session_for` rather than restating
`_live` also buys the `user_id` clause. A token with no `sid` is refused;
`services.auth_service.issue_tokens`, the only mint site, always sets one. The `| None` stays in
`core.security._session_id`, `current_session_uuid`, `to_public_session` and
`revoke_other_sessions(except_uuid=None)`. The 409 on revoking your own session stays:
`POST /auth/logout` also clears the cookie and blacklists the pair, and a self-revoke is a
half-logout. `TestARevokedSessionEndsItsAccessToken` in `tests/test_sessions.py` calls the
dependency directly.

## A dive has recordings, and a file belongs to one of them

`dive_recording` sits between `dive` and its files and profiles: one row is one device's record of
one dive. A dive holds an ordered list with ordinal 0 primary; a recording holds `dive_file` rows
and at most one `dive_profile`. One file per dive cannot represent two computers on one dive, one
computer exported twice, or a Perdix surfacing mid-dive. What stays: `ux_dive_file_user_id_sha256`,
so `reconcile()` asks about the dive, not the recording; `dive_file.dive_id` and
`dive_profile.dive_id` as denormalised read keys; `dive_file.storage_key`. The `ON DELETE CASCADE`s
from `dive_recording` really fire, unlike those from `dive` (deletion is `is_deleted`), so
`services/dive_recordings.py` reads storage keys before deleting for `delete_after_commit`. Routes:
`POST /dive/{uuid}/recordings` (not `PUT`: two different files are two additions),
`GET`/`DELETE /dive/{uuid}/file/{fid}`, `GET /dive/{uuid}/recording/{rid}/profile`. The migration
gives every dive with a file or profile one recording inheriting `start_time`, offset, `duration`
and `max_depth`; device columns stay NULL until `backfill_tech_fields` re-parses.

## Recording identity is device plus start, and three gates use it

`services/dive_recordings.py` alone decides whether two records are one recording, one dive, or
neither, within one account from one indexed read on `(user_id, start_time)`. `same_device` and
`devices_differ` share one rule — an absent member never makes two devices differ — so a pair may be
neither; the model branch applies only with at most one serial in play. Gates: same recording — same
device, starts and sampled spans within 2 s; same dive strict — Subsurface's `likely_same` on a
different device, where one-sided figures block; same dive loose — the start window alone. Δ is
between instants when both sides carry an offset, else between wall clocks; rejected: borrowing the
account's or dive's offset, which DiveJSON §5.2 forbids. The 26-hour candidate window is wider than
any gate; attach applies same-recording within the target dive; import applies same-recording then
strict, never loose.

## A second file of one recording fills, and never overwrites

Everything a file says about a recording comes from the first file that recorded it:
`fill_device_fields`, `fill_gate_figures`, `fill_start`, `fill_tech_scalars`, `fill_channels`,
`fill_dive_mixtures`; `git grep -n "def fill_" -- src/app/services` is the list. Rejected: later
wins, losing corrections. Profiles fill by channel, never by sample. Fills are a `COALESCE` per
column except `fill_start`, whose two columns hold one value (filling a NULL `utc_offset_minutes`
also converts the stored wall clock to an instant), and `fill_dive_mixtures`, whose join is
positional. The dive's scalars are the primary recording's. `fill_mixture_fields` writes
`FILLABLE_MIXTURE_FIELDS` only where the stored row has none, sharing `merge_mixture_fields`' join,
never its overwrite of `gas_number`; `_fill_is_storable` drops per row a fill the `CHECK`s would
refuse. `_rederive_recording` requires `fresh` (this upload created the recording, so scalars write
outright) and `joined` (new bytes on an existing recording; only that fills cylinders);
`_repeat_upload` and `delete_dive_file` pass `joined=False`.

## A profile has one of three provenances, and a recording need not have a file

`dive_profile.parser_key` is one of three: a `DiveParser.key` (re-readable from files),
`divejson_import` or `merge` (written by `POST /dives/merge`). The last two are
`UNREPRODUCIBLE_PROVENANCES`; clients see the narrower `ProfileProvenance`. A recording with no
files is first-class: logbook import stores no bytes for a converted or bare document, and it is
deleted only through the recording route. `should_extract` refuses an unreproducible profile, and
both backfills select on the profile's provenance, not a file's. A recording survives its last file
only when no file can re-yield its profile. `source_sha256` is the file's digest for one file, and
the SHA-256 over digests in attach order for more, so single-file rows keep their ETag.
`backfill_tech_fields` overwrites a scalar only with a value a stored file yields, since an import
can fill `cns_end` from a document alone. Rejected: a per-scalar provenance column; the cost: the
backfill cannot null a bogus stored reading.

## A version bump can add a reader, and only a test notices

The accepted import set is `divejson.read_formats()`, computed per call and never written out as a
list. Every guard on both sides of the seam tolerates an unknown format: `formats_this_build_reads`
falls back to the raw id, so a reader the pin gains is accepted while the sentence describing what
the API reads renders a bare id like `suunto_xml` and the web picker's own extension list greys it
out. No CI job, route test or exception sees that.

So `_FORMAT_LABELS` is a table, and `test_every_read_format_has_a_label` fails the build when the
pin outgrows it. The mirror case is checked too: a label for a format the library has dropped is a
format this build advertises and refuses. The prose that spells the set out — `README.md`,
`api/v1/logbook_import.py`, `schemas/logbook_import.py` — cannot be derived, so the test's message
names those files.

## Merging two dives keeps the gap and synthesises nothing

`POST /dives/merge` folds two of one diver's dives: a computer restarting mid-water logs two records
the strict gate refuses to fold unasked. Hand-entered dives get a 422 naming which lacks a
recording.

The earlier dive survives by `starts_before` (`delta_seconds`' rule, `services/dive_recordings.py`),
`_orders_first` breaking ties by id; the loser is soft-deleted. Same-device records fold into one
recording; different devices or a NULL start append. The offset is the recordings' delta, never the
dives'. The gap stays empty: `join_profiles` is not `fill_channels`; pressure joins by `gas_number`;
markers all stay. Provenance is `merge`, so `should_extract` never re-extracts; files stay.
`duration`, `max_depth` and `dive_figures` recompute, `None` meaning leave alone; `start_time` and
oxygen readings stay, `refresh_tech_scalars` uncalled. An `avg_depth` failing
`ck_dive_avg_depth_within_max` is a 422, not a write. `relabel_gas_numbers` precedes the join,
keeping `usage`; moved profiles use `replace_profile_samples`, never `store_profile`. Join rows
re-point, collisions stay, notes append within `NOTES_MAX_LENGTH`. `_rederive_recording` and
`delete_recording` are not reused.

## `PlannedRecordingMatch` carries an ordinal, because a fill can land on a secondary recording

`_fill_recording` in the import writer writes two things that belong to the dive, not the matched
recording: oxygen-exposure readings (`fill_tech_scalars`) and cylinders (`fill_dive_mixtures`). Both
are the primary recording's — a second computer's CNS clock is its own arithmetic, its cylinder
labelling its own numbering — and `_rederive_recording` already returns before both for
`ordinal != 0`.

So `PlannedRecordingMatch` carries `ordinal`: `None` on an `attach`, where no stored recording is
named and the writer computes the slot with `next_ordinal`, and the writer returns before both
writes for anything but ordinal 0. Otherwise a Suunto export imported as a second reading of a
secondary recording credits the primary with the Suunto's numbers; `fill_mixture_fields`'
`(oxygen, helium)` join guards cylinders only by accident, the scalars not at all. Required, not
defaulted, as `PlannedRecordingMatch.mixtures` is: an empty default can leave a whole path
unreachable unnoticed.

## The profile's provenance is published as a closed enum, not as `parser_key`

`ProfileProvenance`, a closed `file`/`divejson_import`/`merge`, says what produced a recording's
samples; `dive_profile.parser_key` is not published. The column is an open set — two sentinels plus
every parser key — so a client hard-codes the sentinels and renaming one is a wire break; the
overload stays in storage as the re-extractable flag. `ProfileEventType` and `GasRole` take the same
shape. Rejected: raw `parser_key` (`files[].parser_key` already names the parser) and a boolean
`reproducible`, collapsing the two cases.

`FILE` is not "has files": a merged recording keeps both halves' files, samples still `merge`.

`DiveProfileInfo` gains the member; `DiveProfileRead` cannot, being `ExportRecording.profile` — the
DiveJSON `profile` object, `additionalProperties: false`. `GET /dive/{uuid}/recording/{rid}/profile`
serves `RecordingProfileRead`, a subclass adding `provenance`, and `to_recording_read_schema` widens
`to_read_schema`'s result. An export cannot say a profile is a merge; `extensions.opendiving` (spec
§5.5) is the slot.

`provenance_of` reads anything but the sentinels as `FILE`, so
`test_every_unreproducible_provenance_has_a_wire_value` asserts its keys are
`UNREPRODUCIBLE_PROVENANCES`.

## A deletion re-derives the dive's readings only where it touched the primary recording

`refresh_tech_scalars` rewrites every `DiveTechScalars` field outright from ordinal 0. After a
secondary's file is deleted on a multi-recording dive it is a loss: a file-less logbook-import
primary (`services/logbook_import/writer.py`) gives `read_recording` nothing, so every field goes
`None`; a primary with files loses the document's `cns_end` and unparsed `otu_end`.

So it takes a required `touched_primary`; `False` is a no-op. The answer cannot be read inside,
`renumber_ordinals` having closed the gap: `delete_dive_file` uses the ordinal it reads before the
deleting branch, `erase_dive_recording` asks `primary_recording_ids` before `delete_recording`, a
promotion answers `True`. Required, not defaulted, like `_rederive_recording`'s `fresh` and
`joined`.

Rejected: filling instead of clearing when the primary has no files
(`test_the_last_file_takes_its_recording_and_the_dives_readings` pins the last file taking the
reading), and gating on the primary having files, which misses the second shape. A promotion onto a
file-less recording still clears: the diver chose it.

Pinned in `tests/test_dive_recordings_rows.py` by `TestDeletingASecondComputersFile`,
`TestDeletingARecording` and `TestPromotingARecording`.

## A stored vocabulary is read back as a string

Read schemas carry `StoredVocabulary` (`core/schemas.py`), an alias for `str`; enums stay on the
create/update schemas. *"`GearItem.type` is a closed vocabulary, but has no DB `CHECK` constraint"*
lets a direct SQLAlchemy write store any value, so a read schema typed with the enum asserts an
invariant nothing enforces, and Pydantic fails the whole response: one `gear_service_schedule.kind`
outside `ServiceKind` 500s `GET /gear-items` and `GET /gear-service-due` through
`GearServiceScheduleInfo`.

Applied to every stored vocabulary — `gear_item.type`, `dive.water_type`,
`dive_mixture.role`/`usage`, `course.agency`/`status`, `certification.agency`, `user.units`,
`user.dive_form_hidden_fields`, `dive_form_preset.hidden_fields`; derive the list, never trust it.
`user.units` is the one a schema sweep misses, since `get_current_user` validates through `UserRead`
on every request. `certification_file.side` is exempt: structural, and server-written from a
validated path parameter.

*Rejected:* a DB `CHECK` (every vocabulary addition becomes DDL); dropping the row
(`GearServiceDueResponse.truncated` exists because silent under-reporting is the wrong failure);
coercing to `OTHER`/`NULL`; `ServiceKind | str`, which collapses to the `str` arm in smart mode.

## Stored vocabulary: Where the enum stays

`schemas/export.py` keeps its enums: they are DiveJSON's own, on objects the format closes
(`additionalProperties: false`), and widening one lets an export emit a document that is not
DiveJSON. It must not 500 either, since the migration leaves a colliding row unrepaired. The
envelope splits on REQUIRED (`_speakable`/`_sayable`, `services/export/envelope.py`): a REQUIRED
member outside the vocabulary — `gear_service_schedule.type`, `gear_service_record.type`,
`certification.agency` — omits the record (spec §5.6); an OPTIONAL one (`gear_item.type`,
`dive.water_type`, `dive_mixture.role`/`usage`, `course.agency`/`status`) costs only the field.
`gear_service_schedule` is the one omittable collection anything references, and
`gear_service_record.gear_service_schedule_uuid` is OPTIONAL, so it goes absent through
`_schedule_uuid`. A course's `agency` and `agency_other` are written as a pair or not at all
(`_course_agency`), the schema admitting `agency_other` only beside `other`.

A read shape re-validated as a write one repeats the widening: every `DiveMixtureCreate` rebuild
goes through `as_create` (`schemas/dive_mixture.py`), and `validate_agency_pairing` takes
`agency: str | None`, not `CertificationAgency`. `services/export/tabular.py` carries the stored
string rather than `ServiceKind(schedule.kind).value`; `services/export/uddf.py` picks its element
through `_gear_type`, falling back to `<variouspieces>` as `OTHER` does.

## Stored vocabulary: The migration names two literals, and is not a vocabulary sweep

Revision `f9d04a823776` rewrites `kind = 'inspection'` to `'visual_inspection'` on both gear service
tables and clears `water_type = 'soda'` to NULL. Both are test-fixture artifacts
(`tests/helpers/generators.py`, `test_dive_check_constraints.py`) that no API request could store. A
`WHERE kind NOT IN (...)` is the `CHECK` constraint by another name and would rewrite the value the
unconstrained column exists to admit.

The schedule arm skips a row whose rename would collide with
`ux_gear_service_schedule_item_kind_label`, since an aborting migration inside the startup
`alembic upgrade head` leaves the container down. The skip is safe only because every reader
tolerates the leftover — a repair allowed to skip is a repair whose leftovers something must read.

Pinned by `TestAnUnrecognizedKindDoesNotFiveHundred` (`tests/test_gear_service.py`),
`TestAnUnrecognizedVocabularyValueDoesNotFiveHundredTheExport` (`tests/test_export_json.py`),
`TestAStoredFieldNameOutsideTheVocabulary` (`tests/test_dive_form_presets.py`) and
`tests/test_vocabulary_repair.py`, which runs the revision's statements against Postgres for the
collision skip and idempotence: a revision failing partway rolls back without advancing
`alembic_version` and restarts from the top.

## The WoRMS credit is a link, and any edit to it is a cache-version change

`_WORMS_ATTRIBUTION` (`services/species_service.py`) is
`[World Register of Marine Species](https://www.marinespecies.org) (CC BY)`: a credit names origin
and licence and links to the licence text; WoRMS's text is CC BY. It is written linked, like the
geocoder's `_DEFAULT_ATTRIBUTION` and `_MARINE_ATTRIBUTION`: a statement this project makes, not a
provider string to fold. `(CC BY)` sits outside the link, the label being the destination and the
licence a fact, which is safe because `parseAttribution` on the client interleaves links and plain
runs; the client parses a shape before the API emits it.

`_CACHE_VERSION` bumps with it: a cached search answer holds `attribution` for a month and nothing
fails when the bump is skipped. Any edit to a string the normalizer writes into a cached row is a
cache-version change.

`README.md` carries WoRMS's citation template; its `Accessed <date>` is harmless because the app
queries the register live, holding no snapshot. Pinned by `TestTheWormsCreditIsALink` in
`tests/test_species.py`.

## The edge channel publishes on every merge, and deploys what it publishes

`publish-image.yml` runs on `push: branches: [main]`: every merge publishes `:edge` and `:sha-<12>`,
and `deploy` hands that digest to Render. No `:latest` or semver alias, which pin people; only
`prepare`'s version block assembles those.

Concurrency is `publish-image-edge` for `main` pushes, `publish-image-release` otherwise, else a
merge cancels a pending tag run; a lost edge run is fine, each built commit keeping its `:sha-<12>`.

`prepare` checks out `github.sha`, not `github.ref`, a branch resolving to its tip after queueing;
on `workflow_dispatch` `inputs.ref` feeds `needs.prepare.outputs.sha`.

The deploy names a digest read through the `sha-<12>` tag in `merge`, never `edge`: the hook call is
the deploy (`imgURL`) and `edge` hooks land in either order. `RENDER_DEPLOY_HOOKS` (comma- or
newline-separated: `api`, `worker`) unset means a notice and exit zero, forks having none; a failing
hook is red. `::add-mask::` per hook; status via `-w '%{http_code}'`.

Migrations are fix-forward: a wrong revision gets a follow-up, never an edit.

## `/api/v1/health` reports the commit, because the version moves only on a release

`APP_VERSION` comes from the installed distribution's metadata and moves only on a release, so on
the edge channel a fortnight of merges all report `0.1.0`; a bug report needs a build identifier and
the AGPL §13 source offer something a user can check out. So `publish-image.yml` passes the resolved
commit as the `APP_COMMIT` build arg, the `Dockerfile`'s runtime stage exports it,
`AppSettings.APP_COMMIT` reads it, and `GET /api/v1/health` returns it as `commit` beside `version`.

- The resolved SHA, not `github.sha`: they differ on `workflow_dispatch`; it equals the
  `org.opencontainers.image.revision` label by construction.
- `ARG` at the bottom of the runtime stage, below the `COPY`s, so the per-build value invalidates
  one metadata layer.
- Empty by default and surfaced as `"unknown"`, as a source checkout and `docker compose up --build`
  produce, mirroring `version`.
- Not in `src/.env.example`: a build identity in the template freezes at whenever it was copied.

## Every foreign key column leads an index, and a test says so

Postgres never indexes a foreign key's referencing side, so a child `parent_id` without one is
scanned whole on every cascade, every `DELETE` proving nothing references it, and every by-parent
read; every key into `user.id` cascades, so an account purge is that scan per table. Nothing else
notices a `ForeignKey(...)` lacking `index=True`: autogenerate and `alembic check` pass.

`tests/test_foreign_key_indexes.py` walks `Base.metadata` without a database. Coverage means:

- Leading, not merely present: `(dive_id, position)` covers `dive_id`, `(sort_key, parent_id)`
  nothing; `trip_part.trip_id` and `dive_recording.user_id` depend on this.
- A unique index, primary key or `UniqueConstraint` counts (`dive_file.user_id` via
  `ux_dive_file_user_id_sha256`); the last two are absent from `Table.indexes` and asked separately.
- A partial index (`postgresql_where`) counts as none, the lookup carrying no predicate;
  `ix_gear_service_record_gear_item_id` and `ix_gear_service_record_schedule_id` exist for this.

`_leading_column_name` unwraps a `.desc()` `UnaryExpression` and requires a `Column`:
`func.lower(label)` reports `.name` `"lower"` and covers nothing. No Postgres half: `alembic check`
pins models to revisions.

## Autovacuum is left at its defaults, and a storage parameter is never a revision

Nothing here sets an autovacuum parameter: not the compose file, a migration or a model. The tables
that churn hardest are the ones the hourly sweeps in `core/worker/settings.py` empty (tokens,
authentication requests, sessions, audit rows, invitations), and `autovacuum_vacuum_threshold` (50)
plus `autovacuum_vacuum_scale_factor` (0.2) fires on those every time; `dive` churns at human speed.
A parameter set now is a guess about volume.

The reading that says otherwise is `n_dead_tup` on `dive` climbing and staying up in
`pg_stat_user_tables`, and the remedy belongs to whoever operates that database:

```sql
ALTER TABLE dive SET (autovacuum_vacuum_scale_factor = 0.05);
```

Never as an Alembic revision: a revision runs unattended on every install, and a threshold is a
property of one database's write volume, not of the schema — a busy instance's number lands verbatim
on a one-diver logbook. Autogenerate does not see storage parameters and `alembic check` has no
opinion on them, so a hand-written one drifts unseen. It is reversible with `RESET`.

## A replayed refresh token takes its session with it

A replayed refresh token revokes its session. `/auth/refresh` already spends the presented cookie;
the winner's replacement pair is what a theft is about, and one `UPDATE` on `user_session` ends it,
since the `sid` survives every rotation and `get_current_user` asks the same row. `issue_tokens`
continues one session per rotation, so `user_session` is the lineage.

The revocation sits past `_REFRESH_REPLAY_THRESHOLD` (five seconds), so a two-tab race gets only the
401; the `WARNING` logs regardless, and `tests/test_auth_audit.py` bounds the number.

The site emits `REFRESH_REPLAY_DETECTED` alone, and the `UPDATE` rides `record_auth_event`'s commit,
since `async_get_db` does not commit on unwind before `raise UnauthorizedException`.
`tests/test_auth_refresh.py` checks the commit on a second connection.

`revoke_session` takes the `sid` from `token_session_id` and no `user_id`, as `POST /auth/logout`
does: a token in `token_blacklist` was issued here with that `sid`. No `sid` revokes nothing and
still 401s; cookie and `detail` are unchanged, so the endpoint is no oracle.

## Two checkouts running the suite at once share one database, and one of them drops it

`tests/conftest.py` names the test database `POSTGRES_DB` plus `_test` and nothing else, so every
worktree pointed at one Postgres runs against the same `opendive_test`, where `CONTRIBUTING.md`'s
host-run recipe points them all.

Concurrent runs interfere, and it looks like flakiness: one or two failures per run in a different
unrelated module, passing on re-run. Anything counting or emptying a shared table is a candidate
(`TestTheBootstrapExemption` asserts an empty `user` table, the import gates count rows they
seeded), and the race tests are the most sensitive:
`TestTheBootstrapExemption::test_two_concurrent_first_completions_produce_exactly_one_superuser` and
`TestRecordAssertion::test_exactly_one_of_two_concurrent_recordings_wins`, which a third racer from
another checkout falsifies. A run told to `DROP DATABASE opendive_test` after a branch switch drops
it out from under a sibling mid-run.

There is no lock and no per-checkout database. Check before believing a failure: `ps` for another
`pytest`, or `select datname, count(*) from pg_stat_activity group by datname` for a second set of
connections to `opendive_test`.

## The decompression channels are a profile's, and the model is a recording's

Six channels (`ndl`, `tts`, `ppo2`, `cns`, `gradient_factor`, `surface_gradient_factor`) are
per-sample readouts and live in `dive_profile.data` beside `depth` and `ceiling`, so there is one
time axis. The model (`mode`, `deco_model`) is one setting per dive and lives as five columns on
`dive_recording` (`deco_algorithm`, `deco_name`, `deco_gf_low`, `deco_gf_high`,
`deco_conservatism`); `DECO_MODEL_COLUMNS` in `services/dive_recordings.py` is the only
member-to-column mapping. Both belong to the recording, never the dive: two computers can run
different modes and gradient factors on one dive. Nothing derives any of the six; each depends on
the device's model and the diver's history, which a logged dive does not carry, so a reconstructed
ppO₂ curve would be a derivation the format requires a reader to declare.

## Deco channels: The scales, and why each is what it is

`schemas/dive_profile.py` declares one scale constant per channel beside `DEPTH_SCALE`, including
the ones that are 1, so no channel looks like the unscaled odd one out.

| channel                                      | unit                | why                                                                |
| -------------------------------------------- | ------------------- | ------------------------------------------------------------------ |
| `ndl`, `tts`                                 | seconds             | the format's duration unit; devices report minutes or seconds      |
| `ppo2`                                       | hundredths of a bar | tenths cannot tell 1.30 from 1.32; Shearwater exports two decimals |
| `cns`                                        | tenths of a percent | Suunto JSON records `0.069` where the XML rounds to `7`            |
| `gradient_factor`, `surface_gradient_factor` | whole percent       | every source reports whole percent                                 |

Rejected: millibar for ppO₂ (no source resolves below a hundredth) and reusing tenths-of-a-bar
because it is "a pressure". `cns` the channel and `dive.cns_start`/`cns_end` are different
quantities, neither derived from the other; the dive columns are the device's own whole-percent
figures and follow *"A second file of one recording fills, and never overwrites"*.

## Zero is a reading, a negative is an absent-marker, and neither rule is the ceiling's

`ceiling_cm` drops a zero because for that quantity zero means "no ceiling". The six deco channels
take the opposite rule, which is why `unsigned_int_or_none` in `dive_parsers/channels.py` is a
sibling of `ceiling_cm`, not a generalisation. A zero is a reading: NDL 0 is the moment a dive
became a decompression dive. A negative is the absent-marker devices write (`NoDecTime: -1`,
`gf99: -100`), and the format floors all six. A value at a display cap (`NoDecTime: 6000`,
`<nodecotime>5940</nodecotime>`) is a reading meaning "at least this". A zero that means "no figure"
in one format is that parser's call: `suunto_json.py` treats `TimeToSurface: 0` as absent. Nothing
is clamped above; `gf99` reaches five digits on real ascents and Suunto publishes no definition, so
a cap would be a guess.

## Deco channels: Which summary extreme, per quantity

`dive_profile` gains one column per channel (`min_ndl_s`, `max_tts_s`, `max_ppo2_bar100`,
`max_cns_pct10`, `max_gradient_factor_pct`, `max_surface_gradient_factor_pct`) and `channels` on the
read schema is derived from which came back non-NULL. Min for NDL, max for the rest: the maximum NDL
is the display cap on most dives and says nothing, the minimum is how close the dive came to its
limit, and a stored `0` there is a reading, so NULL has to mean absence. `_SUMMARY_COLUMNS` in
`services/dive_profiles.py` is the one table naming column and extreme per channel; two writers and
one reader walk it, so a channel added without an entry is a `KeyError` at the first write rather
than a curve that never appears.

## Deco channels: What the parsers still refuse, and why

A parser reads what its format's mapping document maps and nothing else. Suunto DM5 XML carries none
of the six; it carries `<Mode>` and `<PersonalMode>` (conservatism, P−2 to P2), while `<Algorithm>`
is an undocumented enum and stays unread, like `<Type>` on a `<DiveMixture>`. Suunto app JSON
carries four of the six, never `ppo2` or `cns`; `gfLeadingTissue` is a compartment number, not a
loading; the header's `DiveMode`, `Algorithm` and `Conservatism` are the mode and model. FIT has
`record` fields for four (`ndl_time`, `time_to_surface`, `cns_load`, `po2`) and none is read,
because every file in hand carries them empty; `dive_settings.gf_low`/`gf_high` are the model, and
`model` is read only where it states `zhl_16c`. A family is never derived from a product string:
`deco_algorithm` comes from a two-entry table of known strings, anything else fills `deco_name`
verbatim and leaves the family absent.

## `deco_conservatism` is the one reading in this app with no floor

It is Suunto's P−2 to P2 scale stored as the device's own number: `0` is P0 and `-1` is P−1, both
real settings in the owner's exports. Reading a negative as the absent-marker, the right answer for
every channel, would delete a setting. The number means nothing without `deco_name` and the device
columns beside it, which is why it is stored beside them rather than normalised across vendors.

## `gf_low ≤ gf_high` is enforced by the writers, and the constraint is the backstop

`ck_dive_recording_deco_gf_low_within_high` states the rule on the row, but both write paths drop an
inverted pair before it can fire: `ParsedDecoModel._pair_the_gradient_factors` on the parse path and
`_plan_deco_model` on the import path, with a note. An `IntegrityError` on the parse path surfaces
as "the file changed while this upload was in flight", wrong and unactionable; on the import path
the whole archive is one transaction, so one bad pair would take every dive with it. Neither number
says which is wrong, so both go and the record stays, as `_plan_cylinders` does for oxygen and
helium summing past 100. The pair travels whole: one gradient factor alone names no setting, so a
`COALESCE` fill fills both or neither.

## The decompression channels arrive for new dives only

`PROFILE_EXTRACTOR_VERSION` is 4 and `should_extract` re-extracts any profile behind it, so the
existing script picks the corpus up unchanged:

```bash
docker compose exec api python -m src.scripts.backfill_dive_profiles
```

Nothing runs it. Profiles already stored keep the four channels they have until their recording is
re-uploaded; the new data is worth having for new dives without a pass over old ones. Running the
backfill changes every profile ETag (`{source_sha256}:{extractor_version}`) and flushes the dive
caches of every user it touches, which is the cost of running it on a whim. A migration is the wrong
place regardless: re-extraction reads dive-computer files out of the blob store, and an Alembic
revision has neither the blob store nor the parsers. Stale rows read harmlessly: `profile_from_data`
and `to_read_schema` read every channel with `.get`, so a payload with no key for a newer channel
raises no `KeyError`, and its NULL summary columns are what `channels` means by no such curve.

## The dive *detail* cache key carries no version, and any suffix goes after the colon

`_cached_read_dive` is keyed `user_{user_id}_dive` with no per-key version; the per-build namespace
(*"A deploy cannot serve the previous build's response cache"*) covers stale bodies, and the next
reshaped response owes no `:v3`. A stale detail body is a wrong answer rather than a 500:
`RecordingRead.mode` and `.deco_model` are `Field(default=None)` and `DiveProfileInfo.channels` is a
plain `list[str]`, so an old entry validates and tells a diver their dive has no mode and four
curves for the hour-long TTL; `DiveReadWithMixtures.species` and `.recordings` carry
`default_factory=list` for the same reason. Only the detail read is exposed: `DiveProfileInfo`
reaches the wire through `RecordingRead.profile` under `GET /dive/{uuid}`, `_cached_read_dives`
builds `DiveRead` without `recordings` or `profile`, and `read_dive_profile` is uncached. Any suffix
on this key must follow the colon: `invalidate_dive_caches` sweeps `user_{id}_dives:*` and
`user_{id}_dive:*` (two patterns so the shorter does not eat the dive-site list), and
`user_{id}_dive_v2:…` falls outside both, so invalidation would silently stop.

## `other` is storage's spelling of an absent event type, and it never reaches the wire

`ProfileEventType` has fourteen values; thirteen are DiveJSON's and `OTHER` is storage's only.
DiveJSON §6.6 spells unclassified as an absent `type` beside a REQUIRED `label`, and an `"other"`
value would be a second spelling. Storage keeps `OTHER` because a JSONB key and an enum want a
value. They meet at two functions. `_published_event_type` in `services/dive_profiles.py` maps
`OTHER` to `None` on the way out inside `to_read_schema`, which the profile route and
`logbook.divejson` both use; the schema's `profile` object is `additionalProperties: false`, so
`"type": "other"` would invalidate every exported document, and `exclude_none=True` drops the null
as §5.4 requires. `_events` in `logbook_import/planner.py` reads an absent or unrecognized `type` as
`OTHER` on the way in (`_unknown_is_absent` nulls later-version values first). The Suunto JSON
parser's `_ALERT_TYPES` maps alarm wording to a class and keeps the label; an unmapped string stays
`OTHER` with its wording. FIT's `dive_alert` stays unclassified: its `data` enum is undocumented.

## The UDDF export carries every deco readout the format has an element for

`services/export/uddf.py` writes `<nodecotime>`, `<calculatedpo2>`, `<cns>` and `<gradientfactor>`
as `waypointType` children from `ndl`, `ppo2`, `cns` and `gradient_factor`, and `<divemode type>` on
the first waypoint from the primary recording's `mode`. `<gradientfactor>` goes out as the
documented fraction, not Shearwater's whole percent: `divejson`'s `PERCENT_GRADIENT_FACTORS` keys on
the generator and `APP_NAME` is in nobody's table, so our own import reads our export by the
fraction branch. `gauge` has no `divemodeType` value; `_DIVE_MODE_TYPE` spells it as an explicit
`None` (no element; UDDF defaults to open circuit) so the import-time assert can equal `DiveMode`.
`freedive` is `apnoe`, not the 2017 `apnea`. Three members stay out: `deco_model`, because
`<decomodel>` requires a `<tissue>` table the recording lacks and every document is XSD-asserted (so
`deco_gf_low`/`deco_gf_high` reach the file nowhere); `tts` and `surface_gradient_factor`, because
3.2.2 has no element, and the test pins the closed set of waypoint children. `divejson`'s
`uddf_write.py` deliberately shares no code with this.

## A deploy cannot serve the previous build's response cache

`core/utils/cache.py` prefixes its keys with `resp:{build}:`; `{build}` is `APP_COMMIT` truncated,
else `_source_fingerprint`, a digest of every `.py` under `app/` returning `None` for an empty walk
(`TestTheSourceFingerprint`). `@cache` replays a stored body without running the route, so a body
predating a required field fails `response_model` validation (`ResponseValidationError`) for the
decorator's 3600-second default. Rejected: a per-key suffix (a human must bump it; nothing fails
otherwise) and a shape check on read (defaulted members validate stale data; FastCRUD lists are
`-> dict`). `species:` and `geocode:` keep `_CACHE_VERSION` unnamespaced: a month of a remote
register's answers, validated on read. `_delete_keys_by_pattern` widens to `resp:*:`
(`across_builds`) so `erase_user` reaches every build's rows. Rate-limit counters and
`auth:passkey-challenge:*` stay unnamespaced. A payload cached from a bug still needs a flush, never
`FLUSHALL`:

```bash
docker compose exec -T redis redis-cli --scan --pattern 'resp:*' | xargs -r docker compose exec -T redis redis-cli DEL
```

On Render, SSH refuses exec (use `ssh -tt`) and the image has no `redis-cli`: use
`/app/.venv/bin/python` with `redis.Redis.from_url` and `scan_iter`, URL from
`REDIS_CACHE_HOST`/`REDIS_CACHE_PORT`/`REDIS_PASSWORD` in `os.environ`.

## A startup refusal names `.env`, because that is the file every reader of it has

`Settings._require_s3_credentials` and `blob_store.backend_for` refuse an `s3` backend with settings
missing and point at "the object-storage block in your `.env`", never at `src/.env.example`. A
self-hoster runs the front door's bundle (`docker-compose.yml`, `Caddyfile`, `install.sh`,
`example.env`) and has no `src/` on the machine; they meet the first refusal at startup and the
second through `docker compose exec api python -m src.scripts.migrate_blobs`. An error naming a file
the reader cannot open reads as an instruction. `.env` is true for both audiences: `install.sh`
writes it from `example.env` line by line so the comments around each setting survive, and a
developer's `src/.env` is `src/.env.example` by the same route. Every other refusal in
`core/config.py` names settings and an alternative, no file: the shape to copy. This wording and
`_reject_placeholder_secret_key`'s differ on purpose: a block of settings is missing here, one
value's provenance is wrong there; `.env` fixes the file reference, not the sentence.

## The placeholder refusal names the file the placeholder is *in*, not the one it came from

`Settings._reject_placeholder_secret_key` points at the reader's `.env`, never at
`src/.env.example`. `example.env` in opendiving/opendiving ships
`SECRET_KEY=change-me-openssl-rand-hex-32`, the string `PLACEHOLDER_SECRET_KEYS` lists, and the
by-hand install in its `docs/install.md` is `curl -Lo .env …/example.env` then edit six values, so
the operator's `.env` starts as the template. `install.sh` never reaches here (it generates
`SECRET_KEY` or dies), so every reader is editing `.env`. A startup error may name a file only if
every reader has it: never a template, always the file they edited. The message keeps why the value
matters (it signs every token) and the fix (`openssl rand -hex 32`). The opening
`SECRET_KEY is unset or still a placeholder` stays verbatim, the `.env` pointer appended after a
dash, because the front door's `docs/troubleshooting.md` indexes on it;
`test_the_refusal_keeps_the_prefix_the_front_door_indexes_on` pins that. An error string quoted in
another repository's docs is a shared interface: `git grep -F 'SECRET_KEY is unset' origin/main` in
opendiving/opendiving.

## The labelling step retries, and "already exists" is not a failure

`.github/workflows/pr-title.yml` wraps every `gh` call in `gh_retry`: three attempts with a widening
pause, since a single `HTTP 500 (https://api.github.com/graphql)` says nothing about the repository.
`gh_retry` matches a glob of expected non-zero output before the generic failure arm, so
`label with name "feat" already exists` costs one call. A label that already exists is the goal,
reached by somebody else (a concurrent run, or an earlier attempt whose reply flapped); `--force` is
rejected: it overwrites a hand-tuned colour. Three failures on any read or write stop the job with
an `::error::` quoting gh, the read of the PR's current labels included: an empty answer there
leaves a retitled PR under its old type. `.github/release.yml`'s `"*"` catch-all files an unlabelled
PR under *Other changes*, so silence is wrong notes weeks later. Every repository generating release
notes from these labels carries its own copy; nothing checks they agree, so diff them.

## The component release publishes itself, and the product release keeps the human pass

`publish-image.yml`'s release job runs `gh release create --generate-notes` with no `--draft`, so a
`v*` tag leaves a published release. Self-hosters read the product release in
`opendiving/opendiving` (*The install bundle lives next door*), which keeps the human pass and stays
a draft; this one records what went into an image. Auto-publishing that one is rejected: generated
notes omit an empty category, so it would carry no Breaking section rather than "None". One dispatch
in the product repository cuts the release; a draft here is a step nothing takes. Cost: this
publishes before the product guard runs, so a guard failure leaves a component release for a version
that never shipped, whose tag only an organisation admin can delete (`tags` ruleset). With nobody
reading a draft, `.github/release.yml`'s category order (`breaking` before `feat`) is the whole
categorisation. The job id is `publish-release` here and in `opendiving-web`; rename both at once or
neither.

## The `/openapi.json` route serves `application.openapi()`, never a hand-rolled document

`create_application` serves `/openapi.json` through `application.openapi()`, not a hand-rolled
`get_openapi(title=…, version=…, routes=…)` call. FastAPI's own method passes on every field it
holds (`description`, `contact`, `license_info`, summary, terms, webhooks, tags, servers), so a
forgotten field cannot drop metadata silently, and it caches until the route set changes. This is
the only `/openapi.json`: the built-in one is off (`openapi_url=None` for `EnvironmentSettings`) and
`/docs` and `/redoc` point here. `version` comes from `APP_VERSION` with the `or "unknown"` that
`/api/v1/health` uses, because FastAPI's default `"0.1.0"` is a plausible answer and `info.version`
is required by the spec. Contact and license are built conditionally: `src/.env.example` ships
`LICENSE`, `CONTACT_NAME` and `CONTACT_EMAIL` commented out, and `license_info={"name": None}` fails
the document's own validation, so passing it straight through 500s `/openapi.json` for every
operator who never set `LICENSE`; an all-`None` contact publishes an empty `"contact": {}`.
`tests/test_openapi_document.py` pins the unset case on its status code as well as its `info` block.

## The DiveJSON export is laid out a member and a record to a line

`envelope.write_divejson` writes every top-level member on its own line with a space after each
colon, and every record of every collection on a line of its own; `_encode_collection` joins
per-record encodings with newlines, matching the streamed `dives`. A single-line `sites` runs to
hundreds of kilobytes. Records stay compact internally: a profile is thousands of samples across up
to ten channels, and indenting one multiplies the payload for no reader. Collections are small and
held whole; `dives` is streamed a dive at a time. `format` and `version` remain the first two
members (spec §3, §4), which `tests/test_export_endpoints.py` asserts against the raw bytes.
`TestTheLayout` in `tests/test_export_json.py` reads the byte stream, since a parser cannot see
layout: one test asserts each top-level member opens a line, one walks every collection off the
parsed document, not a written-out list, and checks each record starts a line.

## A request never names its owner; the session does

No create body and no paginated list route names the account it belongs to: the row is the token's,
and every handler scopes by `current_user["id"]`. The eight create schemas are `extra="forbid"`, so
a body naming an owner is a 422 like any other unknown key. *Rejected:* an owner field the handler
compares to the session's and 403s on — it has authority over nothing. *Rejected:* keeping it
optional as an on-behalf-of hook for a future admin path; nothing fetches another user's data
through this API.

`tests/test_request_identity.py` holds the wire-level pins, and a structural one over the served
OpenAPI document: no request body schema publishes an owner property and no operation declares one
as a query parameter, so a schema written later cannot reintroduce it unnoticed.

## The check-in details are columns on `user`, and an explicit null clears one

Date of birth, phone, the emergency contact's three fields and the insurance provider, policy number
and expiry are eight nullable columns on `user`, listed once as `CHECK_IN_FIELDS` in
`schemas/user.py`. *Rejected:* a diver-owned table allowing several policies or contacts — it buys a
second policy nobody asked for. Nullable rather than defaulted: unfilled is the ordinary state, and
`{"emergency_contact_name": null}` is how a diver removes a contact, so none of them joins
`NON_NULLABLE_FIELDS`. They reach the admin panel through `UserAdminUpdate`'s inheritance, unhidden,
the panel being off by default and retiring. On export they are the Diver's `born_on`, `phone`,
`emergency_contacts` and `insurances` (§6.1), each column as wide as its member, and import applies
them per *The `diver` member's identity and settings are never applied, and its check-in details
only as confirmed*. `PATCH /user` refuses a contact without a name or an insurance without a
provider, the format's anchors; the export omits an older row in that state.

## A trip's list order is an aggregate, so the query is hand-written

`GET /trips` sorts by `min(trip_part.start_date) DESC NULLS LAST` with a `uuid` tie-break, and no
path through `OwnedResourceCache` produces it: `get_multi`'s `sort_orders` is `'asc'`/`'desc'` with
no null placement, and `search_multi` builds a bare `.desc()` from a single *named* column, which an
aggregate over another table is not. So `crud_trips.get_trips_page` is one hand-written `select()`
serving both branches. `NULLS LAST` is behaviour, not tidiness: a trip whose parts carry no dates is
legal, and Postgres's `DESC` default would read it as "soonest" and float it to the top of every
diver's list.

No second index. `ix_trip_part_trip_id_position` leads with `trip_id`, so the correlated subquery
reads a handful of rows per trip. *Rejected:* `(trip_id, start_date)`, which changes what
`tests/test_foreign_key_indexes.py` accounts for, for no measured gain; a change that finds the
query plan says otherwise should add it and say so.

## A place is one object, stored flat, and the index keys on its name

A dive site's locality and a trip part's place are one thing — DiveJSON §6.9's Location: `name` (the
place as a person writes it, "Dahab, Egypt"), `full_name` (the fullest form a lookup returned), a
centre and a box. `schemas/location.py` holds the one validator both hosts use. Storage is flat,
bare on `trip_part` and under `location_` on `dive_site`, because a site carries its own pin as well
and the two positions must not read alike. `ux_dive_site_user_id_name_location_lower` keys on
`location_name` alone: a place is identified by what it is called, so two sites in "Dahab, Egypt"
collide whether or not a geocoder filled a centre in for one of them. *Rejected:* a nested JSON
column, which no functional index and no `ilike` picker search can reach; and keying on the name
plus a rounded position, which collapses to the name for almost every row.

## The dive-site location contract changes without a write shim

`POST`/`PATCH /dive-site` take `location` as a place object and nothing accepts the string that
preceded it, so between this API's deploy and the web client's every site write is a 422 and every
rendered site throws on an object child. That window is accepted rather than covered: the only
affected client is one this project deploys itself, and its matching change follows immediately.
*Rejected:* the optional-and-ignored write shim the trip-parts rename used, which reads both shapes
for the few minutes of skew and then costs a second pass over the same schemas, routes and tests to
take out — and which, held longer than that, is a second spelling of a member the format defines
once. The migration is a separate question and keeps its guards: it is about data already stored,
not about a window.
