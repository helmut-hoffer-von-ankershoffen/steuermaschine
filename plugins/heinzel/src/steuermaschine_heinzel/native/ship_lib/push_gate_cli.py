#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""push-gate PR|BRANCH [--min 15]: CI traffic information before a push that triggers PR CI.

Always OK (exit 0). Prints one info line when main CI has runs queued >= MIN minutes and one when a CI run on a release/v* branch
is active: PR CI may queue behind them, but a push or PR is never refused for it. The release candidate is protected by the merge
freeze on main merges, not by pushes. Fails open (OK, unchecked) when gh cannot be reached (any gh error or >5 s).
"""
import argparse
import json
import subprocess
import time

from . import edge  # moved: siblings are imported relatively, no sys.path entry
from .push_gate_rules import decide, queue_age_min, release_hold, release_run_active

R = "helmut-hoffer-von-ankershoffen/heinzel"
HOME = edge.records_dir()


def gh(*a):
    return json.loads(edge.run(["gh", *a], capture_output=True, text=True, check=True, timeout=60).stdout or "[]")


def release_check(pr, target, budget=5.0):
    """The active release candidate as one info line, fail OPEN: any gh error or >5 s TOTAL (one deadline) => no line."""
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
        return release_hold(release_run_active(runs, br))
    except Exception:
        return 0, ""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("target", help="PR number, or a branch name that has no PR yet")
    ap.add_argument("--min", type=int, default=15, help="main CI queue age in minutes that triggers WAIT (default 15)")
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
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as e:
        return print(f"OK (unchecked): gh unavailable ({type(e).__name__}); push")
    held = release_check(pr, a.target)
    if held[1]:
        print(held[1])
    code, msg = decide(age, a.min)
    print(msg)
    return code


# moved: the script's last line `sys.exit(main() or OK)` is gone; the gate runs only when called (push_gate.check)
