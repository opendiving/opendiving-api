# opendiving-api

FastAPI + SQLAlchemy + PostgreSQL + Redis.

`DECISIONS.md` in this repo holds the reasoning and the exact remedies behind most of what follows —
read the relevant section before changing anything unfamiliar, and append to it when you hit a new
gotcha. It and the other markdown docs are formatted: run `uv run mdformat *.md docs` after editing
one rather than matching the wrapping by hand.

## Environment & Setup

- Location: `src/`

- Credentials: `src/.env` (keep safe, but logging to console is OK locally)

- **`cp src/.env.example src/.env` is not a working configuration, on purpose.** The template's
  `SECRET_KEY` is a published placeholder and startup rejects it in every environment — generate one
  with `openssl rand -hex 32`. Same shape elsewhere: no `CONTACT_FORM_EMAIL` default (the endpoint
  503s unset), no `ADMIN_EMAIL` default and the line commented out (unset means the first-superuser
  script creates nothing), no missing `SMTP_HOST` allowed on any `ENVIRONMENT` but `local`, admin
  panel commented out. See *"The config template stopped being a working configuration"* in
  `DECISIONS.md` before adding a setting that can be silently wrong.

- Runs in: Docker Compose — `db` (postgres), `redis`, `api`, `worker` (arq), and `admin_init`
  (one-shot, seeds the admin panel before `api` starts)

- **Two compose files, and they are not variants of each other.** The root `docker-compose.yml` is
  for development: it builds from `./src`, bind-mounts it, and publishes the API and Postgres on the
  host. `deploy/docker-compose.yml` is what people install — pulled images, digest-pinned
  third-party ones, a bundled Caddy under the `proxy` profile, and nothing published but 80/443. It
  is uploaded as a release artifact along with `deploy/Caddyfile` and `deploy/example.env`, so a
  change to any of the three ships on the next tag. Anything that changes how the app is
  *configured* (a new required setting, a renamed one, a new service) has to be made in both, and
  `docs/self-hosting/` is where an installer reads about it. Nothing that runs from the published
  image may depend on `src/` being present — only the installed `app` package and `migrations/` are
  in it.

- **Schema changes**: every one ships an Alembic revision. `alembic upgrade head` runs in the API's
  lifespan (`core/setup.py`), so a schema change reaches a database — yours or a self-hoster's — by
  the container starting, and nothing else. `create_all()` is gone from the app; the only place it
  survives is `tests/conftest.py`, which builds the test schema straight from the models. To change
  the schema:

  1. Add the field to the model in `src/app/models/` and the schema in `src/app/schemas/`.
  2. `cd src && uv run alembic revision --autogenerate -m "what changed"`, then **read the generated
     file** — autogenerate is a first draft, not an answer, and it does not see a data backfill at
     all.
  3. `docker compose restart api` to apply it — `src/migrations` is bind-mounted, so the new
     revision is already inside the container and the lifespan runs it. (`restart` reuses the
     existing container, so it is *not* enough after editing `docker-compose.yml`; that needs
     `docker compose up -d api`. After editing the **`Dockerfile`** bare `up -d` is not enough
     either — it recreates the container from the *stale* image, so anything the Dockerfile newly
     creates is simply absent. That needs `docker compose up -d --build`, and the case that proves
     it is `/data/files`: a named volume copies the image directory's ownership on first mount, so
     mounting over a path the stale image lacks gives you a root-owned volume and a uid-1000
     container that fails its own writability check.)

  `CheckConstraint`s in `__table_args__` are picked up by autogenerate like anything else — the
  hand-written `ALTER TABLE ... ADD CONSTRAINT` era is over. CI fails a PR whose models and
  revisions disagree (`alembic check`). See *"Migrations run on startup, and every schema change
  ships one"* in `DECISIONS.md`, and *"Schema changes have no migration tool"* for the era before it
  — a database created then needs one `alembic stamp head` plus an `alembic check`, per
  `CONTRIBUTING.md`.

- **Auth flow**: Magic-link sign-in URLs appear in `docker compose logs api` (not emailed).
  Copy/paste URL into browser.

- **SMTP_HOST**: Unset locally; sign-in links go to logs instead of email.

- **Admin panel**: CRUDAdmin at `/admin` (`CRUD_ADMIN_MOUNT_PATH`), **off by default** — set
  `CRUD_ADMIN_ENABLED=true` plus `ADMIN_USERNAME`/`ADMIN_PASSWORD` in `src/.env` to use it. The
  `admin_init` compose service creates its tables and seeds the first admin before `api` starts, and
  no-ops when the panel is disabled. Set `CRUD_ADMIN_DB_URL` as well: left unset, the panel's SQLite
  file is created relative to the one-shot container's working directory, where `api` cannot read it
  — the panel then looks initialized but every login fails.

