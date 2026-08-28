<!--
Maintainer note. Block-level HTML comments are stripped before this file enters Claude's
context, so this costs nothing to keep here.

The project instructions live in AGENTS.md, imported below, so every coding agent reads one
file instead of a copy that drifts. Claude Code does not read AGENTS.md on its own - the
import is what loads it. Only genuinely Claude-specific instructions belong beneath it. See
"The project instructions live in AGENTS.md" in DECISIONS.md.
-->

@AGENTS.md

## Running the tests

`CONTRIBUTING.md` is the single source of truth for the commands. Two caveats it states that are
easy to miss, both of which make a run *look* clean while proving nothing:

- **mypy runs as more than one invocation** — `CONTRIBUTING.md` says how many and why. `ENVIRONMENT`
  and `SECRET_KEY` must be set.
- **The Postgres-backed tests skip themselves** unless `POSTGRES_SERVER` names a host the *test
  process* can reach. `src/.env` sets it to the compose hostname `db`, so a run on the host skips
  every one of them even with the stack up and the port published; `POSTGRES_SERVER=localhost` is
  what makes them run **in a checkout that has `src/.env`**. A worktree doesn't: the file is
  gitignored, so it never follows a `git worktree add`, and the settings fall back to
  `postgres`/`postgres` against a database named `postgres` while the dev stack uses its own
  password and `opendive`. The override alone then fails on the credentials instead of the host and
  the tests skip identically — copy `src/.env` in first. CI already does this and fails if they
  skip, so the gap is local feedback, not merge safety.
