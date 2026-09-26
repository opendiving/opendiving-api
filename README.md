# OpenDiving API

**The backend component of [OpenDiving](https://github.com/opendiving/opendiving) — an open-source,
self-hostable dive log.** FastAPI + PostgreSQL + Redis.

Your dive history should outlive any app. This API keeps every original dive-computer export
alongside the dive it recorded, serves the lot over a clean, documented REST API, and takes the
whole log back out in open formats in a single request — and back **in** again, so your data is
never more than one `curl` away in either direction. Run your own copy and the Postgres database
underneath it is yours too.

## Looking to run OpenDiving?

**Start at [opendiving/opendiving](https://github.com/opendiving/opendiving).** That is the front
door: the pitch, the four-command install, the release the compose file comes from, and every
self-hosting guide — installing, configuring, reverse proxies, backup and restore, upgrades and
troubleshooting.

This repository is one of the two application containers that install brings up, alongside
[opendiving-web](https://github.com/opendiving/opendiving-web). It publishes an image and nothing
else; what is here is the source, and the notes for working on it.

## What it does

- **Dive log API** — dives with gas mixtures (O₂/He, pressures), multiple ordered dive sites per
  dive, trips, weights, water type, altitude, and notes. Soft deletes throughout.
- **Technical diving** — per-cylinder ppO₂ limits and gas roles, each computer's CNS/OTU and surface
  pressure persisted from imports, and per-tank gas consumption derived from recorded gas switches
  on multi-tank dives.
- **Dive-computer file parsing** — upload a FIT file (Garmin Descent, Suunto Ocean/D5) or a Suunto
  XML/JSON export to `POST /dive/parse` and get structured dive data back to pre-fill a form. Attach
  the file to the dive afterwards and the **per-sample profile** (depth, temperature, tank pressure,
  deco ceiling, **the computer's own no-decompression clock, time to surface, ppO₂, CNS clock and
  both gradient factors**, dive events) is extracted server-side and served with ETag caching.
- **Recordings** — a dive holds what recorded it, in order, and each of those holds its own files
  and its own profile. So a diver on two computers keeps both accounts of the dive, the same
  computer exported twice fills one record rather than making two, and every device the file named —
  brand, model, serial, firmware, its own name and its own dive counter — is kept beside the
  samples, along with **the mode, salinity and decompression model that computer ran with, and its
  CNS, OTU and surface-pressure readouts**, all of which two computers on one dive legitimately
  disagree about. A computer that shut down mid-water logs the dive twice, so two dives can be
  **merged** into one: the two records land on a single axis with the stretch the computer was off
  left empty, and nothing is invented to bridge it.
- **Air consumption** — SAC and RMV derived automatically, per tank on multi-tank dives, plus a
  gas-use history endpoint powering the dashboard trend chart.
- **Species log** — record what you saw on a dive, from a catalog searched live against
  [WoRMS](https://www.marinespecies.org/) for the taxonomy and [Wikidata](https://www.wikidata.org/)
  for the common names — so typing "clownfish" finds *Amphiprion ocellaris*, which WoRMS alone would
  not. Catalog rows are shared across the instance, and search falls back to what is already stored
  rather than failing when a register is unreachable. Distinct species seen is part of the dive
  statistics, and `GET /user/species` is the whole life list: every species you have logged, with
  how many dives saw it and when. The taxonomy is WoRMS's, whose text content is available under
  [CC BY](https://creativecommons.org/licenses/by/4.0/) and which asks to be cited in full: *WoRMS
  Editorial Board (2026). World Register of Marine Species. Available from
  https://www.marinespecies.org at VLIZ. Accessed 2026-09-11. doi:10.14284/170* — an accessed date a
  README cannot keep current, and does not need to, because it stands for no copy of anything:
  **this app queries the register live rather than holding a snapshot of it**, so an instance holds
  whatever WoRMS answered on the days its divers went looking.
- **Species photos** — a freely licensed photograph per species, chosen from
  [Wikimedia Commons](https://commons.wikimedia.org/), fetched **once** and stored by this instance
  itself, so no visitor's browser ever contacts Wikimedia. A species whose candidates cannot be told
  apart gets no photo rather than a picture of a different animal, and the author, licence and
  source travel with it so a credit line can be rendered.
- **Dive-site suggestions** — `GET /dive-sites/suggest` answers from a catalog of real dive sites
  bundled in the image, so the site form can offer "SS Thistlegorm" rather than only the town it is
  near. A geocoder knows where Dahab is, not where the Blue Hole's north entry is. No account, no
  key and no outbound call: the file ships with the app, and picking a suggestion just fills in a
  dive site of your own that you can rename, move and annotate.
- **Gear** — items, gear sets with default weights, service **schedules** (by months and/or dives),
  service history, a due-soon endpoint, and a scheduled email reminder digest.
- **Certifications** — c-card records with front/back card images.
- **Training courses** — the course a card came out of and the dives logged on it, with the agency,
  status, instructor and the contact that ran it. No mainstream logbook models this: the agency apps
  tie dives to a course *or* cards to a course, and none of them let the record outlive the agency.
- **Contacts** — the dive centers, schools, shops, clubs and places you stayed, each kept once with
  its phone, email, website and address and picked for a dive, a course, a card, a gear service or a
  trip part. Imported from UDDF's dive bases, shops and accommodation.
- **Full export** — everything out in open formats (DiveJSON, UDDF, CSV, and an archive of all three
  plus your original files) in one request. Owner-only, never cached; the DiveJSON validates against
  the published 1.0 schema and the UDDF against the 3.2.2 one.
- **[DiveJSON](https://divejson.org)** — the open dive-log interchange format this project
  maintains, and this is its reference implementation: a lossless structured copy of the whole
  logbook, where UDDF measurably loses trips, gear, weights and UTC offsets.
- **Logbook import** — put a whole logbook into an account, in two phases: a preview that reports
  exactly what would be created, linked to something you already have, restored from your deleted
  records or skipped, and then an apply that writes the lot in one transaction. A DiveJSON document
  or a full-export archive goes in as it is; a UDDF file, a Subsurface `.ssrf`, a FIT file, a Suunto
  app export, a Suunto DM5 XML export, or a `.zip` whose files are all one of those is converted on
  the way in by the [`divejson`](https://pypi.org/project/divejson/) package, and the report says
  what the conversion could not carry. Restore a backup, migrate between instances, or bring a
  logbook across from whatever you were keeping it in.
- **Passwordless auth** — email sign-in (over SMTP, so any relay or provider works), Google Sign-In,
  and passkeys, with automatic account linking, short-lived access tokens, and httpOnly refresh
  cookies. The sign-in email carries a magic link *and* a six-digit code, so reading your mail on a
  different device than you started on still works. A passkey is a third *first* factor rather than
  a second one — unphishable and one tap, and the only method needing no third-party service at all.
  No password storage at all. The full design, with sequence diagrams, is in
  [docs/authentication.md](docs/authentication.md).

## Planned

- **More formats in** — a reader is a contribution to the `divejson` package rather than a change
  here, and this app reads whatever the release it pins registers. Shearwater Cloud's whole-database
  export is the one still outstanding, and it is not an adapter's worth of work: the sample data
  sits in the computer's own binary log rather than in readable rows, so getting at it takes a
  dive-computer parser. Shearwater Cloud's UDDF export of the same dives already imports.
- **Public share links** — read-only dive/trip pages.
- **Statistics endpoints** — records, per-year aggregates, site maps, a life list.

## Running it from source

For working on it, rather than for running it — this compose file builds from `./src`, mounts it for
live reload, and publishes the API and Postgres on the host. To *run* an instance rather than
develop against one, install it from
[opendiving/opendiving](https://github.com/opendiving/opendiving) instead.

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
[the configuration reference](https://github.com/opendiving/opendiving/blob/main/docs/configuration.md)
covers that one. From source, everything is configured through `src/.env`, which starts as a copy of
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

# Where the frontend's support form (POST /api/v1/support) delivers to. No default:
# unset, that endpoint answers 503 and the form is simply off, which beats mailing
# your users' support requests to somebody else's inbox.
CONTACT_FORM_EMAIL="you@example.com"

# Optional: Google Sign-In. Both halves of one OAuth client, or neither - an id with no
# secret refuses to start. The id must match the frontend's NEXT_PUBLIC_GOOGLE_CLIENT_ID;
# the secret stays here. Register {FRONTEND_URL}/auth/google/callback as an Authorized
# redirect URI on that client.
GOOGLE_CLIENT_ID="your-client-id.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET="your-client-secret"
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
restore) and **user** (profile, avatar, check-in portrait, the check-in link, email change, dive
statistics, gas-use history, the species life list, account deletion); **dives**, the bulk of it,
with their **recordings** — attach an export, read one recording's per-sample profile back, fold two
dives into one — alongside **trips**, **dive sites**, the shared **species** catalog a dive can
reference, and a **geocoding** helper for naming a site pinned on a map; **gear** as items, sets,
service schedules and service records; **certifications** with their card images and the **courses**
that issued them; **export** in DiveJSON, UDDF, CSV or full-archive form and **import** back from
either of the first and the last, or from any format the converter reads; **invitations**, which
exist only where the operator has closed registration (`REGISTRATION_MODE`, documented with the rest
of the settings in `src/.env.example`) — a member sends and revokes their own, and the routes answer
404 on an open instance; and **admin**, the operator's own — the queue of people who have asked to
be let in, and inviting or removing them in a batch — which is the one family gated on
`is_superuser` rather than merely on having a token. All of those want a bearer token. The ones that
don't are **support**, the auth routes themselves, the two health checks — `/health` says the
process is up, `/health/ready` says Postgres and Redis answered, and 503s when they didn't —
`POST /invite-requests`, which is how somebody with no account asks a closed instance for an
invitation, `GET /config`, which tells the web app whether registration is open - and whether the
project itself operates the instance - before anyone has signed in, `GET /species/{uuid}/photo`,
which serves a public Commons image to an `<img>` tag that has no way to send a token, and
`GET /checkin/{token}` with its `/portrait` and `/certification/{uuid}/front`, the check-in page a
diver shared as a link, where the token in the path is the credential.
`tests/test_route_authentication.py` is the guard that keeps the *anonymous* half of that list
honest — it compares the app's real route table against its own allowlist and holds the reason for
each — but nothing checks this paragraph, so a new route family belongs here by hand.

A typical import flow:

```bash
# 1. Parse an export to pre-fill the dive form
curl -X POST http://localhost:8000/api/v1/dive/parse \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@Dive_2026-04-17.json"

# 2. Create the dive, then attach the original file to it. The response says which
#    *recording* it landed in: a second export of a computer the dive already has
#    fills that record, and a different computer becomes a recording of its own.
curl -X POST http://localhost:8000/api/v1/dive/{uuid}/recordings \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@Dive_2026-04-17.json" -F "file_token=$FILE_TOKEN"

# 3. One recording's per-sample profile, served with an ETag. A diver on two computers
#    has two of them; `GET /dive/{uuid}` lists the recordings and their uuids.
curl http://localhost:8000/api/v1/dive/{uuid}/recording/{rid}/profile -H "Authorization: Bearer $TOKEN"
```

And a whole logbook, in and out:

```bash
# Out: one DiveJSON document, or a zip with the stored files beside it
curl -OJ http://localhost:8000/api/v1/export/divejson -H "Authorization: Bearer $TOKEN"

# Back in, in two phases. The preview writes nothing and reports what it would do;
# the token it returns says which bytes that report was about. The file can be any
# format the converter reads - `dives.uddf`, `logbook.ssrf`, `garmin-export.zip` -
# and `conversion` in the report then says what the conversion could not carry.
curl -X POST http://localhost:8000/api/v1/import/logbook/preview \
  -H "Authorization: Bearer $TOKEN" -F "file=@logbook.divejson"

curl -X POST http://localhost:8000/api/v1/import/logbook \
  -H "Authorization: Bearer $TOKEN" -F "file=@logbook.divejson" -F "token=$PREVIEW_TOKEN"
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

|                                                                |                                                     |
| -------------------------------------------------------------- | --------------------------------------------------- |
| [opendiving](https://github.com/opendiving/opendiving)         | **The front door** — install, docs, and the release |
| [opendiving-web](https://github.com/opendiving/opendiving-web) | Next.js web app                                     |
| [opendiving-ios](https://github.com/opendiving/opendiving-ios) | SwiftUI app (early scaffold, parked)                |

## License

[AGPL-3.0](LICENSE). Run it, change it, self-host it freely — but if you offer a modified version as
a service, you share your changes. Divers' data stays free.

**Two bundled data files are not covered by it**, because the AGPL is a *software* copyleft and says
nothing about a vendored database. Both record their own licence inside themselves, where it cannot
drift away from the data:

- `src/app/data/dive_site_catalog.json` — the dive-site suggestion catalog, a Derivative Database of
  OpenStreetMap under the
  [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/). ©
  OpenStreetMap contributors, <https://osm.org/copyright>. It also carries public-domain (CC0)
  records from Wikidata, and its country and region names come from Natural Earth (public domain).
  Redistributing it — which anyone who redistributes this repository or an image built from it does
  — carries ODbL's share-alike and notice conditions with it.
- `src/app/data/marine_areas.geojson` — Natural Earth sea polygons, public domain.

Both are regenerated by the scripts in `scripts/`, which is what makes the method of producing them
available alongside the result (ODbL §4.6). See
[CONTRIBUTING.md](CONTRIBUTING.md#refreshing-the-vendored-data-files).
