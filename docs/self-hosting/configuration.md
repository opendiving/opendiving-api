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
| `FRONTEND_URL`       | `https://${DOMAIN}` | Where emailed links point, the API's single allowed CORS origin, and the passkey domain — see [Sign-in](#sign-in) before changing its hostname. Override for a plain-HTTP instance.         |
| `SITE_URL`           | `https://${DOMAIN}` | The web app's own origin, used for link previews. Override alongside `FRONTEND_URL`.                                                                                                        |
| `AUTH_COOKIE_SECURE` | `true`              | The refresh cookie's `Secure` flag. `false` only for plain HTTP, where the browser otherwise drops it and every reload signs the user out.                                                  |
| `WEB_HSTS`           | `on`                | `Strict-Transport-Security`, sent only on requests that already arrived over HTTPS. `off` hands the header to a proxy in front, or supports an instance that must stay reachable over HTTP. |
| `WEB_NOINDEX`        | `off`               | `true` disallows all crawlers and adds `X-Robots-Tag: noindex, nofollow` to every page.                                                                                                     |
| `OPENDIVING_VERSION` | `latest`            | The image tag both containers run. Pin it once this instance holds dives you'd miss.                                                                                                        |
| `LOG_LEVEL`          | `INFO`              | Applied to the API and the worker alike.                                                                                                                                                    |

## Sign-in

Sign-in is passwordless, and an instance offers up to three ways in. **Email always works**: the
sign-in mail carries a link *and* a six-digit code, either of which completes it — the code is there
for the ordinary case of typing your address on a laptop and reading the mail on a phone. **Google**
appears if you set `GOOGLE_CLIENT_ID`. **Passkeys** appear when the visitor's browser can do
WebAuthn against this instance, which is a property of how you deployed it rather than a setting —
see below.

Nothing here is required. The defaults are the tested configuration, and an instance that sets none
of it still has working sign-in as long as mail is delivered.

| Variable                               | Default | What it does                                                                                                                                                                                    |
| -------------------------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `SIGN_IN_CODE_ATTEMPTS_MAX`            | `5`     | Wrong guesses allowed against the emailed code. Running out spends the code only — the link in the same mail still works.                                                                       |
| `PASSKEY_CHALLENGE_TTL_SECONDS`        | `600`   | How long a started ceremony stays completable. It is spent on the first attempt either way, so this bounds only an abandoned one.                                                               |
| `PASSKEY_MAX_CREDENTIALS_PER_USER`     | `10`    | Passkeys one account may hold. Abuse hygiene, not product policy.                                                                                                                               |
| `PASSKEY_OPTIONS_RATE_LIMIT_PER_IP`    | `240`   | Per IP, per `MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS`. High because one is minted per signed-out page view that offers passkey autofill, and an office behind one NAT gateway is a single IP here. |
| `PASSKEY_VERIFY_RATE_LIMIT_PER_IP`     | `30`    | Per IP, same window.                                                                                                                                                                            |
| `PASSKEY_REGISTER_RATE_LIMIT_PER_USER` | `10`    | Per signed-in account, same window.                                                                                                                                                             |

There is deliberately **no on/off switch for passkeys**. The browser's own capability detection is
the switch: where a ceremony cannot work the web app hides the option rather than offering one that
fails, and a server flag would only be a second place for the answer to be wrong.

### Passkeys need HTTPS, and a hostname

Browsers expose WebAuthn only in a secure context, and they scope a credential to a *domain*. So an
instance is eligible when it is reached over HTTPS at a **hostname**:

- **Plain HTTP** gets no WebAuthn at all. The passkey option simply never appears; the emailed link
  and code serve that instance fully. This is the `AUTH_COOKIE_SECURE=false` LAN shape in
  [reverse-proxy.md](reverse-proxy.md) — nothing about it is broken, it just has two methods instead
  of three.
- **An IP address is never eligible**, certificate or not. `https://192.168.1.10` is a secure
  context, so the browser offers the API and then fails every ceremony: an IP is not a valid
  relying-party id. A hostname is the fix, not a better certificate.
- **`localhost` is exempt** by specification, which is why passkeys work in local development over
  plain HTTP.

The bundled Caddy gets a certificate for `DOMAIN` on its own, so the default install is already
eligible. A LAN box can become eligible without going public: anything that gives it a real hostname
and a certificate the browser trusts — a Tailscale HTTPS name, an internal CA — works, as long as
`FRONTEND_URL` is then set to that `https://` hostname.

