"""The two Claude Code PreToolUse hooks on Bash, as decisions: `git_refusal` (hooks that skip, force pushes) and
`heavy_refusal` (bare heavy runs, the banned desktop tests).

Moved from the former hook scripts. Both read the hook JSON the same way (`{"tool_input": {"command": ...}, "cwd": ...}`)
but fail differently, and that is kept: the git guard fails CLOSED (input it cannot read blocks the call), the heavy-run
guard fails OPEN (input it cannot read lets the call pass). `read_command` and `read_heavy_input` say which.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

HEAVY = {"pytest", "vitest", "tsc", "mypy", "playwright", "make"}
RUNNERS = {"uv", "uvx", "npx", "pnpm", "npm", "yarn", "bunx", "python", "python3", "node", "exec", "time", "nice", "env"}
BANNED = ("desktop_installation_test", "desktop_framework_wrap_test")

GIT_BLOCKED = "Blocked: no --no-verify or force push. If pre-push fails, wait for lower load or report it."
HEAVY_BLOCKED = "Blocked: heavy runs go through the run lock (one at a time). Prefix the command with `sm gate run --`."
BANNED_BLOCKED = "Blocked: the desktop tests provoke the macOS 'damaged' dialog on the owner's screen; hosted CI runs them."

GIT = re.compile(r"\bgit\b[^;&|\n]*?\b(push|commit)\b[^;&|\n]*")
SKIPPING = re.compile(r"(^|\s)(--no-verify|--force|--force-with-lease|-f|-n)(\s|$|=)")
COMMIT_F = re.compile(r"\s-f\b")


class Unreadable(Exception):
    """The hook input is not the JSON the guard expects."""


def hook_input(text: str) -> dict:
    """The hook JSON as a dict. Raises Unreadable for anything else (a guard decides what that means)."""
    try:
        data = json.loads(text)
    except ValueError as err:
        raise Unreadable(str(err)) from None
    if not isinstance(data, dict):
        raise Unreadable("the hook input is not a JSON object")
    return data


def read_command(text: str) -> str:
    """The git guard's read: `json.load(sys.stdin).get("tool_input", {}).get("command", "")`; anything unusable raises
    Unreadable, and the caller blocks (fail closed)."""
    tool_input = hook_input(text).get("tool_input", {})
    if not isinstance(tool_input, dict):
        raise Unreadable("tool_input is not an object")
    command = tool_input.get("command", "")
    if not isinstance(command, str):
        raise Unreadable("command is not a string")
    return command


def git_refusal(command: str) -> str | None:
    """The reason to block a git push or commit that skips hooks or forces, else None."""
    for match in GIT.finditer(command):
        if SKIPPING.search(match.group(0)) and not (match.group(1) == "commit" and COMMIT_F.search(match.group(0))):
            return GIT_BLOCKED
    return None


# --- heavy runs ---------------------------------------------------------------------------------------------------

def in_management(cwd: str, marker: str) -> bool:
    p = Path(cwd or ".").resolve()
    return any((d / marker).exists() for d in (p, *p.parents))


def segments(command: str) -> list[list[str]]:
    out = []
    for seg in re.split(r"&&|\|\||;|\||\n", command):
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
            toks.pop(0)
        if toks:
            out.append(toks)
    return out


def gated(toks: list[str]) -> bool:
    """A segment that already runs under the run lock: the former gate, or `sm gate run`."""
    first = Path(toks[0]).name
    # old name kept: processes of the retired tools can still run on a machine; they keep their protection
    return first == "hz-gate" or (first == "sm" and toks[1:3] == ["gate", "run"])


def heavy_refusal(command: str, cwd: str, marker: str = "HLD.md") -> str | None:
    # old name kept: processes of the retired tools can still run on a machine; they keep their protection
    if any(b in command for b in BANNED) and re.search(r"\b(pytest|hz-gate|sm|python|uv)\b", command):
        return BANNED_BLOCKED
    if in_management(cwd, marker):
        return None
    for toks in segments(command):
        first = Path(toks[0]).name
        if gated(toks):
            continue
        heavy = first in HEAVY or (first in RUNNERS and any(Path(t).name in HEAVY for t in toks[1:]))
        if heavy:
            return HEAVY_BLOCKED
    return None


def read_heavy_input(text: str) -> tuple[str, str] | None:
    """(command, cwd) from the hook JSON, or None when it cannot be read (the heavy-run guard then lets the call pass)."""
    try:
        data = json.loads(text)
        return data.get("tool_input", {}).get("command", "") or "", data.get("cwd", "") or ""
    except (ValueError, AttributeError):
        return None
