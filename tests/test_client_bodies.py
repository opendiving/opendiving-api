"""The client check (`scripts/client_bodies.py`): a client's recorded bodies against a document.

A small document written here stands in for the API's, so each case names the one shape it is
about; the check's job in CI runs the same step against the document this tree builds.
"""

import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent

# `scripts/` is not a package, and the checker reads its sibling as a script does.
sys.path.insert(0, str(_ROOT / "scripts"))
client_bodies: Any = importlib.import_module("client_bodies")


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _json(schema: dict[str, Any]) -> dict[str, Any]:
    return {"content": {"application/json": {"schema": schema}}}


DOCUMENT: dict[str, Any] = {
    "openapi": "3.1.0",
    "paths": {
        "/api/v1/dive/{uuid}": {
            "get": {"responses": {"200": _json(_ref("DiveRead")), "304": {"description": "Not modified"}}},
        },
        "/api/v1/dives": {
            "get": {"responses": {"200": _json({"type": "array", "items": _ref("DiveRead")})}},
        },
        "/api/v1/dive": {
            "post": {"requestBody": _json(_ref("DiveCreate")), "responses": {"201": _json(_ref("DiveRead"))}},
        },
    },
    "components": {
        "schemas": {
            "DiveRead": {
                "type": "object",
                "properties": {
                    "uuid": {"type": "string"},
                    "depth": {"type": "number"},
                    "water": {"type": "string", "enum": ["salt", "fresh"]},
                    "site": {"anyOf": [_ref("SiteRead"), {"type": "null"}]},
                    "buddies": {"type": "array", "items": _ref("Buddy")},
                    "extras": {"type": "object", "additionalProperties": {"type": "string"}},
                    "rows": {
                        "type": "array",
                        "items": {
                            "oneOf": [_ref("SiteRow"), _ref("PlaceRow")],
                            "discriminator": {"propertyName": "kind"},
                        },
                    },
                },
                "required": ["uuid", "depth"],
            },
            "SiteRead": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            "Buddy": {"type": "object", "properties": {"name": {"type": "string"}}},
            "SiteRow": {
                "type": "object",
                "properties": {"kind": {"const": "site"}, "name": {"type": "string"}},
                "required": ["kind"],
            },
            "PlaceRow": {
                "type": "object",
                "properties": {"kind": {"const": "place"}, "name": {"type": "string"}},
                "required": ["kind"],
            },
            "DiveCreate": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"depth": {"type": "number"}, "notes": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
                "required": ["depth"],
            },
            "Error": {"type": "object", "properties": {"detail": {"type": "string"}}},
        }
    },
}

DIVE = "GET /api/v1/dive/{uuid} 200"


def _client(
    tmp_path: Path, bodies: dict[str, Any], entries: list[dict[str, Any]] | None = None, *, unlisted: bool = False
) -> Path:
    directory = tmp_path / "Fixtures"
    directory.mkdir()
    for name, body in bodies.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(json.dumps(body))
    listed = entries if entries is not None else [{"file": name, "is": [{"response": DIVE}]} for name in bodies]
    path = tmp_path / "api-bodies.json"
    path.write_text(json.dumps({"directory": "Fixtures", "bodies": listed}))
    return path


def _findings(tmp_path: Path, body: Any, *specs: dict[str, str], **entry: Any) -> list[tuple[str | None, bool]]:
    path = _client(
        tmp_path, {"body.json": body}, [{"file": "body.json", "is": list(specs or [{"response": DIVE}]), **entry}]
    )
    return [(finding.place, finding.on_document) for finding in client_bodies.check(path, DOCUMENT)]