### `FRONTEND_URL` is the passkey domain

The relying-party id and the expected origin are both derived from it; there is no separate setting.
Two consequences worth knowing before you edit it:

- **Changing its hostname orphans every passkey already registered.** Browsers will not offer a
  credential created under one domain to another, and the API will not accept one. Nobody is locked
  out — the emailed link is the recovery path, and everyone re-adds a passkey afterwards — but it is
  silent, so treat a hostname change as "everyone signs in by email once".
- **Write it without a trailing slash.** The origin is rebuilt from the parsed URL rather than
  concatenated, so `https://dive.example.com/` is tolerated — but keep it clean anyway, since the
  same value is compared against what the browser sends.

`SITE_URL` is the web app's own origin and should move with it.

### When something is down

The three methods fail independently, which is most of the argument for having three:

| Down       | Email link / code    | Passkey                    | Google |
| ---------- | -------------------- | -------------------------- | ------ |
| Mail relay | ✗                    | ✓                          | ✓      |
| Redis      | ✓ (limits fail open) | ✗ (challenges fail closed) | ✓      |
| Google     | ✓                    | ✓                          | ✗      |
| Postgres   | ✗                    | ✗                          | ✗      |

Redis is deliberately the odd one out. Rate limiting there fails *open* — an outage must not lock
everyone out of an app — but a passkey challenge **is** the replay protection, so with Redis
unreachable the passkey endpoints answer 503 rather than verifying a ceremony without one. Email
sign-in is pure Postgres and is unaffected.

## Database, cache, migrations

`POSTGRES_SERVER`, `POSTGRES_PORT`, `REDIS_CACHE_HOST` and `REDIS_QUEUE_HOST` are set by the compose
file to the service names and are not yours to change. `POSTGRES_USER` and `POSTGRES_DB` both
default to `opendiving` and can be overridden in `.env` before the first start (afterwards they name
a database that already exists under a different name).

| Variable           | Default       | What it does                                                                                                                                                                                                                                                                                                        |
| ------------------ | ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MIGRATE_ON_START` | `true`        | Runs `alembic upgrade head` as the API starts, which is what makes an upgrade `pull` + `up -d`. Turn it off only if you'd rather run `docker compose run --rm api alembic upgrade head` yourself.                                                                                                                   |
| `REDIS_PASSWORD`   | *(none)*      | For pointing the app at a managed Redis instead of the bundled one. The bundled one needs no password and is not reachable outside the compose network.                                                                                                                                                             |
| `FILE_STORAGE_DIR` | `/data/files` | Where uploaded dive-computer exports and c-card images are written inside the container. The compose file mounts the `files-data` volume there, so there is nothing to set unless you replaced that volume with a bind mount — and then the host directory has to be owned by uid 1000 or the API refuses to start. |

Redis holds cache entries, open rate-limit windows and in-flight passkey challenges. Losing it costs
a cold cache and interrupts passkey sign-in until it is back (see [Sign-in](#sign-in)); nothing
durable lives there. What is durable lives in two places, and a backup has to cover both: the
records are in Postgres, and the uploaded files themselves are on the `files-data` volume. See
[backup-restore.md](backup-restore.md).

## Optional features

| Variable                                                    | Default   | What it does                                                                                                                    |
| ----------------------------------------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `CONTACT_FORM_EMAIL`                                        | *(none)*  | Where the contact form delivers. Unset, that endpoint answers 503 and the form is off.                                          |
| `CONTACT_EMAIL`                                             | *(none)*  | Shown on the contact page as a fallback. Display only.                                                                          |
| `GOOGLE_CLIENT_ID`                                          | *(none)*  | Offers Google Sign-In. Unset, the button is hidden and `accounts.google.com` leaves the web app's CSP. See [Sign-in](#sign-in). |
| `GRAVATAR_ENABLED`                                          | `false`   | Avatars from Gravatar. See *Third-party calls* below before turning it on.                                                      |
| `MAP_TILE_URL`, `MAP_TILE_URL_DARK`, `MAP_TILE_ATTRIBUTION` | Carto     | The dive-site picker's basemap. The web app's CSP follows these automatically.                                                  |
| `GEOCODER_URL`                                              | Nominatim | Turns a map pin into a place name, server-side. Set to `""` to switch geocoding off entirely.                                   |
| `WORMS_API_URL`, `WIKIDATA_API_URL`                         | public    | The species picker's two registers, also called server-side.                                                                    |

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
