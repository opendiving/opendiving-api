"""An in-process request capability, never encoded into an HTTP header or token.

Only the curated dispatcher creates this scope entry. Dependency overrides would be
process-global and could authenticate an unrelated concurrent external request.
"""

from dataclasses import dataclass
from typing import Any

from starlette.requests import Request

_CAPABILITY = object()
SCOPE_KEY = "opendiving.mcp.actor"


@dataclass(frozen=True)
class DelegatedActor:
    account: dict[str, Any]
    scopes: frozenset[str]
    capability: object = _CAPABILITY


def internal_actor(request: Request) -> DelegatedActor | None:
    actor = request.scope.get(SCOPE_KEY)
    if isinstance(actor, DelegatedActor) and actor.capability is _CAPABILITY:
        return actor
    return None
