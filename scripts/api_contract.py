"""The contract check: fail a change to the API's released surface that would break a client, unless
the pull request's title carries `!`.

    uv run python scripts/api_contract.py compare origin/main HEAD --title 'feat!: ...'
    uv run python scripts/api_contract.py pull-request --base-branch origin/main --head HEAD --title '...'
    uv run python scripts/api_contract.py merge-group --head <sha>

`compare` judges any two refs. `pull-request` judges a branch against the commit it was cut from on
its base branch, and `merge-group` judges a merge-queue entry's squash commit against its parent,
with that commit's subject as the title: the two ways CI calls it. Each side is built from its own
tree with its own locked dependencies (`api_document.py`), with every experimental feature off, and
the two documents are compared by `oasdiff` at the release pinned below, which is downloaded on
first use and run on local files only.

What fails is the table in `ROWS`, and a change no row covers when the differ rates it ERR. The
rows are the contributor guide's definition of a wire break; `api_contract_proof.py` proves each
one against the real document.
"""

import argparse
import hashlib
import io
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from api_document import build

# renovate: datasource=github-releases depName=oasdiff/oasdiff
OASDIFF_VERSION = "v1.38.0"

#: The marker, after the type and the optional scope, as the title check spells both.
MARKER = re.compile(r"^[a-z]+(\([a-z0-9._-]+\))?!: ")


@dataclass(frozen=True)
class Row:
    change: str
    fails: bool
    #: The differ's rules this row sets the level of: ERR where it fails, INFO where it passes.
    rules: tuple[str, ...]


ROWS: tuple[Row, ...] = (
    Row(
        "An operation is removed, or loses a success status or its JSON body",
        True,
        (
            "api-removed-without-deprecation",
            "api-removed-with-deprecation",
            "api-removed-before-sunset",
            "api-path-removed-without-deprecation",
            "api-path-removed-with-deprecation",
            "api-path-removed-before-sunset",
            "response-success-status-removed",
            "response-media-type-removed",
        ),
    ),
    Row(
        "A response member is removed or renamed, whether or not its schema lists it as required",
        True,
        (
            "response-required-property-removed",
            "response-required-property-removed-with-deprecation",
            "response-required-property-removed-before-sunset",
            "response-optional-property-removed",
        ),
    ),
    Row(
        "A response member changes type, or one that was never null may now be",
        True,
        (
            "response-property-type-changed",
            "response-property-type-generalized",
            "response-property-type-specialized",
            "response-property-type-compatible",
            "response-property-list-of-types-widened",
            "response-property-became-nullable",
            "response-body-type-changed",
            "response-body-type-generalized",
            "response-body-type-specialized",
            "response-body-type-compatible",
            "response-body-list-of-types-widened",
            "response-body-became-nullable",
        ),
    ),
    Row(
        "A value joins an enum a response carries",
        True,
        ("response-property-enum-value-added", "response-mediatype-enum-value-added"),
    ),
    Row(
        "A request-body member is removed or renamed",
        True,
        (
            "request-property-removed",
            "request-property-removed-with-deprecation",
            "request-property-removed-before-sunset",
        ),
    ),
    Row(
        "A request-body member or a parameter becomes required, or a new required one appears",
        True,
        (
            "request-property-became-required",
            "request-property-became-required-with-default",
            "new-required-request-property",
            "request-parameter-became-required",
            "new-required-request-parameter",
        ),
    ),
    Row(
        "A request-body member or a parameter changes type, stops accepting null, or loses an enum value",
        True,
        (
            "request-property-type-changed",
            "request-property-type-generalized",
            "request-property-type-compatible",
            "request-property-list-of-types-narrowed",
            "request-property-became-not-nullable",
            "request-property-enum-value-removed",
            "request-body-type-changed",
            "request-body-type-generalized",
            "request-body-type-compatible",
            "request-body-list-of-types-narrowed",
            "request-body-became-not-nullable",
            "request-body-enum-value-removed",
            "request-parameter-type-changed",
            "request-parameter-type-generalized",
            "request-parameter-list-of-types-narrowed",
            "request-parameter-became-not-nullable",
            "request-parameter-enum-value-removed",
            "request-parameter-property-type-changed",
            "request-parameter-property-type-generalized",
            "request-parameter-property-type-specialized",
            "request-parameter-property-list-of-types-narrowed",
            "request-parameter-property-became-not-nullable",
            "request-parameter-property-enum-value-removed",
        ),
    ),
    Row(
        "A parameter is removed",
        True,
        (
            "request-parameter-removed",
            "request-parameter-removed-with-deprecation",
            "request-parameter-removed-before-sunset",
        ),
    ),
    Row(
        "An operation, a response member, an optional request-body member or an optional parameter is added",
        False,
        (
            "endpoint-added",
            "response-optional-property-added",
            "response-required-property-added",
            "new-optional-request-property",
            "new-optional-request-parameter",
        ),
    ),
    Row(
        "A response member stops being listed as required, with nothing else about it changed",
        False,
        ("response-property-became-optional",),
    ),
    Row(
        "A value leaves an enum a response carries, or joins an enum a request accepts",
        False,
        (
            "response-property-enum-value-removed",
            "response-mediatype-enum-value-removed",
            "request-property-enum-value-added",
            "request-body-enum-value-added",
            "request-parameter-enum-value-added",
            "request-parameter-property-enum-value-added",
        ),
    ),
    Row(
        "A request-body member or a parameter starts accepting null",
        False,
        (
            "request-property-list-of-types-widened",
            "request-property-became-nullable",
            "request-body-list-of-types-widened",
            "request-body-became-nullable",
            "request-parameter-list-of-types-widened",
            "request-parameter-became-nullable",
            "request-parameter-property-list-of-types-widened",
            "request-parameter-property-became-nullable",
        ),
    ),
    Row(
        "A response member that could be null never is",
        False,
        (
            "response-property-list-of-types-narrowed",
            "response-property-became-not-nullable",
            "response-body-list-of-types-narrowed",
            "response-body-became-not-nullable",
        ),
    ),
    Row(
        "A description, a title, an example or the document's version changes",
        False,
        ("api-version-changed",),
    ),
)

