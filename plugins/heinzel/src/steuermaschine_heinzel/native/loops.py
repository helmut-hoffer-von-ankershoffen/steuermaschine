"""mini loops: which long-lived management loops under launchd run an older sm release than the installed one. Read-only.

A loop imports its code once. The former check compared the process start time with the mtimes of the old scripts;
the loops now run sm, so the judge is the release: the stable launcher starts `<install>/releases/<id>/venv/...`
and the install's pointer (`current`) names the installed release. A loop that runs another release is stale.

The core already ends a loop when the pointer moves (native.py: the loop compares the pointer after every tick), so
launchd starts it again on the installed release. A loop that stays stale is stuck inside a tick; one that runs no
sm release at all still runs an old program and needs its launchd job changed. Neither is fixed by a restart from
here: this command never restarts anything (the former `--apply` is retired, see parity/mini-loops.md).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from steuermaschine.config import environment
from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime

RELEASE = re.compile(r"(\S*)/releases/([^/\s]+)/venv/")
LIVE_SERVICE = ".heinzel."  # the live product's labels (daemon, web): never judged here


class LoopsSettings(BaseModel):
    """Settings of `sm mini loops` (mixin of the plugin's settings table)."""

    mini_loops_labels: list[str] = Field([], description="The launchd labels of the management loops to judge (queue, train, guards); "
                                                         "labels of the live service are ignored. Empty: the command refuses.")
    mini_loops_release_pointer: Path | None = Field(None, description="The stable install's pointer to the installed release (its `current`); "
                                                                      "none: the pointer the stable launcher set for this process.")


class Loop(Model):
    label: str
    state: str  # fresh | stale | foreign | not running | not loaded | unreadable
    pid: int | None = None
    started: str = ""  # as ps prints it
    release: str = ""  # the release id the process runs
    detail: str = ""


class Loops(Model):
    """Per management loop: the release it runs against the installed one."""

    installed: str  # the installed release id
    loops: list[Loop]
    skipped: list[str] = []  # labels of the live service, never judged
    read_only: bool = True

    @property
    def exit_code(self) -> int:
        return 1 if any(row.state in ("stale", "foreign", "unreadable") for row in self.loops) else 0

    def render(self) -> list[str]:
        out = [f"{row.label.rsplit('.', 1)[-1]:14} {row.state:12} {row.detail}" for row in self.loops]
        out += [f"{label.rsplit('.', 1)[-1]:14} skipped      the live service's label, never judged" for label in self.skipped]
        if any(row.state == "stale" for row in self.loops):
            out.append("STALE: a loop runs an older release. It ends after its running tick and launchd starts it on the installed one;"
                       " one that stays stale is stuck in a tick: read its log before anyone restarts it (a restart may cut a merge).")
        if any(row.state == "foreign" for row in self.loops):
            out.append("FOREIGN: a loop runs no sm release (an old program): its launchd job must change; tell the integrator.")
        return out


def setting(name: str) -> Any:
    rt: Any = runtime()
    value = (rt.settings or {}).get(name)
    return LoopsSettings.model_fields[name].default if value is None else value


def installed_release() -> Path:
    configured = setting("mini_loops_release_pointer")
    pointer = Path(str(configured)).expanduser() if configured else None
    if pointer is None:
        launched = environment().steuermaschine_release
        pointer = Path(launched) if launched else None
    if pointer is None:
        raise Refuse("no installed release is known: run the stable sm, or set mini_loops_release_pointer", "sm self config", code=2)
    if not pointer.exists():
        raise Refuse("the release pointer names no release", "sm self config", code=1)
    return pointer.resolve()


def launchd_pids(listing: str) -> dict[str, int | None]:
    """`launchctl list` rows: label -> pid, None for a loaded job without a process."""
    out: dict[str, int | None] = {}
    for line in listing.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            out[parts[2]] = int(parts[0]) if parts[0].isdigit() else None
    return out


def judge(label: str, pid: int, ps_out: str, installed: Path) -> Loop:
    """The verdict on one running loop from `ps -o lstart=,command=`."""
    words = ps_out.split()
    if len(words) < 6:
        return Loop(label=label, state="unreadable", pid=pid, detail="its process could not be read")
    started, command = " ".join(words[:5]), " ".join(words[5:])
    found = RELEASE.search(command)
    if not found:
        return Loop(label=label, state="foreign", pid=pid, started=started, detail=f"process started {started} runs no sm release")
    runs = Path(f"{found.group(1)}/releases/{found.group(2)}")
    resolved = runs.resolve() if runs.exists() else runs
    if resolved == installed:
        return Loop(label=label, state="fresh", pid=pid, started=started, release=found.group(2), detail=f"process started {started} runs the installed release {found.group(2)}")
    return Loop(label=label, state="stale", pid=pid, started=started, release=found.group(2),
                detail=f"process started {started} runs release {found.group(2)}, installed is {installed.name}")


@native("mini loops", writes=False, replaces=("hz-reload-loops",), help=H(
    "Which management loops under launchd run an older sm release than the installed one.",
    "Use after a promote, or when the queue or the train seems not to pick up a fix.",
    "nothing (read-only: `launchctl list` and `ps`; it never restarts or signals a job).",
    "sm mini loops",
    "a STALE loop that stays stale: read its log; a FOREIGN one: its launchd job must change (the integrator).",
    "Per label of the setting mini_loops_labels: fresh (runs the installed release), stale (another release), foreign\n"
    "(no sm release: an old program), not running (an interval job between runs), not loaded. Exit 1 when one is stale,\n"
    "foreign or unreadable. Labels of the live service are never judged."))
def mini_loops() -> Loops:
    labels = list(setting("mini_loops_labels") or [])
    if not labels:
        raise Refuse("the setting mini_loops_labels is not set: which launchd jobs are the management loops", "sm self config", code=2)
    installed = installed_release()
    commands = runtime().providers.commands
    listing = commands.run(["launchctl", "list"], timeout=30)
    if listing.rc != 0:
        raise Refuse("launchctl list failed: nothing judged", "sm mini loops", code=1)
    pids = launchd_pids(listing.out)
    rows, skipped = [], []
    for label in labels:
        if LIVE_SERVICE in label:
            skipped.append(label)
            continue
        if label not in pids:
            rows.append(Loop(label=label, state="not loaded", detail="no launchd job of this label"))
            continue
        pid = pids[label]
        if pid is None:
            rows.append(Loop(label=label, state="not running", detail="no process (interval job or stopped)"))
            continue
        ps = commands.run(["ps", "-o", "lstart=,command=", "-p", str(pid)], timeout=30)
        rows.append(judge(label, pid, ps.out if ps.rc == 0 else "", installed))
    return Loops(installed=installed.name, loops=rows, skipped=skipped)
