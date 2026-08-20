# Reverse proxy

The bundle ships Caddy and uses it by default: `COMPOSE_PROFILES=proxy` in `.env` is what runs it,
it is the only container that publishes a port, and `DOMAIN` is all it needs to get and renew a
certificate.

If you already run a proxy on this machine — Traefik, nginx, Nginx Proxy Manager, HAProxy — that is
a supported setup and this page is for you. Two proxies cannot both hold 80/443, so the bundled one
has to go first.

## Bring your own proxy

**1. Turn Caddy off.** Comment out one line in `.env`:

```bash
# COMPOSE_PROFILES=proxy
```

**2. Point your proxy at `web:3000`.** One upstream, no path splitting: the web container carries
`/api/v1` through to the API itself, so it is a complete front end. Whether your proxy reaches it by
joining this stack's network (`opendiving_default`) or by a published port is up to you — if the
latter, add a `ports:` mapping to the `web` service in an override file rather than editing the
shipped compose file.

**3. Tell the API which proxy to believe.** This is the step that is easy to skip and silently
expensive:

```bash
TRUSTED_PROXY_IPS=172.29.0.0/16,10.1.2.3
```

Every per-IP rate limit — magic-link requests, sign-in verification, the contact form, ten of them
in total — is keyed on the caller's address. Behind a proxy that address arrives in
`X-Forwarded-For`, and the API believes that header **only** from an address listed here. Get it
wrong in either direction and something breaks quietly:

- **Not listed**: every caller looks like your proxy. All the per-IP buckets merge into one, and a
  single bot exhausting the magic-link limit locks sign-in for everybody.