_ROW_OF_RULE = {rule: row for row in ROWS for rule in row.rules}

#: The differ's levels, as its JSON output numbers them.
_LEVELS = {3: "ERR", 2: "WARN", 1: "INFO"}


@dataclass(frozen=True)
class Finding:
    level: str
    rule: str
    operation: str
    text: str

    @property
    def row(self) -> Row | None:
        return _ROW_OF_RULE.get(self.rule)

    @property
    def breaks(self) -> bool:
        return self.level == "ERR"

    def describe(self) -> str:
        where = f"{self.operation}: " if self.operation else ""
        why = self.row.change if self.row else f"no row of the table; the differ's rule `{self.rule}` rates it ERR"
        return f"  {where}{self.text}\n      {why}"


def _oasdiff_asset() -> str:
    version = OASDIFF_VERSION.removeprefix("v")
    system, machine = platform.system(), platform.machine().lower()
    if system == "Darwin":
        return f"oasdiff_{version}_darwin_all.tar.gz"
    if system == "Linux" and machine in {"x86_64", "amd64"}:
        return f"oasdiff_{version}_linux_amd64.tar.gz"
    if system == "Linux" and machine in {"aarch64", "arm64"}:
        return f"oasdiff_{version}_linux_arm64.tar.gz"
    raise RuntimeError(f"no oasdiff {OASDIFF_VERSION} release for {system} {machine}; set OASDIFF to a binary of it")


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - a fixed https URL
        body: bytes = response.read()
    return body


