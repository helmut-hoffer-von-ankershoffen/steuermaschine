"""Why one PR does not merge (`sm why PR`, in why_subject.py): one screen for "why is this PR not merging" (GitHub state, the required checks with older failed
duplicates, its queue entry, holds, main, its role in a combine PR) and the single most likely reason. Read-only.

`explain(world, number)` is the importable function; other areas build on it instead of reading the same things again.
"""

from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from steuermaschine.helpdoc import H
from steuermaschine.models import Model

from . import merge_tier
from .go import world
from .go_lib import legacy, probes
from .queue_lib import core_facts

REQUIRED = core_facts.CONTEXTS  # the required contexts of Core's release policy at main


class WhySettings(BaseModel):
    why_combine_members_glob: str | None = Field(None, description="Glob of the combine member lists (one file per combine PR, PR numbers inside); none: roles are unknown.")


class CheckState(Model):
    name: str
    latest: str  # SUCCESS, FAILURE, MISSING ...
    older_not_green: list[str] = []


class MainRun(Model):
    run: int | None = None
    head: str = ""
    attempt: int | None = None
    status: str = ""
    conclusion: str = ""

    def line(self) -> str:
        return f"main run {self.run} {self.head[:8]} attempt {self.attempt} {self.status} {self.conclusion}"


class PullRequestWhy(Model):
    """Why one PR does not merge."""

    number: int
    state: str = ""
    head: str = ""
    merge_state: str = ""
    title: str = ""
    checks: list[CheckState] = []
    queue: str = ""  # its queue entry ("queued ...", "needs work ..."), or "not queued"
    freeze: bool = False
    release_holds: list[str] = []
    main_run: MainRun | None = None  # main's latest CI run
    queue_waits_on: str = ""  # what the last queue tick waited on
    roles: list[str] = []
    roles_known: bool = False
    gate_notes: list[str] = []  # clauses of the merge gate's verdict that only inform (Core ADR 0149): never red
    advisory_failing: dict[str, list[str]] = {}  # advisory lane -> its failing names (consumers: the plugins)
    why: str = ""
    more: list[str] = Field([], description="`sm why PR` with a red check: the CI cause and, for a red merge gate, Sonar's verdict.")

    def render(self) -> list[str]:
        out = [f"PR {self.number} {self.state or None} {self.head[:7]} github={self.merge_state or None}  {self.title[:50]}"]
        out += [f"  {c.name}: {c.latest}" + (f"  DUPLICATE older non-success: {','.join(c.older_not_green)}" if c.older_not_green else "") for c in self.checks]
        out.append(f"  queue: {self.queue}")
        out.append(f"  holds: freeze={'ON' if self.freeze else 'off'} release-holds={self.release_holds or 'none'}")
        out.append(f"  {self.main_run.line() if self.main_run else 'main run: ?'}")
        out.append(f"  last tick waiting: {self.queue_waits_on}" if self.queue_waits_on else "  ")
        out += [f"  gate note (not red): {note}" for note in self.gate_notes]
        out.append("  role: " + (", ".join(self.roles) or ("standalone" if self.roles_known else "unknown (no combine member lists configured)")))
        out.append(f"  WHY: {self.why}")
        return out + self.more


def per_name(runs: list[dict[str, Any]], names: Any = REQUIRED) -> list[CheckState]:
    """Per required check: the latest conclusion and the older ones that are not success, from the check runs of one SHA."""
    out = []
    for name in names:
        mine = sorted((r for r in runs if r.get("name") == name), key=lambda r: r.get("completed_at") or "9")
        if not mine:
            out.append(CheckState(name=name, latest="MISSING"))
            continue
        last = mine[-1]
        out.append(CheckState(name=name, latest=(last.get("conclusion") or last.get("status") or "?").upper(),
                              older_not_green=[(r.get("conclusion") or r.get("status") or "?").upper() for r in mine[:-1] if r.get("conclusion") != "success"]))
    return out


