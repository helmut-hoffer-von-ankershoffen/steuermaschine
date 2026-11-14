"""Block until a CI run or a pull request's checks are done, then give only the outcome and the failing job and test
names (from the log). Code 0 green, 1 red, 2 timeout.

A number below 10,000,000 is a pull request, a larger one a run id; a pull-request or run URL is understood, and so is
`plugins 38`. The advisory `review` check is ignored. A merged PR counts as green.

Moved from the former watch tool with its decisions unchanged; `hub.py` (here `hzlib`) is how it reaches GitHub and
the clock. `FOUND` carries the failing names of the last outcome as data, for the result model.
"""

import re

from . import hub as hzlib

URL = re.compile(r"github\.com/([^/]+/[^/]+)/(actions/runs|pull)/(\d+)")
POLL = 30
FOUND: dict[str, list[str]] = {}


def parse(words: list[str], repo: str) -> tuple[str, str, str]:
    """-> (kind, repo, number) where kind is `run` or `pr`."""
    match = URL.search(words[0])
    if match:
        return ("run" if match.group(2) == "actions/runs" else "pr"), match.group(1), match.group(3)
    if len(words) == 1 and words[0].isdigit() and int(words[0]) >= 10_000_000:
        return "run", repo, words[0]
    pr_repo, number, _ = hzlib.split_key(words)
    return "pr", pr_repo if len(words) > 1 else repo, number


def tests_for(repo: str, runs: list[int]) -> list[str]:
    return list(dict.fromkeys(t for run in runs for t in hzlib.failing_tests(repo, run)))


def outcome(label: str, verdict: str, jobs: list[str], tests: list[str]) -> str:
    FOUND.update(failed=list(jobs), tests=list(tests))
    lines = [f"{label}: {verdict}"]
    if jobs:
        lines.append("  failed: " + ", ".join(jobs))
    if tests:
        lines += ["  tests:"] + [f"    {t}" for t in tests]
    return "\n".join(lines)


def watch_run(repo: str, run_id: str, deadline: float) -> tuple[int, str]:
    while True:
        info = hzlib.ghj("run", "view", run_id, "-R", repo, "--json", "status,conclusion,jobs", default={}) or {}
        if info.get("status") == "completed":
            break
        if hzlib.now() > deadline:
            return 2, f"run {run_id}: timeout"
        hzlib.sleep(POLL)
    label = f"run {run_id}"
    if info.get("conclusion") == "success":
        return 0, f"{label}: success"
    jobs = [j["name"] for j in info.get("jobs", []) if j.get("conclusion") in ("failure", "timed_out", "cancelled")]
    return 1, outcome(label, info.get("conclusion") or "unknown", jobs, hzlib.failing_tests(repo, int(run_id)))


def watch_pr(repo: str, pr: str, deadline: float) -> tuple[int, str]:
    merge = hzlib
    label = f"PR #{pr}" if repo == hzlib.CORE else f"PR {repo.split('/')[-1].removeprefix('heinzel-')} #{pr}"
    while True:
        info = hzlib.ghj("pr", "view", pr, "-R", repo, "--json", "state,statusCheckRollup", default={}) or {}
        rollup = [c for c in info.get("statusCheckRollup", []) if c.get("name") != "review"]
        if info.get("state") == "MERGED":
            return 0, f"{label}: merged"
        if info.get("state") == "CLOSED":
            return 1, f"{label}: closed"
        if merge.settled(rollup):
            break
        if hzlib.now() > deadline:
            return 2, f"{label}: timeout"
        hzlib.sleep(POLL)
    bad = [c for c in rollup if c.get("conclusion") in merge.FAILED]
    if not bad:
        return 0, f"{label}: green"
    return 1, outcome(label, "red", [c.get("name") or "?" for c in bad], tests_for(repo, hzlib.failed_runs(rollup)))


def watch(target: list[str], repo: str, timeout_minutes: float) -> tuple[str, str, str, int, str]:
    """The former entry point without its option parsing: (kind, repo, number, code, text)."""
    FOUND.clear()
    kind, repo, number = parse(target, repo)
    deadline = hzlib.now() + timeout_minutes * 60
    code, text = (watch_run if kind == "run" else watch_pr)(repo, number, deadline)
    return kind, repo, number, code, text
