"""State files of the scripted era that the guards adopt READ-ONLY, one function per file. Nothing here writes.

The former tools kept: the load guard's paused trees (stopped.json) and the governor's state (hz-governor.json under
the legacy state directory). While an old job still runs beside the new one, its records are honoured: a tree it
paused is still resumed.
"""

from __future__ import annotations

import json
from pathlib import Path


def _json_or(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def loadguard_stopped(directory: Path) -> list[dict]:
    """`<directory>/stopped.json`: the process trees the former load guard paused. Unreadable: none."""
    data = _json_or(directory / "stopped.json", [])
    return [e for e in data if isinstance(e, dict) and {"pid", "cmd", "pids", "at"} <= set(e)] if isinstance(data, list) else []


def governor_state(directory: Path) -> dict:
    """`<directory>/hz-governor.json`: pids the former governor stopped. Unreadable: nothing stopped."""
    data = _json_or(directory / "governor.json", None)
    if isinstance(data, dict) and isinstance(data.get("stopped"), dict):
        return {"stopped": data["stopped"], "last_resume": data.get("last_resume", 0)}
    return {"stopped": {}, "last_resume": 0}