class TestAResponse:
    def test_that_holds_passes(self, tmp_path: Path) -> None:
        body = {"uuid": "a", "depth": 12.5, "water": "salt", "site": None, "extras": {"any": "thing"}}
        assert _findings(tmp_path, body) == []

    def test_lacking_a_member_passes_even_a_required_one(self, tmp_path: Path) -> None:
        assert _findings(tmp_path, {"water": "fresh", "site": {}}) == []

    @pytest.mark.parametrize(
        ("body", "place"),
        [
            ({"uuid": "a", "shoe_size": 44}, "/shoe_size"),
            ({"uuid": "a", "buddies": [{"name": "Ana"}, {"name": "Bo", "age": 30}]}, "/buddies/1/age"),
            ({"uuid": "a", "site": {"name": "Canyon", "country": "EG"}}, "/site/country"),
        ],
        ids=["at the top", "inside an array", "behind a null union"],
    )
    def test_with_a_member_the_schema_lacks_names_it(self, tmp_path: Path, body: Any, place: str) -> None:
        assert _findings(tmp_path, body) == [(place, True)]

    def test_with_a_member_the_schema_lacks_names_it_nested(self, tmp_path: Path) -> None:
        path = _client(
            tmp_path,
            {"body.json": {"site": {"name": "x", "x": 1}}},
            [{"file": "body.json", "is": [{"schema": "DiveRead"}]}],
        )
        (finding,) = client_bodies.check(path, DOCUMENT)
        assert finding.place == "/site/x"
        assert "it has name" in finding.text

    def test_with_a_value_of_the_wrong_type_names_what_is_allowed(self, tmp_path: Path) -> None:
        path = _client(tmp_path, {"body.json": {"depth": "deep"}})
        (finding,) = client_bodies.check(path, DOCUMENT)
        assert (finding.place, finding.on_document) == ("/depth", True)
        assert "is a string, where the schema allows number" in finding.text

    def test_with_a_value_outside_an_enum_names_the_enum(self, tmp_path: Path) -> None:
        path = _client(tmp_path, {"body.json": {"water": "brackish"}})
        (finding,) = client_bodies.check(path, DOCUMENT)
        assert finding.place == "/water"
        assert '["salt", "fresh"]' in finding.text

    def test_of_a_discriminated_union_is_judged_by_its_own_branch(self, tmp_path: Path) -> None:
        body = {"rows": [{"kind": "site", "name": "Canyon"}, {"kind": "place", "name": "Dahab", "x": 1}]}
        assert _findings(tmp_path, body) == [("/rows/1/x", True)]

    def test_matching_no_branch_says_so_where_the_union_stands(self, tmp_path: Path) -> None:
        assert _findings(tmp_path, {"rows": [{"kind": "dive_centre"}]}) == [("/rows/0", True)]

    def test_listed_for_two_operations_is_judged_by_each(self, tmp_path: Path) -> None:
        body = [{"uuid": "a", "x": 1}]
        assert _findings(tmp_path, body, {"response": "GET /api/v1/dives 200"}, {"response": DIVE}) == [
            ("/0/x", True),
            ("", True),
        ]


class TestARequest:
    def test_that_holds_passes(self, tmp_path: Path) -> None:
        assert _findings(tmp_path, {"depth": 3, "notes": None}, {"request": "POST /api/v1/dive"}) == []

    def test_with_an_unknown_member_names_it(self, tmp_path: Path) -> None:
        assert _findings(tmp_path, {"depth": 3, "mood": "calm"}, {"request": "POST /api/v1/dive"}) == [("/mood", True)]

    def test_without_a_required_member_names_it(self, tmp_path: Path) -> None:
        path = _client(
            tmp_path, {"body.json": {"notes": "x"}}, [{"file": "body.json", "is": [{"request": "POST /api/v1/dive"}]}]
        )
        (finding,) = client_bodies.check(path, DOCUMENT)
        assert (finding.place, finding.on_document) == ("", True)
        assert "lacks the required member depth" in finding.text


class TestTheList:
    def test_a_body_with_no_entry_is_a_finding_about_the_list(self, tmp_path: Path) -> None:
        path = _client(tmp_path, {"a.json": {}, "nested/b.json": {}}, [{"file": "a.json", "is": [{"response": DIVE}]}])
        (finding,) = client_bodies.check(path, DOCUMENT)
        assert finding.file.endswith("Fixtures/nested/b.json")
        assert (finding.place, finding.on_document) == (None, False)

    def test_an_entry_whose_file_is_gone_is_a_finding_about_the_list(self, tmp_path: Path) -> None:
        path = _client(tmp_path, {}, [{"file": "gone.json", "is": [{"response": DIVE}]}])
        assert [(f.place, f.on_document, "is not there" in f.text) for f in client_bodies.check(path, DOCUMENT)] == [
            (None, False, True)
        ]

    @pytest.mark.parametrize(
        ("spec", "missing"),
        [
            ({"response": "DELETE /api/v1/dive/{uuid} 204"}, "no operation DELETE /api/v1/dive/{uuid}"),
            ({"response": "GET /api/v1/dive/{uuid} 404"}, "has no 404 response"),
            ({"response": "GET /api/v1/dive/{uuid} 304"}, "has no JSON body"),
            ({"request": "GET /api/v1/dives"}, "takes no JSON request body"),
            ({"schema": "DiveSummary"}, "no component schema DiveSummary"),
        ],
    )
    def test_an_entry_naming_what_the_document_lacks_depends_on_the_document(
        self, tmp_path: Path, spec: dict[str, str], missing: str
    ) -> None:
        path = _client(tmp_path, {"body.json": {}}, [{"file": "body.json", "is": [spec]}])
        (finding,) = client_bodies.check(path, DOCUMENT)
        assert finding.on_document
        assert missing in finding.text

    @pytest.mark.parametrize("spec", [{"response": "GET /api/v1/dives"}, {"reply": DIVE}, "GET /api/v1/dives 200"])
    def test_an_entry_spelled_wrongly_is_a_finding_about_the_list(self, tmp_path: Path, spec: Any) -> None:
        path = _client(tmp_path, {"body.json": {}}, [{"file": "body.json", "is": [spec]}])
        assert [f.on_document for f in client_bodies.check(path, DOCUMENT)] == [False]

    def test_a_file_listed_twice_is_a_finding_about_the_list(self, tmp_path: Path) -> None:
        entry = {"file": "body.json", "is": [{"response": DIVE}]}
        path = _client(tmp_path, {"body.json": {}}, [entry, entry])
        assert [f.on_document for f in client_bodies.check(path, DOCUMENT)] == [False]


