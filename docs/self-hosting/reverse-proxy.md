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

`WEB_HSTS=off` matters only if the same browser also reaches this instance over HTTPS through
something else; the header is never sent on a request that arrived over plain HTTP.

Don't enable the admin panel on a plain-HTTP instance that is also `ENVIRONMENT=production`: the
panel enforces HTTPS there, so it redirects `/admin` to an `https://` URL this instance does not
answer on. Either put a certificate in front of it or leave the panel off — it is off by default,
and the API's own endpoints are unaffected.
