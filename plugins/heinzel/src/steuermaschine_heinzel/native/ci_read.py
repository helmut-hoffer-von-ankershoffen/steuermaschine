"""ci why, ci faillog: why did CI fail, in one read. Read-only.

`ci why PR|RUN` is the one answer: the cause line, the failed jobs with their class (DOWNLOAD, BROWSER_SETUP_TIMEOUT,
TEST, UNKNOWN), the failing tests, the first error lines, whether the failure is a KNOWN one, and the next command.
For a pull request it starts from `why.explain` (required checks, queue entry, holds). `ci faillog` is the longer
condensed log, or with --raw the failed-step log itself. Neither command ever calls the outside classifier: that is
`sm ci classify`, a reviewed write command of its own (ci_classify.py).

Heinzelmännchen knowledge kept here: the aggregate job of the CI run repeats its real failures;
`merge-gate` (Sonar) and `coverage-floor` come from their own workflow and are not jobs of the CI run; a cancelled job
is a deferral or a superseded run, not a code failure; an UNKNOWN cause is never rerun; a rerun verifies, it is never
the fix.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, Field

from steuermaschine.engine import Refuse
from steuermaschine.github import Gh, logs
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import Group, native

from . import merge_tier, rejudge
from .ci_lib import cause, faillog
from .go import world
from .go_lib import probes
from .queue_lib import (
    core_facts,
    known_failures,  # the registry reader of the queue slice: one copy
)
from .why import PullRequestWhy, explain

CI = Group("ci", H("CI reads and operations: why a run failed, the failing log, flakes, triage, reruns.",
                   "Use when a run or a pull request is red, queued, cancelled or flaky.",
                   "nothing by itself; see each command.", "sm ci why 795", "sm ci faillog 795"))
RUN_ID_FLOOR = 100000  # a number above this is a run id, below it a pull request number
GATE_ROLE_TEXT = {"sonar": "Sonar's quality gate and ratings: sm head sonar {pr}", "coverage": "the coverage floor of the pull request"}


def gate_text(name: str) -> str:
    """What the merge-gate workflow's check `name` judges (its role in Core's release policy)."""
    roles = {check: role for role, check in core_facts.at_main().gate_checks.items()}
    return GATE_ROLE_TEXT.get(roles.get(name, ""), "a check of the merge-gate workflow")
TRAPS = {
    "download": "A rerun verifies, it is never the fix: once, and name the tool that failed to download in the handover.",
    "known": "KNOWN is a reviewed registry entry with a deadline, not a licence: one rerun of that job, no code change, no builder message.",
    "test": "Read every failure before the head changes; a rerun of a TEST failure hides it. No flakiness is accepted.",
    "unknown": "An UNKNOWN cause is never rerun: read the log first.",
    "merge_rerun": "Merge tier (Core ADR 0149): a rerun of a red PR or main run is a valid repair, and a pass after it is filed as a "
                   "flaky bead (sm ci flake-ledger). Read the log when it fails again; release/* keeps the first-attempt rule.",
    "blocked": "BLOCKED with every check green: merge-gate and coverage-floor are attached by SHA to another check suite. "
               "Close/reopen and judge reruns do not fix it: new head.",
    "attached": "A check posted by another check suite has no job to rerun: it is judged again only for a new head.",
    "gate": "merge-gate and coverage-floor are judged by the head's Merge gate run, never by the suite they show under: a rerun "
            "of another suite only copies the old verdict. A real finding needs a new head; a reviewed closure needs a re-judge.",
    "pending": "Logs exist only for finished jobs; do not push while the run is in flight.",
    "cancelled": "A cancelled run is a deferral or a superseded head, not a code failure: do not message a builder.",
}


class CiSettings(BaseModel):
    """Settings of the CI reads (mixin of the plugin's settings table)."""

    ci_known_failures_file: Path | None = Field(default=None, description="The reviewed registry of known failures (TOML, [[known]] rows with match, bead, added, until); none: nothing is KNOWN.")
    ci_research_dir: Path | None = Field(default=None, description="Where the review files of flagged pull requests are (review-N.md, review-N-*.md); read only.")


class FailedJob(Model):
    name: str
    steps: list[str] = []  # the failed steps
    cause: str  # DOWNLOAD | BROWSER_SETUP_TIMEOUT | TEST | UNKNOWN | AGGREGATE (repeats the failures of the other jobs)
    source: str = ""  # the tool or subsystem, when identified
    known: str = ""  # the bead of a known-failure registry entry that matches the job

    def line(self) -> str:
        return (f"  {self.cause:<9} {self.name}" + (f" ({self.source})" if self.source else "") + (f"  [KNOWN {self.known}]" if self.known else "")
                + (f"  step: {'; '.join(self.steps)[:80]}" if self.steps else ""))


class RunFailure(Model):
    run: int
    workflow: str = ""
    status: str = ""
    conclusion: str = ""
    attempt: int = 1
    head: str = ""
    checks: list[str] = []  # for a pull request: its failing checks that belong to this run
    verdict: str = ""
    rerun_is_next: bool = False  # only when every failed job is a download
    jobs: list[FailedJob] = []
    cancelled_jobs: int = 0
    tests: list[str] = []  # failing test names read from the log
    known: str = ""  # the bead of a registry entry matching a failing test
    hint: str = ""  # a hosted-only gate the log names
    log: list[str] = []  # the condensed failing part
    log_bytes: int = 0
    unknown: str = ""  # why the run could not be read

    def render(self, with_log: bool = True) -> list[str]:
        title = f"RUN      {self.run} {self.workflow} attempt {self.attempt}: {self.status} {self.conclusion}".rstrip()
        out = [title + (f"   checks: {', '.join(sorted(set(self.checks)))[:120]}" if self.checks else "")]
        if self.unknown:
            return [*out, f"  UNKNOWN  {self.unknown}"]
        out += [j.line() for j in self.jobs]
        out += [f"  {self.cancelled_jobs} cancelled job(s): a deferral or a superseded run, not a code failure"] if self.cancelled_jobs else []
        out += [f"  TESTS    {len(self.tests)} failing" + (f"  [KNOWN {self.known}]" if self.known else "") + ": " + "; ".join(self.tests[:6])[:400]] if self.tests else []
        out += [f"  HINT     {self.hint}"] if self.hint else []
        if with_log and self.log:
            out += [f"  LOG      {self.log_bytes} bytes of failed steps; the failing part:", *[f"    {line}" for line in self.log]]
        return out


class CiWhy(Model):
    """Why CI failed for one pull request or one run."""

    target: int
    kind: str  # pr | run
    cause: str  # the one line
    pull_request: PullRequestWhy | None = None
    runs: list[RunFailure] = []
    gates: list[str] = []  # failing gate checks that are not jobs of the CI run, each with what judges it
    pending: list[str] = []  # checks still running
    next: str = ""
    trap: str = ""
    unknown: list[str] = []
    read_only: bool = True

    def render(self) -> list[str]:
        out = [f"CAUSE    {self.cause}"]
        if self.pull_request:
            out += [f"         {line.strip()}" for line in self.pull_request.render()[:1]]
            out += [f"         {c.name}: {c.latest}" + (f"  (older not green: {','.join(c.older_not_green)})" if c.older_not_green else "") for c in self.pull_request.checks]
        for run in self.runs:
            out += run.render()
        out += [f"GATE     {g}" for g in self.gates]
        out += [f"PENDING  {', '.join(self.pending)[:160]}"] if self.pending else []
        out += [f"UNKNOWN  {u}" for u in self.unknown]
        return [*out, f"NEXT     {self.next}" if self.next else "NEXT     nothing to run", *([f"TRAP     {self.trap}"] if self.trap else [])]


class FailLog(Model):
    """The failing part of the logs of one pull request, one run or one log file."""

    target: str
    runs: list[RunFailure] = []
    lines: list[str] = []  # --file and --raw: the lines themselves
    next: str = ""
    unknown: list[str] = []
    read_only: bool = True

    @property
    def verbatim(self) -> bool:
        """Log lines are what CI printed: their stamps stay as they are."""
        return True

    def render(self) -> list[str]:
        if self.lines:
            return self.lines
        out: list[str] = []
        for run in self.runs:
            out += [f"## run {run.run}: {', '.join(sorted(set(run.checks)))[:150] or run.workflow}  ({run.log_bytes} bytes of failed steps; all of it: sm ci faillog {run.run} --raw)"]
            out += [f"UNKNOWN  {run.unknown}"] if run.unknown else run.log
        out += [f"UNKNOWN  {u}" for u in self.unknown]
        return out + ([f"verify with: {self.next}   (hosted-only failure; do not push-and-wait for full CI)"] if self.next else [])


# ------------------------------------------------------------------ reads
def known_registry(w: probes.World) -> list[Any]:
    path = w.settings.get("ci_known_failures_file")
    return known_failures.load_known(str(Path(str(path)).expanduser())) if path else []


def failed_log(w: probes.World, run: int | str) -> tuple[str, str]:
    """(the failed-step log of a run, why it could not be read)."""
    res = w.gh.run(["run", "view", str(run), "-R", w.repo, "--log-failed"], timeout=120)
    return (res.out, "") if res.rc == 0 else ("", w.gh.why(res))


def read_run(w: probes.World, run: int | str, checks: list[str] | None = None, known: list[Any] | None = None, k: int = 4, tail: int = 6) -> RunFailure:
    """One run: its failed jobs classified from the failed steps and their log, the failing tests, the condensed log."""
    info, why = w.gh.try_json(["run", "view", str(run), "-R", w.repo, "--json", "status,conclusion,jobs,workflowName,attempt,headSha"], timeout=30)
    if why or not isinstance(info, dict):
        return RunFailure(run=int(run), checks=checks or [], unknown=why or "no answer for the run")
    jobs = info.get("jobs") or []
    base = RunFailure(run=int(run), workflow=str(info.get("workflowName") or ""), status=str(info.get("status") or ""), conclusion=str(info.get("conclusion") or ""),
                      attempt=int(info.get("attempt") or 1), head=str(info.get("headSha") or ""), checks=checks or [],
                      cancelled_jobs=sum(1 for j in jobs if j.get("conclusion") == "cancelled"))
    failed = [j for j in jobs if j.get("conclusion") == "failure"]
    if not failed:
        text = "no failed jobs" if base.status == "completed" else f"{base.status or 'unknown'}, logs not available yet, retry later"
        return base.model_copy(update={"verdict": f"run {run}: {text}"})
    text, why = failed_log(w, run)
    classes = cause.classify_jobs(jobs, cause.split_log(text))
    line, rerun = cause.verdict(classes, run)
    tests = list(dict.fromkeys(logs.failing_tests(text)))
    rows = [FailedJob(name=j["name"], steps=[s["name"] for s in j.get("steps", []) if s.get("conclusion") == "failure"], cause=classes[j["name"]][0],
                      source=classes[j["name"]][1] or "", known=known_failures.known_bead([j["name"]], known) or "") for j in failed]
    return base.model_copy(update={"verdict": line, "rerun_is_next": rerun, "jobs": rows, "tests": tests, "known": known_failures.known_bead(tests, known) or "", "hint": cause.hosted_only_hint(text),
                                   "log": logs.condense(logs.clean(text), k, tail) if text else [], "log_bytes": len(text.encode()), "unknown": why})


def rollup_of(w: probes.World, number: int) -> tuple[list[dict[str, Any]], str]:
    data, why = w.gh.try_json(["pr", "view", str(number), "-R", w.repo, "--json", "statusCheckRollup"], timeout=30)
    if why or not isinstance(data, dict):
        return [], why or "no answer for the pull request"
    return list(data.get("statusCheckRollup") or []), ""


def decide(target: int, kind: str, runs: list[RunFailure], gates: list[str], pending: list[str], pr: PullRequestWhy | None,
           traced: bool = False, rerun_repairs: bool = False) -> tuple[str, str, str]:
    """(cause line, next command, trap) from what was read: the order a person would test it. traced: a red gate check
    was traced to the Merge gate run of the head, which a re-judge re-runs. rerun_repairs: the merge tier is at main and
    the target is no release candidate, so a TEST failure is rerun first."""
    label = f"PR {target}" if kind == "pr" else f"run {target}"
    jobs = [(r, j) for r in runs for j in r.jobs if j.cause != "AGGREGATE"]
    if jobs:
        kinds = sorted({j.cause for _, j in jobs})
        first_run, first = jobs[0]
        names = ", ".join(f"{j.name[:40]}={j.cause}" + (f"({j.source})" if j.source else "") for _, j in jobs[:4]) + (f" and {len(jobs) - 4} more" if len(jobs) > 4 else "")
        known = next((j.known for _, j in jobs if j.known), "") or next((r.known for r in runs if r.known), "")
        if all(j.cause == "DOWNLOAD" for _, j in jobs):
            return f"{label}: every failed job is a tool DOWNLOAD -> {names}", f"sm ci one rerun {target} --wait", TRAPS["download"]
        if known and all(j.known or r.known for r, j in jobs):
            return f"{label}: KNOWN failure (bead {known}) -> {names}", f'sm ci one rerun {target} --job "{first.name}" --wait', TRAPS["known"]
        hint = next((r.hint for r in runs if r.hint), "")
        if hint:
            return f"{label}: {hint.split(':')[0]} -> {names}", f"sm ci faillog {first_run.run}", TRAPS["test"]
        if "UNKNOWN" in kinds:
            return f"{label}: {len(jobs)} failed job(s), cause not identified for at least one -> {names}", f"sm ci faillog {first_run.run}", TRAPS["unknown"]
        if rerun_repairs:
            return f"{label}: {len(jobs)} failed job(s) ({', '.join(kinds)}) -> {names}", f"sm ci one rerun {target} --wait", TRAPS["merge_rerun"]
        return f"{label}: {len(jobs)} failed job(s) ({', '.join(kinds)}) -> {names}", f"sm ci faillog {first_run.run}", TRAPS["test"]
    unread = [r for r in runs if r.unknown]
    if unread:
        return f"{label}: run {unread[0].run} could not be read: {unread[0].unknown}", f"sm ci why {target}", ""
    if gates:
        posted = [g.split(":")[0] for g in gates]
        if traced:
            return (f"{label}: no CI job failed; red gate check(s) judged by the head's Merge gate run: {', '.join(posted)}",
                    f"sm head sonar {target}   (clean: sm ci merge-gate rejudge {target})", TRAPS["gate"])
        return (f"{label}: no CI job failed; red check(s) posted by another check suite: {', '.join(posted)}",
                f"sm head sonar {target}" if core_facts.gate_check("sonar") in posted else f"sm why {target}", TRAPS["attached"])
    if pending or any(r.status and r.status != "completed" for r in runs):
        return f"{label}: still running ({len(pending)} check(s) pending), no failure yet", f"sm ci watch {target}", TRAPS["pending"]
    if any(r.cancelled_jobs or r.conclusion == "cancelled" for r in runs):
        return f"{label}: cancelled, nothing failed", "sm ci rerun --dry-run", TRAPS["cancelled"]
    if pr is not None:
        blocked = pr.merge_state == "BLOCKED" and all(c.latest == "SUCCESS" for c in pr.checks)
        return f"{label}: CI is not the reason; {pr.why}", f"sm why {target}", TRAPS["blocked"] if blocked else ""
    return f"{label}: no failed jobs", "", ""


def gate_line(check: dict[str, Any], target: int, run: dict[str, Any] | None, found_by: str) -> str:
    """One red gate check and the Merge gate run that judged it (found through its details URL or the head)."""
    suite = check.get("workflowName") or "another workflow"
    if run is None:  # not traced: as before, with what judges it
        return f"{check.get('name')}: {check.get('conclusion')}, posted by the check suite of {suite}, not a job of the CI run; {gate_text(str(check.get('name'))).format(pr=target)}"
    state = f"{run.get('status')} {run.get('conclusion') or ''}".rstrip()
    return (f"{check.get('name')}: {check.get('conclusion')}, shown under the check suite of {suite}; judged by Merge gate run {run.get('id')} "
            f"(attempt {run.get('run_attempt') or 1}, {state}, from {found_by}); {gate_text(str(check.get('name'))).format(pr=target)}")


def rerun_repairs(w: probes.World, target: int, kind: str) -> bool:
    """The merge tier is at main and the target is no release candidate (release/* keeps the first-attempt rule)."""
    gh = Gh(w.commands, repo=w.repo)  # type: ignore[arg-type]
    if kind == "run":
        data, why = gh.api(f"actions/runs/{target}")
        branch = str((data or {}).get("head_branch") or "") if not why else ""
    else:
        data, why = gh.api(f"pulls/{target}")
        branch = str(((data or {}).get("head") or {}).get("ref") or "") if not why else ""
    return bool(branch) and not branch.startswith("release/") and merge_tier.active_at_main(gh, w.settings)


def explain_ci(w: probes.World, target: int) -> CiWhy:
    known = known_registry(w)
    if target > RUN_ID_FLOOR:
        runs = [read_run(w, target, known=known)]
        tests = any(j.cause == "TEST" for r in runs for j in r.jobs)
        line, nxt, trap = decide(target, "run", runs, [], [], None, rerun_repairs=tests and rerun_repairs(w, target, "run"))
        return CiWhy(target=target, kind="run", cause=line, runs=runs, next=nxt, trap=trap)
    pr = explain(w, target)
    rollup, why = rollup_of(w, target)
    red_gates = [c for c in rollup if c.get("name") in rejudge.GATE_CHECKS and c.get("conclusion") in logs.FAILING]
    others = [c for c in rollup if c not in red_gates]  # a gate check's details URL names the Merge gate run, not a CI job
    by_run = logs.failing_runs(others)
    runs = [read_run(w, run, names, known) for run, names in by_run.items()]
    gate_run, found_by = rejudge.trace(w, [str(c.get("detailsUrl") or "") for c in red_gates], pr.head) if red_gates else (None, "")
    gates = [gate_line(c, target, gate_run, found_by) for c in red_gates]
    gates += [f"{c.get('name')}: {c.get('conclusion')}, posted by the check suite of {c.get('workflowName') or 'another workflow'}, not a job of the CI run"
              for c in logs.attached_checks(others)]
    pending = sorted({c.get("name") or "" for c in rollup if c.get("status") not in ("COMPLETED", None) and not c.get("conclusion")
                      and not (c.get("name") or "").lower().startswith("focused ")})
    tests = any(j.cause == "TEST" for r in runs for j in r.jobs)
    line, nxt, trap = decide(target, "pr", runs, gates, pending, pr, traced=gate_run is not None,
                             rerun_repairs=tests and rerun_repairs(w, target, "pr"))
    return CiWhy(target=target, kind="pr", cause=line, pull_request=pr, runs=runs, gates=gates, pending=pending, next=nxt, trap=trap, unknown=[why] if why else [])


# ------------------------------------------------------------------ commands
TARGET = typer.Argument(..., metavar="PR|RUN", help="A pull request number of the Core repository, or a run id (a number over 100000).")


@native("ci why", writes=False, replaces=("hz-ci-cause",), help=H(
    "Why did CI fail for this pull request or run? Cause line, failed jobs, failing tests, first error lines, next command.",
    "Use when a run or a pull request is red, before any rerun, push or builder message.",
    "nothing (read-only: GitHub reads of the pull request, its runs and their failed-step logs; the queue state and hold files).",
    "sm ci why 795            (a run: sm ci why 43301234567)",
    "the command on the NEXT line, after reading the TRAP line.",
    "Each failed job is classed DOWNLOAD (a pinned tool fetch failed), BROWSER_SETUP_TIMEOUT (the browser dependency\n"
    "install timed out), TEST or UNKNOWN. `[KNOWN bead]` marks a job or test in the reviewed registry of known failures.\n"
    "The aggregate job of the run repeats its real failures. The merge-gate workflow's checks are judged by their\n"
    "own check suite, not by a job of the CI run: they are listed on GATE lines and have no log; a red one is traced to the\n"
    "head's Merge gate run (sm ci merge-gate rejudge PR re-runs it after a reviewed Sonar closure). A cancelled job is a deferral or a superseded\n"
    "run, not a code failure. An UNKNOWN cause is never rerun."))
def ci_why(target: int = TARGET) -> CiWhy:
    return explain_ci(world(), target)


@native("ci faillog", writes=False, replaces=("hz-faillog",), help=H(
    "The failing part of the CI logs of a pull request or a run: per failing step the first errors and the last lines.",
    "Use after `sm ci why` named a TEST or UNKNOWN failure, to read the errors before the head changes.",
    "nothing (read-only: GitHub reads; nothing is written and nothing leaves this machine but those reads).",
    "sm ci faillog 795            (all failed-step lines of a run: sm ci faillog 43301234567 --raw)",
    "the `verify with:` command, once the cause is named; a fix otherwise.",
    "Full logs are never printed unless --raw is given; --file condenses a log you already have."))
def ci_faillog(target: int | None = typer.Argument(None, metavar="PR|RUN", help="A pull request number, or a run id (a number over 100000)."),
               file: Path | None = typer.Option(None, "--file", metavar="LOG", help="Condense this log file instead."),
               k: int = typer.Option(6, "-k", metavar="N", help="How many error places per run."),
               tail: int = typer.Option(12, "--tail", metavar="N", help="How many last lines per run."),
               raw: bool = typer.Option(False, "--raw", help="Print the failed-step log itself (a run id only)."),
               no_hint: bool = typer.Option(False, "--no-hint", help="Leave out the `verify with:` line.")) -> FailLog:
    w = world()
    if file is not None:
        try:
            return FailLog(target=str(file), lines=logs.condense(logs.clean(file.expanduser().read_text(errors="replace")), k, tail))
        except OSError as exc:
            raise Refuse(f"cannot read {file}: {exc}", "sm ci faillog PR|RUN") from None
    if target is None:
        raise Refuse("give a pull request number, a run id or --file LOG", "sm ci faillog 795")
    if raw:
        if target <= RUN_ID_FLOOR:
            raise Refuse("--raw needs a run id; the runs of a pull request are on the RUN lines of `sm ci why`", f"sm ci why {target}")
        text, why = failed_log(w, target)
        if why:
            raise Refuse(f"run {target}: {why}", f"sm ci why {target}")
        return FailLog(target=str(target), lines=text.splitlines())
    if target > RUN_ID_FLOOR:
        by_run, unknown = {str(target): ["run"]}, []
    else:
        rollup, why = rollup_of(w, target)
        by_run, unknown = logs.failing_runs(rollup), [why] if why else []
    if not by_run:
        return FailLog(target=str(target), lines=[f"#{target}: no failing check found" + (f" ({unknown[0]})" if unknown else "")])
    runs = []
    for run, names in by_run.items():
        text, why = failed_log(w, run)
        log = logs.condense(logs.clean(text), k, tail) if not why else []
        runs.append(RunFailure(run=int(run), checks=names, log=log, log_bytes=len(text.encode()), unknown=why))
    job = faillog.rerun_job([n for names in by_run.values() for n in names])
    hint = "" if no_hint else f"sm ci one rerun {target}" + (f' --job "{job}"' if job else "") + " --wait"
    return FailLog(target=str(target), runs=runs, next=hint, unknown=unknown)
