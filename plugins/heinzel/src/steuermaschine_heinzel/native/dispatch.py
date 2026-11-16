"""`sm ci one ...`: re-run the failing job of a CI run, or dispatch one lane or a few test ids on a hosted runner.

The logic is `dispatch_lib/focused.py` (moved) and `dispatch_lib/rules.py` (its pure decisions). This module is the
edge: typed options, the result model, and the binding of the library to the providers and settings of this call.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import typer
from pydantic import BaseModel, Field

from steuermaschine.engine import Refuse
from steuermaschine.github import Gh
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import Group, native, runtime

from . import merge_tier
from .dispatch_lib import focused
from .dispatch_lib import rules as c

JOURNAL = "heinzel.journal"


class DispatchSettings(BaseModel):
    """Settings of hosted single-lane runs, a mixin of the plugin's table."""

    dispatch_state_file: Path | None = Field(None, description="Receipts of the focused runs of the last 24 hours; a start in doubt leaves a .pending file "
                                                               "beside it. Default: dispatch/focused-runs.jsonl in the project's state home.")
    dispatch_scratch_dir: Path | None = Field(None, description="Where the temporary worktree of a throwaway commit goes. Default: dispatch/scratch in the "
                                                                "project's state home.")
    ci_job_cap: int = Field(c.JOB_CEILING, ge=1, description="Jobs the GitHub account runs at once, all repositories together. A hosted run beside a release "
                                                                 "candidate starts only while all jobs stay within it.")
    ci_macos_cap: int = Field(c.ARM_LIMIT, ge=0, description="macOS/ARM jobs the GitHub account runs at once. A run that needs one never starts beside a "
                                                                 "release candidate.")


class JobResult(Model):
    name: str
    conclusion: str = ""


class FocusedRun(Model):
    """What one `sm ci one` command did or found."""

    action: str  # dispatch | rerun | watch
    code: int = 0
    run: int | None = None
    url: str = ""
    attempt: int | None = None
    status: str = ""
    conclusion: str = ""
    jobs: list[JobResult] = []
    artifacts: list[str] = []
    tally: str = ""
    fail_log: str = ""
    waited_minutes: float | None = None
    lines: list[str] = []

    @property
    def exit_code(self) -> int:
        return self.code

    def render(self) -> list[str]:
        return self.lines


class FocusedRow(Model):
    """One focused run of the last 24 hours."""

    run: int
    kind: str
    what: str
    branch: str = ""
    at: str = ""
    status: str = ""
    result: str = ""
    tally: str = ""
    attempt: int = 1

    def render(self) -> list[str]:
        return [c.status_line({"run": self.run, "kind": self.kind, "what": self.what, "at": self.at, "tally": self.tally, "result": self.result,
                               "status": self.status})]


class FocusedStatus(Model):
    """The focused runs of the last 24 hours: in flight, or done with their tally."""

    cap: int
    in_flight: int
    paused: str = ""  # a start whose outcome is unknown blocks every new start until it is reconciled
    runs: list[FocusedRow] = []

    def render(self) -> list[str]:
        lines = [line for row in self.runs for line in row.render()] or [f"no focused run in the last 24 h (max {self.cap} in flight)"]
        return lines + ([f"admission paused: reconcile the uncertain focused start in {self.paused}"] if self.paused else [])


class Rounds(Model):
    """Green rounds that exclude a fail rate at about 95 % (rule of three), capped per job."""

    rounds: int
    fail_rate: float | None = None

    def render(self) -> list[str]:
        return [str(self.rounds)]


def journal(rt, text: str) -> None:
    """One dated line in the project's journal (the store), and on stderr for whoever watches."""
    now = datetime.fromtimestamp(rt.providers.clock.now()).astimezone()
    day = now.strftime("%Y-%m-%d")
    page = rt.providers.store.load(JOURNAL, day) or {"lines": []}
    page["lines"].append(f"{now.strftime('%Y-%m-%d %H:%M')} {text}")
    rt.providers.store.save(JOURNAL, day, page)


