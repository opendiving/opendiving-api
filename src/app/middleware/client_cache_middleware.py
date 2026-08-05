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
        - This middleware never marks a response `public` if the request carried an
          `Authorization` header, and it never overwrites a `Cache-Control` header that
          the endpoint already set, since that would risk a shared proxy/CDN caching one
          user's private response and serving it to another.
    """

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

        # Authenticated requests (identified by an `Authorization` header) may return
        # per-user data. Mark those responses as private/non-cacheable-by-default rather
        # than `public`, so a shared proxy/CDN won't serve one user's response to another.
        if "Authorization" in request.headers:
            response.headers["Cache-Control"] = "private, no-store"
        else:
            response.headers["Cache-Control"] = f"public, max-age={self.max_age}"

        return response
