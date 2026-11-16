"""Hold a client's recorded request and response bodies to the API's contract document.

    uv run python scripts/client_bodies.py --list ../client/api-bodies.json             # this checkout
    uv run python scripts/client_bodies.py --list LIST --ref origin/claude/some-branch   # a ref of it
    uv run python scripts/client_bodies.py --list LIST --tree ../another-api-checkout
    uv run python scripts/client_bodies.py --list LIST --mode enforce

The list is a JSON file a client keeps beside its bodies:

    {
      "directory": "Fixtures",
      "bodies": [
        {"file": "dive.json", "is": [{"response": "GET /api/v1/dive/{uuid} 200"}]},
        {"file": "request-dive.json", "is": [{"request": "POST /api/v1/dive"}]},
        {"file": "error.json", "is": [{"schema": "HTTPValidationError"}]},
        {"file": "search.json", "is": [{"response": "GET /api/v1/dive-sites/search 200"}],
         "exempt": [{"at": "/rows/2", "reason": "a row kind the API does not have, on purpose"}]}
      ]
    }

`directory` is relative to the list, and every `.json` file under it is a body that needs an entry.
An entry says what its body is: the response of an operation (method, path as the document spells
it, status), the request body of one, or a component schema, for a part of a body or an error body;
a body the client serves for several operations names each. An exemption names a place in the body
as a JSON pointer, and silences what is found at it and below it, and nothing else.

A response is judged leniently on what it lacks and strictly on what it holds: every member it
carries must be one the schema has, with a value the schema allows, and a member it lacks is no
finding, so a client may trail the API by a member. A request is judged by its schema as published,
which refuses an unknown member and demands a required one.

A finding about the list and the files alone fails in either mode. One that depends on the document
— a body the schema does not allow, an entry naming what the document does not hold, an exemption
the document no longer needs — fails under `--mode enforce` and is a warning under `--mode report`,
since a client's pull request may be built on an API change that has not merged yet.
"""

import argparse
import copy
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from api_document import build
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).resolve().parent.parent

#: Where the document sits in the validator's registry; every `$ref` in it resolves against this.
_URI = "urn:opendiving-api:contract"

_JSON = "application/json"

#: Keywords whose value is one subschema, a list of them, or a map of names to them.
_ONE = ("additionalProperties", "items", "not", "contains", "if", "then", "else", "propertyNames")
_LIST = ("anyOf", "oneOf", "allOf", "prefixItems")
_MAP = ("properties", "patternProperties", "$defs", "dependentSchemas")

_OPEN = ("additionalProperties", "patternProperties", "unevaluatedProperties")


class Refused(Exception):
    """A document or a list the checker cannot judge soundly; it exits without a verdict."""


@dataclass(frozen=True)
class Finding:
    file: str
    #: A JSON pointer into the body, `""` for the whole of it; `None` where the finding is about
    #: the entry rather than a place in the body.
    place: str | None
    text: str
    #: Whether the finding follows from the document, rather than from the list and the files.
    on_document: bool

    def describe(self) -> str:
        where = self.file if self.place is None else f"{self.file} at {self.place or '(the whole body)'}"
        return f"{where}: {self.text}"


# --- The document ---------------------------------------------------------------------------------


def _subschemas(schema: Any) -> Iterator[Any]:
    if not isinstance(schema, dict):
        return
    for key in _ONE:
        if isinstance(schema.get(key), dict):
            yield schema[key]
    for key in _LIST:
        yield from schema.get(key, [])
    for key in _MAP:
        yield from schema.get(key, {}).values()