def bound(rt, lines: list[str], problems: list[str]) -> focused.Edge:
    """Bind the moved library to this call's providers and settings."""
    s = rt.settings

    def path(name: str, default: str) -> Path:
        return Path(str(s[name])).expanduser() if s.get(name) else Path(rt.project.home) / "dispatch" / default

    clone = s.get("core_repo_dir")  # the clone of the core repository other areas read too; the throwaway commit needs it

    def say(text: str) -> None:
        problems.append(text)
        rt.say(text)

    edge = focused.Edge(commands=rt.providers.commands, clock=rt.providers.clock, store=rt.providers.store,
                        journal=lambda text: journal(rt, text), out=lines.append, err=say)
    focused.bind(edge, str(s.get("repo") or ""), path("dispatch_state_file", "focused-runs.jsonl"), Path(str(clone)).expanduser() if clone else Path("core-clone-not-set"),
                 path("dispatch_scratch_dir", "scratch"), caps(s))
    return edge


def caps(settings: Mapping[str, Any] | None) -> c.Caps:
    """The account's caps from the settings; a missing value is the default."""
    jobs, macos = settings.get("ci_job_cap") if settings else None, settings.get("ci_macos_cap") if settings else None
    return c.Caps(jobs=c.JOB_CEILING if jobs is None else int(jobs), macos=c.ARM_LIMIT if macos is None else int(macos))


def call(action: str, fn, args: SimpleNamespace, next_cmd: str) -> FocusedRun:
    rt = runtime()
    lines: list[str] = []
    problems: list[str] = []
    edge = bound(rt, lines, problems)
    try:
        code = fn(args)
    except SystemExit as stop:
        if isinstance(stop.code, int):  # the SHA guard: its reasons were said line by line
            raise Refuse("\n".join(problems) or f"refused (exit {stop.code})", next_cmd, code=stop.code or 1) from None
        raise Refuse(str(stop.code).removeprefix("refusing: "), next_cmd) from None
    found = edge.result or {}
    started = next((int(line.rsplit(": run ", 1)[1]) for line in lines if ": run " in line and line.rsplit(": run ", 1)[1].isdigit()), None)
    return FocusedRun(action=action, code=int(code or 0), run=found.get("run") or started, url=found.get("url") or "", attempt=found.get("attempt"),
                      status=found.get("status") or ("started" if started else ""), conclusion=found.get("conclusion") or "",
                      jobs=[JobResult(name=j["name"], conclusion=j.get("conclusion") or "") for j in found.get("jobs", [])],
                      artifacts=found.get("artifacts", []), tally=found.get("tally") or "", fail_log=found.get("faillog") or "",
                      waited_minutes=found.get("waited_min"), lines=lines)


CI_ONE = Group("ci one", H(
    "ci one: verify ONE hosted-only failure on the real hosted runner in minutes, not a full CI cycle.",
    "Use when a job fails only on the hosted runner, or a flaky test needs N rounds there.",
    "dispatch and rerun start hosted runs; status, watch and repeats only read.",
    "sm ci one rerun 589 --job \"web-2 (macos-latest)\" --wait",
    "sm ci one status"))


@native("ci one dispatch", writes=True, replaces=("hz-ci-one",), help=H(
    "Dispatch ONE e2e lane or a few test ids of a branch on one hosted runner (focused-job.yml).",
    "Use when a test fails only hosted, or to prove a flake fixed with --repeat N; never to replace full CI.",
    "starts one hosted run on the branch; with --throwaway pushes validation/<name>-<sha8> first (and deletes it with --cleanup); writes a receipt.",
    "sm ci one dispatch fix/clock --test tests/e2e/web_ui_test.py::test_pair --repeat 10 --throwaway --wait --cleanup",
    "sm ci one status   (the tally `k/n green` comes when the run is done)",
    "Guards: never main or release/* without --force; beside an active release/* run only a run that needs no macOS, within the\n"
    "job ceiling; at most 5 focused runs in flight; at the job ceiling (ci_job_cap) or the macOS/ARM cap (ci_macos_cap) it refuses\n"
    "while main CI waits. --wait waits for a slot (30 min), then for the result (45 min).\n"
    "SHA guard (exit 2, --force does not lift it): refuses a branch whose tip is the head of an open PR, a pull_request key of the\n"
    "focused plan registry, or the tip of a remote branch outside validation/*, bisect/*, rehearsal/*. The required checks attach\n"
    "by SHA to the OLDEST check suite of a commit: a dispatch on a PR head leaves that PR BLOCKED with every check green (#790).\n"
    "--throwaway is the way: an empty commit on the tip, pushed to validation/<name>-<sha8>, the same tree on a SHA nothing else uses.\n"
    "The workflow must be on main and on the branch (merge origin/main). A start whose outcome is unknown is never retried:\n"
    "it leaves a .pending file that must be reconciled by hand first."))
