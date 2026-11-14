"""`sm beads refill`: ready beads against free builder slots. Read-only; the rules are refill_lib/slots.py."""
# mypy: disable-error-code="no-untyped-call"

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from steuermaschine.github import Gh
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime
from steuermaschine.runlock import sandbox_load

from .agents_lib import table
from .coord_lib.base import child_env, tracker_dir
from .refill_lib import slots as r


class RefillSettings(BaseModel):
    """Settings of the refill (mixin of the plugin's settings table)."""

    refill_target_slots: int = Field(r.TARGET, description="Builders that may work at once.")
    refill_max_load: float = Field(float(r.LOAD_MAX), description="At or above this 1-minute load no slot is free.")
    refill_stale_minutes: int = Field(90, description="An in-progress bead without a bead update or a pull request push for this long has a dead owner.")
    refill_test_load: float | None = Field(None, description="A fixed load for tests; ignored unless HOME is a sandbox.")


class Refill(Model):
    """Free builder slots, owners that stopped, and the beads to start next."""

    in_progress: int
    free_slots: int
    ready: int
    load: float
    dead: list[str] = []
    start: list[str] = []

    def render(self) -> list[str]:
        return [f"{self.in_progress} in progress, {self.free_slots} free slots, {self.ready} ready, load {self.load:.0f}", *self.dead, *self.start]


@native("beads refill", writes=False, replaces=("hz-refill",), help=H(
    "Ready beads against free builder slots: owners that stopped, and which bead to start next with which model.",
    "Use when a builder finished or stopped, and at every tick while slots are free.",
    "nothing (read-only: the issue tracker, open pull requests, the agent routing table, the load).",
    "sm beads refill",
    "sm beads brief TOPIC BRANCH --bead ID   (for each START line)",
    "Free slots = the target minus beads in progress; none at or above the load limit. DEAD: a bead in progress with\n"
    "no bead update and no pull request push for 90 minutes, with the agent id to resume. START: one line per ready\n"
    "bead that fills a slot: bead, priority, suggested model (Haiku chores and docs, Sonnet small UI, Opus install,\n"
    "update, sign-in and debugging, Fable design)."))
def beads_refill() -> Refill:
    rt: Any = runtime()
    s = rt.settings or {}
    commands, env, root = rt.providers.commands, child_env(s), str(tracker_dir(s))

    def bd(*args: str) -> list[dict[str, Any]]:
        out = commands.run(["bd", *args, "--json"], cwd=root, env=env, timeout=60).out
        try:
            found = json.loads(out[out.index("["):]) if "[" in out else []
        except ValueError:
            return []
        return found if isinstance(found, list) else []

    ready, prog = bd("ready", "--limit", "0"), bd("list", "--status", "in_progress", "--limit", "0")
    repo = str(s.get("repo") or "")
    prs, _why = Gh(commands, repo, clock=rt.providers.clock).try_json(["pr", "list", "-R", repo, "--state", "open", "--limit", "60", "--json", "headRefName,title,updatedAt"])
    upd: dict[str, str] = {}
    for p in prs if isinstance(prs, list) else []:
        b = r.bead_of(p)
        if b and p["updatedAt"] > upd.get(b, ""):
            upd[b] = p["updatedAt"]
    try:
        ids = r.agent_ids(table.table_file(s).read_text())
    except OSError:
        ids = {}
    load = (sandbox_load(s.get("refill_test_load")) or (lambda: os.getloadavg()[0]))()
    free = r.free_slots(prog, load, int(s.get("refill_target_slots") or r.TARGET), float(s.get("refill_max_load") or r.LOAD_MAX))
    now = datetime.fromtimestamp(rt.providers.clock.now(), UTC)
    return Refill(in_progress=len(prog), free_slots=free, ready=len(ready), load=load, dead=r.dead_owners(prog, upd, ids, now, int(s.get("refill_stale_minutes") or 90)),
                  start=r.start_lines(ready, prog, free))
