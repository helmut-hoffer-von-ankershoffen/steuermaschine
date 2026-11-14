"""Deferring PR CI: cancel queued and in-progress CI runs of PR branches to free the hosted runners, record each one
for its later re-run.

The former shell script, statement by statement, in Python. What it never touches is unchanged:
- only an ALLOWLIST is cancelled: PR-event runs of the workflows in `ALLOWED` (and `Merge gate` runs triggered by
  `workflow_run` on the head branch of an open PR);
- never main, release/*, bump/*, a ref (refs/...), a branch named like a tag (v<digit>...), a workflow_dispatch,
  schedule or push run, or a run younger than two minutes;
- a run with a started macOS job is spared (its minutes are being spent), unless the release caller forces it;
- a run whose jobs cannot be read is spared;
- the PRs to keep are checked first: an unknown or non-open PR refuses before anything is cancelled.

Every decision is one row; every attempt of a live deferral is one line in the audit log; every cancelled run is
recorded at once in the deferred list.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from steuermaschine.github import Gh, GitHub
from steuermaschine.protocols import Clock, Commands, Store

from . import deferred

ALLOWED = ("CI", "CodeQL", "Claude review", "Merge gate", "Docs")
PR_EVENTS = ("pull_request", "pull_request_target", "pull_request_review")
MIN_AGE_SECONDS = 120
NOT_STARTED = ("queued", "waiting", "pending")
RUN_FIELDS = "databaseId,headBranch,workflowName,event,createdAt"
TAG_LIKE = re.compile(r"v[0-9]")


class Stop(Exception):
    """Refuse before anything is cancelled. `code` is the exit code (2: the command line or a PR to keep is wrong)."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.message, self.code = message, code


@dataclass
class Edge:
    """How a deferral reaches the world, for one command."""

    commands: Commands
    clock: Clock
    store: Store
    repo: str
    legacy_file: Path | None = None
    caller: Callable[[], str] = lambda: ""
    command: str = "sm ci defer"
    gh: Gh = field(init=False)
    hub: GitHub = field(init=False)

    def __post_init__(self) -> None:
        self.gh = Gh(self.commands, repo=self.repo, clock=self.clock)
        self.hub = GitHub(self.commands, repo=self.repo, clock=self.clock)


def epoch(created: str) -> float | None:
    try:
        return datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except (TypeError, ValueError):
        return None


def eligible(run: dict[str, Any], now: float) -> bool:
    """The allowlist: whether a run may be cancelled at all. Pure."""
    name, event, branch = run.get("workflowName"), run.get("event"), run.get("headBranch")
    if name not in ALLOWED or not isinstance(branch, str) or not branch:
        return False
    if not (event in PR_EVENTS or (name == "Merge gate" and event == "workflow_run")):
        return False
    if branch == "main" or branch.startswith(("release/", "bump/", "refs/")) or TAG_LIKE.match(branch):
        return False
    created = epoch(run.get("createdAt") or "")
    return created is not None and now - created >= MIN_AGE_SECONDS


def macos_started(jobs: list[dict[str, Any]]) -> int:
    """macOS jobs of a run that are past the queue: running or finished. Pure."""
    return sum(1 for job in jobs if "macos" in ",".join(job.get("labels") or []) and job.get("status") not in NOT_STARTED)


def keep_branches(edge: Edge, keep: str) -> list[str]:
    """The head branches of the PRs to keep. Refuses (exit 2) a PR that is not a number, unknown or not open."""
    branches = []
    for number in keep.replace(",", " ").split():
        if not number.isdigit():
            raise Stop(f"refusing: '{number}' is not a PR number", 2)
        info, why = edge.gh.try_json(["pr", "view", number, "-R", edge.repo, "--json", "state,headRefName"])
        if why or not isinstance(info, dict) or not info.get("state"):
            raise Stop(f"refusing: PR #{number} is unknown", 2)
        if info["state"] != "OPEN":
            raise Stop(f"refusing: PR #{number} is {info['state']}, not open", 2)
        branches.append(info.get("headRefName") or "")
    return branches


