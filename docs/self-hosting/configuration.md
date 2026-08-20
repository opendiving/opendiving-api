# Configuration reference

Everything is configured through one `.env` file next to `docker-compose.yml`. The compose file
feeds it to the API and the worker wholesale, and hands the web container only the handful of
variables it needs — so database and SMTP credentials never enter the Node process at all.

An edit to `.env` takes effect on `docker compose up -d`, which recreates what changed. A plain
`docker compose restart` does **not** re-read the file: it restarts the process inside a container
that keeps the environment it was created with.

The API has more settings than are listed here — token lifetimes, every rate limit, the geocoder and
species providers. [`src/.env.example`](../../src/.env.example) in this repository is the full
annotated list and `src/app/core/config.py` is the authority; the groups below say which of them a
self-hoster normally touches, and any setting from that file can be added to `.env` verbatim.

## The six that matter

| Variable                         | Default             | What it does                                                                                                    |
| -------------------------------- | ------------------- | --------------------------------------------------------------------------------------------------------------- |
| `DOMAIN`                         | *(none — required)* | The hostname this instance answers on. Drives the certificate, the emailed links and the web app's own origin.  |
| `SECRET_KEY`                     | *(none — required)* | Signs every token the API issues. `openssl rand -hex 32`. Startup **fails** on the published placeholder value. |
| `POSTGRES_PASSWORD`              | *(none — required)* | The database password, read once when the volume is first created.                                              |
| `SMTP_HOST`, `SMTP_PORT`         | *(none)*            | The mail relay. Required on any `ENVIRONMENT` but `local`, because sign-in is passwordless.                     |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | *(none)*            | Its credentials. Both optional and independent — a relay that authenticates by IP needs neither.                |
| `EMAIL_FROM_ADDRESS`             | *(none)*            | The address mail is sent as. Required as soon as `SMTP_HOST` is set; startup fails without it.                  |

## Serving

| Variable             | Default             | What it does                                                                                                                                                                                |
| -------------------- | ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `COMPOSE_PROFILES`   | `proxy`             | Runs the bundled Caddy. Comment it out to bring your own proxy — [reverse-proxy.md](reverse-proxy.md).                                                                                      |
| `CADDY_SITE_ADDRESS` | `${DOMAIN}`         | The address Caddy answers on. `:80` for a LAN instance with no certificate.                                                                                                                 |
| `TRUSTED_PROXY_IPS`  | `172.29.0.0/16`     | Whose `X-Forwarded-For` and `X-Forwarded-Proto` the API believes. Every per-IP rate limit depends on it, as do the admin panel's HTTPS enforcement and its IP allowlist.                    |
| `ENVIRONMENT`        | `production`        | `production` hides `/docs`. `staging` puts them behind a superuser; both require `SMTP_HOST`. `local` opens the docs and logs sign-in links instead of emailing them.                       |
| `FRONTEND_URL`       | `https://${DOMAIN}` | Where emailed links point, and the API's single allowed CORS origin. Override for a plain-HTTP instance.                                                                                    |
| `SITE_URL`           | `https://${DOMAIN}` | The web app's own origin, used for link previews. Override alongside `FRONTEND_URL`.                                                                                                        |
| `AUTH_COOKIE_SECURE` | `true`              | The refresh cookie's `Secure` flag. `false` only for plain HTTP, where the browser otherwise drops it and every reload signs the user out.                                                  |
| `WEB_HSTS`           | `on`                | `Strict-Transport-Security`, sent only on requests that already arrived over HTTPS. `off` hands the header to a proxy in front, or supports an instance that must stay reachable over HTTP. |
| `WEB_NOINDEX`        | `off`               | `true` disallows all crawlers and adds `X-Robots-Tag: noindex, nofollow` to every page.                                                                                                     |
| `OPENDIVING_VERSION` | `latest`            | The image tag both containers run. Pin it once this instance holds dives you'd miss.                                                                                                        |
| `LOG_LEVEL`          | `INFO`              | Applied to the API and the worker alike.                                                                                                                                                    |

## Database, cache, migrations

`POSTGRES_SERVER`, `POSTGRES_PORT`, `REDIS_CACHE_HOST` and `REDIS_QUEUE_HOST` are set by the compose
file to the service names and are not yours to change. `POSTGRES_USER` and `POSTGRES_DB` both
default to `opendiving` and can be overridden in `.env` before the first start (afterwards they name
a database that already exists under a different name).

