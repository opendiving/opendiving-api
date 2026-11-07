"""Which credentials can call each operation, published on the OpenAPI document.

The document's `security` says only "a bearer header": an access token and an API key share it, so
it cannot tell a caller that a read key stops at the safe methods, that the credential routes refuse
any key, or that `/admin` needs a superuser. Each operation carries `x-opendiving-credentials`, the
list of `CREDENTIALS` it accepts, so a docs page can show one credential's routes.

The list is derived from the dependencies that enforce it, the same walk the route guards in
`tests/` make, so a route cannot change who it accepts without the document following.
"""

from collections.abc import Callable
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts

from ..api.dependencies import (
    SAFE_METHODS,
    get_current_superuser,
    get_current_user,
    get_downloading_user,
    refuse_api_keys,
    refuse_unscoped_api_keys,
)
from ..authorization.policy import permission_requirements
from .security import bearer_scheme

EXTENSION = "x-opendiving-credentials"

Credential = Literal["anonymous", "session", "superuser", "api-key-read", "api-key-write", "api-key-scoped"]

#: Credential modes shown in the API reference. Scoped access depends on each operation's permissions.
CREDENTIALS: tuple[Credential, ...] = (
    "anonymous",
    "session",
    "superuser",
    "api-key-read",
    "api-key-write",
    "api-key-scoped",
)


def _calls(dependant: Dependant) -> set[Callable[..., Any]]:
    calls = set() if dependant.call is None else {dependant.call}
    for sub_dependant in dependant.dependencies:
        calls |= _calls(sub_dependant)
    return calls


def credentials_for(method: str, dependant: Dependant, path: str) -> list[Credential]:
    """The credentials a request with this method may present to a route with this dependant.

    A key is accepted only where `get_current_user` runs, since that is where its prefix is
    resolved: `POST /auth/logout`, which takes `bearer_scheme` alone, reads an access token and
    nothing else. A key's account is never a superuser, so a superuser route refuses every key.
    """
    calls = _calls(dependant)
    if get_current_superuser in calls:
        return ["superuser"]
    # `get_downloading_user` resolves a bearer header through `get_current_user`, keys included.
    resolves_keys = bool(calls & {get_current_user, get_downloading_user})
    if not resolves_keys and bearer_scheme not in calls:
        return list(CREDENTIALS)

    accepted: list[Credential] = ["session", "superuser"]
    if resolves_keys and refuse_api_keys not in calls:
        if refuse_unscoped_api_keys not in calls:
            if method.upper() in SAFE_METHODS:
                accepted.append("api-key-read")
            accepted.append("api-key-write")
        if permission_requirements(method.upper(), path):
            accepted.append("api-key-scoped")
    return accepted


def annotate_credentials(app: FastAPI, document: dict[str, Any]) -> None:
    """Set `EXTENSION` on every operation of `document`, the document `app.openapi()` built."""
    paths = document.get("paths", {})
    for context in iter_route_contexts(app.routes):
        if not isinstance(context.original_route, APIRoute) or not context.original_route.include_in_schema:
            continue
        operations = paths.get(context.path_format)
        if operations is None or context.dependant is None:
            continue
        for method in context.methods or set():
            operation = operations.get(method.lower())
            if operation is not None:
                operation[EXTENSION] = credentials_for(method, context.dependant, context.path_format or "")
                requirements = permission_requirements(method.upper(), context.path_format or "")
                if "api-key-scoped" in operation[EXTENSION] and requirements:
                    operation["x-opendiving-permissions"] = {"anyOf": [list(scopes) for scopes in requirements]}
