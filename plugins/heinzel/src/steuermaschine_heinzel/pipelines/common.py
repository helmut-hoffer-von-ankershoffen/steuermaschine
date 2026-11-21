"""What both pipelines share: run a native command as a step, turn its result into a
detail line, a `Refuse` or a `Wait`, and read the few facts the probes need."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from steuermaschine import USAGE, WAITING
from steuermaschine.engine import Ctx, Refuse, Wait
from steuermaschine.native import call, runtime

# Tests replace a native command's function here (path -> callable taking the same keyword arguments).
OVERRIDE: dict[str, Callable[..., Any]] = {}
SHA = re.compile(r"[0-9a-f]{40}")


def command(path: str) -> Any:
    """The registered native command at PATH. Looked up in the registry by name, not imported: the commands a
    pipeline composes are their own write units, so `sm self reviews` names a change to one of them as reaching that
    command, not the pipeline."""
    import importlib

    registry = importlib.import_module(__package__.rsplit(".", 1)[0] + ".native")
    found = next((c for c in registry.commands() if getattr(c, "path", None) == path), None)
    if found is None:
        raise Refuse(f"`sm {path}` is not a command of this build: the pipeline cannot run this step", "sm self tree")
    return found


def run(path: str, **kwargs: Any) -> Any:
    """Run `sm PATH` in process with these options and return its result model, whatever its exit code."""
    cmd = command(path)
    if path not in OVERRIDE:
        return call(cmd, **kwargs)
    return OVERRIDE[path](**kwargs)


def ok(path: str, **kwargs: Any) -> Any:
    """Run `sm PATH`; exit 0 returns the result, 75 is a wait, anything else a refusal with what it printed."""
    result = run(path, **kwargs)
    code = getattr(result, "exit_code", 0) or 0
    if code == 0:
        return result
    text = "; ".join(line for line in (result.render() if hasattr(result, "render") else []) if line.strip())[:600]
    if code == WAITING:
        raise Wait(f"sm {path}: {text or 'waiting'}", text or f"sm {path}")
    raise Refuse(f"sm {path} exit {code}: {text or 'no detail'}", code=code if code != USAGE else USAGE)


def need(ctx: Ctx, name: str, why: str) -> str:
    value = str(ctx.inputs.get(name) or "")
    if not value:
        raise Refuse(f"the input '{name}' is missing: {why}. Give it once with --set {name}=VALUE; it is remembered", code=USAGE)
    return value


def path_input(ctx: Ctx, name: str, why: str) -> Path:
    return Path(need(ctx, name, why)).expanduser()


def core_repo(value: Any, how: str) -> Path:
    """The Core repository a pipeline works on. Empty, unset or "." (what `Path("")` prints) refuses: a missing
    setting must never become the current directory."""
    text = str(value if value is not None else "").strip()
    path = Path(text).expanduser() if text else Path(".")
    if not text or str(path) == ".":
        raise Refuse(f"no Core repository: {how}", "sm self config", code=USAGE)
    return path


def packet_sha(path: Path) -> str:
    """The digest of a packet as admission binds it: sha256 of the bytes of packet.json (focused_slots.digest)."""
    import hashlib

    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise Refuse(f"cannot read the packet {path}: {exc}") from None


def setting(name: str, default: Any = None) -> Any:
    value = runtime().settings.get(name)
    return default if value in (None, "") else value


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "head"


def lines(result: Any) -> str:
    return "; ".join(x for x in (result.render() if hasattr(result, "render") else []) if x.strip())[:300]