class TestAnExemption:
    def test_silences_its_place_and_below_and_the_rest_is_still_judged(self, tmp_path: Path) -> None:
        body = {"site": {"name": "x", "country": "EG", "region": {"a": 1}}, "depth": "deep"}
        exempt = [{"at": "/site/country", "reason": "on purpose"}, {"at": "/site/region", "reason": "on purpose"}]
        assert _findings(tmp_path, body, exempt=exempt) == [("/depth", True)]

    def test_at_a_place_the_body_does_not_hold_is_a_finding_about_the_list(self, tmp_path: Path) -> None:
        assert _findings(tmp_path, {}, exempt=[{"at": "/site", "reason": "on purpose"}]) == [("/site", False)]

    def test_that_exempts_nothing_depends_on_the_document(self, tmp_path: Path) -> None:
        exempt = [{"at": "/depth", "reason": "it used to be a string"}]
        assert _findings(tmp_path, {"depth": 3}, exempt=exempt) == [("/depth", True)]

    def test_without_a_reason_is_a_finding_about_the_list(self, tmp_path: Path) -> None:
        assert _findings(tmp_path, {"x": 1}, exempt=[{"at": "/x", "reason": " "}]) == [(None, False), ("/x", True)]


class TestTheModes:
    @pytest.mark.parametrize(
        ("on_document", "enforce", "passes"),
        [(True, False, True), (True, True, False), (False, False, False), (False, True, False)],
        ids=["the document's, reported", "the document's, enforced", "the list's, reported", "the list's, enforced"],
    )
    def test_decide_whether_a_finding_fails(self, on_document: bool, enforce: bool, passes: bool) -> None:
        finding = client_bodies.Finding("body.json", "/x", "is a member the schema does not have", on_document)
        text, passed = client_bodies.report([finding], enforce=enforce, annotate=True)
        assert passed is passes
        assert f"::{'warning' if passes else 'error'} file=body.json,title=api-contract::" in text

    @pytest.mark.parametrize("mode", ["report", "enforce"])
    def test_are_named_on_the_first_line(self, tmp_path: Path, mode: str) -> None:
        document = tmp_path / "document.json"
        document.write_text(json.dumps(DOCUMENT))
        path = _client(tmp_path, {"body.json": {"uuid": "a", "x": 1}})
        result = subprocess.run(
            [
                sys.executable,
                str(_ROOT / "scripts" / "client_bodies.py"),
                "--list",
                str(path),
                "--document",
                str(document),
                "--mode",
                mode,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.stdout.splitlines()[0].startswith("Enforcing:" if mode == "enforce" else "Reporting:")
        assert result.returncode == (1 if mode == "enforce" else 0)


class TestTheDocument:
    def test_that_composes_with_all_of_is_refused_naming_the_schema(self, tmp_path: Path) -> None:
        document = json.loads(json.dumps(DOCUMENT))
        document["components"]["schemas"]["Buddy"] = {"allOf": [_ref("SiteRead"), {"properties": {"age": {}}}]}
        with pytest.raises(client_bodies.Refused, match="the component Buddy composes schemas with allOf"):
            client_bodies.check(_client(tmp_path, {}), document)

    def test_is_not_changed_by_judging(self, tmp_path: Path) -> None:
        before = json.dumps(DOCUMENT, sort_keys=True)
        client_bodies.check(_client(tmp_path, {"body.json": {"uuid": "a"}}), DOCUMENT)
        assert json.dumps(DOCUMENT, sort_keys=True) == before