- **Listed too widely** (a network that isn't actually in front of the app): callers can forge the
  header and skip the limits entirely.

Add the address your proxy connects *from*. If it joins the compose network, that is already covered
by the shipped `172.29.0.0/16`.

Write each entry as a bare address (`10.1.2.3`) or as a network with its host bits zeroed
(`10.0.0.0/8`). `10.1.2.3/8` is the shape to avoid: the app would read it as the block, but the same
value also configures the server's forwarded-header trust, which rejects it — and the `api`
container exits at startup with `Error: 10.1.2.3/8 has host bits set`.

Two consequences beyond the rate limits, both admin-panel-only and both invisible until you turn the
panel on: the app decides whether a request arrived over HTTPS from the same forwarded headers, so a
proxy it hasn't been told about makes `/admin` redirect to the HTTPS URL it is already on, forever;
and the panel's `CRUD_ADMIN_ALLOWED_IPS`/`..._NETWORKS` allowlist matches whatever address the app
believes, which is your proxy rather than the caller. Make sure your proxy sets `X-Forwarded-Proto`
as well as `X-Forwarded-For` — the snippets below do.

**4. Set `AUTH_COOKIE_SECURE=false`** only if your proxy serves the app over plain HTTP. Terminating
TLS at the proxy and forwarding HTTP internally is fine and needs no change — the browser is what
the cookie flag concerns.

**5. Route `/admin` yourself** if you enable the admin panel. It mounts on the API, not on the web
app, so it needs a second upstream: `/admin*` → `api:8000`. Leave it alone otherwise; the panel is
off by default.

**6. Leave the response headers alone.** There is nothing to add here, and that is the point of this
step: both containers set their own, so a proxy that simply passes responses through — which is the
default behaviour of every proxy on this page — gets it right without being configured.

The API sends `Content-Security-Policy: frame-ancestors 'none'`, `X-Frame-Options: DENY` and
`X-Content-Type-Options: nosniff` on every response. `/admin` is the reason: it is a full
create/update/delete interface over every table, and CRUDAdmin ships no headers of its own. The web
app sets a per-request, nonce-based CSP plus `Referrer-Policy`, `Permissions-Policy` and the same
two above.

Three ways to undo that, all of them things you have to type:

- **Hiding them.** `proxy_hide_header` in nginx, or its equivalent elsewhere. There is no reason to
  reach for it against either upstream.
- **Adding your own copy.** nginx's `add_header` *appends*, it does not replace, so a well-meant
  `add_header X-Frame-Options SAMEORIGIN;` reaches the browser as `DENY, SAMEORIGIN`. Frame
  protection survives that — a conflicting value fails *closed*, and the browser blocks the frame —
  but you do not get the policy you typed, which is worth knowing if you had a reason to want
  `SAMEORIGIN`. The one that actually breaks something is CSP: duplicate `Content-Security-Policy`
  headers are enforced *together* rather than resolved, so a site-wide `default-src 'self'` added
  here stacks on top of the API's `frame-ancestors 'none'` and blocks the admin panel's webfont.
  Keep site-wide security headers on your other vhosts, not on this one.
- **Losing `X-Forwarded-Proto`.** HSTS is the one header this stack will not send unprompted: the
  web app emits it only on a request that already arrived over HTTPS, and behind a proxy that
  terminates TLS the sole evidence of that is the header from step 3. Without it, no HSTS on any
  page of the site.

If you would rather your proxy own HSTS — one place for every site on the box, which is a reasonable
way to run a machine — set `WEB_HSTS=off` and send `Strict-Transport-Security` yourself. Pick one or
the other; two things sending it is how they end up disagreeing about `max-age`.

## Snippets

Adapt the upstream host to however your proxy reaches the stack.

### nginx

```nginx
server {
    listen 443 ssl;
    server_name dives.example.com;

    # ssl_certificate ... ;
    # ssl_certificate_key ... ;

    # The app accepts card images up to 10 MB and dive-computer files up to 5 MB. nginx's
    # default is 1 MB, which would reject the larger half of those before the request ever
    # reaches the app - so the caller gets nginx's bare 413 instead of the app's
    # explanatory one. A little above the app's own ceiling keeps the app the thing that
    # enforces it.
    client_max_body_size 12m;

    # No `add_header` here, deliberately - see step 6. Both upstreams set their own
    # security headers, and nginx's `add_header` appends rather than replaces: a second
    # `Content-Security-Policy` is enforced alongside theirs, not instead of it.
    location / {
        proxy_pass http://127.0.0.1:3000;   # the `web` container
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Only if you enabled the admin panel.
    # location /admin { proxy_pass http://127.0.0.1:8000; ... }
}
```

### Traefik (compose labels)

Add to the `web` service in an override file, with Traefik on the same network:

```yaml
services:
  web:
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.opendiving.rule=Host(`dives.example.com`)"
      - "traefik.http.routers.opendiving.entrypoints=websecure"
      - "traefik.http.routers.opendiving.tls.certresolver=letsencrypt"
      - "traefik.http.services.opendiving.loadbalancer.server.port=3000"
```

Traefik sets `X-Forwarded-For` itself; make sure its address is in `TRUSTED_PROXY_IPS`, and that its
entrypoint is not configured to strip forwarded headers.

### Nginx Proxy Manager

Add a Proxy Host: domain `dives.example.com`, scheme `http`, forward hostname `web`, forward port
`3000`, *Websockets support* on, and request a certificate on the SSL tab. NPM sets the forwarded
headers for you. Its container's address goes in `TRUSTED_PROXY_IPS`, and NPM's own default body
limit (1 MB) needs raising in *Advanced* for card uploads:

```nginx
client_max_body_size 12m;
```

Leave the *HSTS* toggles on the SSL tab off unless you also set `WEB_HSTS=off`, and keep any
`add_header` you use elsewhere out of that *Advanced* box — both for the reasons in step 6. *Block
Common Exploits* is unrelated to any of this and safe to leave however you have it.

## LAN, or no domain at all

An instance on a home network with no public name and no certificate. In `.env`:

```bash
CADDY_SITE_ADDRESS=:80              # Caddy cannot get a certificate for an IP address
FRONTEND_URL=http://192.168.1.10    # where emailed sign-in links point
SITE_URL=http://192.168.1.10        # the web app's own origin
AUTH_COOKIE_SECURE=false            # or every reload signs the user out
```

`DOMAIN` still has to be set to something — the compose file uses it to derive the two URLs above,
and setting them explicitly is what overrides it.

Sign-in still needs a mail relay: the magic link has to reach an inbox. A LAN instance can run
`ENVIRONMENT=local` with no `SMTP_HOST`, which logs the link to `docker compose logs api` instead of
emailing it — fine for one person who has shell access, and no way to onboard anyone else.

Passkeys are not offered on this shape, and that is expected: browsers hand out WebAuthn only in a
secure context, and an IP address is not a valid passkey domain even behind a certificate. The
emailed link and code are the whole sign-in story here. Giving the box a real hostname with a
certificate the browser trusts — a Tailscale HTTPS name, an internal CA — makes it eligible; point
`FRONTEND_URL` at that `https://` name and the option appears. See
[configuration.md](configuration.md#sign-in).

`WEB_HSTS=off` matters only if the same browser also reaches this instance over HTTPS through
something else; the header is never sent on a request that arrived over plain HTTP.

Don't enable the admin panel on a plain-HTTP instance that is also `ENVIRONMENT=production`: the
panel enforces HTTPS there, so it redirects `/admin` to an `https://` URL this instance does not
answer on. Either put a certificate in front of it or leave the panel off — it is off by default,
and the API's own endpoints are unaffected.
