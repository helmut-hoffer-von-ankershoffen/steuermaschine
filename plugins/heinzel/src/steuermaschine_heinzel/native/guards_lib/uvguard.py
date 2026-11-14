"""The uv guard: refuses `uv tool install|upgrade|uninstall|reinstall` for heinzel or heinzel-* (and `upgrade --all`)
unless UV_TOOL_DIR and UV_TOOL_BIN_DIR are both set outside the live directories or the live opt-in is set. Everything
else (including `uv tool run`) passes through to the real uv.

Moved from the former PATH shim with its decision unchanged (the bash became Python line by line). Isolation is judged
on the literal UV_TOOL_DIR / UV_TOOL_BIN_DIR strings (no symlink resolution; a HOME-only isolation is refused, which is
safe). The decision is pure on (args, environment, home); passing through and recording the opt-in is the edge's.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

ALLOW_ENV = "HZ_ALLOW_LIVE_UV"
FLAGS_WITH_VALUE = {"--directory", "--project", "--config-file", "--cache-dir", "--color", "--python-preference", "--python-fetch",
                    "--allow-insecure-host", "--python", "-p", "--env-file"}
TOOL_COMMANDS = {"install", "upgrade", "uninstall", "reinstall"}
NAMED = re.compile(r'^name *= *"heinzel', re.I | re.M)


def tool_command(args: Sequence[str]) -> tuple[str, list[str]] | None:
    """('install', the arguments after it) when the arguments are `[global flags] tool install|upgrade|uninstall|reinstall ...`."""
    n, i = len(args), 0
    while i < n and args[i].startswith("-"):
        i += 2 if args[i] in FLAGS_WITH_VALUE else 1
    if i < n and args[i] == "tool" and i + 1 < n and args[i + 1] in TOOL_COMMANDS:
        return args[i + 1], list(args[i + 2:])
    return None


def names_heinzel(rest: Sequence[str], live_tools: Path) -> bool:
    for a in rest:
        low = a.lower()
        if "heinzel" in low:  # names, `heinzel @ git+...`, URLs, wheels, paths, --from=heinzel
            return True
        if low in ("--all", "-a"):
            if (live_tools / "heinzel").is_dir():
                return True
        elif low.startswith("-"):
            continue
        elif Path(a).is_dir():  # a local source dir: refuse when its pyproject names heinzel*
            try:
                if NAMED.search((Path(a) / "pyproject.toml").read_text(errors="ignore")):
                    return True
            except OSError:
                pass
    return False


def outside(value: str, live_tools: Path, live_bin: Path) -> bool:
    value = value[:-1] if value.endswith("/") else value
    if not value:
        return False
    return not any(value == live or value.startswith(live + "/") for live in (str(live_tools), str(live_bin)))


def decide(args: Sequence[str], environ: Mapping[str, str], home: Path) -> str:
    """'pass' (not a live heinzel tool change), 'isolated' (both tool directories outside the live ones), 'allowed' (the live
    opt-in; the edge records it) or 'refused'."""
    found = tool_command(args)
    if not found:
        return "pass"
    live_tools, live_bin = home / ".local/share/uv/tools", home / ".local/bin"
    if not names_heinzel(found[1], live_tools):
        return "pass"
    if outside(environ.get("UV_TOOL_DIR", ""), live_tools, live_bin) and outside(environ.get("UV_TOOL_BIN_DIR", ""), live_tools, live_bin):
        return "isolated"
    if environ.get(ALLOW_ENV) == "1":
        return "allowed"
    return "refused"


def refusal(args: Sequence[str]) -> str:
    return (f"REFUSED 'uv {' '.join(args)}' (bead ppww): it would recreate the LIVE heinzel tool venv (web 500, plugins lost on 2026-11-08).\n"
            "  Isolate: UV_TOOL_DIR=$(mktemp -d) UV_TOOL_BIN_DIR=$(mktemp -d) uv tool ...   (both outside ~/.local/share/uv/tools and ~/.local/bin)\n"
            "  Live update is the owner's job: HZ_ALLOW_LIVE_UV=1 (the use is recorded in the guard state).")
