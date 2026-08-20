# Backup and restore

**One `pg_dump` is the whole logbook.** Uploaded dive-computer exports and c-card images are stored
in Postgres alongside the dives they belong to, not on a separate media volume, so there is no
second thing to remember and no way for the two to drift apart. That is a deliberate trade at this
scale, and the backup story is what it buys.

Nothing else in the stack holds anything durable: Redis is cache and open rate-limit windows, and
Caddy's volume holds certificates that re-issue themselves.

## Back up

```bash
cd /path/to/opendiving
docker compose exec -T db pg_dump -U opendiving -d opendiving --clean --if-exists \
  | gzip > opendiving-$(date +%F).sql.gz
```

`--clean --if-exists` makes the dump restorable over an existing database — it drops what it is
about to recreate, and does not complain when there is nothing to drop. `-T` keeps Docker from
allocating a TTY, which would corrupt the stream.

The dump can be taken while the stack is running. It is a consistent snapshot: `pg_dump` runs in a
single transaction as far as the data is concerned.

Nightly, out of cron:

```bash
0 3 * * * cd /path/to/opendiving && docker compose exec -T db pg_dump -U opendiving -d opendiving --clean --if-exists | gzip > /backups/opendiving-$(date +\%F).sql.gz
```

Keep the backups somewhere that is not this machine, and keep more than one — a corrupt database
faithfully dumped every night is a corrupt database in every file you have.

Worth copying alongside them: your `.env`. It is not secret from you, it is short, and without
`SECRET_KEY` and `POSTGRES_PASSWORD` a restore is a stranger's database.

## Restore

Into a fresh, empty stack:

```bash
cd /path/to/opendiving
docker compose up -d db
gunzip -c opendiving-2026-08-20.sql.gz | docker compose exec -T db psql -U opendiving -d opendiving
docker compose up -d
```

Over an existing one, the same command works — the dump drops and recreates everything it carries.
Bring the app down first so nothing is writing underneath it:

```bash
docker compose down
docker compose up -d db
gunzip -c opendiving-2026-08-20.sql.gz | docker compose exec -T db psql -U opendiving -d opendiving
docker compose up -d
```

The restored dump carries the schema *and* the `alembic_version` row that says which migration it is
at, so a restore into a newer version of the app is migrated forward on the next start like any
other upgrade.

`psql` will print `NOTICE: table "..." does not exist, skipping` lines on a restore into an empty
database. That is `--if-exists` doing its job, not an error.

## Drill it

A backup nobody has restored is a hypothesis. Once, on purpose:

```bash
docker compose down -v          # destroys the volumes - that is the point
docker compose up -d db
gunzip -c opendiving-2026-08-20.sql.gz | docker compose exec -T db psql -U opendiving -d opendiving
docker compose up -d
```

Then sign in and open a dive that has an uploaded original attached. If the file downloads
byte-for-byte, the backup covers everything — because that file lived in the dump too.

## Moving to another machine

Copy the dump and `.env`, install per [install.md](install.md), restore before the first sign-in.
Nothing is tied to the old host: no absolute paths, no machine-bound keys. If the domain changes,
change `DOMAIN` too — sessions survive, but emailed links point at whatever it says.