def _schemas(document: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every schema the document holds at the top, named as a finding would name it."""
    for name, schema in document.get("components", {}).get("schemas", {}).items():
        yield f"the component {name}", schema
    for path, item in document.get("paths", {}).items():
        for method, operation in item.items():
            if not isinstance(operation, dict):
                continue
            body = operation.get("requestBody", {}).get("content", {}).get(_JSON, {})
            if "schema" in body:
                yield f"the request body of {method.upper()} {path}", body["schema"]
            for status, response in operation.get("responses", {}).items():
                content = response.get("content", {}).get(_JSON, {})
                if "schema" in content:
                    yield f"the {status} response of {method.upper()} {path}", content["schema"]


def _composes(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    # A `$ref` beside members of its own is a composition as surely as `allOf` is.
    if "allOf" in schema or ("$ref" in schema and any(key in schema for key in ("properties", *_OPEN))):
        return True
    return any(_composes(sub) for sub in _subschemas(schema))


def refuse_composition(document: dict[str, Any]) -> None:
    """A response member the schema lacks is found by closing every object schema. Closing the
    parts of a composition one by one would refuse each part's members in the others, so a
    document that composes objects is refused rather than judged wrongly."""
    for name, schema in _schemas(document):
        if _composes(schema):
            raise Refused(
                f"{name} composes schemas with allOf, which this checker cannot close soundly: "
                "a response's members would be refused by the parts that do not declare them"
            )


def _as_a_response(schema: Any) -> Any:
    """`schema` as a response is judged: closed on what it declares, demanding nothing, and
    satisfied by any branch of a union, since a body that lacks the members telling two branches
    apart may match both."""
    if not isinstance(schema, dict):
        return schema
    schema = dict(schema)
    for key in _ONE:
        if isinstance(schema.get(key), dict):
            schema[key] = _as_a_response(schema[key])
    for key in _LIST:
        if key in schema:
            schema[key] = [_as_a_response(sub) for sub in schema[key]]
    for key in _MAP:
        if key in schema:
            schema[key] = {name: _as_a_response(sub) for name, sub in schema[key].items()}
    if "oneOf" in schema:
        schema["anyOf"] = schema.pop("oneOf")
    schema.pop("required", None)
    # An object that declares no members is a free-form map in this document, and stays open.
    if "properties" in schema and not any(key in schema for key in _OPEN):
        schema["additionalProperties"] = False
    return schema


def response_view(document: dict[str, Any]) -> dict[str, Any]:
    view = copy.deepcopy(document)
    for name, schema in view.get("components", {}).get("schemas", {}).items():
        view["components"]["schemas"][name] = _as_a_response(schema)
    for item in view.get("paths", {}).values():
        for operation in item.values():
            if not isinstance(operation, dict):
                continue
            for response in operation.get("responses", {}).values():
                content = response.get("content", {}).get(_JSON)
                if content and "schema" in content:
                    content["schema"] = _as_a_response(content["schema"])
    return view


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def pointer(segments: Any) -> str:
    return "".join(f"/{_escape(str(segment))}" for segment in segments)


@dataclass(frozen=True)
class Target:
    """What an entry says its body is, resolved to a schema of the document."""

    label: str
    #: The schema's place in the document, a pointer; `None` where the document lacks it.
    schema: str | None
    missing: str | None
    request: bool


_RESPONSE = re.compile(r"^([A-Z]+) (/\S*) (\S+)$")
_REQUEST = re.compile(r"^([A-Z]+) (/\S*)$")


def resolve(document: dict[str, Any], spec: Any) -> Target:
    """Raises `ValueError` for an entry the list spells wrongly."""
    if not isinstance(spec, dict) or len(spec) != 1:
        raise ValueError(f"{spec!r} is not one of response, request or schema")
    ((kind, value),) = spec.items()
    if not isinstance(value, str):
        raise ValueError(f"{spec!r} is not one of response, request or schema")
    if kind == "schema":
        found = value in document.get("components", {}).get("schemas", {})
        missing = None if found else f"the document has no component schema {value}"
        return Target(f"the schema {value}", pointer(["components", "schemas", value]), missing, False)
    shape = _RESPONSE if kind == "response" else _REQUEST if kind == "request" else None
    match = shape.match(value) if shape else None
    if not match:
        raise ValueError(
            f"{spec!r} is not one of response 'METHOD /path STATUS', request 'METHOD /path' or schema 'Name'"
        )
    method, path = match.group(1), match.group(2)
    operation = document.get("paths", {}).get(path, {}).get(method.lower())
    label = f"the {kind} {value}" if kind == "response" else f"the request body of {value}"
    if operation is None:
        return Target(label, None, f"the document has no operation {method} {path}", kind == "request")
    if kind == "request":
        content = operation.get("requestBody", {}).get("content", {})
        place = ["paths", path, method.lower(), "requestBody", "content", _JSON, "schema"]
        if _JSON not in content:
            return Target(label, None, f"{method} {path} takes no JSON request body", True)
        return Target(label, pointer(place), None, True)
    status = match.group(3)
    response = operation.get("responses", {}).get(status)
    if response is None:
        return Target(label, None, f"{method} {path} has no {status} response", False)
    if _JSON not in response.get("content", {}):
        return Target(label, None, f"the {status} response of {method} {path} has no JSON body", False)
    place = ["paths", path, method.lower(), "responses", status, "content", _JSON, "schema"]
    return Target(label, pointer(place), None, False)


class Judge:
    def __init__(self, document: dict[str, Any]) -> None:
        refuse_composition(document)
        self.document = document
        self._validators = {
            request: self._registry(document if request else response_view(document)) for request in (True, False)
        }

    @staticmethod
    def _registry(document: dict[str, Any]) -> Registry[Any]:
        registry: Registry[Any] = Registry().with_resource(_URI, DRAFT202012.create_resource(document))
        return registry

    def errors(self, target: Target, body: Any) -> Iterator[tuple[str, str]]:
        """Each place in `body` the target's schema does not allow, with what it allows there."""
        assert target.schema is not None
        validator = Draft202012Validator({"$ref": f"{_URI}#{target.schema}"}, registry=self._validators[target.request])
        for error in validator.iter_errors(body):
            yield from _explain(error, list(error.absolute_path))


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int | float):
        return "a number"
    if isinstance(value, str):
        return "a string"
    return "an array" if isinstance(value, list) else "an object"


