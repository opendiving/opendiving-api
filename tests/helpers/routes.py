"""Walking the app's real route table, for the tests that enumerate it.

Two structural guards ask the same question of every registered route - "does this one
require authentication?" and "does this one resolve ownership?" - so both start here
rather than each growing its own walk.

`app.routes` is not a flat list of `APIRoute` any more. Since FastAPI 0.141
`include_router` stores a lazy `_IncludedRouter` wrapper, and the *effective* routes -
the ones carrying the `/api` and `/v1` prefixes - are composed on demand. Iterating
`app.routes` directly finds three objects: two wrappers and the admin `Mount`.
`iter_route_contexts` is the public flattener FastAPI's own `get_openapi` uses, so the
guards go through it instead of reaching into the private wrapper.

Going through it buys one thing beyond the paths. A `RouteContext` carries the *merged*
dependant, which a route's own `route.dependant` is not: a dependency attached at
`include_router(..., dependencies=[...])` lives only on the include context. Nothing
does that today, but a walk that missed it would report a protected route as anonymous
and send whoever hit it looking for a bug in the route.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts

API_PREFIX = "/api/v1"


@dataclass(frozen=True)
class RouteInfo:
    """One route *and method*, since the two differ in what they're allowed to do."""

    method: str
    path: str
    endpoint: Callable[..., Any]
    dependant: Dependant

    @property
    def key(self) -> tuple[str, str]:
        return self.method, self.path

    def __str__(self) -> str:
        return f"{self.method:6} {self.path}"


def iter_api_routes(app: FastAPI) -> Iterator[RouteInfo]:
    """Every `APIRoute` under `/api/v1`, one `RouteInfo` per HTTP method.

    Scoped to the API prefix on purpose, and the scope is part of what the guards mean.
    `/docs`, `/redoc` and `/openapi.json` are registered conditionally on `ENVIRONMENT`
    (`core/setup.py`) - absent on production, superuser-gated on staging, wide open on
    local, which is what the suite runs as. They would otherwise read as anonymous and
    need allowlisting for a reason that is really "this is a different property".

    Mounts are skipped explicitly rather than left to fall out of a default. The admin
    panel is a `starlette.routing.Mount` with its own session auth, and while
    `CRUD_ADMIN_ENABLED` is normally false, a developer's `src/.env` may well enable it -
    a guard that only holds on one configuration is not a guard.
    """
    for context in iter_route_contexts(app.routes):
        if not isinstance(context.original_route, APIRoute):
            continue
        path = context.path or ""
        if not path.startswith(API_PREFIX):
            continue
        endpoint, dependant = context.endpoint, context.dependant
        assert endpoint is not None and dependant is not None, f"{path} has no endpoint or dependant"
        for method in sorted((context.methods or set()) - {"HEAD", "OPTIONS"}):
            yield RouteInfo(method=method, path=path, endpoint=endpoint, dependant=dependant)


def dependency_calls(dependant: Dependant) -> list[Any]:
    """Every callable a request to this route runs, `Depends` nested to any depth.

    Note there is no separate security-scheme list to walk. `Dependant` carried one
    (`security_requirements`) up to FastAPI 0.140; from 0.141 a security scheme is an
    ordinary sub-dependant whose `call` *is* the scheme instance, so `oauth2_scheme`
    turns up here like anything else. `test_the_bearer_scheme_alone_counts` pins that
    down, because the day it stops being true is the day this walk quietly stops seeing
    the only marker two routes have.
    """
    calls = [] if dependant.call is None else [dependant.call]
    for sub_dependant in dependant.dependencies:
        calls.extend(dependency_calls(sub_dependant))
    return calls