def verdict(number: int, state: str, merge_state: str, checks: list[CheckState], item: dict[str, Any] | None, frozen: bool, holds: list[Any], main_ok: bool) -> str:
    """The single most likely reason, in the order a person would test it."""
    if state != "OPEN":
        return f"nothing: PR is {state}"
    bad = [c.name for c in checks if c.latest != "SUCCESS"]
    if bad:
        return f"required check not green: {', '.join(bad)}"
    dups = [c.name for c in checks if c.older_not_green]
    if merge_state == "BLOCKED" and dups:
        return (f"GitHub BLOCKED with all latest required checks green and an older failed duplicate on {', '.join(dups)}: duplicate is the only "
                "difference to a CLEAN PR; push a new head or re-run the whole run that owns it")
    if merge_state not in ("CLEAN", "HAS_HOOKS", "UNSTABLE"):
        return f"GitHub mergeStateStatus {merge_state}"
    if item is None:
        return f"not in the queue: sm queue add {number}"
    if item.get("status") == "needs work":
        return f"queue says needs work ({item.get('detail', '')[:60]}): re-ship with sm queue add (stale after a push or reopen)"
    if frozen or holds:
        return f"merge hold: {'freeze file' if frozen else ''} {' '.join(holds)}".strip()
    if not main_ok:
        return "main's latest run is not green for the queue"
    return "ready: waits for the next 5 minute batch"


def roles(pattern: str | None, number: int) -> tuple[list[str], bool]:
    """The combine PRs that list this PR as a member; (names, whether member lists are configured at all)."""
    if not pattern:
        return [], False
    out = []
    for name in sorted(glob.glob(os.path.expanduser(str(pattern)))):
        try:
            if re.search(rf"\b{number}\b", Path(name).read_text()):
                out.append(Path(name).name.replace("-members.txt", ""))
        except OSError:
            continue
    return out, True


def check_runs(w: probes.World, sha: str) -> list[dict[str, Any]]:
    res = w.gh.run(["api", f"repos/{w.repo}/commits/{sha}/check-runs", "--paginate", "-q", ".check_runs[]|{name,status,conclusion,completed_at,summary:.output.summary}"], timeout=60)
    out = []
    for line in (res.out.splitlines() if res.rc == 0 else []):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def gate_notes(runs: list[dict[str, Any]]) -> merge_tier.Verdict:
    """The informative clauses of the newest summary of a merge-gate check (the merge gate's verdict): reviewed workflow change,
    advisory lanes (consumers: N failing: a, b) and reruns. They never make a check red; its conclusion does."""
    floor = sorted((r for r in runs if r.get("name") in core_facts.API_CHECKS and r.get("summary")), key=lambda r: r.get("completed_at") or "9")
    return merge_tier.verdict(str(floor[-1].get("summary") or "") if floor else "")


def explain(w: probes.World, number: int) -> PullRequestWhy:
    pr, _ = w.gh.try_json(["pr", "view", str(number), "-R", w.repo, "--json", "state,mergeStateStatus,headRefOid,title"], timeout=30)
    pr = pr if isinstance(pr, dict) else {}
    sha = str(pr.get("headRefOid") or "")
    runs_of_head = check_runs(w, sha) if sha else []
    checks = per_name(runs_of_head)
    data = legacy.queue_state(w.state_dir)
    items = [i for i in data["items"] if str(i.get("key")) == str(number)]
    item = next((i for i in items if i.get("status") != "merged"), None) or (items[0] if items else None)
    holds, frozen = legacy.release_holds(w.state_dir), legacy.merge_frozen(w.state_dir)
    runs = probes.main_runs(w)
    main = MainRun(run=runs[0].get("databaseId"), head=str(runs[0].get("headSha") or ""), attempt=runs[0].get("attempt"), status=str(runs[0].get("status") or ""),
                   conclusion=str(runs[0].get("conclusion") or "")) if runs else None
    blocked = str(data.get("blocked") or "")
    main_ok = bool(main) and main.status == "completed" and main.conclusion == "success" and "not green" not in blocked
    found, known = roles(w.settings.get("why_combine_members_glob"), number)
    notes = gate_notes(runs_of_head)
    return PullRequestWhy(number=number, state=str(pr.get("state") or ""), head=sha, merge_state=str(pr.get("mergeStateStatus") or ""), title=str(pr.get("title") or ""),
                          checks=checks, queue=f"{item['status']} {str(item.get('detail', ''))[:50]}" if item else "not queued", freeze=frozen, release_holds=holds,
                          main_run=main, queue_waits_on=blocked, roles=found, roles_known=known, gate_notes=notes.info,
                          advisory_failing=notes.advisory_failing,
                          why=verdict(number, pr.get("state", "?"), pr.get("mergeStateStatus", "?"), checks, item, frozen, holds, main_ok))

