#!/usr/bin/env python3
"""Block a Bash command that turns git commit signing off.

Wired up as a PreToolUse hook on Bash in .claude/settings.json. It exists
because `git -c commit.gpgsign=false commit` overrides every config file,
which is exactly what agents reach for to dodge a passphrase prompt that
this project does not have - and the result is a PR full of commits GitHub
marks Unverified. The pre-push hook in .githooks/ catches the same thing
later; this one catches it while there is still nothing to rewrite.

Exit 2 blocks the call and hands stderr back to the agent as the reason.
"""

import json
import re
import sys

OFF = re.compile(
    r"""
      --no-gpg-sign\b
    | \b(?:commit|tag)\.gpgsign \s* [=\ ] \s* (?: false | 0 | no | off | n )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

REASON = """\
Blocked: that command disables commit signing.

commit.gpgsign is on globally and signing works here without a passphrase
prompt, so there is nothing to work around - and a commit made this way
shows up as Unverified on GitHub forever. Run the commit without the flag.

If signing genuinely fails, say so and stop; do not bypass it.\
"""


def strip_heredoc_bodies(command: str) -> str:
    """Drop heredoc bodies - writing *about* the flag is not using it.

    Commit messages and docs travel as heredocs, and a good number of them
    have to quote the very thing this hook rejects. The opening line is kept
    (a real `git -c ... commit -F - <<'EOF'` puts the flag there, not in the
    body). An opener whose terminator never arrives is not a heredoc at all -
    `1 << bits` reads as one - so nothing is dropped in that case.
    """
    lines = command.split("\n")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        match = HEREDOC_START.search(line)
        if not match:
            continue
        terminator = match.group(2)
        end = index
        while end < len(lines) and lines[end].strip() != terminator:
            end += 1
        if end == len(lines):
            continue  # no terminator: `1 << bits` is a shift, not a heredoc
        index = end + 1  # skip the body and the terminator line
    return "\n".join(kept)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        # One type, not the `except A, B:` pair this repo's style would write: the
        # file runs under whatever `python3` a shell resolves, not the pinned 3.14,
        # and PEP 758 syntax on an older one is a SyntaxError - which exits 1, which
        # the hook system treats as non-blocking. The guard would fail *open*.
        # (JSONDecodeError and a UnicodeDecodeError off stdin are both ValueErrors.)
        return 0  # a payload we cannot read is not a payload we should judge
    command = (payload.get("tool_input") or {}).get("command") or ""
    if not OFF.search(strip_heredoc_bodies(command)):
        return 0
    print(REASON, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
