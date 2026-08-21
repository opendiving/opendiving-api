# Backup and restore

**A backup is two artifacts: a `pg_dump` and a copy of the files volume.** The database holds every
dive, dive site, trip and certification record; the `files-data` volume holds the uploaded
dive-computer exports, c-card images and profile pictures themselves, one ordinary file each.
Neither is a backup on its own — a restore of the dump alone gives you a logbook whose file
downloads all fail.

Take them **in that order, database first**, and the pair is consistent: a file is written before
the row that references it, and files are never modified in place, so a copy taken after the dump is
a superset of everything the dump points at. The one gap is a file *deleted* between the two steps,
which leaves a row in the restore pointing at a file the copy no longer has. If you want exactness
rather than "one dangling row in the unlucky case", stop the stack first.

Nothing else in the stack holds anything durable: Redis is cache, open rate-limit windows and
passkey challenges that expire in minutes, and Caddy's volume holds certificates that re-issue
themselves.

**A backup outlives an erasure, and restoring one brings the erased account back.** When a diver
deletes their account the purge job destroys the rows and unlinks their files
([configuration.md](configuration.md#account-deletion)) — from the live stack, and from nowhere
else. Every dump and every files archive taken before that moment still carries them, and a restore
puts them back as surely as it puts back anything else. This is a property of restoring, not a hole
in the purge: nothing the app does can reach a tarball on another machine. The defensible position,
and the standard one, is that restoring from backup is a separate process and an erasure re-applies
on top of it — so if you restore a backup older than a deletion request you have just recreated data
someone asked you to destroy, and it is on you to run the deletion again. Backup rotation is what
bounds how long that can be true, which is one more reason to have a retention period rather than an
ever-growing pile.

Registered passkeys are ordinary rows, so the dump carries them — but they are bound to the hostname
in `FRONTEND_URL`, and a restore that comes up under a different one leaves every passkey unusable.
Signing in by email still works, which is how everyone gets back in and re-adds one. See
[configuration.md](configuration.md#sign-in).

## Back up

The database:

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

Then the files, through a throwaway container that mounts the volume read-only — the volume is not a
directory on your host, so there is nothing to `tar` directly:

```bash
docker run --rm -v opendiving_files-data:/files:ro -v "$PWD":/out alpine \
  tar czf /out/opendiving-files-$(date +%F).tar.gz -C /files .
```

`opendiving_files-data` is the volume's full name: the compose project (`opendiving`, set by `name:`
at the top of the compose file) plus the volume name. `docker volume ls` will confirm it.

Keep the `-C /files .` shape rather than `tar czf … /files`. It puts the directory itself in the
archive as `./`, carrying its ownership — which is what makes a restore into a brand-new volume land
owned by the container's user instead of by root, whichever order you do the steps in.

Nightly, out of cron — same order, both artifacts:

```bash
0 3 * * * cd /path/to/opendiving && docker compose exec -T db pg_dump -U opendiving -d opendiving --clean --if-exists | gzip > /backups/opendiving-$(date +\%F).sql.gz && docker run --rm -v opendiving_files-data:/files:ro -v /backups:/out alpine tar czf /out/opendiving-files-$(date +\%F).tar.gz -C /files .
```

Keep the backups somewhere that is not this machine, and keep more than one — a corrupt database
faithfully dumped every night is a corrupt database in every file you have.

Somewhere private, too, and that goes for **both** artifacts. The files archive carries every
account's c-card scans — ID-like documents with a diver's name, photo and certification number on
them. The dump carries the names, email addresses and dive history those scans belong to, which is
no less identifying. If either is going anywhere off hardware you control, a cloud bucket or a sync
folder included, encrypt it before it leaves.

Worth copying alongside them: your `.env`. It is not secret from you, it is short, and without
`SECRET_KEY` and `POSTGRES_PASSWORD` a restore is a stranger's database.

## Restore

Into a fresh, empty stack — database first, files second, mirroring how they were taken:

```bash
cd /path/to/opendiving
docker compose up -d db
gunzip -c opendiving-2026-08-20.sql.gz | docker compose exec -T db psql -U opendiving -d opendiving
docker run --rm -v opendiving_files-data:/files -v "$PWD":/in alpine \
  tar xzf /in/opendiving-files-2026-08-20.tar.gz -C /files
docker compose up -d
```

Over an existing one, the same commands work — the dump drops and recreates everything it carries,
and the untar writes the files back over whatever is there. Bring the app down first so nothing is
writing underneath it:

```bash
docker compose down
docker compose up -d db
gunzip -c opendiving-2026-08-20.sql.gz | docker compose exec -T db psql -U opendiving -d opendiving
docker run --rm -v opendiving_files-data:/files -v "$PWD":/in alpine \
  tar xzf /in/opendiving-files-2026-08-20.tar.gz -C /files
docker compose up -d
```

If you start the app between the two steps, it will log a CRITICAL line saying the files volume
looks unmounted or unrestored. That is this exact window, and it stops mattering the moment the
untar finishes — the app keeps serving throughout, it just cannot hand out files it does not have.

The restored dump carries the schema *and* the `alembic_version` row that says which migration it is
at, so a restore into a newer version of the app is migrated forward on the next start like any
other upgrade.

`psql` will print `NOTICE: table "..." does not exist, skipping` lines on a restore into an empty
database. That is `--if-exists` doing its job, not an error.

## Drill it

A backup nobody has restored is a hypothesis. Once, on purpose:

```bash
docker compose down -v          # destroys the volumes, files-data included - that is the point
docker compose up -d db
gunzip -c opendiving-2026-08-20.sql.gz | docker compose exec -T db psql -U opendiving -d opendiving
docker run --rm -v opendiving_files-data:/files -v "$PWD":/in alpine \
  tar xzf /in/opendiving-files-2026-08-20.tar.gz -C /files
docker compose up -d
```

Then sign in and open a dive that has an uploaded original attached, and a certification with a card
image. If both download byte-for-byte, the backup covers everything — and it is now proving two
artifacts rather than one, which is exactly why the drill is worth doing again after this change.

## Moving to another machine

Copy the dump, the files archive and `.env`, install per [install.md](install.md), restore both
before the first sign-in. Nothing is tied to the old host: no absolute paths, no machine-bound keys.
If the domain changes, change `DOMAIN` too — sessions survive, but emailed links point at whatever
it says.

Delete the copies once the restore checks out. The move leaves that pair on two machines and on
whatever you staged it through in between, and together they are the whole logbook.
