"""Core's split main CI: Linux push runs per SHA and the dispatched macOS lane ("CI macOS main SHA", group ci-main-macos).

Until Core splits main, every run here is a push run that carries its own macOS cells; every reader below then returns what
it always did. Once split, a push run proves the Linux side of X, and a green macOS-lane run at Y proves the macOS side for X
when Y is X or a descendant of X. A cancelled macOS-lane run is a pending run the group replaced: it is no evidence either way.
"""
from __future__ import annotations

import re

MACOS_TITLE = "CI macOS main"
DISPATCH_JOB = "Dispatch the coalesced macOS main run"  # the push run's job that queues the lane: its presence means a split main
LANE_CONCLUSIONS = {"success", "failure", "timed_out", "startup_failure"}
SHA = re.compile(r"[0-9a-f]{40}")


def is_macos_lane(row) -> bool:
    """A run of the dispatched macOS lane, by its run name (the title) and, when known, its event."""
    if not isinstance(row, dict) or not str(row.get("displayTitle") or row.get("display_title") or "").startswith(MACOS_TITLE):
        return False
    return row.get("event", "workflow_dispatch") == "workflow_dispatch"


def split_runs(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(push runs, macOS-lane runs), newest first as given. Cancelled lane runs are dropped: GitHub cancels an older pending
    one when a newer is queued, so it says nothing about the commit."""
    push = [r for r in rows if not is_macos_lane(r)]
    lane = [r for r in rows if is_macos_lane(r) and r.get("conclusion") != "cancelled"]
    return push, lane


def is_split_push(jobs: list[dict] | None) -> bool:
    """True when a push run's jobs include the lane dispatcher: its macOS cells run in the lane, not in this run."""
    return any(isinstance(j, dict) and j.get("name") == DISPATCH_JOB for j in jobs or [])


def is_macos_cell(name: str) -> bool:
    return "macos" in (name or "").lower()


def required_names(jobs: list[dict] | None, names: set[str]) -> set[str]:
    """The names a full pass of this run must contain: macOS cells are dropped for a split push run."""
    if is_split_push(jobs):
        return {n for n in names if not is_macos_cell(n)}
    return set(names)


def lane_pass(jobs: list[dict] | None, aggregate: str) -> bool:
    """A macOS-lane run passed: it has jobs, all completed green, and its aggregate succeeded."""
    if not jobs or any(j.get("status") != "completed" or j.get("conclusion") not in ("success", "skipped", "neutral") for j in jobs):
        return False
    return any(j.get("name") == aggregate and j.get("conclusion") == "success" for j in jobs)


def covers(x: str, y: str, compare) -> bool:
    """Y is X or a descendant of X; `compare(x, y)` is GitHub's compare status ("identical", "ahead", ...)."""
    if not (isinstance(x, str) and isinstance(y, str) and SHA.fullmatch(x) and SHA.fullmatch(y)):
        return False
    return x == y or compare(x, y) in ("ahead", "identical")


def macos_proof(x: str, lane_runs: list[dict], compare) -> dict | None:
    """The newest completed, green macOS-lane run at X or a descendant of X; None when there is none."""
    for run in lane_runs:
        if run.get("status") == "completed" and run.get("conclusion") == "success" and covers(x, run.get("headSha"), compare):
            return run
    return None
