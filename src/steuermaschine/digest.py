"""A digest: the text of several read-only native commands in one result, for a status page or an hourly update.

    facts = digest.collect(commands, ["queue list", "doc asks"])      # commands: what a plugin's `sm_native` returns

Each path is looked up among the native commands and its function is called with the defaults of its options, inside
the runtime of the calling command. A path that is not a command (not ported, misspelt) is reported as `missing`; a
command registered `writes=True`, or one that needs an argument, is never called (`skipped`); one that refuses or
fails is reported with its message. A digest therefore cannot write and cannot stop because one source is down.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .engine import Refuse, Wait

REQUIRED = (inspect.Parameter.empty, ...)


@dataclass
class Fact:
    path: str
    status: str  # ok | missing | skipped | refused | failed
    lines: list[str] = field(default_factory=list)
    exit_code: int = 0


def defaults(fn: Any) -> dict[str, Any] | None:
    """The keyword arguments a command function gets when nothing is typed; None when it needs an argument."""
    out = {}
    for name, parameter in inspect.signature(fn).parameters.items():
        value = parameter.default
        if value.__class__.__name__ in ("OptionInfo", "ArgumentInfo"):  # Typer's declarations carry the default
            value = value.default
        if any(value is required for required in REQUIRED):
            return None
        out[name] = value
    return out


def collect(commands: Sequence[Any], paths: Sequence[str]) -> list[Fact]:
    by_path = {getattr(c, "path", ""): c for c in commands if hasattr(c, "fn")}
    facts = []
    for path in paths:
        cmd = by_path.get(" ".join(path.split()))
        if cmd is None:
            facts.append(Fact(path, "missing", ["not available in this version"]))
            continue
        kwargs = None if cmd.writes else defaults(cmd.fn)
        if kwargs is None:
            facts.append(Fact(path, "skipped", ["a digest runs read-only commands without arguments only"]))
            continue
        try:
            result = cmd.fn(**kwargs)
        except Refuse as no:
            facts.append(Fact(path, "refused", [no.message], no.code))
        except Wait as wait:
            facts.append(Fact(path, "refused", [f"waiting: {wait.message}"], 75))
        except Exception as exc:
            facts.append(Fact(path, "failed", [f"{type(exc).__name__}: {exc}"], 70))
        else:
            many = result if isinstance(result, list) else [result]
            facts.append(Fact(path, "ok", [line for r in many if r is not None for line in r.render()], max((getattr(r, "exit_code", 0) or 0 for r in many), default=0)))
    return facts
