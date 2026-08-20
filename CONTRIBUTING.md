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
cp src/.env.example src/.env
openssl rand -hex 32           # SECRET_KEY; the rest of the defaults are fine locally
docker compose up
```

That gives you the API on <http://localhost:8000> (docs at `/docs`), PostgreSQL, Redis, and the arq
worker. Leave `SMTP_HOST` unset locally — magic-link sign-in URLs are then written to the API logs
instead of emailed, which is what you want for development.

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

If your PR touches `src/app/models/`, add the migration drift check — CI runs it and the suite does
not (see *Things that will bite you* below):

```bash
cd src && POSTGRES_SERVER=localhost uv run alembic check
```

Note that lint and type-checking cover `tests/` and the build-time `scripts/` as well as `src/`.
mypy runs as three separate invocations, for two different reasons. `src` and `tests` are split
because the app is importable as both `app.*` (via `mypy_path`) and `src.app.*` (how the tests
import it), and asking it to check both roots at once fails with "source file found twice under
different module names". `scripts` is third only because it sits under neither root — it imports
nothing from the app, and needs no environment either.

mypy and pytest need `ENVIRONMENT=local` and a `SECRET_KEY` in the environment. Any value except a
placeholder: `Settings._reject_placeholder_secret_key` rejects the one `src/.env.example` ships and
a handful of stock stand-ins (`changeme`, `secret`, …), in every environment, so a deployment cannot
run on a published key. CI uses a throwaway `test-secret-key-for-testing-only`, which is fine — the
guard is a list of known placeholders, not an entropy check.

**The suite runs without a database.** Almost every test mocks the session (`mock_db`), so a cold
checkout with nothing else running gives you a green run in under a second. The exceptions are the
modules that insert real rows through a session to verify what Postgres itself settles — the
constraints it enforces, the window functions a renumber runs, whether a query filtered by `user_id`
at all. They share one `skipif(not db_available())` guard from `tests/conftest.py`, so with no
database reachable they **skip silently** rather than fail — and that shared guard is also how you
find them: `grep -rl 'skipif(not db_available' tests/`. Deliberately not listed by name here, and
not counted either, because the set grows with nearly every change to the suite and a list in a doc
is the copy that stops being true. (Grep the guard, not the bare `db_available` — that also matches
`conftest.py`, which defines it rather than being one of them.)

That matters if you touch `models/` or add a `CheckConstraint`: your local run can be green because
the tests that would have caught you never executed.

**Bringing the stack up is not enough on its own**, and this is the trap worth knowing. The skip is
a connection attempt against `POSTGRES_SERVER`, which `src/.env` sets to `db` — the *compose*
hostname, which does not resolve on the host. So `docker compose up` followed by `uv run pytest` on
the host still skips every one of them, no matter that Postgres is up and its port is published.
Point the host run at the published port instead:

```bash
POSTGRES_SERVER=localhost ENVIRONMENT=local SECRET_KEY=testsecret uv run pytest -q
```

That is the difference between a run ending `… passed, … skipped` and one ending `… passed` with no
skip line at all. **The skip line is the whole signal** — the totals themselves say nothing, since
they move with every test added, and this is the only thing on screen that tells you which of the
two runs you just did. CI sets exactly that variable and fails the job if anything skips (see
below), so this is about getting the answer before you push rather than after — but the skip is
silent and a green local run looks identical either way, so it is easy to spend a review round
believing those tests ran.

Alternatively use the containerised suite, where `db` resolves and nothing needs overriding — note
that `docker-compose.test.yml` is an *overlay*, so it has to be passed alongside the base file
rather than on its own:

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml up --build --abort-on-container-exit api
```

CI does run Postgres and Redis as service containers, and fails the job if *any* test skips — so
unlike before, a green CI run means the database-backed ones actually executed. The run passes
`-rs`, so a job that fails this way names the tests that skipped rather than only counting them.

There is no allowlist: the check is a grep over pytest's counts line, which cannot tell a deliberate
skip from an unreachable database. The suite has no deliberate skips today, and adding the first one
means reworking that step in `.github/workflows/tests.yml` — against the `SKIPPED` lines `-rs`
prints, say — rather than filling in a slot that already exists.

