# Upgrade

```bash
cd /path/to/opendiving
docker compose pull
docker compose up -d
```

That is the whole procedure. Database migrations run themselves: the API executes
`alembic upgrade head` as it starts, under an advisory lock so the four workers in the container
cannot race each other, and only then begins serving. A schema change is therefore not a manual step
and never has been one for an installed instance.

Take a backup first anyway — [backup-restore.md](backup-restore.md), one command — because the one
thing that is not automatic is going back.

## Read the release notes

Releases are cut deliberately, not per merge, and each one carries an explicit **Breaking** section
that says "None" in so many words when there is nothing. Breaking here means something *you* have to
do: an edited `.env`, a changed configuration contract, a removed behaviour. A schema change on its
own is not breaking, because it applies itself.

<https://github.com/opendiving/opendiving-api/releases>

## Pin the version

`OPENDIVING_VERSION` in `.env` selects the tag both images run, and it defaults to `latest`. Once
this instance holds dives you'd miss, pin it:

```bash
OPENDIVING_VERSION=0.4.0
```

Upgrading is then editing that line and running the two commands above. The api and web images
always carry the same version number — they are released together, and mixing them is not a
supported configuration.

Available tags for a released version `X.Y.Z`: `X.Y.Z` (exact), `X.Y` (patches only), `latest`, and
`sha-<12>`. A bare major (`1`, `2`, …) appears from 1.0.0 onwards; below it there is deliberately no
`0` alias, because "any 0.x" is exactly the range whose minor versions are allowed to break.

## Downgrades are not supported

Same stance as Immich, and for the same reason: migrations move forward only. There is no
`alembic downgrade` path maintained across releases, and a newer schema handed to an older image
fails in whatever way that image happens to fail.

Rolling back means restoring the backup you took before upgrading, into the version you took it
from. That is what makes the first line of this page a real instruction rather than a formality.

## Upgrading the database itself

Postgres major versions are pinned in the compose file and move only when a release says so — the
data directory is version-specific, and a new major cannot read the old one's files in place. When
one comes, its release notes carry the procedure. Until then, `docker compose pull` never changes
your Postgres major underneath you: every third-party image in the bundle is pinned to a digest, not
just a tag.

## If something goes wrong

`docker compose logs api` is where a failed migration reports, with the revision it stopped on. The
container exits rather than serving against a schema it doesn't match, which is the intended
behaviour: a half-migrated database that is up and answering is worse. Restore the backup, pin
`OPENDIVING_VERSION` back to what you were running, and open an issue with the log — a migration
that fails on a real installation is a bug in the release, not in your instance.
