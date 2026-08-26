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
| `REFRESH_TOKEN_EXPIRE_DAYS`            | `7`     | How long a browser stays signed in **without the app being used**. A rolling window, not a session length — read below before changing it.                                                      |

**Two things worth knowing before you change the number.** The refresh cookie is single-use: each
time it is spent a fresh one replaces it with the clock started again, so this setting bounds
*inactivity* rather than the session. A browser used every day stays signed in indefinitely; one
left alone for longer than this asks for a sign-in link again. That is the behaviour the web app
describes, so raising or lowering it changes what the app does rather than only how long a token
lives.

And the number is quoted back to divers in two places the web image ships as prose — the note under
the sign-in button and the bundled privacy page's section on the sign-in cookie, both of which say
"about a week" from the default of 7. Neither reads this setting, so an instance that changes it has
two lines of copy that no longer match it. It is the same coupling as the privacy page's "within 30
days" against [account deletion](#account-deletion), and the same remedy: pick a number your own
copy can honestly stand behind, or expect to edit those two lines.

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

| Variable           | Default       | What it does                                                                                                                                                                                                                                                                                                                          |
| ------------------ | ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MIGRATE_ON_START` | `true`        | Runs `alembic upgrade head` as the API starts, which is what makes an upgrade `pull` + `up -d`. Turn it off only if you'd rather run `docker compose run --rm api alembic upgrade head` yourself.                                                                                                                                     |
| `REDIS_PASSWORD`   | *(none)*      | For pointing the app at a managed Redis instead of the bundled one. The bundled one needs no password and is not reachable outside the compose network.                                                                                                                                                                               |
| `FILE_STORAGE_DIR` | `/data/files` | Where uploaded dive-computer exports, c-card images and profile pictures are written inside the container. The compose file mounts the `files-data` volume there, so there is nothing to set unless you replaced that volume with a bind mount — and then the host directory has to be owned by uid 1000 or the API refuses to start. |

Redis holds cache entries, open rate-limit windows and in-flight passkey challenges. Losing it costs
a cold cache and interrupts passkey sign-in until it is back (see [Sign-in](#sign-in)); nothing
durable lives there. What is durable lives in two places, and a backup has to cover both: the
records are in Postgres, and the uploaded files themselves are on the `files-data` volume. See
[backup-restore.md](backup-restore.md).

## Optional features

| Variable                                                    | Default   | What it does                                                                                                                       |
| ----------------------------------------------------------- | --------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `CONTACT_FORM_EMAIL`                                        | *(none)*  | Where the contact form delivers. Unset, that endpoint answers 503 and the form is off.                                             |
| `CONTACT_EMAIL`                                             | *(none)*  | Shown on the contact page as a fallback. Display only.                                                                             |
| `GOOGLE_CLIENT_ID`                                          | *(none)*  | Offers Google Sign-In. Unset, the button is hidden and `accounts.google.com` leaves the web app's CSP. See [Sign-in](#sign-in).    |
| `MAP_TILE_URL`, `MAP_TILE_URL_DARK`, `MAP_TILE_ATTRIBUTION` | Carto     | The basemap behind every map the web app draws — see [Third-party calls](#third-party-calls). The CSP follows these automatically. |
| `GEOCODER_URL`                                              | Nominatim | Turns a map pin into a place name, server-side. Set to `""` to switch geocoding off entirely.                                      |
| `WORMS_API_URL`, `WIKIDATA_API_URL`                         | public    | The species picker's two registers, also called server-side.                                                                       |

### Account deletion

| Variable                      | Default | What it does                                                                                                              |
| ----------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------- |
| `ACCOUNT_DELETION_GRACE_DAYS` | `14`    | How long a deleted account stays restorable before it and everything it owns are destroyed. `0` purges on the next sweep. |

Deleting an account takes effect immediately — the app stops opening, on every device — but the data
is not destroyed until the grace period runs out. A cron in the `worker` container runs hourly at
:30 and issues a real `DELETE FROM "user"` for each account past its deadline, taking that diver's
dives, dive sites, certifications, gear, trips and every uploaded file with it. Nothing else purges
an account, and nothing decides on a user's behalf that one should go: the job only ever executes a
request the user already made.

**Two things worth knowing before you change the number.** The bundled privacy page states that
personal information is permanently deleted within 30 days. That sentence is true at the default and
stays true up to about 29 days; raise this past that and the page your instance serves is making a
promise your configuration breaks. And at `0` there is no way back at all — the confirmation email
says so instead of naming a date, but a misclick is then final.

**You are the controller.** OpenDiving is software; the operator of an instance is who data
protection law has obligations for. This setting is the knob that erasure requests are served by —
what it does, and how promptly, is your call to make and to document.

**Two things the purge cannot reach**, and both are yours to handle rather than the app's. Your
backups keep a deleted account until they rotate, and restoring one older than the request brings
that account back — see [backup-restore.md](backup-restore.md). And if you have turned the admin
panel on, its own event and audit tables hold a second copy of whatever you edited there; see
[The admin panel](#the-admin-panel).

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

**The panel signs its administrators in with cookies of its own**, set under `CRUD_ADMIN_MOUNT_PATH`
when someone logs in to it. They are part of the surface you operate rather than anything a diver
meets: nobody using the app receives one, and they exist only in the browser of whoever administers
this copy. That is why the bundled privacy page — written for the people whose dives your instance
holds — does not describe them, and points operators here instead. They belong to `crudadmin` and
their names are its business, not this app's, so read them out of your own browser rather than from
a list here that a dependency upgrade could quietly falsify. Leave the panel off, as it ships, and
there are none.

**Turning it on gives you a second copy of personal data, and account deletion does not reach it.**
The panel keeps its own tables — `admin_event_log`, a row per action with the admin's address and
user agent, and `admin_audit_log`, which for every create, update and delete stores the row's JSON
state *before* and *after*. Edit a diver through the panel and their email address is now in that
audit row as well as on the `user` row. One setting gates both tables, `CRUD_ADMIN_TRACK_EVENTS`,
and it defaults to on — so enabling the panel enables these unless you say otherwise. They live
wherever `CRUD_ADMIN_DB_URL` points, which the compose file points at the app's own Postgres, so a
`pg_dump` carries them too.

The [account purge](#account-deletion) deliberately leaves them alone. Those tables have no foreign
key to `user` and a different lifecycle: they are a record of what *an operator* did, which is the
one thing an audit log is for, and a purge that quietly rewrote it would be an audit log worth
nothing.

That makes them yours to manage, and nothing manages them for you — `crudadmin` has a retention
helper but nothing in this app calls it, so both tables grow for as long as the panel is enabled. If
you turn it on, prune them yourself on whatever schedule matches what you tell your users:

```bash
docker compose exec -T db psql -U opendiving -d opendiving \
  -c "DELETE FROM admin_audit_log WHERE timestamp < now() - interval '90 days';" \
  -c "DELETE FROM admin_event_log WHERE timestamp < now() - interval '90 days';"