def _shown(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 60 else f"{_kind(value)}"


def _rules_out(error: ValidationError) -> bool:
    """Whether a branch's error says the body was never meant for that branch: the wrong type
    where the union stands, or a tag member with another value, as a discriminated union's are."""
    if error.validator == "type" and not error.relative_path:
        return True
    return error.validator == "const" and len(error.relative_path) == 1


def _explain(error: ValidationError, at: list[Any]) -> Iterator[tuple[str, str]]:
    if error.validator in ("anyOf", "oneOf") and error.context:
        branches: dict[Any, list[ValidationError]] = defaultdict(list)
        for sub in error.context:
            branches[sub.relative_schema_path[0]].append(sub)
        plausible = [errors for errors in branches.values() if not any(_rules_out(sub) for sub in errors)]
        if len(plausible) == 1:
            for sub in plausible[0]:
                yield from _explain(sub, at + list(sub.relative_path))
            return
        yield pointer(at), f"{_shown(error.instance)} matches none of the {len(branches)} shapes the schema allows here"
        return
    value = error.validator_value
    if error.validator == "additionalProperties" and value is False:
        declared = set(error.schema.get("properties", {}))
        patterns = list(error.schema.get("patternProperties", {}))
        allowed = ", ".join(sorted(declared)) or "none"
        for member in error.instance:
            if member not in declared and not any(re.search(pattern, member) for pattern in patterns):
                yield pointer([*at, member]), f"is a member the schema does not have; it has {allowed}"
    elif error.validator == "required":
        for member in value:
            if member not in error.instance:
                yield pointer(at), f"lacks the required member {member}"
    elif error.validator == "type":
        allowed = " or ".join(value) if isinstance(value, list) else value
        yield pointer(at), f"is {_kind(error.instance)}, where the schema allows {allowed}"
    elif error.validator == "enum":
        yield pointer(at), f"is {_shown(error.instance)}, where the schema allows {json.dumps(value)}"
    elif error.validator == "const":
        yield pointer(at), f"is {_shown(error.instance)}, where the schema allows only {json.dumps(value)}"
    else:
        yield pointer(at), f"{error.message} ({error.validator})"


# --- The list -------------------------------------------------------------------------------------


def _holds(body: Any, place: str) -> bool:
    if place and not place.startswith("/"):
        return False
    node = body
    for raw in place.split("/")[1:]:
        segment = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and segment in node:
            node = node[segment]
        elif isinstance(node, list) and segment.isdigit() and int(segment) < len(node):
            node = node[int(segment)]
        else:
            return False
    return True


def _under(place: str, exemption: str) -> bool:
    return place == exemption or place.startswith(exemption + "/")


def _shown_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def check(list_path: Path, document: dict[str, Any]) -> list[Finding]:
    """Every finding the list, its bodies and the document give. Raises `Refused` for a list it
    cannot read at all, and for a document it cannot judge."""
    judge = Judge(document)
    list_path = list_path.resolve()
    try:
        listing = json.loads(list_path.read_text())
        directory = (list_path.parent / listing["directory"]).resolve()
        entries = listing["bodies"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Refused(f"{list_path} is not a list of bodies: {error!r}") from error
    if not isinstance(entries, list) or not directory.is_dir():
        raise Refused(f"{list_path} names no directory of bodies, or `bodies` is not a list")

    shown_list = _shown_path(list_path)
    findings: list[Finding] = []
    listed: set[Path] = set()
    for entry in entries:
        findings += _check_entry(judge, directory, shown_list, entry, listed)
    for path in sorted(directory.rglob("*.json")):
        if path not in listed and path != list_path:
            findings.append(Finding(_shown_path(path), None, f"has no entry in {shown_list}", False))
    return findings


def _check_entry(judge: Judge, directory: Path, shown_list: str, entry: Any, listed: set[Path]) -> list[Finding]:
    if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
        return [Finding(shown_list, None, f"the entry {json.dumps(entry)} names no file", False)]
    path = (directory / entry["file"]).resolve()
    shown = _shown_path(path)
    if path in listed:
        return [Finding(shown, None, f"has more than one entry in {shown_list}", False)]
    listed.add(path)
    if not path.is_file():
        return [Finding(shown, None, f"is listed in {shown_list} and is not there", False)]
    try:
        body = json.loads(path.read_text())
    except ValueError as error:
        return [Finding(shown, None, f"is not JSON: {error}", False)]

    findings: list[Finding] = []
    specs = entry.get("is")
    if not isinstance(specs, list) or not specs:
        return [Finding(shown, None, "its entry says nothing about what the body is", False)]
    exemptions: list[str] = []
    for exemption in entry.get("exempt", []):
        place, reason = (exemption.get("at"), exemption.get("reason")) if isinstance(exemption, dict) else (None, None)
        if not isinstance(place, str) or not isinstance(reason, str) or not reason.strip():
            findings.append(
                Finding(shown, None, f"the exemption {json.dumps(exemption)} needs `at` and a reason", False)
            )
        elif not _holds(body, place):
            findings.append(Finding(shown, place, "is exempted, and the body holds nothing there", False))
        else:
            exemptions.append(place)

    used: set[str] = set()
    # An exemption can be said to exempt nothing only where every target was judged.
    judged_all = True
    for spec in specs:
        try:
            target = resolve(judge.document, spec)
        except ValueError as error:
            findings.append(Finding(shown, None, str(error), False))
            judged_all = False
            continue
        if target.missing:
            findings.append(Finding(shown, None, f"is listed as {target.label}, and {target.missing}", True))
            judged_all = False
            continue
        for place, text in judge.errors(target, body):
            covering = [exemption for exemption in exemptions if _under(place, exemption)]
            used.update(covering)
            if not covering:
                findings.append(Finding(shown, place, f"{text} ({target.label})", True))
    if judged_all:
        findings += [
            Finding(shown, place, "is exempted, and the document now allows what is there: remove it", True)
            for place in exemptions
            if place not in used
        ]
    return findings


# --- Running it -----------------------------------------------------------------------------------


def contract_document_at(repo: Path, ref: str) -> dict[str, Any]:
    """The contract document `ref` builds, from its committed tree alone."""
    with tempfile.TemporaryDirectory(prefix="client-bodies-tree-") as tree:
        archive = subprocess.run(["git", "-C", str(repo), "archive", ref], capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(tree, filter="data")
        document: dict[str, Any] = build(Path(tree), released=False)
        return document


def _annotation(level: str, finding: Finding) -> str:
    text = finding.describe().replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::{level} file={finding.file},title=api-contract::{text}"


def report(findings: list[Finding], *, enforce: bool, annotate: bool) -> tuple[str, bool]:
    failing = [finding for finding in findings if enforce or not finding.on_document]
    warning = [finding for finding in findings if finding not in failing]
    lines: list[str] = []
    for heading, group, level in (("Fails", failing, "error"), ("Warns", warning, "warning")):
        if not group:
            continue
        lines += ["", f"{heading} ({len(group)}):"]
        lines += [f"  {finding.describe()}" for finding in group]
        if annotate:
            lines += [_annotation(level, finding) for finding in group]
    lines.append("")
    if failing:
        lines.append("Fails.")
    elif warning:
        lines.append(
            "Passes, with warnings: each follows from the API's document, and fails this check in the merge "
            "queue unless the body, its entry or the API changes first."
        )
    else:
        lines.append("Passes: every body holds.")
    return "\n".join(lines), not failing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", type=Path, required=True, help="the client's list of bodies")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--tree", type=Path, help="the API tree to build the document from (default: this checkout)")
    source.add_argument("--ref", help="a ref of this checkout to build the document from, committed tree only")
    source.add_argument("--document", type=Path, help="a contract document already built")
    parser.add_argument("--mode", choices=("report", "enforce"), default="report")
    parser.add_argument("--annotate", action="store_true", help="also print GitHub Actions annotations")
    args = parser.parse_args()

    enforce = args.mode == "enforce"
    if enforce:
        print("Enforcing: a finding that depends on the API's document fails this check.")
    else:
        print("Reporting: a finding that depends on the API's document is a warning; one about the list fails.")
    if args.document:
        print(f"Document: {args.document}")
        document = json.loads(args.document.read_text())
    elif args.ref:
        print(f"Document: built from {args.ref} ({_commit(ROOT, args.ref)})")
        document = contract_document_at(ROOT, args.ref)
    else:
        tree = (args.tree or ROOT).resolve()
        print(f"Document: built from {tree} ({_commit(tree, 'HEAD')})")
        document = build(tree, released=False)
    print(f"List:     {args.list}")

    try:
        findings = check(args.list, document)
    except Refused as error:
        print(f"\nCannot judge: {error}", file=sys.stderr)
        sys.exit(2)
    text, passes = report(findings, enforce=enforce, annotate=args.annotate)
    print(text)
    sys.exit(0 if passes else 1)


def _commit(repo: Path, ref: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%h %s", ref], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else "not a git checkout"


if __name__ == "__main__":
    main()
