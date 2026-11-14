"""Experimental features: merged to `main`, gated, and off unless the instance's
`EXPERIMENTAL_FEATURES` names them or says `all`.

The setting is the only thing consulted - not the environment, not the account, not the
client - so a feature switched on at an instance shows to everyone there. A name exists where a
gate names it: one the setting holds and no gate asks about is inert.

Where a feature is off, its routes answer as a build without them would and the API document
leaves them out (`documented_routes`), components only they reach included, since a model's name
and fields show a feature as surely as its path does.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, RouteContext, iter_route_contexts
from starlette.routing import BaseRoute

from .config import EVERY_EXPERIMENTAL_FEATURE, EXPERIMENTAL_FEATURE_PATTERN, settings
from .exceptions.http_exceptions import NotFoundException


def _feature_name(name: str) -> str:
    if name == EVERY_EXPERIMENTAL_FEATURE or not re.fullmatch(EXPERIMENTAL_FEATURE_PATTERN, name):
        raise ValueError(
            f"{name!r} cannot name an experimental feature: a name is lowercase letters, digits and "
            f"hyphens, and never {EVERY_EXPERIMENTAL_FEATURE!r}."
        )
    return name


def experimental_feature_on(name: str) -> bool:
    """Whether this instance switches the feature on, for what is not a route: a cron, a
    lifespan hook, a sender."""
    feature = _feature_name(name)
    features = settings.experimental_features
    return EVERY_EXPERIMENTAL_FEATURE in features or feature in features


@dataclass(frozen=True)
class ExperimentalFeatureGate:
    """The dependency `require_experimental_feature` builds; `feature` is what finds it again in a
    route's dependencies."""

    feature: str

    def __call__(self) -> None:
        # The detail an unrouted path answers with, so an off feature's route reads as absent.
        if not experimental_feature_on(self.feature):
            raise NotFoundException("Not Found")


def require_experimental_feature(name: str) -> ExperimentalFeatureGate:
    """Gate a router or a single route on the feature, as `dependencies=[Depends(...)]`.

    List it ahead of every other dependency: FastAPI runs them in order, and one that signs the
    caller in first would answer 401 where a build without the route answers 404. A router's
    dependencies run before its routes', so under a router that signs the caller in, the gate goes
    on that router or on the `include_router` that adds it.
    """
    return ExperimentalFeatureGate(_feature_name(name))


def gating_features(dependant: Dependant) -> set[str]:
    """The experimental features a request through this dependant needs, `Depends` nested to any
    depth."""
    features = {dependant.call.feature} if isinstance(dependant.call, ExperimentalFeatureGate) else set()
    for sub_dependant in dependant.dependencies:
        features |= gating_features(sub_dependant)
    return features


def documented_routes(routes: Iterable[BaseRoute]) -> list[RouteContext]:
    """The route table less every route a feature that is off gates, which is what the API
    document is built from."""
    return [
        context
        for context in iter_route_contexts(list(routes))
        if not (
            isinstance(context.original_route, APIRoute)
            and context.dependant is not None
            and not all(experimental_feature_on(feature) for feature in gating_features(context.dependant))
        )
    ]
