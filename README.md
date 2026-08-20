# OpenDiving API

**The backend for [OpenDiving](https://github.com/opendiving/opendiving-web) — a self-hosted,
open-source dive log.** FastAPI + PostgreSQL + Redis, one `docker compose up` away.

Your dive history should outlive any app. This API keeps it in your own Postgres database, stores
the original dive-computer export alongside every imported dive, and serves it all over a clean,
documented REST API — so your data is never more than one `curl` away.

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
- **Passwordless auth** — email magic links (over SMTP, so any relay or provider works) and Google
  Sign-In, with automatic account linking, short-lived access tokens, and httpOnly refresh cookies.
  No password storage at all. The full design, with sequence diagrams, is in
  [docs/authentication.md](docs/authentication.md).

## Planned

- **More parsers** — Subsurface XML and UDDF (which also admits Apple Watch dives via Oceanic+'s
  UDDF export), then Shearwater Cloud exports; a pluggable importer layer so every supported format
  is a migration path in.
- **Self-hosting hardening** — a single compose bundle including the web app and TLS, prebuilt
  images, and Alembic migrations before 1.0 (schema changes are currently applied manually during
  prototyping — see [DECISIONS.md](DECISIONS.md)). Email is already vendor-free: it goes out over
  plain SMTP, so any relay works.
- **Public share links** — read-only dive/trip pages.
- **Statistics endpoints** — records, per-year aggregates, site maps, species log.

## Quickstart

```bash
git clone https://github.com/opendiving/opendiving-api.git
cd opendiving-api
cp src/.env.example src/.env
openssl rand -hex 32           # put this in SECRET_KEY, and change POSTGRES_PASSWORD
docker compose up
```

That's the whole stack: the API on [http://localhost:8000](http://localhost:8000) (interactive docs
at `/docs`), PostgreSQL, Redis, and an [arq](https://arq-docs.helpmanual.io/) worker for emails and
scheduled jobs. Pair it with [opendiving-web](https://github.com/opendiving/opendiving-web) for the
frontend.

### Configuration

Everything is configured through `src/.env`, which starts as a copy of
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
# SMTP_HOST itself is required on ENVIRONMENT=production: sign-in is passwordless,
# so a production instance with no relay cannot let anybody in at all.
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

All endpoints are mounted under `/api/v1`; the live OpenAPI docs at `/docs` are the source of truth.

| Area           | Endpoints                                                                                                      |
| -------------- | -------------------------------------------------------------------------------------------------------------- |
| Auth           | `/auth/email/request`, `/auth/email/verify`, `/auth/google`, `/auth/complete`, `/auth/refresh`, `/auth/logout` |
| User           | `/user`, `/user/dive-stats`, `/user/gas-use-history`, email-change flow                                        |
| Dives          | `/dive`, `/dives`, `/dive/parse`, `/dive/{uuid}/file`, `/dive/{uuid}/profile`                                  |
| Trips & sites  | `/trip(s)`, `/dive-site(s)`                                                                                    |
| Gear           | `/gear-item(s)`, `/gear-set(s)`, `/gear-service-schedule(s)`, `/gear-service-record(s)`, `/gear-service-due`   |
| Certifications | `/certification(s)`, `/certification/{uuid}/file/{side}`                                                       |
| Export         | `/export/uddf`, `/export/csv`, `/export/archive` — the caller's whole logbook, owner-only, never cached        |
| Contact        | `/contact` (unauthenticated, rate-limited; forwards to `CONTACT_FORM_EMAIL`)                                   |

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
- `docs/authentication.md` — the auth design, with sequence diagrams for every flow.
- `DECISIONS.md` — non-obvious choices and gotchas (schema-change workflow, check constraints,
  caching strategy…). Read it before your first PR.
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
