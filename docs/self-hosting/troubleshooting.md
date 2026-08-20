# Troubleshooting

Start here:

```bash
docker compose ps          # who is up, and who is healthy
docker compose logs api    # the API says why it refused to start
```

Every service has a healthcheck, and the API's asks `/api/v1/health/ready`, which round-trips
Postgres and Redis — so `healthy` means "can serve", not "has a process".

## The stack won't start

**`SECRET_KEY is unset or still a placeholder`** — exactly what it says. The template value is
published in this repository, so anyone could mint a token for any account with it.
`openssl rand -hex 32`, put it in `.env`, `docker compose up -d`.

**`ENVIRONMENT is production but SMTP_HOST is not set`** — sign-in is passwordless, so an instance
with no relay cannot let anybody in, including you on the first day. Configure the `SMTP_*` block,
or run `ENVIRONMENT=local` while you are still poking at it.

**`SMTP_HOST is set but EMAIL_FROM_ADDRESS is not`** — the from-address has no safe default: it has
to be an address on a domain your relay may send for, and a wrong one fails hours later in
somebody's spam folder rather than here.

**`CRUD_ADMIN_ENABLED is true in production but ADMIN_PASSWORD is unset`** — the panel is a full
CRUD interface over every model. Set a real password, or turn it off.

**An edit to `.env` seems to have done nothing** — `docker compose restart` does *not* re-read the
file. It restarts the process inside a container that keeps the environment it was created with.
`docker compose up -d` recreates what changed, and that is what applies an edit.

## No certificate

Caddy asks Let's Encrypt for one as it starts, and it can only get one for a name that already
resolves to this machine. `docker compose logs caddy` says which step failed. The usual causes:

- **DNS doesn't point here yet**, or hasn't propagated.
- **Port 80 is not reachable from the internet.** The HTTP-01 challenge needs it even though the
  site ends up on 443 — check the host firewall and, on a home connection, the router's port
  forwarding.
- **Something else holds 80/443.** `docker compose up` reports the bind failure; if you already run
  a proxy, that is a supported setup — see [reverse-proxy.md](reverse-proxy.md).
- **An IP address, not a name.** Certificates are not issued for those. Run the LAN setup in the
  same page.

## No sign-in email

The link is generated whether or not it can be delivered, so this is a mail problem rather than an
app one nine times in ten.

- `docker compose logs api` shows what the relay said. Authentication failures, refused senders and
  blocked ports all surface there verbatim.

- **Check the spam folder**, then check that `EMAIL_FROM_ADDRESS` is on a domain your relay is
  authorized to send for. A mismatch is the most common cause of silent non-delivery.

- **Port 587 with `SMTP_TLS_MODE=starttls`** is the usual combination; 465 wants
  `SMTP_TLS_MODE=tls`. Many hosts block outbound 25 entirely.

- **`ENVIRONMENT=local` with no `SMTP_HOST`** logs the link instead of emailing it:

  ```bash
  docker compose logs api | grep "magic link"
  ```

  That is the local development flow, and it is deliberately unavailable in production — the logged
  link is a live credential, so the API raises there rather than writing one to the log.

## Everyone shares one rate-limit bucket

Symptom: one person's retries lock sign-in for everybody, or the contact form starts answering 429
to callers who have used it once.

The API believes `X-Forwarded-For` only from an address listed in `TRUSTED_PROXY_IPS`. Unset — or
set to something that isn't actually in front of the app — every caller looks like the proxy and the
per-IP buckets merge into one. The shipped value covers the bundled Caddy. If you run your own
proxy, add its address; see step 3 of [reverse-proxy.md](reverse-proxy.md).

The reverse error is quieter and worse: trusting a network that is *not* in front of the app lets
callers forge the header and skip the limits entirely. List the proxy, not the world.

## `api` exits with "has host bits set"

An entry in `TRUSTED_PROXY_IPS` is a host address carrying a network prefix — `10.1.2.3/8`. That
value also configures which peers may set the forwarded headers, and that parser is strict about
what this one is lenient about. Write the entry as `10.1.2.3` if you mean that address, or
`10.0.0.0/8` if you mean the block. The message quotes the offending value and names no setting at
all, which is why it looks unrelated to anything you edited.

## `/admin` redirects forever, or 403s me

Both are the same setting. The panel enforces HTTPS on `ENVIRONMENT=production` and applies its IP
allowlist to the address the app believes the caller has — and the app believes a forwarded address
only from a proxy listed in `TRUSTED_PROXY_IPS`. Unlisted, every request looks like plain HTTP from
the proxy itself: the panel redirects to the HTTPS URL it is already on, and any honest allowlist
value matches nobody. Fix the list, not the panel, and make sure your proxy sends
`X-Forwarded-Proto` — the bundled Caddy does, and the shipped value already names it.

## Signing in works, then a reload signs me out

The refresh cookie is `Secure`, so a browser will not send it back over plain HTTP. On an instance
without TLS, set `AUTH_COOKIE_SECURE=false` — and know what it means: the cookie now travels in the
clear, which is acceptable on a LAN and not on the internet.

The admin panel has its own equivalent (`SESSION_SECURE_COOKIES`), with the same caveat.

## `docker compose up` says the address pool overlaps

The compose file pins the network to `172.29.0.0/16` so that `TRUSTED_PROXY_IPS` can name it. If
something else on this host already uses that range, change the `subnet:` at the bottom of
`docker-compose.yml` **and** `TRUSTED_PROXY_IPS` in `.env` together — a preset that doesn't match
the network is the rate-limit failure above.

## I changed `POSTGRES_PASSWORD` and now nothing connects

That variable initializes the cluster the first time the volume is created, and is only read again
as the app's connection password. Changing it in `.env` afterwards changes what the app sends, not
what Postgres expects. Change it in the database too:

```bash
docker compose exec db psql -U opendiving -d opendiving \
  -c "ALTER USER opendiving WITH PASSWORD 'the-new-password';"
docker compose up -d
```

## Everything looks healthy, the browser sees a 502

Caddy is up before the app is ready only if you started them separately; `depends_on` handles the
normal case. Otherwise `docker compose logs web` and `docker compose logs api` in that order — the
web container proxies `/api/v1` to `api:8000`, so an API that is restarting shows up as a gateway
error from the web app rather than as an obvious API failure.

## Starting over

The local data is yours, and this destroys it:

```bash
docker compose down -v
```

Take a dump first if there is anything in there — [backup-restore.md](backup-restore.md).

## Reporting a bug

`GET /api/v1/health` reports the version this instance is running. Include that, the relevant
`docker compose logs` output, and whether the instance runs the bundled Caddy or your own proxy:
<https://github.com/opendiving/opendiving-api/issues>
