"""The responses every route serving stored bytes builds, and the 404 a credential-free route
returns rather than raises."""

from fastapi import Response
from fastapi.responses import JSONResponse

# `frame-ancestors` is spelled out because it does not fall back to `default-src`: a response
# carrying its own policy opts out of `SecurityHeadersMiddleware`'s default and would otherwise
# be framable however strict the rest of this is.
BINARY_CSP = "default-src 'none'; sandbox; frame-ancestors 'none'"


def not_modified(etag: str, *, cache_control: str) -> Response:
    """The bare 304 for a request whose `If-None-Match` is `etag`. A bare `Response`, so a
    route with a `response_model` skips the validation an empty body would fail."""
    return Response(status_code=304, headers={"ETag": etag, "Cache-Control": cache_control})


def stored_bytes_response(
    data: bytes, *, media_type: str, etag: str, cache_control: str, disposition: str | None = None
) -> Response:
    """Stored bytes under their digest. `nosniff` and the sandboxing policy always: these are
    bytes a diver or a third party supplied, and the browser must not be free to re-interpret
    them as something scriptable at this origin. `disposition` is each route's own call.
    """
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": BINARY_CSP,
        "Cache-Control": cache_control,
        "ETag": etag,
    }
    if disposition is not None:
        headers["Content-Disposition"] = disposition
    return Response(content=data, media_type=media_type, headers=headers)


def uncacheable_not_found(detail: str) -> JSONResponse:
    """A 404 labelled `private, no-store` - returned, not raised, because a raised one reaches
    `ClientCacheMiddleware` unlabelled, and it labels a credential-free `GET` publicly cacheable.
    Where a token in the path is the credential, a revoked one has to stop answering at the next
    request rather than when a shared cache lets go."""
    return JSONResponse(status_code=404, content={"detail": detail}, headers={"Cache-Control": "private, no-store"})
