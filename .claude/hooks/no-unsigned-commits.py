#!/usr/bin/env python3
"""Block a Bash command that switches off signing git reports as on.

Wired up as a PreToolUse hook on Bash in .claude/settings.json. It exists
because `git -c commit.gpgsign=false commit` overrides every config file,
which is what an agent reaches for when it fears a commit is about to hang
- and what it leaves behind is a PR of commits GitHub marks Unverified.
The pre-push hook in .githooks/ catches the same thing later, wherever
someone has enabled it; this one catches it while there is still nothing
to rewrite.

Nothing is blocked unless git itself reports the key the matched command
would disable as on, so a clone that does not sign is never stopped from
committing - a contributor's commits do not need to be signed, because
PRs are squash-merged and GitHub signs the commit that lands on main.
That gate is also why the patterns cover the shapes that *remove* the
setting rather than override it: one of those slipping through would stop
signing and leave this hook reading a config the same command had just
deleted, inert from then on.

Exit 2 blocks the call and hands stderr back to the agent as the reason.
"""

import json
import re
import subprocess
import sys

# One pattern per key, because the gate asks git about the key the matched
# command would actually disable. Enforcing on either key blocks a commit on
# the strength of a *tag* setting and then names the wrong one in the message,
# which is the falsehood this whole gate exists to remove.
#
# The quote handling is not decoration. This matches command *text*, while the
# shell strips quotes before git sees the argument, so one falsy value reaches
# git through `=false`, `="false"`, `"commit.gpgsign=false"` and, empty,
# through `=`, `=""` and `"commit.gpgsign="` - all the same command to git, and
# an empty value is false to it. See DECISIONS.md.
DISABLE = (
    (
        "commit.gpgsign",
        re.compile(
            r"""
              --no-gpg-sign\b                      # commit/rebase/cherry-pick spelling
            | \b commit\.gpgsign \s* [=\ ] \s* ["']? (?: false | 0 | no | off | n )\b
            | \b commit\.gpgsign                   # an empty value is false to git
              (?: = ["']{0,2} | \s+ (?: "" | '' ) )
              (?! [^\s;&|()<>] )                   # word ends: space, chain, or EOL
            | (?<![\w-]) (?:--)? unset (?:-all)? \b    # removal, naming the key
              [^\n]* \b commit\.gpgsign \b
            | (?<![\w-]) (?:--)? (?: remove | rename ) -section \b
              [^\n]* \b commit \b                  # removal, naming only the section
            """,
            re.IGNORECASE | re.VERBOSE,
        ),
    ),
    (
        "tag.gpgsign",
        re.compile(
            r"""
              \b tag\.gpgsign \s* [=\ ] \s* ["']? (?: false | 0 | no | off | n )\b
            | \b tag\.gpgsign
              (?: = ["']{0,2} | \s+ (?: "" | '' ) )
              (?! [^\s;&|()<>] )
            | (?<![\w-]) (?:--)? unset (?:-all)? \b
              [^\n]* \b tag\.gpgsign \b
            | (?<![\w-]) (?:--)? (?: remove | rename ) -section \b
              [^\n]* \b tag \b
            """,
            re.IGNORECASE | re.VERBOSE,
        ),
    ),
)

HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

# The key sits on a line of its own so naming two of them cannot reflow the
# paragraph under it. It is the only thing this message may assert about the
# reader's environment: what git answered, for that key, in the directory the
# command would have run in.
REASON = """\
Blocked: that command switches signing off where git reports it on.

git reports {keys} on here.

Overriding that inline, or removing it from the config, costs provenance
and buys nothing else - what it leaves behind is history GitHub marks
Unverified forever. Run the command without that part.

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


def signing_is_on(key: str) -> bool:
    """Ask git whether `key` is on, and read every failure as "not on".

    `--type=bool` canonicalises 1/yes/on. The subprocess inherits this
    process's working directory deliberately: it has to read the config the
    blocked command would itself have read, and $CLAUDE_PROJECT_DIR stays at
    the session root while a session working in a worktree does not.

    Each way this can fail allows the command, and each is safe. The key
    unset exits 1 with no output. No git at all raises OSError. A bad boolean
    value (`commit.gpgsign = yess`) is `fatal: bad boolean config value`,
    exit 128 - and that one is safe rather than a silent disarm, because
    `git commit` fatals on the same value, so there is no unsigned commit to
    miss. "Not a repository" is not a failure case at all: global config is
    still read and the exit is 0. Nothing may raise past here - an exception
    escaping main() exits 1, which the hook system treats as non-blocking.
    """
    try:
        answer = subprocess.run(
            ["git", "config", "--type=bool", "--get", key],
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return answer.stdout.strip() == "true"


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
    scrubbed = strip_heredoc_bodies(command)
    # Regex first, so the hot path costs no subprocess on every Bash call.
    matched = [key for key, pattern in DISABLE if pattern.search(scrubbed)]
    on = [key for key in matched if signing_is_on(key)]
    if not on:
        return 0
    print(REASON.format(keys=" and ".join(on)), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
