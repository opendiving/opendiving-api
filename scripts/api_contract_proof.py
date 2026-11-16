"""Prove every row of the contract check's table against the real document, with the pinned differ.

    uv run python scripts/api_contract_proof.py                     # builds this checkout's document
    uv run python scripts/api_contract_proof.py --document doc.json

Each row is a mutation of the released surface with its verdict asserted, and the rows that name a
member are tried on a member spelled as a null union (`anyOf: [X, {"type": "null"}]`, the spelling
this document uses for an optional value) as well as on one that is not; the null rows on a scalar
and on an object. A differ upgrade that changes a verdict fails here, on the pull request that
makes it.

Every target is found in the document rather than named, so a schema renamed or removed elsewhere
never fails this; a document that holds no member of a shape a row needs fails with that shape
named. Response mutations touch only schemas no request or parameter reaches, and request
mutations only schemas no response reaches, so a verdict is the row's alone.
"""

import argparse
import copy
import json
import sys
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from api_contract import ROWS, Row, diff
from api_document import ROOT, build

Document = dict[str, Any]
Schema = dict[str, Any]

_NULL = {"type": "null"}
#: What FastAPI writes beside a null union rather than inside its value branch.
_OUTER = ("description", "title", "default", "examples")


class MissingShape(Exception):
    pass


