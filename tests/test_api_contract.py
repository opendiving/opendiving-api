"""Tests for `scripts/api_document.py`, which builds the API's document from a tree, and for what
`scripts/api_contract.py` compares.

The table of verdicts is not tested here: it needs the pinned differ, and its proof is a step of the
contract check's own job (`scripts/api_contract_proof.py`), which runs on every pull request.
"""

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.app.api import router
from src.app.core.config import settings
from src.app.core.setup import OPENAPI_URL, create_application
from src.app.mcp.transport import install_mcp

_ROOT = Path(__file__).resolve().parent.parent

# `scripts/` is not a package, and the check reads its sibling as a script does.
sys.path.insert(0, str(_ROOT / "scripts"))
api_document: Any = importlib.import_module("api_document")
api_contract: Any = importlib.import_module("api_contract")


@pytest.fixture(scope="module")
def released() -> str:
    return str(api_document.serialise(api_document.build(_ROOT, released=True)))


class TestTheDocument:
    def test_is_what_the_route_serves_under_the_same_settings(
        self, monkeypatch: pytest.MonkeyPatch, released: str
    ) -> None:
        monkeypatch.setattr(settings, "EXPERIMENTAL_FEATURES", "")
        app = create_application(router=router, settings=settings, apply_migrations_on_start=False)
        install_mcp(app)
        served = TestClient(app).get(OPENAPI_URL).json()
        built = api_document.json.loads(released)

        # `info` carries the instance's name, contact and version, which this process reads from
        # whatever `src/.env` the checkout holds and the builder never does.
        served.pop("info")
        built.pop("info")
        assert built == served

    def test_is_the_same_bytes_built_twice(self, released: str) -> None:
        assert api_document.serialise(api_document.build(_ROOT, released=True)) == released

    def test_does_not_read_the_trees_env_file(self, tmp_path: Path, released: str) -> None:
        tracked = subprocess.run(
            ["git", "-C", str(_ROOT), "ls-files", "-z"], capture_output=True, text=True, check=True
        ).stdout.split("\0")
        tree = tmp_path / "tree"
        for name in filter(None, tracked):
            source = _ROOT / name
            if source.is_file():
                (tree / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, tree / name)
        (tree / "src" / ".env").write_text(
            "APP_NAME=Somebody's instance\nEXPERIMENTAL_FEATURES=all\nMCP_ENABLED=false\n"
            "CONTACT_NAME=Somebody\nLICENSE_NAME=Proprietary\n"
        )

        assert api_document.serialise(api_document.build(tree, released=True)) == released

    def test_is_openapi_3_1_with_nothing_composed_by_all_of(self, released: str) -> None:
        # The differ's verdicts were settled on this shape; a document that composes with allOf
        # is compared under rules nobody has proven against it.
        assert api_document.json.loads(released)["openapi"].startswith("3.1.")
        assert '"allOf"' not in released


class TestTheMarker:
    @pytest.mark.parametrize("title", ["feat!: x", "fix(dives)!: x", "refactor(dive-sites)!: x (#12)"])
    def test_is_a_bang_after_the_type_and_scope(self, title: str) -> None:
        assert api_contract.MARKER.match(title)

    @pytest.mark.parametrize("title", ["feat: x!", "feat: x", "feat(dives): a ! inside", "Feat!: x", "feat !: x"])
    def test_is_nowhere_else(self, title: str) -> None:
        assert not api_contract.MARKER.match(title)


class TestNullUnions:
    def test_a_null_union_is_spelled_as_the_differ_reads_one(self) -> None:
        node = {"properties": {"a": {"anyOf": [{"$ref": "#/x"}, {"type": "null"}], "description": "d"}}}

        assert api_contract.null_unions_as_one_of(node) == {
            "properties": {"a": {"oneOf": [{"$ref": "#/x"}, {"type": "null"}], "description": "d"}}
        }

    def test_any_other_any_of_is_left_alone(self) -> None:
        node = {
            "anyOf": [{"type": "string", "format": "date-time"}, {"type": "string", "format": "date"}, {"type": "null"}]
        }

        assert api_contract.null_unions_as_one_of(node) == node


# -- what is compared with what ----------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    # The throwaway history is written with no global or system configuration, so a developer's
    # signing, hooks or default branch never reach it.
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env
    ).stdout.strip()


