#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""hz-push-gate PR|BRANCH [--min 15] [--keep-file PATH]: CI traffic control before a push that triggers PR CI.

Prints OK (exit 0) or WAIT with the queue age (exit 75) so the builder defers the push. WAIT when main CI has runs queued
>= MIN minutes (the same test as `hz-ci-defer --if-main-starved`) and the PR is not on the keep list: PR numbers in
research/ci-keep.txt (one per line or comma separated, `#` comments), PRs named `fix PR #N` in research/known-failures.toml,
and the oldest ready PR (default, or when ci-keep.txt contains `auto`). A branch without a PR yet is never kept.
Also WAIT while a CI run on a release/v* branch is queued or in progress (the candidate gets the runners; pushes to release/* are never held;
an explicit --min (hz-gate passes the coordinator's HZ_PUSH_GATE_MIN) waives it and still prints the run id; the hold lifts with a warning after 45 min; any gh error or >5 s fails open). Needs an independent review before it is relied on.
Fails open (OK, unchecked) when gh cannot be reached, so a network error never blocks a fix. Called by `hz-ship N --push`.
Core-side hook: see research/push-gate-core-hook.md.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from . import edge  # moved: siblings are imported relatively, no sys.path entry
from .push_gate_rules import decide, fix_prs, oldest_ready, parse_keep, queue_age_min, release_hold, release_run_active

R = "helmut-hoffer-von-ankershoffen/heinzel"
HOME = edge.records_dir()


def gh(*a):
    return json.loads(edge.run(["gh", *a], capture_output=True, text=True, check=True, timeout=60).stdout or "[]")


def release_check(pr, target, keep=frozenset(), budget=5.0):
    """Release-candidate hold, fail OPEN: any gh error or >5 s TOTAL (one deadline) => no hold. Keep-list PRs are never held. Waiver: explicit --min (hz-gate passes HZ_PUSH_GATE_MIN)."""
    if pr is not None and pr in keep:
        return 0, ""
    try:
        deadline = time.monotonic() + budget

        def g(*x):
            left = deadline - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired("gh", budget)
            return json.loads(edge.run(["gh", *x], capture_output=True, text=True, check=True, timeout=left).stdout or "[]")
        br = target
        if pr is not None:
            br = (g("pr", "view", str(pr), "-R", R, "--json", "headRefName") or {}).get("headRefName")
        runs = []
        for st in ("queued", "in_progress"):
            runs += g("run", "list", "-R", R, "--status", st, "--limit", "50", "--json", "databaseId,headBranch,status,createdAt")
        waived = "--min" in sys.argv or edge.setting("head_push_minimum") is not None
        return release_hold(release_run_active(runs, br), waived)
    except Exception:
        return 0, ""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("target", help="PR number, or a branch name that has no PR yet")
    ap.add_argument("--min", type=int, default=15, help="main CI queue age in minutes that triggers WAIT (default 15)")
    ap.add_argument("--keep-file", default=str(HOME / "research/ci-keep.txt"))
    a = ap.parse_args()
    pr = int(a.target) if a.target.isdigit() else None
    try:
        if pr is None:
            hit = gh("pr", "list", "-R", R, "--head", a.target, "--state", "open", "--json", "number")
            pr = hit[0]["number"] if hit else None
        queued = gh("run", "list", "-R", R, "--status", "queued", "--branch", "main", "--limit", "50", "--json", "createdAt")
        age = queue_age_min([r["createdAt"] for r in queued])
        if age is not None:
            # main CI is serial: a queued main run behind a main run whose JOBS are running is not starvation
            # (the 11-08 gate blocked every builder push for an hour while main CI was progressing)
            ids = [r["databaseId"] for r in gh("run", "list", "-R", R, "--branch", "main", "--event", "push", "--limit", "8", "--json", "databaseId,status")]
            for rid in ids[:6]:
                out = edge.run(["gh", "api", f"repos/{R}/actions/runs/{rid}/jobs", "--paginate", "-q", '[.jobs[]|select(.status=="in_progress")]|length'], capture_output=True, text=True, timeout=60).stdout.split()
                if any(x.isdigit() and int(x) > 0 for x in out):
                    age = None
                    break
        kf = Path(a.keep_file)
        nums, auto = parse_keep(kf.read_text() if kf.exists() else None)
        kn = HOME / "research/known-failures.toml"
        nums |= fix_prs(kn.read_text() if kn.exists() else "")
        ready = None
        if age is not None and age >= a.min and auto:
            ready = oldest_ready(gh("pr", "list", "-R", R, "--state", "open", "--limit", "100", "--json", "number,isDraft,createdAt,statusCheckRollup"))
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as e:
        return print(f"OK (unchecked): gh unavailable ({type(e).__name__}); push")
    held = release_check(pr, a.target, nums)
    if held[1]:
        print(held[1])
        if held[0]:
            return held[0]
    code, msg = decide(pr, age, a.min, nums, ready)
    print(msg)
    return code


# moved: the script's last line `sys.exit(main() or OK)` is gone; the gate runs only when called (push_gate.check)
