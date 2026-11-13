"""The reviewed operation catalog that scoped API keys and MCP connections share.

A route missing from `OPERATIONS` is refused to every scoped credential, so a forgotten route and
a deliberately withheld one look the same at runtime. The walk below makes the difference a
decision someone wrote down.
"""

from src.app.api.dependencies import get_current_superuser, get_current_user, refuse_api_keys
from src.app.authorization.operations import OPERATIONS
from src.app.authorization.policy import permission_requirements
from src.app.main import app
from tests.helpers.routes import dependency_calls, iter_api_routes

# Withheld from scoped keys and MCP on purpose. Routes declaring `refuse_api_keys` (credentials,
# sign-in identity, account deletion, check-in links) and superuser routes are withheld by rule.
EXCLUDED: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/user/invitations"): "account management: who the diver invited to the instance",
    ("POST", "/api/v1/user/invitations"): "account management: invites a person and emails them",
    ("DELETE", "/api/v1/user/invitation/{uuid}"): "account management: withdraws an invitation",
    ("POST", "/api/v1/dive-site/{uuid}/proposals"): "carries the diver's own attestation to publish a site",
    ("GET", "/api/v1/commons/dive-sites/points"): "an instance-wide map feed, not the diver's content",
    ("GET", "/api/v1/map-tiles/{theme}/{z}/{x}/{y}"): "map rendering assets, not the diver's content",
    ("GET", "/api/v1/map-style/{theme}"): "map rendering assets, not the diver's content",
}


def _withheld_by_rule(calls: list[object]) -> bool:
    return refuse_api_keys in calls or get_current_superuser in calls


def test_every_authenticated_route_is_classified() -> None:
    listed = {(op.method, op.path) for op in OPERATIONS}
    unclassified = sorted(
        str(route)
        for route in iter_api_routes(app)
        if get_current_user in (calls := dependency_calls(route.dependant))
        and not _withheld_by_rule(calls)
        and route.key not in listed
        and route.key not in EXCLUDED
    )
    assert not unclassified, (
        "add these routes to OPERATIONS with their permissions, or to EXCLUDED with a reason:\n"
        + "\n".join(unclassified)
    )


def test_exclusions_name_live_routes_outside_the_catalog() -> None:
    routes = {route.key for route in iter_api_routes(app)}
    listed = {(op.method, op.path) for op in OPERATIONS}
    assert set(EXCLUDED) <= routes
    assert not set(EXCLUDED) & listed
    assert all(reason for reason in EXCLUDED.values())


def test_every_operation_names_a_route_scoped_keys_can_reach() -> None:
    routes = {route.key: dependency_calls(route.dependant) for route in iter_api_routes(app)}
    for op in OPERATIONS:
        assert (op.method, op.path) in routes, op.name
        assert not _withheld_by_rule(routes[(op.method, op.path)]), op.name


def test_species_resolve_needs_write_because_it_adds_to_the_shared_catalog() -> None:
    assert permission_requirements("POST", "/api/v1/species/resolve") == [("dives:write",)]