def ci_one_dispatch(branch: str = typer.Argument(..., metavar="BRANCH", help="The branch to test (it must be on the remote)."),
                    suite: str | None = typer.Option(None, "--suite", metavar="LANE", help="One e2e lane: cli, supervised, web-1 or web-2."),
                    test: list[str] | None = typer.Option(None, "--test", metavar="ID", help="A test id (repeatable, at most 10)."),
                    kind: str | None = typer.Option(None, "--kind", help="e2e, pytest or vitest; inferred from the ids."),
                    os_name: str = typer.Option("macos-latest", "--os", help="macos-latest or ubuntu-24.04."),
                    repeat: int = typer.Option(1, "--repeat", metavar="N", help="Rounds inside ONE job and ONE process, 1 to 60 (test ids only)."),
                    stress: bool = typer.Option(False, "--stress", help="Keep every CPU busy while the selection runs."),
                    reviewed_head: str | None = typer.Option(None, "--reviewed-head", metavar="SHA", help="For a parametrized repeat: the reviewed 40-character commit."),
                    throwaway: bool = typer.Option(False, "--throwaway", help="Run on an empty child commit pushed to validation/<name>-<sha8>."),
                    cleanup: bool = typer.Option(False, "--cleanup", help="Delete that validation branch after the result (needs --throwaway and --wait)."),
                    wait: bool = typer.Option(False, "--wait", help="Wait for a slot, then for the result, and print it."),
                    force: bool = typer.Option(False, "--force", help="Allow main or release/* as the branch; no guard is lifted.")) -> FocusedRun:
    if os_name not in c.OSES:
        raise Refuse(f"--os is one of {', '.join(c.OSES)}", "sm ci one dispatch --help", code=2)
    if kind not in (None, "e2e", "pytest", "vitest"):
        raise Refuse("--kind is e2e, pytest or vitest", "sm ci one dispatch --help", code=2)
    args = SimpleNamespace(branch=branch, suite=suite, test=list(test or []), kind=kind, os=os_name, repeat=repeat, stress=stress,
                           reviewed_head=reviewed_head, throwaway=throwaway, cleanup=cleanup, wait=wait, json=False, force=force)
    return call("dispatch", focused.cmd_dispatch, args, "sm ci one status")


@native("ci one rerun", writes=True, replaces=("hz-ci-one",), help=H(
    "Re-run the failing job(s) of the newest CI run of a PR head or of a run, or only the job named.",
    "Use when a job failed only on the hosted runner and you named the cause; a re-run is verification, never the fix.",
    "starts a new attempt of the failed jobs (or of one job) of that run; writes a receipt.",
    "sm ci one rerun 589 --job \"web-2 (macos-latest)\" --wait",
    "sm ci one watch RUN   (when started without --wait)",
    "--job takes the exact name or a unique part of it. --repeat N (at most 10, needs --job) re-runs that job N times one after\n"
    "another and prints the tally; for a flaky TEST prefer `sm ci one dispatch --test ... --repeat N` (one job, one setup).\n"
    "Only runs of the CI workflow; the run must be finished. The same capacity guards as dispatch apply. With Core's merge tier at\n"
    "main (ADR 0149) a rerun of a red PR or main run is a valid repair (a pass is filed as a flake): main needs no --force;\n"
    "release/* always does."))