def _commit(repo: Path, members: list[str], subject: str) -> str:
    (repo / "document").write_text("\n".join(members) + "\n")
    _git(repo, "add", "document")
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


def _document_of(repo: Path, commit: str) -> dict[str, Any]:
    return {"members": _git(repo, "show", f"{commit}:document").splitlines()}


def _removed(base: dict[str, Any], revision: dict[str, Any]) -> list[Any]:
    """A differ for the throwaway history: each member that goes is a wire break."""
    return [
        api_contract.Finding(level="ERR", rule="response-optional-property-removed", operation="GET /x", text=name)
        for name in base["members"]
        if name not in revision["members"]
    ]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    return tmp_path


class TestAPullRequest:
    def test_is_compared_with_the_commit_its_branch_was_cut_from(self, repo: Path) -> None:
        cut = _commit(repo, ["a", "b", "c"], "chore: start")
        _git(repo, "switch", "-q", "-c", "branch")
        head = _commit(repo, ["a", "b", "c", "d"], "feat: add d")
        _git(repo, "switch", "-q", "main")
        _commit(repo, ["a", "c"], "feat!: drop b (#1)")

        base, compared_head = api_contract.pull_request_commits(repo, "main", "branch")
        judgement = api_contract.judge(repo, base, compared_head, "feat: add d", _document_of, _removed)

        assert (base, compared_head) == (cut, head)
        # `main` dropped `b` after the branch was cut, and the branch still has it: neither is the
        # branch's change.
        assert judgement.findings == []
        assert judgement.passes

    def test_that_breaks_fails_without_the_marker_and_passes_with_it(self, repo: Path) -> None:
        _commit(repo, ["a", "b"], "chore: start")
        _git(repo, "switch", "-q", "-c", "branch")
        _commit(repo, ["a"], "drop b")
        base, head = api_contract.pull_request_commits(repo, "main", "branch")

        unmarked = api_contract.judge(repo, base, head, "feat: drop b", _document_of, _removed)
        marked = api_contract.judge(repo, base, head, "feat!: drop b", _document_of, _removed)

        assert [finding.text for finding in unmarked.breaks] == ["b"]
        assert not unmarked.passes
        assert [finding.text for finding in marked.breaks] == ["b"]
        assert marked.passes


class TestAQueueEntry:
    def test_borrows_neither_a_break_nor_a_marker_from_the_entry_ahead(self, repo: Path) -> None:
        _commit(repo, ["a", "b", "c"], "chore: start")
        # The queue's history: each entry squashed on top of the one ahead of it.
        ahead = _commit(repo, ["a", "c"], "feat!: drop b (#1)")
        entry = _commit(repo, ["a"], "feat: drop c (#2)")

        base, head, title = api_contract.merge_group_commits(repo, entry)
        judgement = api_contract.judge(repo, base, head, title, _document_of, _removed)

        assert (base, head, title) == (ahead, entry, "feat: drop c (#2)")
        assert [finding.text for finding in judgement.breaks] == ["c"]
        assert not judgement.passes

    def test_ahead_is_judged_on_its_own_commit_and_title(self, repo: Path) -> None:
        start = _commit(repo, ["a", "b", "c"], "chore: start")
        ahead = _commit(repo, ["a", "c"], "feat!: drop b (#1)")
        _commit(repo, ["a", "c", "d"], "feat: add d (#2)")

        base, head, title = api_contract.merge_group_commits(repo, ahead)
        judgement = api_contract.judge(repo, base, head, title, _document_of, _removed)

        assert (base, head) == (start, ahead)
        assert [finding.text for finding in judgement.breaks] == ["b"]
        assert judgement.passes


class TestTheReport:
    def test_names_the_title_the_commits_the_operation_and_the_row(self, repo: Path) -> None:
        base = _commit(repo, ["a", "b"], "chore: start")
        head = _commit(repo, ["a"], "feat: drop b (#3)")

        text = api_contract.report(
            repo, api_contract.judge(repo, base, head, "feat: drop b (#3)", _document_of, _removed)
        )

        assert "Title:    feat: drop b (#3)" in text
        assert f"Base:     {base} (chore: start)" in text
        assert f"Head:     {head} (feat: drop b (#3))" in text
        assert "GET /x: b" in text
        assert "A response member is removed or renamed" in text
        assert text.endswith("this check runs again when the title is edited.")
