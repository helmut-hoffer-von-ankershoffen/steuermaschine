"""Plan for `sm ci rerun`: which deferred PRs get their cancelled CI re-run now. Pure functions (unit-tested), no I/O.

Rules: only after main CI is running (no queued main run), never while a release/* run is active, never more PRs than fit
the free hosted capacity (or the explicit `n`), queued-for-merge PRs first then oldest, PRs with real failures skipped, drafts skipped.
"""
from .checks import classify_pr

TARGET_JOBS = 14   # safe fill of the 20-job hosted runner limit
JOBS_PER_PR = 4    # jobs one re-run PR starts concurrently (measured: later jobs wait on earlier ones)
LIVE_QUEUE = ("dropped", "needs work", "merged", None)


def parse_deferred(text):
    """Lines of `id branch workflow...` -> {branch: [(id, workflow)]}."""
    out = {}
    for line in text.splitlines():
        p = line.split(None, 2)
        if len(p) >= 2 and p[0].isdigit():
            out.setdefault(p[1], []).append((p[0], p[2] if len(p) > 2 else ""))
    return out


def free_slots(active_jobs, target=TARGET_JOBS, per_pr=JOBS_PER_PR):
    """PRs that fit: free hosted capacity (target minus queued+in_progress jobs) divided by the jobs one PR starts."""
    return max(0, (target - active_jobs) // max(1, per_pr))


MACOS_BUSY_MAX = 2  # of the 4 hosted macOS runners: above this no deferred PR is re-run (velocity 11-08)


def rerun_argv(run_id, job_conclusions, repo):
    """`gh run rerun` argv: --failed (re-runs only failed/cancelled jobs, keeps the completed macOS cells) when some job already succeeded, else a full rerun."""
    argv = ["gh", "run", "rerun", str(run_id), "-R", repo]
    return argv + ["--failed"] if "success" in job_conclusions else argv  # noqa: RUF005


def count_macos_busy(active_runs, fetch):
    """macOS jobs queued or in progress; `fetch(run_id)` -> [(labels, status)]."""
    return sum(1 for r in active_runs for labels, st in fetch(r["databaseId"]) if "macos" in ",".join(labels).lower() and st in ("queued", "in_progress"))


def plan(deferred, prs, queue_items, runs, n=None, active_jobs=None, target=TARGET_JOBS, per_pr=JOBS_PER_PR, macos_busy=None, main_jobs_queued=False):
    """Return (picked, skipped, refusal). `runs` = active gh runs [{status, headBranch}]; `prs` = gh pr list json with rollup.
    Cap: explicit `n` = PRs in flight (override); else adaptive from `active_jobs` (queued+in_progress jobs); neither -> n=5."""
    active = [r for r in runs if r.get("status") in ("queued", "in_progress")]
    if any((r.get("headBranch") or "").startswith("release/") for r in active):
        return [], [], "a release/* run is active: re-queued PR runs would starve the candidate"
    if any(r.get("headBranch") == "main" and r.get("status") == "queued" for r in active):
        return [], [], "main CI is still queued: wait until it runs"
    if main_jobs_queued:
        return [], [], "a main run still has queued jobs: wait until all its jobs run"
    if macos_busy is not None and macos_busy > MACOS_BUSY_MAX:
        return [], [], f"macOS capacity is not free ({macos_busy} macOS jobs queued/in progress > {MACOS_BUSY_MAX})"
    queued_prs = {str(i.get("pr")) for i in queue_items if i.get("status") not in LIVE_QUEUE}
    by_branch = {p["headRefName"]: p for p in prs}
    inflight = sum(1 for p in prs if not p.get("isDraft") and classify_pr(p.get("statusCheckRollup") or [])[0] == "pending")
    if n is None and active_jobs is not None:
        slots, cap = free_slots(active_jobs, target, per_pr), f"free capacity ({active_jobs}/{target} jobs busy)"
    else:
        n = 5 if n is None else n
        slots, cap = max(0, n - inflight), f"the cap of {n} in flight"
    cands, skipped = [], []
    for branch, runs_ in deferred.items():
        pr = by_branch.get(branch)
        if pr is None:
            skipped.append((branch, "no open PR"))
        elif pr.get("isDraft"):
            skipped.append((f"#{pr['number']}", "draft"))
        else:
            kind, real = classify_pr(pr.get("statusCheckRollup") or [])
            if kind == "deferred":
                cands.append((0 if str(pr["number"]) in queued_prs else 1, pr.get("createdAt", ""), pr, runs_))
            else:
                skipped.append((f"#{pr['number']}", f"{kind}" + (f" ({', '.join(real)})" if real else "")))
    cands.sort(key=lambda c: (c[0], c[1]))
    picked = [(c[2]["number"], c[2]["headRefName"], c[3], "queued-for-merge" if c[0] == 0 else "oldest") for c in cands[:slots]]
    skipped += [(f"#{c[2]['number']}", f"over {cap} (waits)") for c in cands[slots:]]
    return picked, skipped, ""


def count_jobs(active_runs, fetch):
    """Jobs still queued or in progress across the active runs. `fetch(run_id)` -> list of job statuses."""
    return sum(1 for r in active_runs for st in fetch(r["databaseId"]) if st in ("queued", "in_progress"))
