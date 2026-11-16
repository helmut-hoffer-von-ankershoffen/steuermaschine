"""The GitHub reads of `sm ci flakes`, through the Commands provider, with the cache of finished attempts.

Finished logs never change, so the failing jobs and tests of an attempt, and the job list of a finished run, are
cached as JSON in one directory (the command's own; no other command reads it). An empty answer means gh failed:
it is never cached.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from steuermaschine.github.logs import failing_tests

from ..queue_lib import core_facts

FIELDS = "databaseId,attempt,conclusion,status,headBranch,headSha,event,createdAt,displayTitle"
GH_TIMEOUT = 120.0


class Reads:
    def __init__(self, commands: Any, repo: str, cache: Path):
        self.commands, self.repo, self.cache = commands, repo, cache

    def gh(self, *args: str) -> str:
        done = self.commands.run(["gh", *args], timeout=GH_TIMEOUT)
        return str(done.out) if done.rc == 0 else ""

    def _json(self, text: str, default: Any) -> Any:
        try:
            return json.loads(text) if text else default
        except ValueError:
            return default

    def stem(self, suffix: str) -> Path:
        return self.cache / f"{self.repo.replace('/', '_')}-{suffix}"

    def run_list(self, limit: int, main_only: bool) -> list[dict[str, Any]]:
        args = ["run", "list", "-R", self.repo, "--workflow", core_facts.ci_file()]
        if main_only:
            args += ["--branch", "main", "--status", "completed"]
        listed = self._json(self.gh(*args, "-L", str(limit), "--json", FIELDS), [])
        return listed if isinstance(listed, list) else []

    def attempt(self, run_id: int, attempt: int) -> dict[str, Any]:
        """Failing jobs and tests of one attempt; the raw log and the result are cached."""
        result = self.stem(f"{run_id}-{attempt}.json")
        if result.exists():
            return dict(json.loads(result.read_text()))
        jobs = self._json(self.gh("run", "view", str(run_id), "-R", self.repo, "--attempt", str(attempt), "--json", "jobs"), {}).get("jobs", [])
        failed = [j["name"] for j in jobs if j.get("conclusion") == "failure"]
        # Job conclusions first: logs are the slow part, so they are fetched only when a job failed.
        log = self.gh("run", "view", str(run_id), "-R", self.repo, "--attempt", str(attempt), "--log-failed") if failed else ""
        data = {"jobs": failed, "tests": failing_tests(log)}
        if jobs:
            self.cache.mkdir(parents=True, exist_ok=True)
            result.with_suffix(".log").write_text(log)
            result.write_text(json.dumps(data))
        return data

    def jobs(self, run_id: int) -> list[dict[str, str]]:
        """Name and conclusion of every job of a finished run (latest attempt), cached."""
        cached = self.stem(f"{run_id}-jobs.json")
        if cached.exists():
            return list(json.loads(cached.read_text()))
        jobs = self._json(self.gh("run", "view", str(run_id), "-R", self.repo, "--json", "jobs"), {}).get("jobs", [])
        slim = [{"name": j["name"], "conclusion": j.get("conclusion") or ""} for j in jobs]
        if slim:
            self.cache.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(slim))
        return slim