def oasdiff() -> Path:
    """The differ at the pinned release: `OASDIFF` when that is set, else a copy downloaded once, its
    checksum verified against the release's own list."""
    if override := os.environ.get("OASDIFF"):
        binary = Path(override)
    else:
        cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "opendiving-api"
        binary = cache / f"oasdiff-{OASDIFF_VERSION}" / "oasdiff"
        if not binary.exists():
            asset = _oasdiff_asset()
            base = f"https://github.com/oasdiff/oasdiff/releases/download/{OASDIFF_VERSION}"
            archive = _download(f"{base}/{asset}")
            sums: dict[str, str] = {}
            for line in _download(f"{base}/checksums.txt").decode().splitlines():
                digest, name = line.split()
                sums[name] = digest
            if hashlib.sha256(archive).hexdigest() != sums.get(asset):
                raise RuntimeError(f"{asset} does not match the checksum its release publishes")
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                member = tar.extractfile("oasdiff")
                if member is None:
                    raise RuntimeError(f"{asset} holds no oasdiff binary")
                binary.parent.mkdir(parents=True, exist_ok=True)
                partial = binary.with_suffix(".partial")
                partial.write_bytes(member.read())
                partial.chmod(0o755)
                partial.replace(binary)
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, check=True).stdout
    if OASDIFF_VERSION.removeprefix("v") not in version.split():
        raise RuntimeError(f"{binary} is {version.strip()!r}, and the check is pinned to {OASDIFF_VERSION}")
    return binary


def severity_levels() -> str:
    """The differ's levels file: every rule a row names, at ERR where the row fails and INFO where
    it passes. A rule no row names keeps the differ's own level."""
    return "".join(f"{rule} {'err' if row.fails else 'info'}\n" for row in ROWS for rule in row.rules)


def null_unions_as_one_of(node: Any) -> Any:
    """`node` with each two-branch `anyOf` that has a null branch spelled as a `oneOf`.

    FastAPI spells an optional value `anyOf: [X, {"type": "null"}]`. The two say the same where `X`
    does not admit null, which is every case this document has, and the differ's nullable rule
    reads only the `oneOf` spelling: under `anyOf` an object member gaining or losing its null
    branch reads as a change of type, or as every member of the object removed."""
    if isinstance(node, list):
        return [null_unions_as_one_of(value) for value in node]
    if not isinstance(node, dict):
        return node
    out = {key: null_unions_as_one_of(value) for key, value in node.items()}
    branches = out.get("anyOf")
    if isinstance(branches, list) and len(branches) == 2 and {"type": "null"} in branches and "oneOf" not in out:
        out["oneOf"] = out.pop("anyOf")
    return out


def diff(base: dict[str, Any], revision: dict[str, Any]) -> list[Finding]:
    """What changed from `base` to `revision`, each change at the level the table gives it."""
    binary = oasdiff()
    with tempfile.TemporaryDirectory(prefix="api-contract-") as scratch:
        work = Path(scratch)
        (work / "base.json").write_text(json.dumps(null_unions_as_one_of(base)))
        (work / "revision.json").write_text(json.dumps(null_unions_as_one_of(revision)))
        (work / "levels.txt").write_text(severity_levels())
        # Run where no `.oasdiff.*` file can be picked up, with no configuration from the environment.
        env = {name: value for name, value in os.environ.items() if name != "OASDIFF_CONFIG"}
        result = subprocess.run(
            [
                binary,
                "changelog",
                "base.json",
                "revision.json",
                "--format",
                "json",
                "--severity-levels",
                "levels.txt",
                "--allow-external-refs=false",
            ],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"oasdiff failed:\n{result.stderr}")
    return [
        Finding(
            level=_LEVELS.get(entry["level"], str(entry["level"])),
            rule=entry["id"],
            operation=" ".join(part for part in (entry.get("operation"), entry.get("path")) if part),
            text=entry["text"],
        )
        for entry in json.loads(result.stdout or "[]")
    ]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def pull_request_commits(repo: Path, base_branch: str, head: str) -> tuple[str, str]:
    """The commit the branch was cut from on its base branch, and its head. A change the base
    branch made since is in neither, so it is attributed to the pull request in neither direction."""
    head_commit = git(repo, "rev-parse", "--verify", f"{head}^{{commit}}")
    return git(repo, "merge-base", base_branch, head_commit), head_commit