- **Commits are signed, and turning that off is not a shortcut.** `commit.gpgsign` is on and signing
  needs no passphrase prompt on this machine, so disabling it on the git command line - the move an
  agent reaches for when it fears a hanging commit - buys nothing and leaves a PR whose every commit
  reads *Unverified* on GitHub. Two guards say so: a `PreToolUse` hook
  (`.claude/hooks/no-unsigned-commits.py`) rejects the command before it runs, and
  `.githooks/pre-push` rejects the push. Enable the second once per clone with
  `git config core.hooksPath .githooks`. If signing genuinely fails, report the error instead of
  routing around it. See *"Signing is enforced by two local hooks, because GitHub cannot do it yet"*
  in `DECISIONS.md`.

Test, lint, format and type-check commands are in `CONTRIBUTING.md`.

## Constraints

Reads are Redis-cached throughout the API. Below is what to watch for when adding an endpoint — the
reasoning and the exact remedies live in `DECISIONS.md`, and this list exists so you know there's
something to look up, not to restate it.

- **Auth before cache.** An ownership check inside a `@cache`-decorated function never runs on a hit
  — that's an IDOR, not a slow path. → *"`@cache` and per-request authorization don't mix directly"*
- **Cache keys stay user-scoped** (`user_{id}_...`) — pattern invalidation depends on it.
- **Mutations invalidate whatever *embeds* the record**, not just the record. →
  `services/cache_invalidation.py`
- **New per-user owned resource → `OwnedResourceCache`**; hand-roll only for reads that enrich rows
  with a second query. → its class docstring, which names the three that opt out
- **New update schema → `RejectsExplicitNulls`**, listing the fields whose columns are `NOT NULL`.
  Every PATCH field is typed `T | None`, so without it an explicit `null` reaches the UPDATE and
  comes back as a 500. → *"Update schemas refuse an explicit null for a `NOT NULL` column"*
- **List endpoints clamp pagination.** → `clamp_pagination`
- **Binary reads use `ETag`/`If-None-Match` → 304.**
- **Uploaded payloads go on the files volume, never in a column.** `services/blob_store.py` is the
  only module that touches a filesystem; keys come from `new_key` (a fresh nonce per write, never
  derived from the row) and are stored on the row. The ordering rule is not optional: write the
  file, *then* commit the row; delete the row, *then* unlink after the commit
  (`delete_after_commit`). → *"File payloads live on the files volume, not in Postgres"*

## Code Style — Python

- **Python 3.14** (`requires-python = "~=3.14.0"`, ruff `target-version = "py314"`). `except A, B:`
  without parentheses is PEP 758, not the Python 2 form — `ruff format` *removes* the parentheses,
  so putting them back fails CI. → *"`except ValueError, TypeError:` is valid, and `ruff format`
  writes it that way"*
- 4-space indent
- `snake_case` for functions, variables, files
- `PascalCase` for classes
- Type hints on all functions (e.g., `def create_user(name: str) -> User:`)
- Docstrings explain *why*, not the signature — the type hints already document shape, so
  `Args:`/`Returns:` blocks that restate them are noise. Required on every route handler (FastAPI
  publishes the docstring as the endpoint description in `/openapi.json`) and on non-obvious logic;
  skip them on self-evident helpers.
- Models in `src/app/models/` with SQLAlchemy relationships and indexes
- Dependency injection for database sessions and auth

## API Conventions

- Paths: kebab-case under `/api/v1` (e.g., `/api/v1/dive-sites`). Collections are plural (`/dives`),
  single resources singular and keyed by public uuid (`/dive/{uuid}`).
- Methods: GET (read), POST (create), PATCH (partial update), DELETE (remove). PUT only for
  idempotent whole-slot replace — currently just `PUT /dive/{uuid}/file` and
  `PUT /certification/{uuid}/file/{side}`.
- Response: the resource schema directly (`response_model=DiveRead`) — no envelope. Success is
  signalled by the status code.
- Errors: FastAPI's `{ "detail": "message" }`; 422 returns `detail` as an array of per-field
  objects. Raise the classes in `core/exceptions/http_exceptions.py` rather than building responses
  by hand. The frontend normalizes both shapes via `getApiErrorMessage`.
- Status codes: 200 (success), 201 (created), 304 (ETag match on file/profile reads), 400 (bad
  request), 401 (unauthenticated), 403 (the caller named *themselves* wrongly — a body or query
  `user_uuid` that isn't the token's; **not** "someone else's resource", which is a 404), 404 (not
  found, *or* found but not the caller's — see *"Ownership checks go through one
  `fetch_owned_or_raise`"* in `DECISIONS.md`), 409 (conflict, e.g. a dive file already linked to
  another dive), 413 (upload over the size limit), 415 (unrecognized dive-computer export), 422
  (validation failure), 500 (server error)
- Paginated endpoints: FastCRUD's `PaginatedListResponse` —
  `{ data: [...], total_count: number, has_more: boolean, page: number, items_per_page: number }`.
  Page/size, not limit/offset; clamp the query params with `clamp_pagination` (see
  `core/utils/pagination.py`).

Changing the API contract? Say so in the PR — the web and iOS clients may need a matching change.
