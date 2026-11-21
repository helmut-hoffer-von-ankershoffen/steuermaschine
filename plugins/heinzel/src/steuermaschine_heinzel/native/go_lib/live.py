"""NEXT names only live, visible commands. Every `sm ...` command in a NEXT, THEN or ALSO line is checked against the command
tree that runs (its path and its options); one that is not there is shown as missing instead of offered to be run.

`checker(rows)` takes the rows of `steuermaschine.helpdoc.tree(root, "sm")`; `rows_of_running_tree()` reads them from
the Click context of the command that runs (None outside one: nothing is checked, as in a direct call).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable

PROG = "sm"
ALWAYS = frozenset({"--help", "-h", "--format"})  # the root and help options every command takes
COMMAND = re.compile(rf"(?:(?<=^)|(?<=[\s:`(\"'])){PROG}\s+[^&;|#`)\n]*")
MISSING = "missing: `{command}` is not a visible command; nothing to run for it now (sm why)"


def checker(rows: list[dict]) -> Callable[[str], str]:
    """A function that returns its text unchanged when every `sm` command in it is live, else the missing line."""
    leaves = {tuple(str(r["path"]).split()[1:]): set(r.get("options") or []) for r in rows if not r.get("group") and not r.get("hidden")}

    def check(text: str) -> str:
        if not text:
            return text
        for found in COMMAND.finditer(text):
            command = found.group(0).strip()
            if not live(command, leaves):
                return MISSING.format(command=command)
        return text

    return check


def live(command: str, leaves: dict[tuple[str, ...], set[str]]) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    words: list[str] = []
    for token in tokens[1:]:
        if token.startswith("-"):
            break
        words.append(token)
    path = next((tuple(words[:n]) for n in range(len(words), 0, -1) if tuple(words[:n]) in leaves), None)
    if path is None:
        return False
    options = {token.split("=", 1)[0] for token in tokens[1:] if token.startswith("--")}
    return options <= leaves[path] | ALWAYS


def rows_of_running_tree() -> list[dict] | None:
    """The command tree of the `sm` that runs, from the Click context; None outside a command."""
    from steuermaschine.helpdoc import tree

    try:  # Typer 0.29 carries its own Click
        from typer._click.globals import get_current_context
    except ImportError:  # pragma: no cover - a Typer that depends on Click
        from click import get_current_context

    context = get_current_context(silent=True)
    if context is None:
        return None
    return tree(context.find_root().command, PROG)