| Variable           | Default  | What it does                                                                                                                                                                                      |
| ------------------ | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MIGRATE_ON_START` | `true`   | Runs `alembic upgrade head` as the API starts, which is what makes an upgrade `pull` + `up -d`. Turn it off only if you'd rather run `docker compose run --rm api alembic upgrade head` yourself. |
| `REDIS_PASSWORD`   | *(none)* | For pointing the app at a managed Redis instead of the bundled one. The bundled one needs no password and is not reachable outside the compose network.                                           |

Redis holds cache entries and open rate-limit windows only. Losing it costs a cold cache; nothing
durable lives there. Everything durable — including every uploaded dive-computer file and c-card
image — is in Postgres, which is why one `pg_dump` is the whole backup.

## Optional features

| Variable                                                    | Default   | What it does                                                                                           |
| ----------------------------------------------------------- | --------- | ------------------------------------------------------------------------------------------------------ |
| `CONTACT_FORM_EMAIL`                                        | *(none)*  | Where the contact form delivers. Unset, that endpoint answers 503 and the form is off.                 |
| `CONTACT_EMAIL`                                             | *(none)*  | Shown on the contact page as a fallback. Display only.                                                 |
| `GOOGLE_CLIENT_ID`                                          | *(none)*  | Offers Google Sign-In. Unset, the button is hidden and `accounts.google.com` leaves the web app's CSP. |
| `GRAVATAR_ENABLED`                                          | `false`   | Avatars from Gravatar. See *Third-party calls* below before turning it on.                             |
| `MAP_TILE_URL`, `MAP_TILE_URL_DARK`, `MAP_TILE_ATTRIBUTION` | Carto     | The dive-site picker's basemap. The web app's CSP follows these automatically.                         |
| `GEOCODER_URL`                                              | Nominatim | Turns a map pin into a place name, server-side. Set to `""` to switch geocoding off entirely.          |
| `WORMS_API_URL`, `WIKIDATA_API_URL`                         | public    | The species picker's two registers, also called server-side.                                           |

### The admin panel

Off by default, and a deliberate opt-in: it is a full CRUD interface over every model and bypasses
the ownership checks the API applies to everything else.

```bash
CRUD_ADMIN_ENABLED=true
ADMIN_PASSWORD=a-real-password        # production refuses to start without one
ADMIN_USERNAME=admin
CRUD_ADMIN_ALLOWED_NETWORKS=10.0.0.0/8   # optional, comma-separated
```

The compose file already points its tables at the app's Postgres (`CRUD_ADMIN_DB_URL`), which is
what makes it work behind four API workers. If your `POSTGRES_PASSWORD` contains `@`, `/`, `:` or
`#`, percent-encode it in that derived URL.

The allowlist matches the caller's address only when `TRUSTED_PROXY_IPS` names the proxy actually in
front of the app — the panel's own middleware reads the forwarded address the app was told to
believe. The same setting is what stops the panel redirecting `/admin` to the HTTPS URL it is
already on: it enforces HTTPS on `ENVIRONMENT=production`, and a proxy the app hasn't been told
about makes every request look like plain HTTP. Both symptoms are one misconfiguration, and the
shipped value covers the bundled Caddy.

Running your own proxy? Route `/admin` to `api:8000` yourself — the web container carries only
`/api/v1` — and make sure your proxy is in `TRUSTED_PROXY_IPS` **and** sets `X-Forwarded-Proto`.

## Third-party calls

Nothing here phones home. What the app can be told to contact:

- **From the browser**: map tiles (the dive-site picker only, and only the `z/x/y` of the area
  shown), and Gravatar if you turn it on — which discloses a hash of every signed-in user's email
  address and their IP to Automattic, on every page. It is off by default.
- **From the server**: the geocoder and the two species registers, on cache misses only. A pinned
  coordinate or a typed search string goes out; nothing identifying the diver does, and the source
  IP is your server's. Both are configurable, and the geocoder can be switched off outright.

There is no analytics of any kind, and the web app's Content-Security-Policy structurally forbids
adding some without also changing the policy.
