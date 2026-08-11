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
          `Authorization` header, and it never overwrites a `Cache-Control` header the
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
    """

    #: Methods whose responses may be marked publicly cacheable. Everything else either
    #: changes state or, as above, hands back a credential.
    SAFE_METHODS = frozenset({"GET", "HEAD"})

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

        # `public` requires both: a safe method, and no `Authorization` header. Either one
        # alone is insufficient - see the class docstring for why the header check on its
        # own labelled every token-minting auth response as publicly cacheable.
        is_safe = request.method in self.SAFE_METHODS
        is_anonymous = "Authorization" not in request.headers

        if is_safe and is_anonymous:
            response.headers["Cache-Control"] = f"public, max-age={self.max_age}"
        else:
            response.headers["Cache-Control"] = "private, no-store"

        return response
