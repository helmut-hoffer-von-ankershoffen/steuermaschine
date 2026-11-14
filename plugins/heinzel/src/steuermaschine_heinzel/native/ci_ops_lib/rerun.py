"""Re-running deferred PR CI: after main CI is running, re-run the CI that a deferral cancelled, a few PRs at a time.

Reads the deferred list. Order: queued-for-merge PRs first, then oldest. Adaptive cap: as many PRs as fit the free hosted capacity (queued+in_progress jobs vs ~14 of the 20-job limit,
~4 jobs per PR); `n` is an explicit cap on PRs in flight. Never while a release/* run is active or a main run is still queued, skips PRs with real failures, drafts and closed PRs.
Re-run runs are removed from the list; the result goes to the journal. Run it from the hourly tick until the list is empty.

`batch` is the body of the former batch tool, `resume_all` the former `--resume` of the deferral script; the plan itself is
`rerun_plan.py` (pure, moved unchanged). What changed is the edge: GitHub through the one `Gh` client (a re-run is a
write and is never retried), the list in the store, the jobs of a run read once per call instead of once per question.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from steuermaschine.github import Gh

from . import deferred
from .rerun_plan import count_jobs, count_macos_busy, parse_deferred, plan, rerun_argv


class Stop(Exception):
    """Refuse before anything is re-run."""


@dataclass
class Edge:
    """How a re-run reaches the world, for one command."""

    commands: object
    clock: object
    store: object
    repo: str
    legacy_file: Path | None = None
    queue_items: Callable[[], list] = lambda: []
    out: Callable[[str], None] = lambda text: None
    journal: Callable[[str], None] = lambda text: None
    gh: Gh = field(init=False)
    result: dict = field(default_factory=dict)  # what the call found and did, for the result model

    def __post_init__(self) -> None:
        self.gh = Gh(self.commands, repo=self.repo, clock=self.clock)


def batch(edge: Edge, n: int | None, dry_run: bool) -> None:
    R = edge.repo
    memo: dict = {}

    def gh(*a):
        return edge.gh.json(list(a)) or []

    def jobs(rid):
        """The jobs of one run, read once per call; a failed read fails again for every question asked of it."""
        if rid not in memo:
            try:
                memo[rid] = gh("api", f"repos/{R}/actions/runs/{rid}/jobs?per_page=100")["jobs"]
            except Exception as error:
                memo[rid] = error
        if isinstance(memo[rid], Exception):
            raise memo[rid]
        return memo[rid]

    rows = deferred.runs(edge.store, edge.legacy_file)
    text = deferred.text(rows)
    found = parse_deferred(text)
    edge.result.update(state="empty", capacity=None, waiting_on="", picked=[], skipped=[], done=[], failed=[], dry_run=dry_run)
    if not found:
        edge.out("nothing deferred")
        return
    prs = gh("pr", "list", "-R", R, "--state", "open", "--limit", "100", "--json", "number,headRefName,createdAt,isDraft,statusCheckRollup")
    runs = gh("run", "list", "-R", R, "--limit", "100", "--json", "status,headBranch,databaseId")
    q = edge.queue_items()
    busy = None
    if n is None:
        try:
            busy = count_jobs([r for r in runs if r.get("status") in ("queued", "in_progress")], lambda rid: [j.get("status") for j in jobs(rid)])
        except Exception:
            busy = None
    main_q = False
    try:  # any non-completed main run with a queued job
        main_q = any("queued" in [j.get("status") for j in jobs(r["databaseId"])] for r in runs if r.get("headBranch") == "main" and r.get("status") != "completed")
    except Exception:
        main_q = True  # unknown: wait
    macos_busy = None
    try:
        macos_busy = count_macos_busy([r for r in runs if r.get("status") in ("queued", "in_progress")], lambda rid: [(j.get("labels") or [], j.get("status")) for j in jobs(rid)])
    except Exception:
        macos_busy = None
    picked, skipped, refusal = plan(found, prs, q, runs, n, busy, macos_busy=macos_busy, main_jobs_queued=main_q)
    edge.result.update(capacity=busy)
    if busy is not None:
        edge.out(f"capacity: {busy} jobs queued/in progress")
    if refusal:
        edge.result.update(state="waiting", waiting_on=refusal)
        edge.out(f"refusing: {refusal}")
        return
    lines = [f"rerun #{n} {b} ({why}): {len(rs)} run(s)" for n, b, rs, why in picked] + [f"skip {w}: {why}" for w, why in skipped]
    edge.out("\n".join(lines) or "nothing to do")
    edge.result.update(state="planned" if dry_run else "done", skipped=[{"what": w, "why": why} for w, why in skipped],
                       picked=[{"pr": n, "branch": b, "why": why, "runs": [{"id": rid, "workflow": name} for rid, name in rs]} for n, b, rs, why in picked])
    if dry_run or not picked:
        return
    done = set()
    for _n, b, rs, _ in picked:
        for rid, _name in rs:
            try:
                concl = [j.get("conclusion") for j in jobs(rid)]
            except Exception:
                concl = []
            if edge.gh.run(rerun_argv(rid, concl, R)[1:], retry=False).rc == 0:
                done.add(rid)
            else:
                edge.result["failed"].append(rid)
                edge.out(f"FAILED rerun {rid} {b}")
    edge.result.update(done=sorted(done, key=int))
    deferred.save(edge.store, [row for row in rows if row["id"] not in done])
    edge.journal("ci rerun: re-ran CI for " + ", ".join(f"#{n}" for n, *_ in picked) + f"; {len(skipped)} skipped or waiting")


def release_runs_active(edge: Edge) -> int:
    """Queued or in-progress runs on a release/* branch."""
    count = 0
    for status in ("in_progress", "queued"):
        rows = edge.gh.json(["run", "list", "-R", edge.repo, "--status", status, "--limit", "100", "--json", "headBranch"]) or []
        count += sum(1 for row in rows if (row.get("headBranch") or "").startswith("release/"))
    return count


def resume_all(edge: Edge, force: bool, dry_run: bool) -> None:
    """Re-run EVERY recorded run and empty the list (also of the runs whose re-run failed, as before). Refuses while
    a release/* run is active unless `force`: re-queued PR runs compete with the candidate."""
    edge.result.update(state="empty", capacity=None, waiting_on="", picked=[], skipped=[], done=[], failed=[], dry_run=dry_run)
    if release_runs_active(edge) and not force:
        raise Stop("refusing: a release/* run is active; re-queued PR runs would starve the candidate (use --all --force to override)")
    rows = deferred.runs(edge.store, edge.legacy_file)
    if not rows:
        edge.out("nothing deferred")
        return
    edge.result.update(state="planned" if dry_run else "done",
                       picked=[{"pr": None, "branch": row["branch"], "why": "all", "runs": [{"id": row["id"], "workflow": row["workflow"]}]} for row in rows])
    for row in rows:
        what = f"{row['id']} {row['branch']} {row['workflow']}".rstrip()
        if dry_run:
            edge.out(f"would rerun {what}")
            continue
        if edge.gh.run(["run", "rerun", row["id"], "-R", edge.repo], retry=False).rc == 0:
            edge.result["done"].append(row["id"])
            edge.out(f"rerun {what}")
        else:
            edge.result["failed"].append(row["id"])
            edge.out(f"FAILED rerun {what}")
    if not dry_run:
        deferred.save(edge.store, [])
