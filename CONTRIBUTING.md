# Contributing to OpenDiving API

Thanks for wanting to help. Bug reports, a fix for a typo in a docstring, a parser for a dive
computer nobody has covered yet — all welcome.

For anything bigger than a small fix, **open an issue first** so we can agree on the shape before
you spend an evening on it. That is especially true for changes to the database schema or the public
API surface, since [opendiving-web](https://github.com/opendiving/opendiving-web) and
[opendiving-ios](https://github.com/opendiving/opendiving-ios) consume it.

Participation is covered by our [Code of Conduct](CODE_OF_CONDUCT.md).

## Getting set up

The whole stack runs from Docker:

```bash
git clone https://github.com/opendiving/opendiving-api.git
cd opendiving-api
cp src/.env.example src/.env   # defaults are fine for local work
docker compose up
```

That gives you the API on <http://localhost:8000> (docs at `/docs`), PostgreSQL, Redis, and the arq
worker. Leave `RESEND_API_KEY` unset locally — magic-link sign-in URLs are then written to the API
logs instead of emailed, which is what you want for development.

For running the tooling (ruff, mypy, pytest) outside the container you need Python 3.14 and
[uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra dev
```

## Before you open a PR

Three workflows run on every pull request, and all must be green. Run them locally first:

```bash
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run mdformat --check *.md docs
uv run mypy src --config-file pyproject.toml
uv run mypy tests --config-file pyproject.toml
uv run mypy scripts --config-file pyproject.toml
uv run pytest --cov=src/app --cov-report=term-missing
```

Note that lint and type-checking cover `tests/` and the build-time `scripts/` as well as `src/`.
mypy runs as three separate invocations on purpose: the app is importable as both `app.*` (via
`mypy_path`) and `src.app.*` (how the tests import it), and asking it to check both roots at once
fails with "source file found twice under different module names".

mypy and pytest need `ENVIRONMENT=local` and a `SECRET_KEY` in the environment (any value — CI uses
a throwaway one).

**The suite runs without a database.** Almost every test mocks the session (`mock_db`), so a cold
checkout with nothing else running gives you a green run in under a second. The exception is
`tests/test_dive_check_constraints.py`, which inserts real rows through a sync session to verify the
constraints Postgres actually enforces. It is marked `skipif` on a connection attempt, so with no
database reachable those tests **skip silently** rather than fail.

That matters if you touch `models/` or add a `CheckConstraint`: your local run can be green because
the tests that would have caught you never executed.

**Bringing the stack up is not enough on its own**, and this is the trap worth knowing. The skip is
a connection attempt against `POSTGRES_SERVER`, which `src/.env` sets to `db` — the *compose*
hostname, which does not resolve on the host. So `docker compose up` followed by `uv run pytest` on
the host still skips all 44, no matter that Postgres is up and its port is published. Point the host
run at the published port instead:

```bash
POSTGRES_SERVER=localhost ENVIRONMENT=local SECRET_KEY=testsecret uv run pytest -q
```

That is the difference between `705 passed, 70 skipped` and `775 passed`. The totals move with every
test added and these two will drift; **`70 skipped` against no skip line at all is the part worth
reading**, and it is the only thing on screen that tells you which of the two runs you just did. CI
sets exactly that variable and fails the job if anything skips (see below), so this is about getting
the answer before you push rather than after — but the skip is silent and a green local run looks
identical either way, so it is easy to spend a review round believing those tests ran.

Alternatively use the containerised suite, where `db` resolves and nothing needs overriding — note
that `docker-compose.test.yml` is an *overlay*, so it has to be passed alongside the base file
rather than on its own:

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml up --build --abort-on-container-exit api
```

CI does run Postgres and Redis as service containers, and fails the job if the database-backed tests
skip — so unlike before, a green CI run means they actually executed.

Two things to know when you do run them against a live database: they write to whatever `POSTGRES_*`
resolves to — your dev database, by default — and the `create_user` helper commits a row per test
that nothing cleans up afterwards, so expect a scattering of faker-named users to accumulate.

Ruff is configured with `fix = true`, so `uv run ruff check src tests scripts` will repair what it
can on its own, and `uv run ruff format src tests` handles the rest. Line length is 120. Everything
under `app.*` is type-checked with `disallow_untyped_defs` — new functions need annotations.

The markdown docs are formatted too — drop the `--check` to rewrite them:

```bash
uv run mdformat *.md docs
```

That covers `DECISIONS.md`, which you will be appending to. Write the new section however it comes
out, run the command, and it gets wrapped to 100 columns like the rest; no counting characters by
hand. The settings are in `.mdformat.toml`, and the paths are listed explicitly because `mdformat .`
would walk into `.venv/`.

Docstring style follows the numpy convention by habit, not by enforcement: no `D` rules are enabled,
so nothing checks it.

One mypy quirk in `tests/`: `call-arg` is disabled there. Pydantic's mypy plugin doesn't read
defaults out of `Annotated[T, Field(default=None)]`, which is the form every schema in `app/schemas`
uses, so it reports a missing argument for every optional field a test omits — 108 false positives.
Every other error code still applies. See `DECISIONS.md`.

## How the code is laid out

```
src/app/
  api/v1/      route handlers - request/response only, thin
  crud/        database access (FastCRUD), one module per table
  services/    business logic that doesn't belong in a route
  models/      SQLAlchemy models
  schemas/     Pydantic request/response schemas
  core/        config, security, exceptions, worker, db setup
src/scripts/   one-shot maintenance scripts, run inside the container
scripts/       build-time tooling, run by hand and never shipped
tests/
```

Follow the existing layering: a route validates and authorizes, a service decides, a crud module
talks to the database. If a handler is growing branches, that logic probably wants to be a service.

Tests live in `tests/`, one module per feature area (`test_dives.py`, `test_gear.py`, …). New
endpoints and new parsing behaviour should come with tests; bug fixes should come with a test that
fails without the fix.

## Two things that will bite you

**There are no migrations yet.** `Base.metadata.create_all()` runs on startup and creates *new*
tables, but never alters existing ones. Adding a column means: update the model and schema, restart
the `api` container, then apply the `ALTER TABLE` by hand against your dev database. If your PR
changes the schema, **put the SQL in the PR description** so everyone else can apply it too. The
same goes for new `CheckConstraint`s. Versioned Alembic migrations are on the roadmap before 1.0.

**Read [DECISIONS.md](DECISIONS.md) before your first PR.** It records the non-obvious choices and
the traps already hit — the schema-change workflow above, check-constraint behaviour, the caching
and cache-invalidation strategy, auth details. When you make a decision that would puzzle the next
person, append a section to it as part of your PR. The auth design, with sequence diagrams for every
flow, is in [docs/authentication.md](docs/authentication.md).

## Adding a dive-computer parser

This is the most useful contribution available right now, and it is self-contained:

1. Implement `DiveParser` (`src/app/services/dive_parsers/base.py`) — `can_parse()`, `parse()`, and
   optionally `parse_profile()` if the format carries per-sample data. A parser that recognizes a
   file and then finds it isn't really its format should raise `UnsupportedDiveFileError` from
   `parse()` so the next candidate gets a turn.
2. Register the class in `dive_parsers/__init__.py`, in the order it should be tried.
3. Pick the `key` carefully. It is written to `dive_file.parser_key` on every stored export and is
   part of the data model — renaming one orphans every row already written under the old name.
4. Add tests to `tests/test_dive_parsers.py` with a small anonymised sample file.

Existing profiles can be re-extracted after a parser fix:

```bash
docker compose exec api python -m src.scripts.backfill_dive_profiles --parser-key suunto_xml
```

## Pull requests

- Branch off `main`, keep the PR focused on one thing.
- Title the PR as a conventional commit — `<type>[(scope)][!]: <description>`, e.g.
  `feat: store the dive-computer export a dive was imported from`. Types: `feat`, `fix`, `refactor`,
  `docs`, `test`, `chore`, `perf`, `ci`, `build`, `revert`. A CI check enforces this, and re-runs
  when you edit the title, so a rejected PR needs no new commit. PRs are squash-merged, so the title
  becomes the commit subject on `main` — commits within your branch can say whatever is useful while
  working. (Older history mixes prefixed and plain subjects; new PRs need the prefix.)
- Say in the description what you changed, why, and anything a reviewer has to do by hand (schema
  SQL, new env vars, a backfill script).
- If the change affects the API contract, mention whether the web or iOS client needs a matching
  change.

## License

By contributing you agree that your work is licensed under [AGPL-3.0](LICENSE), same as the rest of
the project.
