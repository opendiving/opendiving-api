"""Enforce reviewed content permissions before handlers and their caches run."""

from typing import Literal

from fastapi.routing import iter_route_contexts
from starlette._utils import get_route_path
from starlette.requests import Request

from ..core.exceptions.http_exceptions import ForbiddenException
from .account_views import PREFERENCES, PROFILE
from .operations import OPERATIONS

_REQUIREMENTS: dict[tuple[str, str], tuple[str, ...]] = {}
for _op in OPERATIONS:
    if _op.account_view is None:
        _key = (_op.method, _op.path)
        if _key in _REQUIREMENTS and _REQUIREMENTS[_key] != _op.scopes:
            raise ValueError(f"Conflicting content permissions for {_key}")
        _REQUIREMENTS[_key] = _op.scopes

_ACCOUNT_VIEWS = {"profile": PROFILE, "preferences": PREFERENCES}


def account_fields(scopes: frozenset[str], action: Literal["read", "write"]) -> frozenset[str]:
    return frozenset(
        field
        for domain, view in _ACCOUNT_VIEWS.items()
        if f"{domain}:{action}" in scopes
        for field in (view.read_fields if action == "read" else view.write_fields)
    )


def permission_requirements(method: str, path: str) -> list[tuple[str, ...]]:
    """Alternative permission sets for the public operation; each set requires every scope."""
    if path == "/api/v1/user" and method in {"GET", "PATCH"}:
        action = "read" if method == "GET" else "write"
        return [(f"{domain}:{action}",) for domain in _ACCOUNT_VIEWS]
    required = _REQUIREMENTS.get((method, path))
    return [required] if required is not None else []


async def authorize_content(request: Request, scopes: frozenset[str]) -> None:
    # FastAPI's lazy includes leave scope["route"] at the unprefixed original.
    # Resolve its public context as OpenAPI does, including reuse under other prefixes.
    route = request.scope.get("route")
    path = next(
        (
            context.path_format
            for context in iter_route_contexts(request.app.routes)
            if context.original_route is route
            and request.method in (context.methods or set())
            and context.path_regex.fullmatch(get_route_path(request.scope))
        ),
        None,
    )
    if path is None:
        raise ForbiddenException("Permission not granted for this operation.")
    if path == "/api/v1/user" and request.method in {"GET", "PATCH"}:
        action: Literal["read", "write"] = "read" if request.method == "GET" else "write"
        allowed = account_fields(scopes, action)
        if not allowed:
            raise ForbiddenException(f"Account {action} permission required.")
        if action == "write":
            try:
                body = await request.json()
            except ValueError, UnicodeDecodeError:
                raise ForbiddenException("A JSON object containing permitted account fields is required.") from None
            if not isinstance(body, dict) or not set(body) <= allowed:
                raise ForbiddenException("Permission not granted for these account fields.")
    else:
        required = _REQUIREMENTS.get((request.method, path))
        if required is None or not set(required) <= scopes:
            raise ForbiddenException("Permission not granted for this operation.")
    request.state.content_scopes = scopes
