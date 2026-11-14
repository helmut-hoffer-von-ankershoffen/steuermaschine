"""ci flakes: the daily flake count from CI and the green streak of main toward the bar. Read-only.

Default: per day and scope (main, pr) the runs, the clean ones, the failed ones, the flakes (a failed attempt that a
rerun of the same run passed) and the cancelled ones, then the failing jobs and tests of the failed attempts, then main's
streak of runs without a failure or rerun. `--group`: failing tests and jobs across the last N finished main runs, read
from every job's own conclusion (a macOS job that continue-on-error hides still counts), and the full-suite streak.

Main includes push, schedule and workflow_dispatch runs on main. For the core repository the run rows go through the
merge queue's resolution of batch admissions first (an unproven batch withholds the streak). The logic is
flakes_lib/count.py (moved, decisions unchanged); the reads and their cache flakes_lib/reads.py.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, Field

from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime

from .flakes_lib import count
from .flakes_lib.reads import Reads


class FlakesSettings(BaseModel):
    """Settings of `sm ci flakes` (mixin of the plugin's settings table)."""

    ci_flakes_cache_dir: Path | None = Field(None, description="Where `sm ci flakes` caches the failing jobs and tests of finished attempts; none: cache/flakes in the project's state directory.")


class DayRow(Model):
    day: str
    scope: str  # main | pr
    runs: int = 0
    clean: int = 0
    failed: int = 0
    flakes: int = 0  # failed once, passed on a rerun
    cancelled: int = 0


class Named(Model):
    name: str
    count: int
    hidden: int = 0  # --group: runs in which continue-on-error hid this red job


def bar_text(streak: int, bar: int, met: str = ", bar met") -> str:
    return f", {bar - streak} to go" if streak < bar else met


class FlakeDays(Model):
    """The daily flake count and main's streak without a failure or rerun."""

    days: list[DayRow]
    jobs: list[Named]  # failing jobs of the failed attempts, most first (top 12)
    tests: list[Named]
    streak: int
    cancelled_skipped: int
    bar: int
    unknown: int = 0  # batch admissions the queue could not prove: the streak is withheld
    read_only: bool = True

    def render(self) -> list[str]:
        out = ["day         scope  runs  clean  failed  flakes(rerun passed)  cancelled"]
        out += [f"{d.day}  {d.scope:5}  {d.runs:4}  {d.clean:5}  {d.failed:6}  {d.flakes:20}  {d.cancelled:9}" for d in self.days]
        for title, rows in (("Failing jobs", self.jobs), ("Failing tests", self.tests)):
            if rows:
                out += ["", f"{title} (failed attempts):", *[f"  {r.count:3}  {r.name}" for r in rows]]
        if self.unknown:
            out.append(f"streak qualification withheld: {self.unknown} unknown batch admission(s)")
        out += ["", f"main: {self.streak} runs in a row without a failure or rerun, bar {self.bar}" + bar_text(self.streak, self.bar)
                + f" ({self.cancelled_skipped} cancelled skipped)"]
        return out


class FlakeGroups(Model):
    """Failing tests and jobs across the last finished main runs, and the full-suite green streak, macOS included."""

    runs: int
    tests: list[Named]
    jobs: list[Named]
    streak: int
    not_full: int  # newer green runs without a macOS suite job, skipped
    bar: int
    unknown: int = 0
    short: bool = False
    read_only: bool = True

    def render(self) -> list[str]:
        out = [f"Last {self.runs} finished main runs (every job's own conclusion; a macOS job that continue-on-error hides still counts):"]
        for title, rows in (("Failing tests", self.tests), ("Failing jobs", self.jobs)):
            if not rows:
                out.append(f"  {title.lower()}: none")
                continue
            out.append(f"  {title}:")
            out += [f"    {r.name}: {r.count} of {self.runs} runs" + (f" ({r.hidden} hidden by continue-on-error)" if r.hidden else "") for r in rows]
        if self.unknown:
            out.append(f"  streak qualification withheld: {self.unknown} unknown batch admission(s)")
        out.append(f"  full-suite green runs in a row, macOS included: {self.streak} of the bar {self.bar}" + bar_text(self.streak, self.bar)
                   + (f" ({self.not_full} newer run(s) without a macOS suite skipped)" if self.not_full else ""))
        return out


def setting(name: str, default: Any = None) -> Any:
    rt: Any = runtime()
    value = (rt.settings or {}).get(name)
    return default if value is None else value


def cache_dir() -> Path:
    value = setting("ci_flakes_cache_dir")
    rt: Any = runtime()
    return Path(str(value)).expanduser() if value else Path(rt.project.home) / "cache" / "flakes"


def resolve(repo: str, listed: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """The merge queue's resolution of batch admissions, for the core repository only (None: an unproven batch)."""
    from . import queue as queue_commands

    hzlib = queue_commands._queue().hzlib
    if repo != hzlib.CORE:
        return listed
    resolved: list[dict[str, Any]] | None = hzlib.resolve_main_runs(listed)
    return resolved


def main_runs(reads: Reads, count_: int) -> list[dict[str, Any]]:
    listed = resolve(reads.repo, reads.run_list(count_, main_only=True))
    if listed is None:
        return [{"batch_unknown": True}]
    return sorted((r for r in listed if r.get("batch_unknown") or (count.is_main_run(r) and r["status"] == "completed" and r["conclusion"] != "cancelled")),
                  key=lambda r: r.get("createdAt", ""), reverse=True)


def named(counter: Any, top: int, hidden: Any = None) -> list[Named]:
    return [Named(name=name, count=n, hidden=(hidden or {}).get(name, 0)) for name, n in counter.most_common(top)]


def grouped(reads: Reads, runs: list[dict[str, Any]], bar: int, short: bool) -> FlakeGroups:
    real = [r for r in runs if not r.get("batch_unknown")]
    with ThreadPoolExecutor(8) as pool:  # job conclusions of every run at once, then the logs of the failed attempts at once
        all_jobs = dict(zip((r["databaseId"] for r in real), pool.map(lambda r: reads.jobs(r["databaseId"]), real), strict=True))
        wanted = count.wanted_attempts(real, all_jobs)
        read = dict(zip(wanted, pool.map(lambda key: reads.attempt(*key), wanted), strict=True))
    data = count.group(runs, jobs_of=lambda run_id: all_jobs[run_id], attempt_of=lambda run_id, attempt: read[(run_id, attempt)], bar=bar)
    top = 3 if short else 12
    return FlakeGroups(runs=data["runs"], tests=named(data["tests"], top, data["hidden"]), jobs=named(data["jobs"], top, data["hidden"]),
                       streak=data["streak"], not_full=data["not_full"], bar=bar, unknown=data["unknown"], short=short)


def daily(reads: Reads, days: int, bar: int, now: float) -> FlakeDays:
    since = (datetime.fromtimestamp(now, UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    listed = resolve(reads.repo, reads.run_list(1000, main_only=False))
    if listed is None:
        listed = [{"batch_unknown": True}]
    runs = [r for r in listed if r.get("batch_unknown") or (r["status"] == "completed" and r["createdAt"] >= since)]
    wanted = [(r["databaseId"], a) for r in runs if not r.get("batch_unknown") for a in count.attempts_to_read(r)]
    with ThreadPoolExecutor(6) as pool:
        details = dict(zip(wanted, pool.map(lambda key: reads.attempt(*key), wanted), strict=True))
    rows = [DayRow(day=key.split()[0], scope=key.split()[1], **{k: c[k] for k in ("runs", "clean", "failed", "flakes", "cancelled")})
            for key, c in sorted(count.day_rows(runs).items(), reverse=True)]
    jobs, tests = count.tally(runs, details)
    streak, skipped = count.streak(runs)
    return FlakeDays(days=rows, jobs=named(jobs, 12), tests=named(tests, 12), streak=streak, cancelled_skipped=skipped, bar=bar,
                     unknown=sum(bool(r.get("batch_unknown")) for r in runs))


@native("ci flakes", writes=False, replaces=("hz-flakes",), help=H(
    "The daily flake count from CI and main's green streak toward the bar; --group: failing tests and jobs across main runs.",
    "Use when judging a red run before a rerun or a culprit (is it a known flake?), and in the hourly view.",
    "nothing outside its own cache (read-only: GitHub run and log reads; finished attempts are cached in ci_flakes_cache_dir).",
    "sm ci flakes --group --short",
    "sm ci why RUN for one red run; a failing test that recurs: file it as flaky and fix the cause.",
    "Default: per day and scope (main, pr) runs, clean, failed, flakes (a failed attempt a rerun of the same run passed) and\n"
    "cancelled, over --days; then the failing jobs and tests of the failed attempts; the last line counts main runs in a row\n"
    "without a failure or rerun against --bar (cancelled runs skipped). --group: across the last --runs finished main runs,\n"
    "`name: K of M runs` for failing tests and jobs from every job's own conclusion (a macOS job hidden by continue-on-error\n"
    "still counts) and the full-suite streak, macOS included. Main means push, schedule and workflow_dispatch runs on main."))
def ci_flakes(days: int = typer.Option(7, "--days", help="Days of CI runs the daily count reads."),
              bar: int = typer.Option(3, "--bar", help="Main runs in a row the streak must reach."),
              repo: str = typer.Option("", "--repo", help="OWNER/NAME; empty: the setting repo (the core repository)."),
              group_: bool = typer.Option(False, "--group", help="Group failing tests and jobs across the last --runs main runs."),
              runs: int = typer.Option(10, "--runs", help="With --group: how many finished main runs."),
              short: bool = typer.Option(False, "--short", help="With --group: the top 3 of each list.")) -> FlakeDays | FlakeGroups:
    rt: Any = runtime()
    reads = Reads(rt.providers.commands, repo or str(setting("repo", "")), cache_dir())
    if not reads.repo:
        raise Refuse("no repository: pass --repo or set repo", "sm self config", code=2)
    if group_:
        found = main_runs(reads, runs)
        if not found:
            raise Refuse("no finished main runs read; check gh", "sm ci main", code=1)
        return grouped(reads, found, bar, short)
    return daily(reads, days, bar, rt.providers.clock.now())
