# OpenDiving API

**The backend for [OpenDiving](https://github.com/opendiving/opendiving-web) — a self-hosted,
open-source dive log.** FastAPI + PostgreSQL + Redis, one `docker compose up` away.

Your dive history should outlive any app. This API keeps it in your own Postgres database, stores
the original dive-computer export alongside every imported dive, and serves it all over a clean,
documented REST API — so your data is never more than one `curl` away.

## Install it

One command, once you have edited six values — a domain, a key, a database password and a mail
relay:

```bash
mkdir opendiving && cd opendiving
curl -LO https://github.com/opendiving/opendiving-api/releases/latest/download/docker-compose.yml
curl -LO https://github.com/opendiving/opendiving-api/releases/latest/download/Caddyfile
curl -Lo .env https://github.com/opendiving/opendiving-api/releases/latest/download/example.env
$EDITOR .env
docker compose up -d
```

That is the whole product — the API, the web app, PostgreSQL, Redis, the background worker, and
Caddy terminating TLS with a certificate it gets itself. Prebuilt images for amd64 and arm64, so a
Raspberry Pi runs the same bytes as a VPS; migrations apply themselves on startup, so an upgrade is
`docker compose pull && docker compose up -d`; and a backup is two artifacts — a `pg_dump` and a tar
of the files volume the uploaded dive-computer exports and c-card images live on.

| Guide                                                   |                                                |
| ------------------------------------------------------- | ---------------------------------------------- |
| [Install](docs/self-hosting/install.md)                 | The four commands, what you need, what starts  |
| [Configuration](docs/self-hosting/configuration.md)     | Every setting, grouped — and which six matter  |
| [Reverse proxy](docs/self-hosting/reverse-proxy.md)     | Bring your own, or run on a LAN with no domain |
| [Backup & restore](docs/self-hosting/backup-restore.md) | The dump, the files volume, and the drill      |
| [Upgrade](docs/self-hosting/upgrade.md)                 | Pull, up, done — and the stance on downgrades  |
| [Troubleshooting](docs/self-hosting/troubleshooting.md) | Certificates, mail, rate limits, starting over |

The bundle itself is [`deploy/`](deploy/) in this repository; releases publish those three files as
artifacts, which is what the `curl` lines above fetch.

## What it does

- **Dive log API** — dives with gas mixtures (O₂/He, pressures), multiple ordered dive sites per
  dive, trips, weights, water type, altitude, and notes. Soft deletes throughout.
- **Technical diving** — per-cylinder ppO₂ limits and gas roles, CNS/OTU and surface pressure
  persisted from imports, and per-tank gas consumption derived from recorded gas switches on
  multi-tank dives.
- **Dive-computer file parsing** — upload a FIT file (Garmin Descent, Suunto Ocean/D5) or a Suunto
  XML/JSON export to `POST /dive/parse` and get structured dive data back to pre-fill a form. Attach
  the file to the dive afterwards and the **per-sample profile** (depth, temperature, tank pressure,
  **deco ceiling, dive events**) is extracted server-side and served with ETag caching.
- **Air consumption** — SAC and RMV derived automatically, per tank on multi-tank dives, plus a
  gas-use history endpoint powering the dashboard trend chart.
- **Gear** — items, gear sets with default weights, service **schedules** (by months and/or dives),
  service history, a due-soon endpoint, and a scheduled email reminder digest.
- **Certifications** — c-card records with front/back card images.
- **Full export** — everything out in open formats (UDDF, CSV, and a complete JSON + original-files
  archive) in one request. Owner-only, never cached; the UDDF validates against the 3.2.2 schema.
- **Passwordless auth** — email sign-in (over SMTP, so any relay or provider works), Google Sign-In,
  and passkeys, with automatic account linking, short-lived access tokens, and httpOnly refresh
  cookies. The sign-in email carries a magic link *and* a six-digit code, so reading your mail on a
  different device than you started on still works. A passkey is a third *first* factor rather than
  a second one — unphishable and one tap, and the only method needing no third-party service at all.
  No password storage at all. The full design, with sequence diagrams, is in
  [docs/authentication.md](docs/authentication.md).

## Planned

- **More parsers** — Subsurface XML and UDDF (which also admits Apple Watch dives via Oceanic+'s
  UDDF export), then Shearwater Cloud exports; a pluggable importer layer so every supported format
  is a migration path in.
- **Public share links** — read-only dive/trip pages.
- **Statistics endpoints** — records, per-year aggregates, site maps, species log.

## Running it from source

For working on it, rather than for running it — this compose file builds from `./src`, mounts it for
live reload, and publishes the API and Postgres on the host. To *use* OpenDiving, install it with
the bundle above instead.

```bash
git clone https://github.com/opendiving/opendiving-api.git
cd opendiving-api
cp src/.env.example src/.env
openssl rand -hex 32           # put this in SECRET_KEY, and change POSTGRES_PASSWORD
docker compose up
```

That's the backend: the API on [http://localhost:8000](http://localhost:8000) (interactive docs at
`/docs`), PostgreSQL, Redis, and an [arq](https://arq-docs.helpmanual.io/) worker for emails and
scheduled jobs. Pair it with [opendiving-web](https://github.com/opendiving/opendiving-web) for the
frontend.

### Configuration

An *installed* instance is configured through the `.env` beside its compose file —
[docs/self-hosting/configuration.md](docs/self-hosting/configuration.md) is the reference for that
one. From source, everything is configured through `src/.env`, which starts as a copy of
[`src/.env.example`](src/.env.example) — every setting is listed and commented there. `SECRET_KEY`
is not optional: it signs every token the API issues, the template's value is published in this
repository, and the app **refuses to start** on it rather than letting a deployment run on a key
anyone can read. Change `POSTGRES_PASSWORD` too. The parts worth knowing about:

```bash
# Magic-link emails go out over SMTP. Leave SMTP_HOST unset for local development -
# the sign-in link is then only logged, not emailed. Point this at any relay you
# trust (your provider, your host's, your own); don't run your own MTA unless you
# already know why. Resend users: smtp.resend.com, username "resend", password = the
# API key. EMAIL_FROM_ADDRESS is required as soon as SMTP_HOST is set - startup
# fails without it rather than letting an undeliverable address surface hours later.
# SMTP_HOST itself is required on any ENVIRONMENT but local: sign-in is passwordless,
# so a deployed instance with no relay cannot let anybody in at all, and the logged-link
# fallback would put a working sign-in link in whatever collects that instance's logs.
SMTP_HOST="smtp.example.com"
SMTP_PORT=587
SMTP_TLS_MODE="starttls"       # starttls (587) | tls (465) | none (a local relay only)
SMTP_USERNAME="..."            # both optional: an anonymous relay needs neither
SMTP_PASSWORD="..."
EMAIL_FROM_ADDRESS="noreply@yourdomain.example"

# Used to build magic-link URLs ({FRONTEND_URL}/auth/verify?token=...)
FRONTEND_URL="http://localhost:3000"

# Where the frontend's contact form (POST /api/v1/contact) delivers to. No default:
# unset, that endpoint answers 503 and the form is simply off, which beats mailing
# your users' support requests to somebody else's inbox.
CONTACT_FORM_EMAIL="you@example.com"

# Optional: Google Sign-In (must match the frontend's NEXT_PUBLIC_GOOGLE_CLIENT_ID)
GOOGLE_CLIENT_ID="your-client-id.apps.googleusercontent.com"
```

Token lifetimes, rate limits, and the rest have sensible defaults — they're in `src/.env.example`
commented out, and `src/app/core/config.py` is the authoritative list.

#### Reading outgoing mail locally

With `SMTP_HOST` unset, nothing is sent and the sign-in link is written to `docker compose logs api`
— that's the intended local flow and it needs nothing running. To see the mail itself rendered
instead, bring up the opt-in [Mailpit](https://mailpit.axllent.org/) profile and point the app at
it:

```bash
docker compose --profile mail up      # inbox at http://localhost:8025
```

```bash
SMTP_HOST="mailpit"
SMTP_PORT=1025
SMTP_TLS_MODE="none"
```

Comment `SMTP_HOST` back out when you're done: left set with no Mailpit running, sends fail against
a dead host and no link is logged either. Either way, an edit to `src/.env` needs
`docker compose up -d --force-recreate api` to take effect — a plain `restart` reuses the
environment the container was created with.

## API overview

Everything is mounted under `/api/v1`, and the interactive OpenAPI docs at `/docs` are the reference
for it — generated from the routes themselves, so they describe the API as it actually is rather
than as a list here last remembered it.

The families: **auth** (email link or six-digit code, Google, passkeys, refresh, sign-out, account
restore) and **user** (profile, avatar, email change, dive statistics, gas-use history, account
deletion); **dives**, the bulk of it, with their **files** — upload an export, read the per-sample
profile back — alongside **trips**, **dive sites**, the shared **species** catalog a dive can
reference, and a **geocoding** helper for naming a site pinned on a map; **gear** as items, sets,
service schedules and service records; **certifications** with their card images; and **export** in
UDDF, CSV or full-archive form. All of those want a bearer token. The ones that don't are
**contact**, the auth routes themselves, and the two health checks — `/health` says the process is
up, `/health/ready` says Postgres and Redis answered, and 503s when they didn't.

A typical import flow:

```bash
# 1. Parse an export to pre-fill the dive form
curl -X POST http://localhost:8000/api/v1/dive/parse \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@Dive_2026-04-17.json"

# 2. Create the dive, then attach the original file to it (extracts the profile)
curl -X PUT http://localhost:8000/api/v1/dive/{uuid}/file \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@Dive_2026-04-17.json" -F "file_token=$FILE_TOKEN"

# 3. The per-sample profile, served with an ETag
curl http://localhost:8000/api/v1/dive/{uuid}/profile -H "Authorization: Bearer $TOKEN"
```

## Development notes

- [CONTRIBUTING.md](CONTRIBUTING.md) — setup, the checks CI runs, and how to add a dive-computer
  parser.
- [SECURITY.md](SECURITY.md) — how to report a vulnerability privately. Please don't open a public
  issue for one.
- `docs/authentication.md` — the auth design, with sequence diagrams for every flow.
- `DECISIONS.md` — non-obvious choices and gotchas (schema-change workflow, check constraints,
  caching strategy…). Read it before your first PR.
- Schema changes ship an Alembic revision and are applied on startup. A dev database created before
  migrations landed needs one `alembic stamp head` — see [CONTRIBUTING.md](CONTRIBUTING.md).
- Tests: `uv run pytest`, or containerised —
  `docker compose -f docker-compose.yml -f docker-compose.test.yml up --build --abort-on-container-exit api`.
  `docker-compose.test.yml` is an overlay and does nothing on its own. Note that the Postgres-backed
  tests skip themselves unless the test process can reach the database; see
  [CONTRIBUTING.md](CONTRIBUTING.md).
- Re-extract profiles after a parser fix:
  `docker compose exec api python -m src.scripts.backfill_dive_profiles --parser-key suunto_xml`

## Related repositories

|                                                                |                                      |
| -------------------------------------------------------------- | ------------------------------------ |
| [opendiving-web](https://github.com/opendiving/opendiving-web) | Next.js frontend (screenshots there) |
| [opendiving-ios](https://github.com/opendiving/opendiving-ios) | SwiftUI app (early scaffold, parked) |

## License

[AGPL-3.0](LICENSE). Run it, change it, self-host it freely — but if you offer a modified version as
a service, you share your changes. Divers' data stays free.