Two things to know when you do run them against a live database: they write to whatever `POSTGRES_*`
resolves to — your dev database, by default — and the `create_user` helper commits a row per test
that nothing cleans up afterwards, so expect a scattering of faker-named users to accumulate.

Ruff is configured with `fix = true`, so `uv run ruff check src tests scripts` will repair what it
can on its own, and `uv run ruff format src tests scripts` handles the rest. Line length is 120.
Everything under `app.*` is type-checked with `disallow_untyped_defs` — new functions need
annotations.

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
uses, so it reports a missing argument for every optional field a test omits — 108 false positives
when the suite was first pointed at mypy, which is what the exemption exists for. Every other error
code still applies. See `DECISIONS.md`.

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

## Things that will bite you

**Every schema change ships an Alembic revision.** The API runs `alembic upgrade head` in its
lifespan, so your change reaches a database — yours, CI's, or a self-hosted instance's — by the
container starting. Adding a column means:

```bash
cd src && POSTGRES_SERVER=localhost uv run alembic revision --autogenerate -m "add dive.deco_model"
```

`POSTGRES_SERVER=localhost` for the same reason the test suite needs it — `src/.env` names the
compose hostname `db`, which does not resolve on the host — and autogenerate needs a live database
to compare the models against, so the stack has to be up. Read the file it writes before committing
it. Autogenerate compares the models against the database it connects to and is a first draft: it
cannot see a data backfill, it renders a rename as a drop plus an add, and it will happily write
`DROP TABLE` for anything in your database that the models no longer describe. Then
`docker compose restart api` applies it (`src/migrations` is bind-mounted, so the new revision is
already inside the container).

CI fails the PR if the models and the revisions disagree — it runs `alembic upgrade head` against an
empty database and then `alembic check`, which autogenerates again and fails on any difference. That
is also the fastest local check that you generated everything you needed:

```bash
cd src && POSTGRES_SERVER=localhost uv run alembic check
```

**A database that predates migrations needs stamping, once.** Anything created by the old
`create_all()` workflow — every dev database from before this landed — has the tables but no
`alembic_version`, so `upgrade head` tries to create them again and the API fails to start. Tell
Alembic it is already current, then verify that claim:

```bash
cd src
POSTGRES_SERVER=localhost uv run alembic stamp head
POSTGRES_SERVER=localhost uv run alembic check
```

The `check` is the part not to skip. The old workflow applied `ALTER TABLE`s by hand from PR
descriptions, so any given machine may be missing a constraint nobody ran, or still carrying a
column that was dropped from the models — a stamp asserts a state nobody verified, and
migrate-on-start will never notice afterwards. Apply whatever `check` reports by hand, or wipe the
database and let the migrations build it: `docker compose down -v && docker compose up`.

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
  working. (Older history mixes prefixed and plain subjects; new PRs need the prefix.) The same
  workflow puts the type on the PR as a label, which is what files it under the right heading when a
  release's notes are generated — so a retitle is all it takes to move it.
- Say in the description what you changed, why, and anything a reviewer has to do by hand (schema
  SQL, new env vars, a backfill script).
- If the change affects the API contract, mention whether the web or iOS client needs a matching
  change.

## Cutting a release

Releases are cut deliberately, never minted per merge. A version is an event self-hosters read
before they pull, and a stream of releases whose notes are one PR title each trains people onto
`latest` — the tag you least want someone following. Accumulate until the window tells a coherent
story, until a fix somebody is waiting on lands, or until anything needs a pinnable reference:
pre-launch that means "whenever useful", after launch expect every one to four weeks.

