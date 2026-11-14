"""Failing jobs and tests of one attempt of a run, read from its log once and cached."""

import argparse
from . import hzlib
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import edge  # moved: records, processes and what used to be separate scripts
from ... import state as statedir  # the plugin's state paths

CACHE = statedir.path("flakes")
TEST = re.compile(r"FAILED (\S+::\S+)|^\s*FAIL\s+(\S+ > .+)$|^\s*\d+\) (\[[^\]]+\] › .+?)(?: \(\d|$)|✘\s+\d+ (\[[^\]]+\] › .+?)(?: \(\d|$)")
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s?")


def gh(*args: str) -> str:
    """One bounded gh read: its stdout, or "" when it failed or ran past hzlib.GH_READ_TIMEOUT."""
    try:
        done = edge.run(["gh", *args], capture_output=True, text=True, check=False, timeout=hzlib.GH_READ_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"refused: gh {' '.join(args[:3])} timed out after {hzlib.GH_READ_TIMEOUT}s; nothing is assumed from it", file=sys.stderr, flush=True)
        return ""
    return done.stdout if done.returncode == 0 else ""


def test_names(log: str) -> list[str]:
    """Failing test names out of a `--log-failed` text (pytest FAILED lines, vitest FAIL lines, Playwright failures)."""
    names = []
    for raw in log.splitlines():
        line = STAMP.sub("", raw.split("\t")[-1]).strip()
        match = TEST.search(line)
        if match:
            names.append(next(g for g in match.groups() if g).strip())
    return names


def read_attempt(repo: str, run_id: int, attempt: int) -> dict:
    """Failing jobs and tests of one attempt; the raw log and the result are cached, since finished logs don't change."""
    CACHE.mkdir(parents=True, exist_ok=True)
    stem = CACHE / f"{repo.replace('/', '_')}-{run_id}-{attempt}"
    result = stem.with_suffix(".json")
    if result.exists():
        return json.loads(result.read_text())
    jobs = json.loads(gh("run", "view", str(run_id), "-R", repo, "--attempt", str(attempt), "--json", "jobs") or '{"jobs": []}')["jobs"]
    failed = [j["name"] for j in jobs if j["conclusion"] == "failure"]
    # Job conclusions first: logs are the slow part, so they are fetched only when a job failed.
    log = gh("run", "view", str(run_id), "-R", repo, "--attempt", str(attempt), "--log-failed") if failed else ""
    data = {"jobs": failed, "tests": test_names(log)}
    if jobs:  # an empty answer means gh failed; don't cache that
        stem.with_suffix(".log").write_text(log)
        result.write_text(json.dumps(data))
    return data