def main_queued_minutes(edge: Edge) -> int | None:
    """Minutes the oldest queued main run has waited; None when no main run is queued."""
    rows, why = edge.gh.try_json(["run", "list", "-R", edge.repo, "--status", "queued", "--branch", "main", "--limit", "50", "--json", "createdAt"])
    if why:
        raise Stop(f"cannot read the queued main runs: {why}")
    times = [t for t in (epoch(row.get("createdAt") or "") for row in rows or []) if t is not None]
    return int((edge.clock.now() - min(times)) // 60) if times else None


def candidates(edge: Edge) -> tuple[list[dict[str, Any]], list[str]]:
    """(the allowlisted active runs, queued first; the head branches of the open PRs)."""
    listed, _ = edge.gh.try_json(["pr", "list", "-R", edge.repo, "--state", "open", "--limit", "200", "--json", "headRefName"])
    open_branches = [row.get("headRefName") or "" for row in listed or []]
    found = []
    for status in ("queued", "in_progress"):
        rows, why = edge.gh.try_json(["run", "list", "-R", edge.repo, "--status", status, "--limit", "100", "--json", RUN_FIELDS])
        if why:
            raise Stop(f"cannot read the {status} runs: {why}")
        found += [row for row in rows or [] if eligible(row, edge.clock.now())]
    return found, open_branches


def cancel(edge: Edge, kept: list[str], force: bool, live: bool, mode: str) -> list[dict[str, Any]]:
    """Decide every allowlisted active run and, when `live`, cancel the ones left. One row per run:
    {"run", "branch", "workflow", "event", "action", "reason", "would_run"}; action is cancelled | would_cancel |
    kept | spared | failed."""
    runs, open_branches = candidates(edge)
    rows = []

    def row(run: dict[str, Any], action: str, reason: str = "", would_run: str = "") -> None:
        rows.append({"run": str(run["databaseId"]), "branch": run["headBranch"], "workflow": run["workflowName"], "event": run["event"],
                     "action": action, "reason": reason, "would_run": would_run})

    def note(line: str) -> None:
        if live:
            deferred.audit(edge.store, edge.clock, line)

    for run in runs:
        rid, branch, name, event = str(run["databaseId"]), run["headBranch"], run["workflowName"], run["event"]
        if event == "workflow_run" and branch not in open_branches:
            row(run, "spared", "workflow_run, not an open PR branch")
            continue
        if kept and branch in kept:
            row(run, "kept")
            continue
        # never cancel a run whose macOS minutes are already being spent; only runs with no started macOS job are deferred
        started = 0
        if not force:
            data, why = edge.gh.api(f"actions/runs/{rid}/jobs", {"per_page": 100})
            if why or not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
                note(f"spared id={rid} branch={branch} workflow={name} reason=jobs-unknown")
                row(run, "spared", "jobs unknown")
                continue
            started = macos_started(data["jobs"])
        if force:
            note(f"forced id={rid} branch={branch} workflow={name} reason=release-candidate")
        if started > 0:
            note(f"spared id={rid} branch={branch} workflow={name} reason=macos-started n={started}")
            row(run, "spared", f"macOS started: {started} job(s), let it finish")
            continue
        if live:
            note(f"attempt id={rid} workflow={name} event={event} branch={branch} caller={edge.caller()[:160]} self={edge.command} mode={mode}")
        done = edge.hub.cancel_run(rid, live=live)
        if done.state == "cancelled":
            deferred.add(edge.store, edge.legacy_file, rid, branch, name)
            note(f"cancelled id={rid}")
            row(run, "cancelled", would_run=done.would_run)
        elif done.state == "dry_run":
            row(run, "would_cancel", would_run=done.would_run)
        else:
            note(f"FAILED id={rid}")
            row(run, "failed", done.state.removeprefix("unknown: ").replace("_", " "), done.would_run)
    return rows


def line(row: dict[str, Any]) -> str:
    """One row as the line a person reads."""
    what = f"{row['run']} {row['branch']} {row['workflow']}"
    if row["action"] == "failed":
        return f"FAILED cancel {what}" + (f" ({row['reason']})" if row["reason"] else "")
    word = {"would_cancel": "would cancel"}.get(row["action"], row["action"])
    return f"{word} {what}" + (f" ({row['reason']})" if row["reason"] else "")


MAIN_KEEP = 2  # the newest queued main runs that stay: main's tip gates, an older queued run only repeats an ancestor
MAIN_WORKFLOW = "CI"


def supersede_main(edge: Edge, live: bool) -> list[dict[str, Any]]:
    """When more than MAIN_KEEP push runs of main CI are queued, cancel the older ones that never started, keeping the
    newest MAIN_KEEP. Never a run in progress, never another branch (release/* included), never another workflow or
    event. Each run is read again right before its cancel: one that started meanwhile is spared. A superseded run is
    not recorded for a re-run (the newer runs cover its commits); every attempt is in the audit log. One row per run."""
    found, why = edge.gh.try_json(["run", "list", "-R", edge.repo, "--branch", "main", "--status", "queued", "--limit", "50", "--json", RUN_FIELDS])
    if why:
        raise Stop(f"cannot read the queued main runs: {why}")
    queued = [r for r in found or [] if r.get("headBranch") == "main" and r.get("event") == "push" and r.get("workflowName") == MAIN_WORKFLOW
              and epoch(r.get("createdAt") or "") is not None]
    queued.sort(key=lambda r: epoch(r["createdAt"]) or 0.0, reverse=True)  # newest first
    rows: list[dict[str, Any]] = []

    def row(run: dict[str, Any], action: str, reason: str = "", would_run: str = "") -> None:
        rows.append({"run": str(run["databaseId"]), "branch": "main", "workflow": run["workflowName"], "event": "push",
                     "action": action, "reason": reason, "would_run": would_run})

    def note(text: str) -> None:
        if live:
            deferred.audit(edge.store, edge.clock, text)

    for run in queued[:MAIN_KEEP]:
        row(run, "kept", "one of the newest queued main runs")
    for run in queued[MAIN_KEEP:]:
        rid = str(run["databaseId"])
        status = edge.hub.run(rid).status
        if status not in NOT_STARTED:
            note(f"spared id={rid} branch=main workflow={MAIN_WORKFLOW} reason=started status={status}")
            row(run, "spared", f"no longer queued ({status})")
            continue
        if live:
            note(f"attempt id={rid} workflow={MAIN_WORKFLOW} event=push branch=main caller={edge.caller()[:160]} self={edge.command} mode=--supersede-main")
        done = edge.hub.cancel_run(rid, live=live)
        if done.state == "cancelled":
            note(f"superseded id={rid}")
            row(run, "cancelled", "superseded by a newer queued main run", done.would_run)
        elif done.state == "dry_run":
            row(run, "would_cancel", "superseded by a newer queued main run", done.would_run)
        else:
            note(f"FAILED id={rid}")
            row(run, "failed", done.state.removeprefix("unknown: ").replace("_", " "), done.would_run)
    return rows