Versions move in lockstep with [opendiving-web](https://github.com/opendiving/opendiving-web): one
product version, tagged in both repos, so `opendiving-api:0.4.0` and `opendiving-web:0.4.0` are
always a matched pair. That is also why no release tool runs here — semantic-release and
release-please both compute a version per repo from that repo's own commits, which drifts apart on
the first api-only fix and then has to be forced back by hand at every release afterwards.

**Pick the number** by looking at both repos' windows together:

| The window contains                                                                            | Pre-1.0 | From 1.0.0 |
| ---------------------------------------------------------------------------------------------- | ------- | ---------- |
| Anything breaking — a changed config or env contract, removed behaviour, a manual upgrade step | minor   | major      |
| Any user-visible feature                                                                       | minor   | minor      |
| Fixes and internals only                                                                       | patch   | patch      |

"Breaking" is about the operator's experience, not the code's — which is why a schema change on its
own is *not* breaking: migrations run themselves on startup, and the upgrade drill is what keeps
that claim honest. What counts is anything the operator has to do by hand before the new version
will run: an edited `.env`, a changed config contract, a removed behaviour they depended on.

Every PR title is a conventional commit subject, so the breaking half of that table has a scanner:

```bash
git log --format=%s v0.3.0..main | grep -E '^[a-z]+(\([^)]+\))?!:'
```

Then, in both repos:

1. Bump `version` in `pyproject.toml` (and `package.json` in the web repo) — one small PR each,
   titled `chore: release v0.4.0`. Nothing else carries a version number: the API reads its own from
   the installed package metadata.

2. Tag the bump commit and push the tag:

   ```bash
   git tag v0.4.0 && git push origin v0.4.0
   ```

3. The tag push runs **Publish Image**, which builds amd64 and arm64 on native runners and pushes
   `0.4.0`, `0.4`, `latest` and `sha-<12>` — plus the bare major (`1`, `2`, …) once this is past
   1.0.0, which is withheld below it because a `0` alias would read as "any 0.x". The tag has to be
   exactly `vX.Y.Z`: pre-releases and other shapes have no alias story here and are refused. A tag
   whose name disagrees with the manifest version is refused the same way, before anything is built
   — so nothing was published, and the fix is to delete the tag, correct the bump, and re-cut it.

4. The api workflow opens a **draft** release with generated notes, and attaches
   `deploy/docker-compose.yml`, `deploy/Caddyfile` and `deploy/example.env` to it — those three
   files *are* the install, and `releases/latest/download/<name>` is the URL the docs tell people to
   `curl`. Check they are there. Write the headline paragraph and confirm the **Breaking** section:
   say "None" in so many words when it is empty, because generated notes simply omit an empty
   category and silence is not an answer someone deciding whether to upgrade can use. A change to
   the deploy bundle that an existing install has to copy — a new required variable, a new service —
   belongs in that section, since `docker compose pull` does not update the compose file.

5. Before publishing, check that `ghcr.io/opendiving/opendiving-web:0.4.0` exists, or that its
   workflow is green. Tagging the api and forgetting the web repo breaks every pinned install of
   that version, and no per-repo check can catch it.

6. Publish. The web repo's release is plumbing — self-hosters read this one — so it can go out as
   generated.

The first release cut this way is `v0.2.0`. Both manifests already read `0.1.0`, and `v0.1.0` is
spoken for: it is the clock-starter for
[awesome-selfhosted](https://github.com/awesome-selfhosted/awesome-selfhosted-data)'s
first-released-more-than-four-months-ago rule, not something anyone is meant to install. With no
earlier *release* to generate notes against, that first draft enumerates the whole history — throw
it away and recreate it with an explicit floor:

```bash
gh release delete v0.2.0 --yes
gh release create v0.2.0 --draft --generate-notes --notes-start-tag v0.1.0
```

**A published version is never repointed.** A bad release gets a successor, not a rewrite.
Immutability starts at *publish*, so a tag whose build failed before pushing anything published
nothing and may be deleted and re-cut. The one sanctioned reason to rebuild a released version is a
CVE in a base image: run **Publish Image** by hand with `ref` set to the `v` tag, and tick **Also
push :latest** if that version is still the newest. One run recomputes the version's whole alias
set, which is the point — a hand-picked subset would leave everyone following `latest` or `0.4` on
the vulnerable digest.

## License

By contributing you agree that your work is licensed under [AGPL-3.0](LICENSE), same as the rest of
the project.
