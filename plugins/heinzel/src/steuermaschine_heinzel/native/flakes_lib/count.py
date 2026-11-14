"""The flake counts and the green streak of main, from run rows and per-attempt details. Pure: no reads here.

Moved from the former flake counter with its decisions unchanged (`attempts_to_read`, `is_main_run`, `day_rows`,
`tally`, `streak`, `macos_suite`, `group`); the reads it did inline are passed in (`jobs_of`, `attempt_of`).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from typing import Any

MAIN_EVENTS = {"push", "schedule", "workflow_dispatch"}
FAILED = ("failure", "timed_out", "startup_failure")
AGGREGATE = "CI OK"  # the aggregate gate repeats the real failures

Run = dict[str, Any]


def attempts_to_read(run: Run) -> list[int]:
    """Attempts that failed: all earlier ones when the run passed on a rerun, every one when it ended red."""
    if run["conclusion"] == "success":
        return list(range(1, run["attempt"]))
    return list(range(1, run["attempt"] + 1)) if run["conclusion"] == "failure" else []


def is_main_run(run: Run) -> bool:
    return run["event"] in MAIN_EVENTS and run["headBranch"] == "main"


def day_rows(runs: list[Run]) -> dict[str, Counter[str]]:
    days: dict[str, Counter[str]] = defaultdict(Counter)
    for run in runs:
        if run.get("batch_unknown"):
            continue
        counts = days[f"{run['createdAt'][:10]} {'main' if is_main_run(run) else 'pr'}"]
        counts["runs"] += 1
        if run["conclusion"] == "cancelled":
            counts["cancelled"] += 1
        elif run["conclusion"] == "failure":
            counts["failed"] += 1
        if run["conclusion"] == "success" and run["attempt"] > 1:
            counts["flakes"] += 1  # failed once, passed on a rerun
        elif run["conclusion"] == "success":
            counts["clean"] += 1
    return days


def tally(runs: list[Run], details: dict[tuple[int, int], dict[str, Any]]) -> tuple[Counter[str], Counter[str]]:
    jobs: Counter[str] = Counter()
    tests: Counter[str] = Counter()
    for run in runs:
        if run.get("batch_unknown"):
            continue
        for attempt in attempts_to_read(run):
            data = details.get((run["databaseId"], attempt), {})
            jobs.update(j for j in data.get("jobs", []) if j != AGGREGATE)
            tests.update(data.get("tests", []))
    return jobs, tests


def streak(runs: list[Run]) -> tuple[int, int]:
    """Main runs in a row, newest first, with no failure and no rerun; and how many cancelled runs were skipped."""
    count = skipped = 0
    if any(r.get("batch_unknown") for r in runs):
        return 0, 0
    for run in sorted((r for r in runs if is_main_run(r) and r["status"] == "completed"), key=lambda r: r["createdAt"], reverse=True):
        if run["conclusion"] == "cancelled":
            skipped += 1
        elif run["conclusion"] == "success" and run["attempt"] == 1:
            count += 1
        else:
            break
    return count, skipped


def macos_suite(name: str) -> bool:
    lowered = name.lower()
    return "macos" in lowered and "macos-specific" not in lowered and "notifier" not in lowered


def wanted_attempts(runs: list[Run], all_jobs: dict[int, list[dict[str, str]]]) -> list[tuple[int, int]]:
    """The attempts whose failing jobs and tests `group` reads: every replaced attempt, and the last one when a job is red."""
    return sorted({(r["databaseId"], a) for r in runs for a in range(1, r["attempt"] + 1)
                   if a < r["attempt"] or any(j["conclusion"] in FAILED and j["name"] != AGGREGATE for j in all_jobs[r["databaseId"]])})


def group(runs: list[Run], jobs_of: Callable[[int], list[dict[str, str]]], attempt_of: Callable[[int, int], dict[str, Any]], bar: int = 3) -> dict[str, Any]:
    """Failing tests and jobs by number of runs, and the full-suite green streak. `runs` newest first, cancelled already dropped."""
    unknown = sum(bool(r.get("batch_unknown")) for r in runs)
    runs = [r for r in runs if not r.get("batch_unknown")]
    tests: Counter[str] = Counter()
    failing: Counter[str] = Counter()
    hidden: Counter[str] = Counter()
    streak_count, not_full, broken = 0, 0, bool(unknown)
    for run in runs:
        jobs = jobs_of(run["databaseId"])
        red = {j["name"] for j in jobs if j["conclusion"] in FAILED and j["name"] != AGGREGATE}
        run_tests: set[str] = set()
        run_failing = set(red)
        for attempt in range(1, run["attempt"]):  # attempts a rerun replaced
            data = attempt_of(run["databaseId"], attempt)
            run_failing |= set(data.get("jobs", [])) - {AGGREGATE}
            run_tests |= set(data.get("tests", []))
        if red:
            run_tests |= set(attempt_of(run["databaseId"], run["attempt"]).get("tests", []))
        tests.update(run_tests)
        failing.update(run_failing)
        if run["conclusion"] == "success":
            hidden.update(red)
        if not broken:
            if run["conclusion"] != "success" or run["attempt"] > 1 or run_failing:
                broken = True
            elif any(macos_suite(j["name"]) and j["conclusion"] == "success" for j in jobs):
                streak_count += 1
            else:
                not_full += 1
    return {"runs": len(runs), "tests": tests, "jobs": failing, "hidden": hidden, "streak": streak_count, "not_full": not_full, "bar": bar, "unknown": unknown}
