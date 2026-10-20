"""Every sentence logbook import shows a diver, read off the source rather than off a run.

A note on a dive's row, a group in the report, a file's refusal and a request's error all
reach the Import page as written, so each is held to one rule: it names a value of the dive
as the form names it, never a spec section, a member in code form, or the machinery - a
conversion, a reader, a generator, a channel, an axis. Read statically, because a rendered
message can carry a diver's own tag name or check-in label, which no rule here may judge,
and because a test over runs reaches only the branches its fixtures exercise.
"""

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

SERVICES = Path(__file__).resolve().parents[1] / "src" / "app" / "services"

# Which calls carry a sentence, per module: the note helpers and the refusals a module raises.
SITES: dict[Path, frozenset[str]] = {
    SERVICES / "logbook_import" / "planner.py": frozenset({"_note", "_skip", "_dropped", "Bound"}),
    SERVICES / "logbook_import" / "writer.py": frozenset({"_note", "_file_skipped"}),
    SERVICES / "logbook_import" / "check_in.py": frozenset({"note"}),
    SERVICES / "logbook_import" / "reader.py": frozenset(
        {
            "ReaderNote",
            "UnsupportedImportError",
            "MalformedImportError",
            "ImportTooLargeError",
            "DuplicateMemberError",
        }
    ),
    SERVICES / "logbook_import" / "parts.py": frozenset({"MalformedRequestError", "ImportTooLargeError"}),
    SERVICES / "recording_shape.py": frozenset({"drop", "Bound"}),
}

BANNED = re.compile(r"§|`|convert|channel|reader|generator|axis", re.IGNORECASE)


def _called(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _literals(node: ast.AST) -> Iterator[str]:
    """Every string a sentence can be built from inside `node`, f-strings' fixed parts included."""
    for inner in ast.walk(node):
        if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
            yield inner.value


def _names(node: ast.AST) -> set[str]:
    return {inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name)}


def _bindings(scope: ast.AST, name: str) -> Iterator[ast.AST]:
    """What `name` is bound to in `scope`: an assignment's value, or the iterable a loop or a
    comprehension draws it from - so a sentence passed by name is followed to its literals."""
    for inner in ast.walk(scope):
        if isinstance(inner, ast.Assign) and any(name in _names(target) for target in inner.targets):
            yield inner.value
        elif isinstance(inner, ast.AnnAssign) and inner.value is not None and name in _names(inner.target):
            yield inner.value
        elif isinstance(inner, (ast.For, ast.comprehension)) and name in _names(inner.target):
            yield inner.iter


def _scopes(tree: ast.Module) -> Iterator[tuple[ast.AST, ast.AST]]:
    """Every node with the function it sits in, or the module for one at module level."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                yield node, inner
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for inner in ast.walk(node):
                yield tree, inner


def _sentences(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text())
    wanted = SITES[path]
    found: list[tuple[int, str]] = []
    for scope, node in _scopes(tree):
        if not isinstance(node, ast.Call) or _called(node) not in wanted:
            continue
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            found.extend((node.lineno, text) for text in _literals(argument))
            for name in _names(argument) - {"self"}:
                for bound in (*_bindings(scope, name), *_bindings(tree, name)):
                    found.extend((node.lineno, text) for text in _literals(bound))
    return found


@pytest.mark.parametrize("path", sorted(SITES), ids=lambda path: path.name)
def test_no_sentence_names_the_machinery(path: Path) -> None:
    offending = sorted({(line, text) for line, text in _sentences(path) if BANNED.search(text)})
    assert offending == []


@pytest.mark.parametrize("path", sorted(SITES), ids=lambda path: path.name)
def test_the_sweep_reaches_the_module(path: Path) -> None:
    """A renamed helper would leave its module's sentences unread and the test above green."""
    assert _sentences(path)
