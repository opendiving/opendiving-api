"""Build the API's OpenAPI document from a source tree, with no database and no cache running.

    uv run python scripts/api_document.py                       # this checkout, every feature on
    uv run python scripts/api_document.py --released            # every experimental feature off
    uv run python scripts/api_document.py --tree ../other -o other.json

Two documents can be built. The *contract document* is what `GET /api/v1/openapi.json` answers on
an instance with the MCP routes installed and every experimental feature on: everything a client may
call. The *released surface* is the same with every feature off: what an instance that names no
feature serves, which is what the contract check compares. Both are printed with sorted keys and
indentation, so the same tree with the same locked dependencies gives the same bytes.

The tree is built in its own environment (`uv run --project <tree> --locked`), from a scrubbed
process environment and with its `src/.env` ignored, so the answer depends on the tree's code and
lock file alone. The tree only has to hold the application: this file runs itself inside the tree,
so it builds a tree that predates it, which is what lets a pull request that changes the builder
compare against a base without it.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

#: The route the contract is defined by; the document is read through it, credentials annotated.
OPENAPI_PATH = "/api/v1/openapi.json"

#: What crosses into the build from the caller's environment: enough for `uv` to find itself and
#: its cache, nothing the application reads as a setting.
_PASSED_THROUGH = ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT")

# Published on purpose, and accepted only where `ENVIRONMENT` is `local` (`CI_SECRET_KEY`).
_SECRET_KEY = "test-secret-key-for-testing-only"


def _settings(released: bool, scratch: str) -> dict[str, str]:
    return {
        "ENVIRONMENT": "local",
        "SECRET_KEY": _SECRET_KEY,
        "MCP_ENABLED": "true",
        # Empty is every feature off; `all` is every feature a gate names, now or later.
        "EXPERIMENTAL_FEATURES": "" if released else "all",
        "FILE_STORAGE_DIR": scratch,
    }


def serialise(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def build(tree: Path, *, released: bool) -> dict[str, Any]:
    """The document `tree` serves, as `released` says."""
    tree = tree.resolve()
    with tempfile.TemporaryDirectory(prefix="api-document-") as scratch:
        env = {name: os.environ[name] for name in _PASSED_THROUGH if name in os.environ}
        env.update({name: value for name, value in os.environ.items() if name.startswith("UV_")})
        env.update(_settings(released, scratch))
        result = subprocess.run(
            ["uv", "run", "--project", str(tree), "--locked", "--quiet", "python", __file__, "--inner"],
            cwd=tree,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"building the document of {tree} failed:\n{result.stderr}")
    document: dict[str, Any] = json.loads(result.stdout)
    return document


def _inner() -> None:
    """Runs inside the tree's own environment, with its working directory at the tree's root."""
    import starlette.config

    # The settings module reads `src/.env` through starlette's `Config`. A file that is there only
    # in a developer's checkout must not change the document, so it is never read here.
    original = starlette.config.Config.__init__

    def without_env_file(self: Any, env_file: Any = None, *args: Any, **kwargs: Any) -> None:
        original(self, None, *args, **kwargs)

    starlette.config.Config.__init__ = without_env_file  # type: ignore[method-assign]

    sys.path.insert(0, os.getcwd())
    from fastapi.testclient import TestClient

    from src.app.main import app

    # No `with`: entering the client would run the lifespan, which wants Postgres and Redis.
    response = TestClient(app).get(OPENAPI_PATH)
    response.raise_for_status()
    sys.stdout.write(json.dumps(response.json()))


def main() -> None:
    if sys.argv[1:] == ["--inner"]:
        _inner()
        return
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tree", type=Path, default=ROOT, help="the source tree to build (default: this checkout)")
    parser.add_argument("--released", action="store_true", help="every experimental feature off")
    parser.add_argument("-o", "--output", type=Path, help="write here rather than to standard output")
    args = parser.parse_args()
    text = serialise(build(args.tree, released=args.released))
    if args.output:
        args.output.write_text(text)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
