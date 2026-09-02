from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Middleware that puts frame protection and `nosniff` on every response the API sends.

    Why the app and not the proxy
    -----------------------------
        The bundled `Caddyfile` (https://github.com/opendiving/opendiving/blob/main/Caddyfile)
        routes `/api/v1*` and the three docs paths straight to this app, so nothing the web
        container sets reaches them - and a bring-your-own-proxy install
        (https://github.com/opendiving/opendiving/blob/main/docs/reverse-proxy.md) is a
        config file this repository never sees. Setting them here is the only version of this
        that holds for *every* deployment shape, including a developer's `docker compose
        up`, and it is a policy about this app's own responses rather than about a
        surface someone else owns: `/docs` is a route in `core/setup.py`, and the CRUDAdmin
        panel, where an operator has enabled one, is mounted on this FastAPI app.

        The surface that actually needs it is that panel - CRUDAdmin ships no security
        headers of its own, and it is a full create/update/delete interface over `User`,
        `Dive`, `GearItem` and everything else in `admin/views.py`. It is not urgent:
        CRUDAdmin's session cookie is `SameSite=strict` outside debug mode, so a
        cross-site frame carries no cookie and renders a logged-out panel. That is a
        third-party default we don't control, which is the argument for having a header
        of our own rather than against it.

        **`/admin` is the web app's now, and none of the above turns on it.** The operator's
        surface is a superuser-gated section of the web app driving the JSON routes in
        `api.v1.admin`; the bundle's Caddyfile no longer sends `/admin*` here, and an
        operator who still enables the CRUDAdmin panel mounts it elsewhere with
        `CRUD_ADMIN_MOUNT_PATH` and routes it by hand. What reaches this middleware is
        therefore `/api/v1*`, the docs paths, and whatever mount path the panel has - which
        is exactly the set the argument above was always really about.

    What it deliberately does not do
    --------------------------------
        - **The CSP is `frame-ancestors` and nothing else.** A `default-src` here would
          break CRUDAdmin's own templates, which style themselves inline and pull
          `htmx.min.js` and a favicon from the panel's `/static` and a webfont from
          `fonts.googleapis.com`. `frame-ancestors` restricts framing only and
          constrains none of that.
        - **No `Strict-Transport-Security`.** HSTS is recorded per *host*, not per path,
          so the web app's header already covers everything this app serves on any domain
          whose visitor has loaded one page of it. Sending it from here as well would put
          two controls on one behaviour and, worse, ignore `WEB_HSTS=off` - the switch a
          plain-HTTP LAN instance uses precisely because a pin it cannot honour makes the
          instance unreachable.

    Note
    ----
        - Headers already set by the endpoint are left alone, the same contract
          `ClientCacheMiddleware` keeps with `Cache-Control`. The binary-download
          responses (`dives.py`, `certifications.py`, `export.py`, and `users.py`'s
          avatar) set a much stricter `default-src 'none'; sandbox`, and they name
          `frame-ancestors 'none'` themselves - it does *not* fall back to `default-src`,
          so a policy that omits it grants framing however strict the rest of it is.
          Deliberately not counted here: the list grows, and a number in a docstring is
          the copy that stops being true.
        - `X-Frame-Options` is redundant in every browser that supports
          `frame-ancestors` (Chrome 40, Firefox 33, Safari 10), which is every browser
          that can run the panel. It is sent anyway for parity with the web app, which
          made the same call in `next.config.js`, and because an absent one is a finding
          in the scanners self-hosters point at their own instances. Where both are
          present the browser uses the CSP and ignores it, so it cannot conflict.
    """

    #: Set on a response only when it doesn't carry the header already.
    DEFAULT_HEADERS = {
        "Content-Security-Policy": "frame-ancestors 'none'",
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
    }

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Add the default security headers to whatever the rest of the app produced."""
        response: Response = await call_next(request)

        for header, value in self.DEFAULT_HEADERS.items():
            if header not in response.headers:
                response.headers[header] = value

        return response