def merge_group_commits(repo: Path, head: str) -> tuple[str, str, str]:
    """A queue entry's parent, its squash commit, and that commit's subject. Judged on its own
    commit, an entry borrows neither a break nor a marker from the entries queued ahead of it."""
    head_commit = git(repo, "rev-parse", "--verify", f"{head}^{{commit}}")
    return git(repo, "rev-parse", f"{head_commit}^"), head_commit, git(repo, "log", "-1", "--format=%s", head_commit)


def released_surface(repo: Path, commit: str) -> dict[str, Any]:
    """The released surface `commit` builds, from its committed tree alone."""
    with tempfile.TemporaryDirectory(prefix="api-contract-tree-") as tree:
        archive = subprocess.run(["git", "-C", str(repo), "archive", commit], capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(tree, filter="data")
        document: dict[str, Any] = build(Path(tree), released=True)
        return document


@dataclass(frozen=True)
class Judgement:
    title: str
    base: str
    head: str
    findings: list[Finding]

    @property
    def marked(self) -> bool:
        return bool(MARKER.match(self.title))

    @property
    def breaks(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.breaks]

    @property
    def passes(self) -> bool:
        return self.marked or not self.breaks


def judge(
    repo: Path,
    base: str,
    head: str,
    title: str,
    document_of: Callable[[Path, str], dict[str, Any]] = released_surface,
    differ: Callable[[dict[str, Any], dict[str, Any]], list[Finding]] = diff,
) -> Judgement:
    return Judgement(title, base, head, differ(document_of(repo, base), document_of(repo, head)))


def report(repo: Path, judgement: Judgement) -> str:
    def commit(sha: str) -> str:
        return f"{sha} ({git(repo, 'log', '-1', '--format=%s', sha)})"

    lines = [
        f"Title:    {judgement.title}",
        f"Marker:   {'yes' if judgement.marked else 'no'}",
        f"Base:     {commit(judgement.base)}",
        f"Head:     {commit(judgement.head)}",
        "",
    ]
    breaks = judgement.breaks
    if breaks:
        lines.append(f"Wire breaks ({len(breaks)}):")
        lines += [finding.describe() for finding in breaks]
    else:
        lines.append("No wire break.")
    warnings = [finding for finding in judgement.findings if finding.level == "WARN"]
    if warnings:
        lines += ["", f"Rated WARN by the differ, which no row covers and which passes ({len(warnings)}):"]
        lines += [f"  {finding.operation}: {finding.text} (`{finding.rule}`)" for finding in warnings]
    lines.append("")
    if not breaks:
        lines.append("Passes.")
    elif judgement.marked:
        lines.append("Passes: the title carries `!`, which declares the breaks above.")
    else:
        lines.append(
            "Fails: the change breaks the API contract and the title does not say so. If the break is "
            "intended, put `!` after the type and scope (`feat(dives)!: ...`) and say in the pull request "
            "which client needs a matching change; this check runs again when the title is edited."
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    commands = parser.add_subparsers(dest="command", required=True)
    compare = commands.add_parser("compare", help="judge two refs")
    compare.add_argument("base")
    compare.add_argument("head")
    compare.add_argument("--title", default="", help="the title to read the marker from")
    pull_request = commands.add_parser("pull-request", help="judge a branch against where it was cut")
    pull_request.add_argument("--base-branch", required=True)
    pull_request.add_argument("--head", required=True)
    pull_request.add_argument("--title", required=True)
    merge_group = commands.add_parser("merge-group", help="judge a merge-queue entry's squash commit")
    merge_group.add_argument("--head", required=True)
    args = parser.parse_args()

    repo: Path = args.repo
    if args.command == "compare":
        base, head, title = git(repo, "rev-parse", "--verify", args.base), git(repo, "rev-parse", args.head), args.title
    elif args.command == "pull-request":
        (base, head), title = pull_request_commits(repo, args.base_branch, args.head), args.title
    else:
        base, head, title = merge_group_commits(repo, args.head)

    judgement = judge(repo, base, head, title)
    print(report(repo, judgement))
    sys.exit(0 if judgement.passes else 1)


if __name__ == "__main__":
    main()