```

Audit rows carry the id of the event they belong to, but not as a foreign key — nothing stops you
deleting the events and leaving the audit rows pointing at nothing, so keep the two windows the
same. And when you serve an erasure request for someone whose row you once edited by hand, remember
this copy. Leave the panel off, as it ships, and none of this exists.

## Third-party calls

Nothing here phones home. What the app can be told to contact:

- **From the browser**: map tiles, and — only where you have set `GOOGLE_CLIENT_ID` — Google's
  sign-in code. Nothing else, profile pictures included: an avatar is stored on your own files
  volume and served by your own API. (Gravatar used to be an option here, disclosing a hash of every
  signed-in user's email address and their IP to Automattic on every page. It is gone, along with
  its `GRAVATAR_ENABLED` variable.)

  **Tiles** are requested wherever a map is on screen, and carry only the `z/x/y` of the area shown.
  Five surfaces draw one: the form to add or edit a dive site, a dive site's own page, the form to
  add or edit a trip, a trip with places on it, and the page of a dive that has a position — from
  the site it was logged at, or from the GPS reading in the file it was imported from. The two forms
  load a map as soon as they open; the other three load none when there is nothing to show. Your
  tile provider therefore sees a visitor's IP address and roughly where they dive, and nothing else
  — not their account, their dive log, or the name of anything on the map. Point `MAP_TILE_URL` at a
  tile server you run and none of that leaves your machine.

  **Google's sign-in code** loads from `accounts.google.com` as the front page or the sign-in page
  appears, before anyone has chosen Google and whether or not they ever do — so Google sees that
  visitor's IP address and browser at that moment, and may set cookies of its own under its own
  policy, which neither you nor this app can see. Narrowing it so that nothing reaches Google until
  the button is actually clicked is a separate change already in hand. Until then, leaving
  `GOOGLE_CLIENT_ID` unset is what avoids it entirely: the button disappears, and so does the
  bundled privacy page's section disclosing this.

- **From the server**: the geocoder and the two species registers, on cache misses only. A pinned
  coordinate or a typed search string goes out; nothing identifying the diver does, and the source
  IP is your server's. Both are configurable, and the geocoder can be switched off outright. One
  more, only if you have set `GOOGLE_CLIENT_ID`: when somebody signs up with Google, the API fetches
  their Google profile picture once — from `googleusercontent.com`, at account creation and never
  again — and stores it on your files volume. It is best-effort; a failure just means that account
  starts with initials.

There is no analytics of any kind. The web app's Content-Security-Policy narrows where anything
could be *sent*: `connect-src` names this instance's own origin and its API, plus
`accounts.google.com` where Google sign-in is configured, so a `fetch`, an `XMLHttpRequest`, a
WebSocket or a `navigator.sendBeacon` aimed at a third-party collector is refused by the browser
until the policy itself is widened. Take that for what it is and no more — it constrains
destinations, not dependencies. Script bundled into the web app loads under `'strict-dynamic'`,
because your own build already vouches for it, and anything reporting back to this instance's own
origin passes as ordinary first-party traffic.

**Add any of it and the consent duty is yours.** The `/privacy` page is part of the web image, and
it describes exactly the posture above: nothing here is advertising or analytics, which is the whole
reason it offers no cookie banner. Add a tracker, an analytics script, an advertising tag or any new
third-party subresource to your copy and two things happen together — that page stops being true of
your instance, and asking for consent *before* the storage or access happens becomes an obligation
on you, along with building the flow that collects it. Under ePrivacy that duty falls on whoever
operates the service, which is you and not this project, and nothing in the image can discharge it
on your behalf.
