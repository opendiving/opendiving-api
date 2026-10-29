from fastapi import HTTPException
from starlette._utils import get_route_path
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..api import router as api_router
from ..api.v1 import router as v1_router

# The largest single-file upload a route accepts - a picture or a card, 10 MB - plus 1 MB for
# the multipart framing and the form fields around it.
MAX_REQUEST_BODY_SIZE = 11 * 1024 * 1024

# The logbook import routes authenticate before they read a byte, then stream their own body
# under caps of their own, which run to hundreds of megabytes.
_API_PREFIX = f"{api_router.prefix}{v1_router.prefix}"
UNLIMITED_PATHS = frozenset({f"{_API_PREFIX}/import/logbook/preview", f"{_API_PREFIX}/import/logbook"})

_DETAIL = f"Request body too large. The maximum is {MAX_REQUEST_BODY_SIZE // (1024 * 1024)} MB."


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class BodySizeLimitMiddleware:
    """Refuses a request body over `MAX_REQUEST_BODY_SIZE` with a 413.

    FastAPI reads a route's body before any dependency runs, authentication included, and a
    multipart file part is spooled to disk with no cap of its own. Without this, a client
    with no credential streams an upload of any size before it is told 401.

    A declared `Content-Length` over the limit is refused before a byte is read. Anything
    else - a chunked body has no length to declare - is counted as it arrives.
    """

    def __init__(self, app: ASGIApp, max_body_size: int = MAX_REQUEST_BODY_SIZE) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or get_route_path(scope) in UNLIMITED_PATHS:
            await self.app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > self.max_body_size:
            await JSONResponse({"detail": _DETAIL}, status_code=413)(scope, receive, send)
            return

        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_size:
                    # Raised from inside the read rather than answered from here, because the
                    # reader is mid-request: FastAPI re-raises an `HTTPException` out of its
                    # body and form reading, where any other exception becomes a 400, and the
                    # route's exception handler then answers it as this 413.
                    raise HTTPException(status_code=413, detail=_DETAIL)
            return message

        await self.app(scope, counting_receive, send)
