from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint


class ClientCacheMiddleware(BaseHTTPMiddleware):
    """Middleware to set a default `Cache-Control` header on responses that don't already specify one.

    Parameters
    ----------
    app: FastAPI
        The FastAPI application instance.
    max_age: int, optional
        Duration (in seconds) for which the response should be cached. Defaults to 60 seconds.

    Attributes
    ----------
    max_age: int
        Duration (in seconds) for which the response should be cached.

    Methods
    -------
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        Process the request and set the `Cache-Control` header in the response.

    Note
    ----
        - The `Cache-Control` header instructs clients (e.g., browsers)
        to cache the response for the specified duration.
        - `public` is only ever applied to a *safe* (GET/HEAD) request that carried no
          credential at all, and it never overwrites a `Cache-Control` header the
          endpoint already set. Anything else gets `private, no-store`.
        - The method check is not belt-and-braces, it is load-bearing. "No `Authorization`
          header" was previously taken to mean "not user-specific", which is exactly
          backwards for the endpoints that *mint* credentials: `POST /auth/email/verify`,
          `/auth/google`, `/auth/complete` and `/auth/refresh` are unauthenticated by
          nature - the caller has no token yet, that is the point - and every one of them
          returns an access token in the body. They were being labelled
          `public, max-age=60`.
        - A safe method is still not sufficient on its own: a GET whose *query string*
          carries a secret is equally unsafe to cache publicly. `GET /auth/email/verify/check`
          takes a magic-link token and returns the account's email, and sets its own
          `Cache-Control` for that reason - which the first check here preserves.
        - A bearer token is not the only credential this app accepts, which is how the
          same mistake came back in a second shape. CRUDAdmin authenticates with a
          `session_id` **cookie**, so every signed-in request under `CRUD_ADMIN_MOUNT_PATH`
          is a safe method with no `Authorization` header. Its model pages survived that
          only because CRUDAdmin sets `no-store` on them itself and the check above
          preserves it - and CRUDAdmin deliberately skips its own header on the login
          path, on `/static/`, and on every redirect, all of which this middleware was
          labelling `public, max-age=60` for a signed-in admin. `Cookie` therefore counts
          as a credential here alongside `Authorization`, rather than leaving the answer
          to a dependency.
        - Keying on the cookie rather than on the admin's mount path is deliberate.
          `CRUD_ADMIN_MOUNT_PATH` is operator-configurable, and a path check answers only
          for the surface that happened to be known when it was written - the same
          allowlist-by-omission that let the token-minting POSTs through. See
          *"`public` requires the absence of every credential, not just a bearer token"*
          in `DECISIONS.md`, which has the measured before/after.
    """

    #: Methods whose responses may be marked publicly cacheable. Everything else either
    #: changes state or, as above, hands back a credential.
    SAFE_METHODS = frozenset({"GET", "HEAD"})

    #: Request headers that can carry a credential. *Any* of them present means the
    #: response may be specific to whoever sent it. Deliberately the whole `Cookie`
    #: header rather than a list of known session cookie names: a name list is another
    #: thing to keep in step with every auth surface, and being wrong about it fails
    #: open. Being wrong this way only costs a cache hit.
    CREDENTIAL_HEADERS = frozenset({"Authorization", "Cookie"})

    #: What the public branch declares it varied on. Derived from the set above rather
    #: than typed out again: a third credential header added there but forgotten here
    #: would still get `private, no-store` on its own request, while leaving shared
    #: caches free to answer it from the anonymous entry they stored - which is the
    #: fail-open direction, and the exact thing the `Vary` exists to prevent.
    VARY_ON_CREDENTIALS = ", ".join(sorted(CREDENTIAL_HEADERS))

    def __init__(self, app: FastAPI, max_age: int = 60) -> None:
        super().__init__(app)
        self.max_age = max_age

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Process the request and set the `Cache-Control` header in the response.

        Parameters
        ----------
        request: Request
            The incoming request.
        call_next: RequestResponseEndpoint
            The next middleware or route handler in the processing chain.

        Returns
        -------
        Response
            The response object with the `Cache-Control` header set.

        Note
        ----
            - This method is automatically called by Starlette for processing the request-response cycle.
        """
        response: Response = await call_next(request)

        # Never override a `Cache-Control` header the endpoint already set explicitly.
        if "Cache-Control" in response.headers:
            return response

        # `public` requires both: a safe method, and no credential on the request. Either
        # one alone is insufficient - see the class docstring for the two times a partial
        # check labelled a private response publicly cacheable.
        is_safe = request.method in self.SAFE_METHODS
        is_anonymous = not any(header in request.headers for header in self.CREDENTIAL_HEADERS)

        if is_safe and is_anonymous:
            response.headers["Cache-Control"] = f"public, max-age={self.max_age}"
            # The decision above was made by reading request headers, so a shared cache
            # has to be told which ones - otherwise it serves this anonymous entry to the
            # next request for the same URL that *does* carry a credential. Concretely:
            # the login redirect an unauthenticated `GET /admin/` gets, replayed to a
            # signed-in admin. `add_vary_header` merges rather than overwrites, so the
            # `Vary: Origin` the CORS middleware sets survives.
            response.headers.add_vary_header(self.VARY_ON_CREDENTIALS)
        else:
            response.headers["Cache-Control"] = "private, no-store"

        return response