def _refs(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        if isinstance(ref := node.get("$ref"), str):
            yield ref.rsplit("/", 1)[-1]
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


def _operations(doc: Document) -> Iterator[tuple[str, str, Schema]]:
    for path, item in sorted(doc["paths"].items()):
        for method, operation in sorted(item.items()):
            yield path, method, operation


def contexts(doc: Document) -> dict[str, set[str]]:
    """Which of `parameter`, `request` and `response` reach each component schema."""
    schemas = doc["components"]["schemas"]
    reached: dict[str, set[str]] = defaultdict(set)
    for _, _, operation in _operations(doc):
        for context, node in (
            ("parameter", operation.get("parameters", [])),
            ("request", operation.get("requestBody", {})),
            ("response", operation.get("responses", {})),
        ):
            stack = list(_refs(node))
            while stack:
                name = stack.pop()
                if context not in reached[name]:
                    reached[name].add(context)
                    stack += _refs(schemas[name])
    return reached


def null_branch(schema: Schema) -> Schema | None:
    """The value branch of a null union, or None for a schema that is not one."""
    branches = schema.get("anyOf")
    if isinstance(branches, list) and len(branches) == 2 and _NULL in branches:
        value: Schema = branches[1 - branches.index(_NULL)]
        return value
    return None


def wrap_in_null(schema: Schema) -> Schema:
    outer = {key: schema[key] for key in _OUTER if key in schema}
    inner = {key: value for key, value in schema.items() if key not in _OUTER}
    return {**outer, "anyOf": [inner, dict(_NULL)]}


def unwrap_null(schema: Schema) -> Schema:
    value = null_branch(schema)
    assert value is not None
    return {**{key: schema[key] for key in _OUTER if key in schema}, **value}


def _is_object_ref(doc: Document, schema: Schema) -> bool:
    ref = schema.get("$ref")
    return isinstance(ref, str) and doc["components"]["schemas"][ref.rsplit("/", 1)[-1]].get("type") == "object"


# -- what a mutation is aimed at ---------------------------------------------------------------


@dataclass(frozen=True)
class Member:
    """A member of a component schema only one kind of body reaches."""

    component: str
    name: str

    def schema(self, doc: Document) -> Schema:
        schema: Schema = doc["components"]["schemas"][self.component]["properties"][self.name]
        return schema

    def set(self, doc: Document, schema: Schema) -> None:
        doc["components"]["schemas"][self.component]["properties"][self.name] = schema

    def required(self, doc: Document) -> list[str]:
        required: list[str] = doc["components"]["schemas"][self.component].setdefault("required", [])
        return required

    def remove(self, doc: Document) -> None:
        del doc["components"]["schemas"][self.component]["properties"][self.name]
        if self.name in self.required(doc):
            self.required(doc).remove(self.name)


Shape = Callable[[Document, Member], bool]


def member(doc: Document, body: str, shape: Shape, description: str) -> Member:
    """The first member, by name, of a schema only `body` reaches, that has `shape`."""
    reached = contexts(doc)
    for component, schema in sorted(doc["components"]["schemas"].items()):
        if reached[component] != {body} or "properties" not in schema:
            continue
        for name in sorted(schema["properties"]):
            candidate = Member(component, name)
            if shape(doc, candidate):
                return candidate
    raise MissingShape(f"no {body} member {description}")


def _string(schema: Schema) -> bool:
    return schema.get("type") == "string" and "enum" not in schema and "format" not in schema


def plain_string(doc: Document, m: Member) -> bool:
    return _string(m.schema(doc))


def null_string(doc: Document, m: Member) -> bool:
    value = null_branch(m.schema(doc))
    return value is not None and _string(value)


def plain_object(doc: Document, m: Member) -> bool:
    return _is_object_ref(doc, m.schema(doc))


def null_object(doc: Document, m: Member) -> bool:
    value = null_branch(m.schema(doc))
    return value is not None and _is_object_ref(doc, value)


def is_required(shape: Shape) -> Shape:
    return lambda doc, m: m.name in m.required(doc) and shape(doc, m)


def is_optional(shape: Shape) -> Shape:
    return lambda doc, m: m.name not in m.required(doc) and shape(doc, m)


def defaulted(doc: Document, m: Member) -> bool:
    return m.name not in m.required(doc) and "default" in m.schema(doc) and null_branch(m.schema(doc)) is None


@dataclass(frozen=True)
class Parameter:
    path: str
    method: str
    name: str

    def entry(self, doc: Document) -> Schema:
        parameters = doc["paths"][self.path][self.method]["parameters"]
        entry: Schema = next(p for p in parameters if p["name"] == self.name)
        return entry

    def remove(self, doc: Document) -> None:
        doc["paths"][self.path][self.method]["parameters"].remove(self.entry(doc))


def parameter(doc: Document, shape: Callable[[Schema], bool], description: str) -> Parameter:
    for path, method, operation in _operations(doc):
        for entry in operation.get("parameters", []):
            if entry["in"] == "query" and not entry.get("required") and shape(entry["schema"]):
                return Parameter(path, method, entry["name"])
    raise MissingShape(f"no optional query parameter {description}")


def plain_integer(schema: Schema) -> bool:
    return schema.get("type") == "integer" and "enum" not in schema


def null_scalar(schema: Schema) -> bool:
    value = null_branch(schema)
    return value is not None and value.get("type") in {"string", "integer"} and "enum" not in value


def enum(doc: Document, body: str, spelled_null: bool) -> str:
    """An enum schema only `body` reaches, every reference to which is spelled as `spelled_null` says."""
    reached = contexts(doc)
    spellings: dict[str, set[bool]] = defaultdict(set)

    def walk(node: Any, in_null_union: bool) -> None:
        if isinstance(node, dict):
            if isinstance(ref := node.get("$ref"), str):
                spellings[ref.rsplit("/", 1)[-1]].add(in_null_union)
            for key, value in node.items():
                walk(value, key == "anyOf" and null_branch(node) is not None)
        elif isinstance(node, list):
            for value in node:
                walk(value, in_null_union)

    walk(doc, False)
    name: str
    for name, schema in sorted(doc["components"]["schemas"].items()):
        if "enum" in schema and reached[name] == {body} and spellings[name] == {spelled_null}:
            return name
    raise MissingShape(f"no enum only a {body} reaches, spelled {'in' if spelled_null else 'outside'} a null union")


def json_operation(doc: Document) -> tuple[str, str, str]:
    for path, method, operation in _operations(doc):
        for status, response in sorted(operation.get("responses", {}).items()):
            if status.startswith("2") and "application/json" in response.get("content", {}):
                return path, method, status
    raise MissingShape("no operation with a JSON success response")


# -- the mutations -----------------------------------------------------------------------------


def _response(doc: Document, shape: Shape, description: str) -> Member:
    return member(doc, "response", shape, description)


def _request(doc: Document, shape: Shape, description: str) -> Member:
    return member(doc, "request", shape, description)


def _remove_operation(doc: Document) -> None:
    path = next(p for p, item in sorted(doc["paths"].items()) if len(item) > 1)
    del doc["paths"][path][sorted(doc["paths"][path])[0]]


def _drop_success(doc: Document) -> None:
    path, method, status = json_operation(doc)
    del doc["paths"][path][method]["responses"][status]


def _drop_json(doc: Document) -> None:
    path, method, status = json_operation(doc)
    del doc["paths"][path][method]["responses"][status]["content"]["application/json"]


def _add_operation(doc: Document) -> None:
    path, method, _ = json_operation(doc)
    operation = copy.deepcopy(doc["paths"][path][method])
    operation["operationId"] = f"{operation.get('operationId', 'probe')}_probe"
    doc["paths"]["/api/v1/contract-probe"] = {method: operation}


def _enum_add(name: str) -> Callable[[Document], None]:
    return lambda doc: doc["components"]["schemas"][name]["enum"].append("contract-probe")


def _enum_drop(name: str) -> Callable[[Document], None]:
    return lambda doc: doc["components"]["schemas"][name]["enum"].pop()


def _set(m: Member, change: Callable[[Schema], Schema]) -> Callable[[Document], None]:
    return lambda doc: m.set(doc, change(m.schema(doc)))


def _require(m: Member) -> Callable[[Document], None]:
    return lambda doc: m.required(doc).append(m.name)


def _unrequire(m: Member) -> Callable[[Document], None]:
    return lambda doc: m.required(doc).remove(m.name)


def _add_member(component: str, schema: Schema, required: bool) -> Callable[[Document], None]:
    def add(doc: Document) -> None:
        target = doc["components"]["schemas"][component]
        target["properties"]["contract_probe"] = schema
        if required:
            target.setdefault("required", []).append("contract_probe")

    return add


def _add_parameter(schema: Schema, required: bool) -> Callable[[Document], None]:
    def add(doc: Document) -> None:
        path, method, _ = json_operation(doc)
        operation = doc["paths"][path][method]
        operation.setdefault("parameters", []).append(
            {"in": "query", "name": "contract_probe", "required": required, "schema": schema}
        )

    return add


def _parameter_schema(p: Parameter, change: Callable[[Schema], Schema]) -> Callable[[Document], None]:
    def apply(doc: Document) -> None:
        entry = p.entry(doc)
        entry["schema"] = change(entry["schema"])

    return apply


def _parameter_required(p: Parameter) -> Callable[[Document], None]:
    return lambda doc: p.entry(doc).update(required=True)


def _parameter_enum(doc: Document) -> str:
    name: str
    for name in sorted(doc["components"]["schemas"]):
        if "enum" in doc["components"]["schemas"][name] and contexts(doc)[name] == {"parameter"}:
            return name
    raise MissingShape("no enum only a parameter reaches")


def _other_type(schema: Schema) -> Schema:
    """`schema` with its value's type swapped between string and integer, a null branch kept."""
    changed = copy.deepcopy(schema)
    target = null_branch(changed) or changed
    target["type"] = {"string": "integer", "integer": "string"}[target["type"]]
    return changed


def _describe(doc: Document) -> None:
    path, method, _ = json_operation(doc)
    doc["paths"][path][method]["description"] = "Reworded."


def _retitle(doc: Document) -> None:
    m = _response(doc, plain_string, "that is a plain string")
    m.schema(doc)["title"] = "Reworded"


def _re_example(doc: Document) -> None:
    m = _response(doc, plain_string, "that is a plain string")
    m.schema(doc)["examples"] = ["contract-probe"]


def _bump_version(doc: Document) -> None:
    doc["info"]["version"] = "999.0.0"


def mutations(doc: Document) -> Iterator[tuple[Row, str, Callable[[Document], None]]]:
    """Each row's mutations, in table order, as (row, what is done, how)."""
    rows = iter(ROWS)

    row = next(rows)
    yield row, "an operation is removed", _remove_operation
    yield row, "an operation loses its success status", _drop_success
    yield row, "an operation loses its JSON body", _drop_json

    row = next(rows)
    for shape, said in (
        (is_required(plain_string), "required, a plain string"),
        (defaulted, "with a default"),
        (null_string, "a null union"),
    ):
        m = _response(doc, shape, said)
        yield row, f"response member {m.component}.{m.name}, {said}, is removed", m.remove

    row = next(rows)
    for shape, said in ((plain_string, "a plain string"), (null_string, "a string in a null union")):
        m = _response(doc, shape, said)
        yield row, f"response member {m.component}.{m.name}, {said}, changes type", _set(m, _other_type)
    for shape, said in ((plain_string, "a scalar"), (plain_object, "an object")):
        m = _response(doc, shape, said)
        yield row, f"response member {m.component}.{m.name}, {said}, may now be null", _set(m, wrap_in_null)

    row = next(rows)
    for spelled_null in (False, True):
        name = enum(doc, "response", spelled_null)
        yield row, f"a value joins {name}, spelled {'in' if spelled_null else 'outside'} a null union", _enum_add(name)

    row = next(rows)
    for shape, said in ((plain_string, "a plain string"), (null_string, "a null union")):
        m = _request(doc, shape, said)
        yield row, f"request member {m.component}.{m.name}, {said}, is removed", m.remove

    row = next(rows)
    for shape, said in ((is_optional(plain_string), "a plain string"), (is_optional(null_string), "a null union")):
        m = _request(doc, shape, said)
        yield row, f"request member {m.component}.{m.name}, {said}, becomes required", _require(m)
    m = _request(doc, plain_string, "a plain string")
    yield row, f"a required member joins {m.component}", _add_member(m.component, {"type": "string"}, True)
    for test, said in ((plain_integer, "a plain integer"), (null_scalar, "a null union")):
        p = parameter(doc, test, said)
        yield row, f"parameter {p.method.upper()} {p.path} {p.name}, {said}, becomes required", _parameter_required(p)
    yield row, "a required parameter appears", _add_parameter({"type": "string"}, True)

    row = next(rows)
    for shape, said in ((plain_string, "a plain string"), (null_string, "a string in a null union")):
        m = _request(doc, shape, said)
        yield row, f"request member {m.component}.{m.name}, {said}, changes type", _set(m, _other_type)
    for shape, said in ((null_string, "a scalar"), (null_object, "an object")):
        m = _request(doc, shape, said)
        yield row, f"request member {m.component}.{m.name}, {said}, stops accepting null", _set(m, unwrap_null)
    for spelled_null in (False, True):
        name = enum(doc, "request", spelled_null)
        where = "in" if spelled_null else "outside"
        yield row, f"a value leaves {name}, spelled {where} a null union", _enum_drop(name)
    for test, said in ((plain_integer, "a plain integer"), (null_scalar, "a null union")):
        p = parameter(doc, test, said)
        yield row, f"parameter {p.path} {p.name}, {said}, changes type", _parameter_schema(p, _other_type)
    p = parameter(doc, null_scalar, "a null union")
    yield row, f"parameter {p.path} {p.name} stops accepting null", _parameter_schema(p, unwrap_null)
    name = _parameter_enum(doc)
    yield row, f"a value leaves {name}, which a parameter takes", _enum_drop(name)

    row = next(rows)
    for test, said in ((plain_integer, "a plain integer"), (null_scalar, "a null union")):
        p = parameter(doc, test, said)
        yield row, f"parameter {p.method.upper()} {p.path} {p.name}, {said}, is removed", p.remove

    row = next(rows)
    yield row, "an operation is added", _add_operation
    m = _response(doc, plain_string, "a plain string")
    yield row, f"a member joins {m.component}", _add_member(m.component, {"type": "string"}, False)
    yield (
        row,
        f"a null-union member joins {m.component}",
        _add_member(m.component, wrap_in_null({"type": "string"}), False),
    )
    m = _request(doc, plain_string, "a plain string")
    yield row, f"an optional member joins {m.component}", _add_member(m.component, {"type": "string"}, False)
    yield (
        row,
        f"an optional null-union member joins {m.component}",
        _add_member(m.component, wrap_in_null({"type": "string"}), False),
    )
    yield row, "an optional parameter appears", _add_parameter({"type": "string"}, False)
    yield row, "an optional null-union parameter appears", _add_parameter(wrap_in_null({"type": "string"}), False)

    row = next(rows)
    for shape, said in ((is_required(plain_string), "a plain string"), (is_required(null_string), "a null union")):
        m = _response(doc, shape, said)
        yield row, f"response member {m.component}.{m.name}, {said}, stops being required", _unrequire(m)

    row = next(rows)
    for spelled_null in (False, True):
        where = "in" if spelled_null else "outside"
        name = enum(doc, "response", spelled_null)
        yield row, f"a value leaves {name}, spelled {where} a null union", _enum_drop(name)
        name = enum(doc, "request", spelled_null)
        yield row, f"a value joins {name}, spelled {where} a null union", _enum_add(name)

    row = next(rows)
    for shape, said in ((plain_string, "a scalar"), (plain_object, "an object")):
        m = _request(doc, shape, said)
        yield row, f"request member {m.component}.{m.name}, {said}, starts accepting null", _set(m, wrap_in_null)
    p = parameter(doc, plain_integer, "a plain integer")
    yield row, f"parameter {p.path} {p.name} starts accepting null", _parameter_schema(p, wrap_in_null)

    row = next(rows)
    for shape, said in ((null_string, "a scalar"), (null_object, "an object")):
        m = _response(doc, shape, said)
        yield row, f"response member {m.component}.{m.name}, {said}, is never null", _set(m, unwrap_null)

    row = next(rows)
    yield row, "an operation's description changes", _describe
    yield row, "a member's title changes", _retitle
    yield row, "a member's example changes", _re_example
    yield row, "the document's version changes", _bump_version


def prove(doc: Document) -> list[str]:
    """What each mutation did, and a failure line for each verdict that is not its row's."""
    lines, failures = [], 0
    for row, said, mutate in mutations(doc):
        revision = copy.deepcopy(doc)
        mutate(revision)
        findings = diff(doc, revision)
        breaks = [finding for finding in findings if finding.breaks]
        if row.fails:
            held = any(finding.row is row for finding in breaks)
            verdict = "fails" if breaks else "passes"
        else:
            held = not breaks
            verdict = "passes" if held else "fails"
        failures += not held
        lines.append(f"{'ok  ' if held else 'FAIL'} {said}: {verdict}, as the row says" if held else f"FAIL {said}")
        if not held:
            lines.append(f"     row: {row.change} ({'fails' if row.fails else 'passes'})")
            lines += [
                f"     {finding.level} {finding.rule}: {finding.operation} {finding.text}" for finding in findings
            ]
    lines.append(f"{failures} mutation(s) did not hold." if failures else "Every row holds.")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--document", type=Path, help="a released surface already built (default: build this checkout's)"
    )
    args = parser.parse_args()
    doc: Document = json.loads(args.document.read_text()) if args.document else build(ROOT, released=True)
    try:
        lines = prove(doc)
    except MissingShape as missing:
        print(f"The document holds {missing}, which a row of the table needs to be proven on.", file=sys.stderr)
        sys.exit(1)
    print("\n".join(lines))
    sys.exit(0 if lines[-1] == "Every row holds." else 1)


if __name__ == "__main__":
    main()
