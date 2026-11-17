"""Prove every row of the contract check's table against the real document, with the pinned differ.

    uv run python scripts/api_contract_proof.py                     # builds this checkout's document
    uv run python scripts/api_contract_proof.py --document doc.json

Each row is a mutation of the released surface with its verdict asserted, and the rows that name a
member are tried on a member spelled as a null union (`anyOf: [X, {"type": "null"}]`, the spelling
this document uses for an optional value) as well as on one that is not; the null rows on a scalar
and on an object. The row for a renamed component, which `ROWS` does not carry, is proven by
renaming union branches and changing what is inside them (`branch_mutations`). A differ upgrade
that changes a verdict fails here, on the pull request that makes it.

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
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from api_contract import ROWS, Finding, Row, diff
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


# -- a union's branches, compared by content ---------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """What a mutation must come to: a pass; a fail at `row`, with each of `members` named by a
    break there; or, with no row, a fail at a rule of the differ's that no row covers."""

    fails: bool
    row: Row | None = None
    members: tuple[str, ...] = ()

    def holds(self, breaks: list[Finding]) -> bool:
        if not self.fails:
            return not breaks
        if self.row is None:
            return bool(breaks) and all(finding.row is None for finding in breaks)
        at_row = [finding for finding in breaks if finding.row is self.row]
        return bool(at_row) and all(any(f"/{name}`" in f.text for f in at_row) for name in self.members)

    def __str__(self) -> str:
        if not self.fails:
            return "passes"
        if self.row is None:
            return "fails, at a rule of the differ's that no row covers"
        named = f", naming {' and '.join(self.members)}" if self.members else ""
        return f"fails at the row: {self.row.change}{named}"


Mutation = Callable[[Document], None]

_ROW_OF = {rule: row for row in ROWS for rule in row.rules}


@dataclass(frozen=True)
class UnionOf:
    """A union whose branches name components, where it sits in the document, and whether a
    discriminator names its branches on the wire."""

    at: tuple[str | int, ...]
    components: tuple[str, ...]
    discriminated: bool

    def node(self, doc: Document) -> Schema:
        node: Any = doc
        for key in self.at:
            node = node[key]
        found: Schema = node
        return found


def _branch_name(node: Any) -> str | None:
    if isinstance(node, dict) and len(node) == 1 and isinstance(ref := node.get("$ref"), str):
        return ref.rsplit("/", 1)[-1]
    return None


def unions(doc: Document) -> list[UnionOf]:
    """Every null union with a `$ref` branch, and every discriminated union of `$ref` branches, in
    the order a sorted walk of `doc` meets them."""
    found: list[UnionOf] = []

    def walk(node: Any, at: tuple[str | int, ...]) -> None:
        if isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, (*at, index))
            return
        if not isinstance(node, dict):
            return
        for key in ("anyOf", "oneOf"):
            branches = node.get(key)
            if not isinstance(branches, list):
                continue
            names = tuple(name for branch in branches if (name := _branch_name(branch)))
            if isinstance(node.get("discriminator"), dict) and len(names) == len(branches):
                found.append(UnionOf(at, names, True))
            elif len(branches) == 2 and _NULL in branches and names:
                found.append(UnionOf(at, names, False))
        for key, value in sorted(node.items()):
            walk(value, (*at, key))

    walk(doc, ())
    return found


def _renamed(name: str) -> str:
    return f"{name}Renamed"


def rename(old: str) -> Mutation:
    """`old` renamed everywhere it is named, its title following as a renamed class's does."""
    ref, new = f"#/components/schemas/{old}", _renamed(old)

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(value) for value in node]
        return f"#/components/schemas/{new}" if node == ref else node

    def apply(doc: Document) -> None:
        rewritten = walk(doc)
        schemas = rewritten["components"]["schemas"]
        schemas[new] = {**schemas.pop(old), "title": new}
        doc.clear()
        doc.update(rewritten)

    return apply


def then(*steps: Mutation) -> Mutation:
    def apply(doc: Document) -> None:
        for step in steps:
            step(doc)

    return apply


def _only_object_of(doc: Document, reached: dict[str, set[str]], name: str, body: str) -> bool:
    schema = doc["components"]["schemas"][name]
    return schema.get("type") == "object" and "properties" in schema and reached[name] == {body}


def _first(candidates: Iterator[str], description: str) -> str:
    for candidate in candidates:
        return candidate
    raise MissingShape(description)


def _string_member(doc: Document, component: str, *, avoid: Collection[str] = (), optional: bool = False) -> str | None:
    """The first member of `component`, by name, that is a string, plain or in a null union."""
    name: str
    for name in sorted(doc["components"]["schemas"][component]["properties"]):
        m = Member(component, name)
        if (
            name not in avoid
            and not (optional and name in m.required(doc))
            and (plain_string(doc, m) or null_string(doc, m))
        ):
            return name
    return None


def _add_branch(union: UnionOf, like: str) -> Mutation:
    """A branch modelled on `like` joins `union` under a wire name of its own."""

    def apply(doc: Document) -> None:
        node, schemas = union.node(doc), doc["components"]["schemas"]
        discriminator = node["discriminator"]
        branch = copy.deepcopy(schemas[like])
        branch["title"] = "ContractProbe"
        branch["properties"][discriminator["propertyName"]] = {"const": "contract-probe", "type": "string"}
        schemas["ContractProbe"] = branch
        node["oneOf" if "oneOf" in node else "anyOf"].append({"$ref": "#/components/schemas/ContractProbe"})
        discriminator["mapping"]["contract-probe"] = "#/components/schemas/ContractProbe"

    return apply


