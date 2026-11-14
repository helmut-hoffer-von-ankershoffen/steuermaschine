"""The invocation log: one JSON line per `sm` command, appended under the project's state directory.

It records what is needed to see which commands are used, how long they take and where they refuse: the command path,
the NAMES of the flags used (never a value, never an argument), the start time, the duration, the exit code, a reason
code when the command refused or waited, and the caller (`SM_CALLER`, else "unknown"). `record` never raises and never
waits: any error is swallowed, because a statistics line must not change what a command does. The file rotates by size.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .protocols import Commands

FILE = "invocations.jsonl"
MAX_BYTES = 1_000_000  # above this the file moves to `invocations.jsonl.1` (one generation is kept)
FLAG = re.compile(r"^--?[A-Za-z][A-Za-z0-9-]*")
NOTE: dict[str, str] = {}  # the reason of the refusal in progress, set by `reason()`, read and cleared by `record`


def reason(message: str) -> None:
    """Remember a reason code for the current refusal: the leading words of its message up to the first ':', only words
    of letters (a number, a path or a name with a digit is dropped), at most six, joined with '-'."""
    words = [w for w in re.split(r"[^A-Za-z]+", message.split(":", 1)[0].lower()) if len(w) > 1]
    NOTE["reason"] = "-".join(words[:6])[:60]


def flag_names(args: list[str]) -> list[str]:
    """The distinct option names in `args`, in order: `--name=value` gives `--name`, `-n12` gives `-n`; nothing after `--`."""
    names: list[str] = []
    for arg in args:
        if arg == "--":
            break
        found = FLAG.match(arg)
        if found is None:
            continue
        name = found.group(0) if arg.startswith("--") else arg[:2]
        if name not in names:
            names.append(name)
    return names


def command_path(root: Any, args: list[str]) -> str:
    """The command words of `args` found by walking the command tree; option values and arguments never end up in it."""
    words: list[str] = []
    node = root
    for arg in args:
        children = getattr(node, "commands", None)
        if arg.startswith("-") or not children or arg not in children:
            continue
        words.append(arg)
        node = children[arg]
    return " ".join(words)


def failure_class(code: int) -> str:
    """"" for success; refused (1, 75), timeout (124), killed (a signal: 128 and up), else failed."""
    if code == 0:
        return ""
    return "refused" if code in (1, 75) else "timeout" if code == 124 else "killed" if code >= 128 else "failed"


def tree(commands: Commands | None) -> str:
    """The tree SHA of HEAD in the current directory, through the process provider the command already built ("" when
    there is none, it is no repository, git is missing or it takes over 2 s)."""
    if commands is None:
        return ""
    try:
        found = commands.run(["git", "rev-parse", "HEAD^{tree}"], cwd=os.getcwd(), timeout=2)
    except Exception:
        return ""
    text = found.out.strip()
    return text if found.rc == 0 and re.fullmatch(r"[0-9a-f]{40,64}", text) else ""


def caller() -> str:
    from .config import environment

    return re.sub(r"[^A-Za-z0-9._-]", "", environment().sm_caller or "")[:40] or "unknown"


def record(home: Path | None, commands: Commands | None, root: Any, args: list[str], started: float, began: float, code: int) -> None:
    """Append one line for a finished command. Never raises."""
    try:
        if home is None:
            return
        line = {
            "command": command_path(root, args) or "go",
            "flags": flag_names(args),
            "started_at": datetime.fromtimestamp(started, UTC).strftime("%Y-%m-%dT%H:%M:%S%z"),
            "duration_ms": round((time.perf_counter() - began) * 1000),
            "exit": code,
            "reason": NOTE.get("reason", "") if code else "",
            "caller": caller(),
            "tree": tree(commands),
            "failure": failure_class(code),
        }
        path = home / FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_BYTES:
            os.replace(path, path.with_name(FILE + ".1"))
        with path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(line, sort_keys=True) + "\n")
    except Exception:
        pass
    finally:
        NOTE.clear()
