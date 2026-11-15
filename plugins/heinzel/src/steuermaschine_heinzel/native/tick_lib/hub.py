"""What the watch needs from GitHub and the clock: the functions it took from the former shared tool library.

`checks.py` refers to this module as `hzlib`, the name that library had, so its lines stay as they were.
`bind` sets the providers and the core repository for one command; a test sets what it needs.

Duplicates until their slices land (replace each by an import then):
- `settled`, `FAILED`: the merge train's (slice heinzel-native-queue-release)
- `failed_runs`: the queue's shared library (slice heinzel-native-queue-release)
- `failing_tests`, `read_attempt`, `test_names`, `TEST`, `STAMP`: the flake counter's (`ci flakes`). The former
  `read_attempt` cached the log and the result of every finished attempt in a directory other tools read; this one
  writes nothing and reads the log once per call.
`split_key`, `CORE`, `PLUGINS` are imported from the admission area's identity module, not copied.
"""

from __future__ import annotations

import json
import re
from typing import Any

from steuermaschine.defaults import SystemClock
from steuermaschine.github import Gh

from ..admission_lib import identity
from ..ship_lib import repos

CORE = identity.CORE
PLUGINS = identity.PLUGINS
split_key = identity.split_key


def checkout_repo() -> str | None:
    """The project repository of the current directory's origin (a bare PR number there means that repository)."""
    return repos.repo_of_checkout(EDGE.commands) if EDGE.commands is not None else None


RUN_URL = re.compile(r"/runs/(\d+)")
FAILED = ("FAILURE", "CANCELLED", "TIMED_OUT")
TEST = re.compile(r"FAILED (\S+::\S+)|^\s*FAIL\s+(\S+ > .+)$|^\s*\d+\) (\[[^\]]+\] › .+?)(?: \(\d|$)|✘\s+\d+ (\[[^\]]+\] › .+?)(?: \(\d|$)")  # noqa: RUF001
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s?")
TIMEOUT, LOG_TIMEOUT = 60.0, 180.0


class Edge:
    """How the functions below reach the world."""

    def __init__(self, commands: Any = None, clock: Any = None):
        self.commands, self.clock = commands, clock or SystemClock()


EDGE = Edge()


def bind(edge: Edge, core: str) -> None:
    global EDGE, CORE
    EDGE, CORE = edge, core or identity.CORE


def gh(*args: str, timeout: float = TIMEOUT) -> tuple[int, str]:
    done = Gh(EDGE.commands, timeout=timeout, clock=EDGE.clock).run(list(args))
    return done.rc, done.out


def ghj(*args: str, default: Any = None) -> Any:
    code, out = gh(*args)
    try:
        return json.loads(out) if code == 0 else default
    except ValueError:
        return default


def sleep(seconds: float) -> None:
    EDGE.clock.sleep(seconds)


def now() -> float:
    return float(EDGE.clock.now())


def settled(rollup: list[dict[str, Any]]) -> bool:
    """Every check completed (a status without a `status` field counts as not completed). `CI OK` is read only when
    the repository has one; others (heinzel-sf) name their gate differently. At least one check must exist."""
    return bool(rollup) and all(c.get("status") == "COMPLETED" for c in rollup)


def test_names(log: str) -> list[str]:
    """Failing test names out of a `--log-failed` text (pytest FAILED lines, vitest FAIL lines, Playwright failures)."""
    names = []
    for raw in log.splitlines():
        line = STAMP.sub("", raw.split("\t")[-1]).strip()
        match = TEST.search(line)
        if match:
            names.append(next(g for g in match.groups() if g).strip())
    return names



def read_attempt(repo: str, run_id: int, attempt: int) -> dict[str, Any]:
    """Failing jobs and tests of one attempt, read from GitHub (nothing is cached here)."""
    code, out = gh("run", "view", str(run_id), "-R", repo, "--attempt", str(attempt), "--json", "jobs")
    jobs = json.loads((out if code == 0 else "") or '{"jobs": []}')["jobs"]
    failed = [j["name"] for j in jobs if j["conclusion"] == "failure"]
    # Job conclusions first: logs are the slow part, so they are fetched only when a job failed.
    code, log = gh("run", "view", str(run_id), "-R", repo, "--attempt", str(attempt), "--log-failed", timeout=LOG_TIMEOUT) if failed else (0, "")
    return {"jobs": failed, "tests": test_names(log if code == 0 else "")}


def failing_tests(repo: str, run_id: int) -> list[str]:
    """Failing test names of a finished run's latest attempt, taken from its log."""
    info = ghj("run", "view", str(run_id), "-R", repo, "--json", "attempt", default={}) or {}
    data = read_attempt(repo, run_id, info.get("attempt", 1))
    return list(dict.fromkeys(data.get("tests", [])))


def failed_runs(rollup: list[dict[str, Any]]) -> list[int]:
    """Run ids behind the failed checks of a pull request."""
    ids = [int(m.group(1)) for c in rollup if c.get("conclusion") in ("FAILURE", "CANCELLED", "TIMED_OUT") and c.get("name") != "review" for m in [RUN_URL.search(c.get("detailsUrl") or "")] if m]
    return list(dict.fromkeys(ids))
