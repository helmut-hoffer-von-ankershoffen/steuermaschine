"""mini loadguard: which ungated test trees the load guard has paused. Read-only.

The guard itself (a launchd job that pauses and continues process trees) is not this command: this only reads the
state file the guard writes, and the load.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime

STOP_LOAD, RESUME_LOAD = 100.0, 40.0  # the guard pauses above the first and continues below the second


class LoadguardSettings(BaseModel):
    """Settings of `sm mini loadguard` (mixin of the plugin's settings table)."""

    loadguard_state_file: Path | None = Field(default=None, description="What the load guard paused (its stopped.json); written by the guard, read here.")


class PausedTree(Model):
    pid: int
    paused_seconds: float
    processes: int
    command: str


class Loadguard(Model):
    """What the load guard has paused, and the load it acts on."""

    paused: list[PausedTree] = []
    load_1m: float = 0.0
    stop_above: float = STOP_LOAD
    resume_below: float = RESUME_LOAD
    read_only: bool = True

    def render(self) -> list[str]:
        return [f"paused {p.pid} for {p.paused_seconds:.0f}s: {p.command[:160]}" for p in self.paused] or ["nothing paused"]


def read_state(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict) and "pid" in e] if isinstance(data, list) else []


@native("mini loadguard", writes=False, replaces=("hz-loadguard",), help=H(
    "Which ungated test process trees the load guard has paused on this machine, and for how long.",
    "Use when a local test run seems stuck, or after a load alarm, to see whether the guard paused it.",
    "nothing (read-only: the guard's state file and the load average; it never signals a process).",
    "sm mini loadguard", "sm gate status (heavy runs belong behind the run lock, where the guard never pauses them)",
    f"The guard pauses the newest ungated test tree above load {STOP_LOAD:.0f} and continues the earliest below {RESUME_LOAD:.0f}."))
def loadguard() -> Loadguard:
    rt = runtime()
    configured = (rt.settings or {}).get("loadguard_state_file")
    if not configured:
        raise Refuse("the setting loadguard_state_file is not set: where the guard records what it paused", "sm self config")
    path = Path(str(configured)).expanduser()
    now = rt.providers.clock.now()
    rows = [PausedTree(pid=int(e["pid"]), paused_seconds=max(now - float(e.get("at") or now), 0.0), processes=len(e.get("pids") or []), command=str(e.get("cmd") or ""))
            for e in read_state(path)]
    return Loadguard(paused=rows, load_1m=round(os.getloadavg()[0], 2))
