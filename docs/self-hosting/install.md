# Install

Four commands, a domain, and a mail relay.

```bash
mkdir opendiving && cd opendiving
curl -LO https://github.com/opendiving/opendiving-api/releases/latest/download/docker-compose.yml
curl -LO https://github.com/opendiving/opendiving-api/releases/latest/download/Caddyfile
curl -Lo .env https://github.com/opendiving/opendiving-api/releases/latest/download/.env.example
```

Edit six values in `.env` — the file explains each one where it sits:

| Variable                          | What to put there                                                    |
| --------------------------------- | -------------------------------------------------------------------- |
| `DOMAIN`                          | The hostname this instance is reached at, e.g. `dives.example.com`   |
| `SECRET_KEY`                      | `openssl rand -hex 32` — the app refuses to start on the placeholder |
| `POSTGRES_PASSWORD`               | `openssl rand -base64 24`                                            |
| `SMTP_HOST` / `SMTP_PORT`         | Your mail relay                                                      |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | Its credentials, if it wants any                                     |
| `EMAIL_FROM_ADDRESS`              | An address on a domain that relay may send for                       |

Then:

```bash
docker compose up -d
```

Point `DOMAIN`'s DNS record at this machine **before** that last command: the bundled Caddy asks
Let's Encrypt for a certificate as it starts, and it can only get one for a name that already
resolves here. Open `https://your-domain`, ask for a sign-in link, and the first account to sign in
is yours.

## What you need

**A mail relay.** Sign-in is passwordless — an emailed magic link is the only way anybody, including
you, gets in — so an instance that cannot send mail cannot be used. Any SMTP relay works: your mail
provider, your host's, or your own existing server. Don't stand up an MTA for this unless you
already know why; getting mail *accepted* (SPF, DKIM, DMARC, IP reputation, a port 25 your host
probably blocks) is the hard part, and it is why every serious self-hosted app asks for relay
credentials instead of bundling Postfix.

**A machine.** Modest: this is a personal dive log, not a photo library.

- 1 vCPU, 1 GB RAM, and 10 GB of disk is enough to start. Postgres and Redis are the memory floor;
  two GB is comfortable.
- **Disk grows with the files you upload**, and the uploads live in Postgres — a dive-computer
  export is tens of kilobytes, a c-card photo up to 10 MB. A thousand dives with photographed cards
  is still comfortably inside a few GB.
- **amd64 and arm64 both**. Every release publishes both architectures, so a Raspberry Pi 4/5, an
  Ampere VPS or an Apple-silicon box runs the same images as an x86 server.

**Docker Engine 25 or newer**, with the Compose plugin (`docker compose`, not `docker-compose`). The
bundle uses `depends_on: condition: service_healthy` and `service_completed_successfully`, both of
which need a recent Compose v2; Docker Desktop and any current distribution package are fine.

**Ports 80 and 443**, free on the host. Caddy needs 80 to answer the ACME challenge even though
everything ends up on 443. Already running another reverse proxy? That is a supported setup — see
[reverse-proxy.md](reverse-proxy.md), and turn the bundled one off before starting.

## What just started

Seven containers, of which exactly one publishes a port:

| Service      | What it is                                                        |
| ------------ | ----------------------------------------------------------------- |
| `caddy`      | TLS and the front door — **the only one on 80/443**               |
| `web`        | The Next.js app, and the proxy that carries `/api/v1` to the API  |
| `api`        | The FastAPI backend                                               |
| `worker`     | Scheduled jobs and the gear-service reminder digest               |
| `admin_init` | One-shot; exits immediately unless the admin panel is switched on |
| `db`         | PostgreSQL 18 — **every dive and every uploaded file is in here** |
| `redis`      | Cache and job queue; nothing durable                              |

`docker compose ps` should show them all `healthy` within a minute or so of the images being pulled.
`docker compose logs -f api` is where the API's startup — including `alembic upgrade head`, which
runs itself — reports in.

## Pin a version once you care about it

The compose file follows `latest` unless you say otherwise. As soon as this instance holds dives
you'd miss, set `OPENDIVING_VERSION` in `.env` to the current version and move it deliberately,
after reading the release notes. The api and web images always carry the same version number — they
are released together.

## Next

- [configuration.md](configuration.md) — every setting, grouped
- [reverse-proxy.md](reverse-proxy.md) — bring your own proxy, or run on a LAN with no domain
- [backup-restore.md](backup-restore.md) — one `pg_dump` is the whole logbook
- [upgrade.md](upgrade.md) — pull, up, done
- [troubleshooting.md](troubleshooting.md) — when it doesn't go like that