def _undiscriminated(union: UnionOf) -> Mutation:
    """`union` with no discriminator, a plain union of its branches as the framework writes one."""

    def apply(doc: Document) -> None:
        node = union.node(doc)
        del node["discriminator"]
        node["anyOf"] = node.pop("oneOf" if "oneOf" in node else "anyOf")

    return apply


def branch_mutations(doc: Document) -> Iterator[tuple[Verdict, str, Mutation, Mutation | None]]:
    """Renamed union-branch components and changes inside branches, as (verdict, what is done, how,
    and how both sides are prepared first, where they are)."""
    schemas, reached, found = doc["components"]["schemas"], contexts(doc), unions(doc)
    branches = {name for union in found for name in union.components}

    kinds = next(
        (
            union
            for union in found
            if union.discriminated
            and len(union.components) >= 2
            and all(_only_object_of(doc, reached, name, "response") for name in union.components)
        ),
        None,
    )
    if kinds is None:
        raise MissingShape("no discriminated union of objects only a response reaches")
    first, second = kinds.components[:2]
    discriminator = kinds.node(doc)["discriminator"]["propertyName"]
    one = _string_member(doc, first, avoid={discriminator})
    other = _string_member(doc, second, avoid={discriminator, one or ""})
    if one is None or other is None:
        raise MissingShape(f"no string member in each of {first} and {second}, under different names")

    def null_branch_of(body: str, *, optional: bool) -> tuple[str, str]:
        """A null-union branch object only `body` reaches, and a string member of it."""
        for union in found:
            for name in union.components if not union.discriminated else ():
                if _only_object_of(doc, reached, name, body) and (m := _string_member(doc, name, optional=optional)):
                    return name, m
        raise MissingShape(f"no null-union branch object only a {body} reaches with a string member")

    in_response, lost = null_branch_of("response", optional=False)
    in_request, becomes_required = null_branch_of("request", optional=True)
    nested = _first(
        (name for union in unions({n: schemas[n] for n in sorted(branches)}) for name in union.components),
        "no union branch inside another union's branch",
    )
    outside = _first(
        (name for name in sorted(schemas) if name not in branches and _only_object_of(doc, reached, name, "response")),
        "no object only a response reaches, outside any union",
    )

    removed = _ROW_OF["response-optional-property-removed"]
    passes = Verdict(False)
    yield passes, f"{first}, a branch of a discriminated union, is renamed", rename(first), None
    yield passes, f"{in_response}, a null-union branch a response carries, is renamed", rename(in_response), None
    yield passes, f"{in_request}, a null-union branch a request carries, is renamed", rename(in_request), None
    yield passes, f"{nested}, a union branch inside another union's branch, is renamed", rename(nested), None
    yield passes, f"{outside}, outside any union, is renamed", rename(outside), None
    yield (
        Verdict(True, removed, (one,)),
        f"{first} is renamed and loses {one}",
        then(rename(first), Member(_renamed(first), one).remove),
        None,
    )
    yield (
        Verdict(True, _ROW_OF["response-property-type-changed"], (one,)),
        f"{first} is renamed and {one} changes type",
        then(rename(first), _set(Member(_renamed(first), one), _other_type)),
        None,
    )
    yield (
        Verdict(True, removed, (lost,)),
        f"{in_response} is renamed and loses {lost}",
        then(rename(in_response), Member(_renamed(in_response), lost).remove),
        None,
    )
    yield (
        Verdict(True, _ROW_OF["request-property-became-required"], (becomes_required,)),
        f"{in_request} is renamed and {becomes_required} becomes required",
        then(rename(in_request), _require(Member(_renamed(in_request), becomes_required))),
        None,
    )
    probe: Schema = {"type": "string"}
    yield (
        passes,
        f"a member joins both {first} and {second}",
        then(_add_member(first, probe, False), _add_member(second, probe, False)),
        None,
    )
    yield (
        Verdict(True, removed, (one, other)),
        f"{first} loses {one} and {second} loses {other}",
        then(Member(first, one).remove, Member(second, other).remove),
        None,
    )
    yield (
        passes,
        f"{first} and {second} are both renamed and a member joins each",
        then(
            rename(first),
            rename(second),
            _add_member(_renamed(first), probe, False),
            _add_member(_renamed(second), probe, False),
        ),
        None,
    )
    yield (
        Verdict(True, removed, (one, other)),
        f"{first} and {second} are both renamed and lose {one} and {other}",
        then(
            rename(first), rename(second), Member(_renamed(first), one).remove, Member(_renamed(second), other).remove
        ),
        None,
    )
    yield Verdict(True), f"a new branch joins the union of {first} and {second}", _add_branch(kinds, second), None
    yield (
        Verdict(True),
        f"{first} is renamed in that union with its discriminator taken away, which reads as a branch added",
        rename(first),
        _undiscriminated(kinds),
    )


def prove(doc: Document) -> list[str]:
    """What each mutation did, and a failure line for each verdict that is not the expected one."""
    cases: list[tuple[Verdict, str, Mutation, Mutation | None]] = [
        (Verdict(row.fails, row if row.fails else None), said, mutate, None) for row, said, mutate in mutations(doc)
    ]
    cases += branch_mutations(doc)
    lines, failures = [], 0
    for verdict, said, mutate, prepare in cases:
        base = doc
        if prepare:
            base = copy.deepcopy(doc)
            prepare(base)
        revision = copy.deepcopy(base)
        mutate(revision)
        findings = diff(base, revision)
        held = verdict.holds([finding for finding in findings if finding.breaks])
        failures += not held
        lines.append(f"ok   {said}: {verdict}" if held else f"FAIL {said}")
        if not held:
            lines.append(f"     expected: {verdict}")
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
