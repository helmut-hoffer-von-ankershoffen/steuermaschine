"""One rule for list options: `--issue A,B` and `--issue A --issue B` mean the same, for every command of every layer.

`install(root)` gives every option that takes several values (`multiple`) the one shared parser `split`, before its
values are converted (so `--pr 1,2` works for a list of numbers too). An option whose values may contain a comma
(`--set NAME=VALUE`) is left alone: its metavar names a `=`. `findings(root)` is the lint: an option that takes one
value but says it is comma-separated must become a list option instead, so no command splits on its own.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

COMMA_HELP = re.compile(r"(?i)\bcomma[- ]separated\b")


def split(values: Iterable[Any]) -> list[Any]:
    """`["A,B", "C"]` -> `["A", "B", "C"]`; empty pieces are dropped; values that are not text pass unchanged."""
    out: list[Any] = []
    for value in values:
        if isinstance(value, str):
            out += [piece.strip() for piece in value.split(",") if piece.strip()]
        else:
            out.append(value)
    return out


def splits(param: Any) -> bool:
    """Whether the shared parser applies: a list option whose values are not NAME=VALUE pairs."""
    return bool(getattr(param, "multiple", False)) and "=" not in str(getattr(param, "metavar", "") or "")


def _commands(root: Any) -> Iterable[Any]:
    yield root
    for child in getattr(root, "commands", {}).values():
        yield from _commands(child)


def install(root: Any) -> None:
    done: set[int] = set()
    for command in _commands(root):
        for param in getattr(command, "params", []):
            if id(param) in done or not splits(param):
                continue
            done.add(id(param))
            process = param.process_value

            def process_value(ctx: Any, value: Any, process: Any = process) -> Any:
                if isinstance(value, list | tuple):
                    value = split(value)
                return process(ctx, value)

            param.process_value = process_value
            if param.help and "A,B" not in param.help:
                param.help = f"{param.help.rstrip()} Repeat it, or give A,B."


def findings(root: Any, prog: str = "sm") -> list[str]:
    """Options that take one value and split it themselves: they must be list options (the one parser)."""
    out = []
    for path, command in _paths(root, prog):
        for param in getattr(command, "params", []):
            if not getattr(param, "multiple", False) and COMMA_HELP.search(getattr(param, "help", "") or ""):
                out.append(f"{path}: {max(param.opts, key=len)} says comma-separated: make it a list option (repeat it, or give A,B)")
    return out


def _paths(node: Any, path: str) -> Iterable[tuple[str, Any]]:
    yield path, node
    for name, child in sorted(getattr(node, "commands", {}).items()):
        yield from _paths(child, f"{path} {name}")
