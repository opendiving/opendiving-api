"""The reviewed operation catalog that scoped API keys and MCP connections share."""

from src.app.authorization.policy import permission_requirements


def test_species_resolve_needs_write_because_it_adds_to_the_shared_catalog() -> None:
    assert permission_requirements("POST", "/api/v1/species/resolve") == [("dives:write",)]
