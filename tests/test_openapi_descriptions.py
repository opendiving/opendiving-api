"""The operation descriptions `/api/v1/openapi.json` publishes are written for a caller.

A route handler's docstring is that description, rendered at `/developers` on every instance,
so it states the contract: what the operation does, what it needs, what it answers and how it
fails. The reasoning behind the route is a comment at the route (`AGENTS.md`, *Code Style*),
which this document never carries. These checks catch the ways that reasoning leaks: a code
identifier named in backticks, an operator setting, a pointer into the repository's own
documents, and history.
"""

import ast
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.app.core.config import settings
from src.app.main import app

APP_ROOT = Path(__file__).resolve().parent.parent / "src" / "app"

# Each one names the code or the repository, never the API. `.md` covers `DECISIONS.md`
# and its siblings; `iddleware` covers both spellings of the word.
BANNED_PHRASES = ("@cache", "Redis", "iddleware", ".md", "used to ", "no longer", "pytest")

BACKTICKED = re.compile(r"`([^`]+)`")


def _operations(document: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for path, methods in document["paths"].items():
        for method, operation in methods.items():
            if isinstance(operation, dict):
                yield f"{method.upper():6} {path}", operation


def _defined_names() -> set[str]:
    """Every module, function and class the application defines, by name alone."""
    names: set[str] = set()
    for source in APP_ROOT.rglob("*.py"):
        names.add(source.stem)
        names.add(source.parent.name)
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names.add(node.name)
    return names


def _contract_names(document: dict[str, Any]) -> set[str]:
    """The names a caller legitimately meets: published schemas, their properties and the
    request parameters."""
    names: set[str] = set()
    for schema_name, schema in document.get("components", {}).get("schemas", {}).items():
        names.add(schema_name)
        names.update(schema.get("properties", {}))
    for _, operation in _operations(document):
        names.update(parameter["name"] for parameter in operation.get("parameters") or [])
    return names


def _internal_mentions(description: str, internal: set[str]) -> set[str]:
    """The backticked spans of a description that name the implementation.

    A span is judged by what it starts with, so `UserRead.avatar_sha256` passes on the
    published schema and `navigator.credentials.get()` on the browser API, while
    `services.refresh_grace` and `auth.check_email_link` fail on the module. It counts when
    that first segment is a module, function or class the app defines and no published
    schema, property or parameter shares the name, when it is a private name, or when the
    whole span is one of the instance's settings.
    """
    found: set[str] = set()
    for span in BACKTICKED.findall(description):
        head = span.split("(")[0]
        first = head.split(".")[0]
        if first.startswith("_") or first in internal:
            found.add(span)
        elif "_" in head and head.isupper() and hasattr(settings, head):
            found.add(span)
    return found


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return app.openapi()


@pytest.fixture(scope="module")
def internal(document: dict[str, Any]) -> set[str]:
    return _defined_names() - _contract_names(document)


def test_the_name_sets_tell_the_implementation_from_the_contract(document: dict[str, Any], internal: set[str]) -> None:
    """Without this the sweeps below pass for sets that caught nothing: a dependency
    nobody calls through the API is internal, a query parameter a caller passes is not,
    even where the app also defines a function by that name."""
    assert {"get_current_user", "fetch_owned_or_raise", "ClientCacheMiddleware", "services"} <= internal
    assert {"move_dives_to", "UserRead"} <= _contract_names(document)
    assert {"move_dives_to", "UserRead"}.isdisjoint(internal)


def test_descriptions_name_no_code(document: dict[str, Any], internal: set[str]) -> None:
    offenders = {
        name: mentions
        for name, operation in _operations(document)
        if (mentions := _internal_mentions(operation.get("description", ""), internal))
    }

    assert not offenders, "these descriptions name the implementation:\n" + "\n".join(
        f"  {name}: {', '.join(sorted(mentions))}" for name, mentions in sorted(offenders.items())
    )


def test_descriptions_carry_no_rationale_markers(document: dict[str, Any]) -> None:
    offenders = {
        name: hits
        for name, operation in _operations(document)
        if (hits := [phrase for phrase in BANNED_PHRASES if phrase in operation.get("description", "")])
    }

    assert not offenders, "these descriptions point at the code or its history:\n" + "\n".join(
        f"  {name}: {', '.join(hits)}" for name, hits in sorted(offenders.items())
    )