def ci_one_rerun(target: int = typer.Argument(..., metavar="PR|RUN", help="A PR number, or a run id."),
                 job: str | None = typer.Option(None, "--job", metavar="NAME", help="Only this job: its exact name or a unique part."),
                 repeat: int = typer.Option(1, "--repeat", metavar="N", help="Sequential re-runs of that one job, 1 to 10."),
                 wait: bool = typer.Option(False, "--wait", help="Wait for a slot, then for the result, and print it."),
                 force: bool = typer.Option(False, "--force", help="Allow a run on main or release/*; no guard is lifted.")) -> FocusedRun:
    rt = runtime()

    def tier() -> bool:
        return merge_tier.active_at_main(Gh(rt.providers.commands, repo=str(rt.settings.get("repo") or "")), rt.settings)

    args = SimpleNamespace(target=str(target), job=job, repeat=repeat, wait=wait, json=False, force=force, merge_tier=tier)
    return call("rerun", focused.cmd_rerun, args, "sm ci one status")


@native("ci one watch", writes=False, replaces=("hz-ci-one",), help=H(
    "Wait for a focused run started earlier and print its result, tally and failing lines.",
    "Use after `sm ci one dispatch` or `rerun` without --wait.",
    "nothing outside this machine; adds one line to the journal.",
    "sm ci one watch 43301234567",
    "sm ci one status"))
def ci_one_watch(run: int = typer.Argument(..., metavar="RUN", help="The run id.")) -> FocusedRun:
    return call("watch", focused.cmd_watch, SimpleNamespace(run=run, json=False), "sm ci one status")


@native("ci one status", writes=False, replaces=("hz-ci-one",), help=H(
    "The focused runs of the last 24 hours: in flight, or done with their tally (k/n green).",
    "Use before a dispatch (5 may be in flight) and to read the tally of a finished --repeat run.",
    "nothing outside this machine; refreshes its own receipts file.",
    "sm ci one status --branch fix/clock",
    "sm ci one watch RUN"))
def ci_one_status(branch: str | None = typer.Option(None, "--branch", metavar="B", help="Only runs on this branch.")) -> FocusedStatus:
    rt = runtime()
    bound(rt, [], [])
    try:
        with focused.admission_lock():
            found = [row for row in focused.rows() if not branch or row.get("branch") == branch]
            pending = focused.STATE.with_suffix(".pending")
            paused = str(pending) if pending.exists() else ""
    except SystemExit as stop:
        raise Refuse(str(stop.code).removeprefix("refusing: "), "sm ci one status") from None
    rows = [FocusedRow(run=row["run"], kind=row["kind"], what=row["what"], branch=row.get("branch") or "", at=row.get("at") or "",
                       status=row.get("status") or "", result=row.get("result") or "", tally=row.get("tally") or "", attempt=row.get("attempt", 1))
            for row in found]
    return FocusedStatus(cap=c.MAX_FOCUSED, in_flight=sum(1 for row in found if row.get("result") is None), paused=paused, runs=rows)


@native("ci one repeats", writes=False, replaces=("hz-ci-one",), help=H(
    "How many green rounds prove a flake fixed: ceil(3 / fail rate), at most 60 in one job; 10 when the rate is unknown.",
    "Use before `sm ci one dispatch --test ID --repeat N`, with the fail rate from the CI history.",
    "nothing (read-only).",
    "sm ci one repeats --fail-rate 1/3",
    "sm ci one dispatch BRANCH --test ID --repeat N --throwaway --wait --cleanup"))
def ci_one_repeats(fail_rate: str | None = typer.Option(None, "--fail-rate", metavar="P", help="Observed fail rate: 1/3, 0.05 or 5%.")) -> Rounds:
    try:
        rate = c.parse_rate(fail_rate)
    except (ValueError, ZeroDivisionError):
        raise Refuse(f"{fail_rate!r} is not a rate: give 1/3, 0.05 or 5%", "sm ci one repeats --fail-rate 1/3", code=2) from None
    return Rounds(rounds=c.rounds_for(rate), fail_rate=rate)
