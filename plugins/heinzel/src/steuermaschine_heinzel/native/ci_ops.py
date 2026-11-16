"""CI write operations: `sm ci defer`, `sm ci deferred`, `sm ci rerun`, `sm ci quarantine ...`, `sm ci sonar-close`.

The logic is in `ci_ops_lib/` (moved from the former tools, one module each). This module is the edge: typed options,
the result models, and the binding of the library to the providers and settings of one call.

Other areas call the plain functions here inside their own command: `defer(...)`, `rerun(...)`, `deferred_ids()`,
`deferred_runs()`, `quarantine_digest(...)`, `quarantine_release_line()`.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import typer
from pydantic import BaseModel, Field

from steuermaschine import WAITING
from steuermaschine.engine import Refuse, Wait
from steuermaschine.github import GhError
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import Group, NativeRuntime, native, runtime
from steuermaschine.protocols import Busy, Providers

from . import rejudge as rejudge_lib
from .ci_ops_lib import defer as defer_lib
from .ci_ops_lib import deferred, legacy, sonar_close
from .ci_ops_lib import quarantine as quarantine_lib
from .ci_ops_lib import rerun as rerun_lib

JOURNAL = "heinzel.journal"  # the project's journal in the store, one document per day (the same kind other areas write)


class CiOpsSettings(BaseModel):
    """Settings of the CI write operations, a mixin of the plugin's table."""

    ci_ops_starved_minutes: int = Field(15, description="`ci defer --if-main-starved` acts when a queued main CI run has waited this many minutes.")
    ci_ops_legacy_deferred_file: Path | None = Field(None, description="The former list of deferred runs. Read-only, and only until a deferral or re-run here has written "
                                                                      "the list to the store; none: only the store's list.")
    ci_ops_queue_state_file: Path | None = Field(None, description="The merge queue's state file. Read-only: PRs queued for merge get their CI re-run first; none: no "
                                                                  "PR counts as queued.")
    ci_ops_sonar_closures_file: Path | None = Field(None, description="The reviewed manifest of Sonar closures: project, and one [[closure]] row per reviewed finding; "
                                                                     "none: `sm ci sonar-close` needs --config.")
    ci_ops_beads_dir: Path | None = Field(None, description="The directory the `bd` CLI runs in when a bead gets a note; none: `sm ci quarantine flag` needs a tracker provider.")
    ci_ops_bead_prefix: str = Field("", description="Prefix that turns the short bead id of a quarantine entry into the full id; an id that carries it already is kept.")
    ci_ops_rejudge_after_close: bool = Field(True, description="`ci sonar-close --pull-request N --apply` ends with `ci merge-gate rejudge N` once something was closed.")
    ci_ops_keep_file: Path | None = Field(None, description="PRs whose CI `sm ci defer` always leaves alone (fix PRs), one number per line, `#` starts a comment; "
                                                           "none: only --keep of the call.")


def setting(rt: NativeRuntime, name: str) -> Any:
    value = rt.settings.get(name) if rt.settings else None
    return CiOpsSettings.model_fields[name].default if value is None else value


def path_of(rt: NativeRuntime, name: str) -> Path | None:
    value = setting(rt, name)
    return Path(os.path.expanduser(str(value))) if value else None


def journal(rt: NativeRuntime, text: str) -> None:
    """One dated line in the project's journal (the store)."""
    now = datetime.fromtimestamp(providers_of(rt).clock.now()).astimezone()
    day = now.strftime("%Y-%m-%d")
    page = providers_of(rt).store.load(JOURNAL, day) or {"lines": []}
    page["lines"].append(f"{now.strftime('%Y-%m-%d %H:%M')} {text}")
    providers_of(rt).store.save(JOURNAL, day, page)


def keep_file_numbers(rt: NativeRuntime) -> list[str]:
    """The PR numbers of the keep file (setting ci_ops_keep_file); [] without the setting. Refuses an unreadable file."""
    path = path_of(rt, "ci_ops_keep_file")
    if path is None:
        return []
    try:
        text = path.read_text()
    except OSError as exc:
        raise Refuse(f"the keep file (setting ci_ops_keep_file) cannot be read: {exc.strerror or exc}", "sm self config", code=2) from None
    numbers = []
    for raw in text.splitlines():
        for word in raw.split("#", 1)[0].replace(",", " ").split():
            if not word.lstrip("#").isdigit():
                raise Refuse(f"the keep file (setting ci_ops_keep_file) has '{word}', not a PR number", "sm self config", code=2)
            numbers.append(word)
    return numbers


def providers_of(rt: NativeRuntime) -> Providers:
    return cast(Providers, rt.providers)


def repo_of(rt: NativeRuntime) -> str:
    repo = str(rt.settings.get("repo") or "")
    if not repo:
        raise Refuse("the setting `repo` (OWNER/NAME of the core repository) is empty", "sm self config")
    return repo


# ============================================================================= defer
class DeferredRun(Model):
    """One run on the list of deferred runs."""

    id: str
    branch: str
    workflow: str = ""


class Deferred(Model):
    """The runs that were cancelled to free runners and still wait for their re-run, oldest first."""

    runs: list[DeferredRun] = []

    def render(self) -> list[str]:
        return [f"{r.id} {r.branch} {r.workflow}".rstrip() for r in self.runs]


class DeferDecision(Model):
    """What was decided for one active run."""

    run: str
    branch: str
    workflow: str
    event: str = ""
    action: str  # cancelled | would_cancel | kept | spared | failed | not_queued
    reason: str = ""
    would_run: str = ""  # the cancel call, as it was or would be run

    def render(self) -> list[str]:
        return [defer_lib.line(self.model_dump())]


class Deferral(Model):
    """What `sm ci defer` cancelled, or would cancel, and what it left alone."""

    state: str  # cancelled | partial | failed | no_changes | dry_run | not_starved
    live: bool
    force_release: bool = False
    keep: list[str] = []  # the head branches of the PRs to keep
    main_queued_minutes: int | None = None  # with --if-main-starved: how long the oldest queued main run has waited
    starved_minutes: int | None = None  # with --if-main-starved: the threshold
    cancelled: int = 0
    would_cancel: int = 0
    failed: int = 0  # all unsuccessful attempts, including not_queued
    not_queued: int = 0
    runs: list[DeferDecision] = []
    notes: list[str] = []

    @classmethod
    def from_decisions(cls, rows: list[dict[str, Any]], live: bool, notes: list[str], **details: Any) -> Deferral:
        runs = [DeferDecision(**row) for row in rows]
        cancelled = sum(row.action == "cancelled" for row in runs)
        would_cancel = sum(row.action == "would_cancel" for row in runs)
        not_queued = sum(row.action == "not_queued" for row in runs)
        failed = sum(row.action == "failed" for row in runs) + not_queued
        if not live:
            state = "dry_run"
        elif failed:
            state = "partial" if cancelled else "failed"
        else:
            state = "cancelled" if cancelled else "no_changes"
        return cls(state=state, live=live, cancelled=cancelled, would_cancel=would_cancel, failed=failed, not_queued=not_queued,
                   runs=runs, notes=notes, **details)

    @property
    def exit_code(self) -> int:
        return int(self.failed > 0)

    def render(self) -> list[str]:
        lines = self.notes + [line for row in self.runs for line in row.render()]
        if self.state == "dry_run":
            lines.append(f"dry run: nothing was cancelled or recorded; `sm ci defer --cancel` cancels the {self.would_cancel} run(s) marked `would cancel`")
        if self.failed:
            lines.append(f"Cancelled {self.cancelled}. Failed {self.failed} ({self.not_queued} GitHub reported as not queued).")
        return lines


def parent_command(rt: NativeRuntime) -> str:
    """The command line of the process that started this one, for the audit log."""
    return providers_of(rt).commands.run(["ps", "-o", "command=", "-p", str(os.getppid())], timeout=10).out.strip()


def defer(cancel: bool = False, if_main_starved: bool = False, minutes: int | None = None, keep: str | None = None, force_release: bool = False,
          supersede_main: bool = False) -> Deferral:
    """The body of `sm ci defer`; the release train calls it with `cancel=True, force_release=True`."""
    rt = runtime()
    help_cmd = "sm ci defer --help"
    if supersede_main and (if_main_starved or force_release or keep is not None or minutes is not None):
        raise Refuse("--supersede-main stands alone (with --cancel or as a dry run)", help_cmd, code=2)
    if force_release and if_main_starved:
        raise Refuse("--force-release cannot be combined with --if-main-starved", help_cmd, code=2)
    if minutes is not None and not if_main_starved:
        raise Refuse("--minutes only with --if-main-starved", help_cmd, code=2)
    if keep is not None and not keep.replace(",", " ").split():
        raise Refuse("--keep needs PR[,PR...]", help_cmd, code=2)
    asked: list[str] = []

    def caller() -> str:  # asked once per command, and only when something is attempted
        if not asked:
            asked.append(parent_command(rt))
        return asked[0]

    edge = defer_lib.Edge(commands=providers_of(rt).commands, clock=providers_of(rt).clock, store=providers_of(rt).store, repo=repo_of(rt),
                          legacy_file=path_of(rt, "ci_ops_legacy_deferred_file"), caller=caller)
    threshold = int(minutes if minutes is not None else setting(rt, "ci_ops_starved_minutes")) if if_main_starved else None
    notes: list[str] = []
    waited = None

    from_file = [] if supersede_main else keep_file_numbers(rt)
    typed = keep.replace(",", " ").split() if keep else []

    def work() -> Deferral:
        nonlocal waited
        if supersede_main:
            rows = defer_lib.supersede_main(edge, cancel)
            result = Deferral.from_decisions(rows, cancel, notes)
            if not result.cancelled + result.would_cancel + result.failed:
                result.notes.append(f"nothing is superseded. The newest {defer_lib.MAIN_KEEP} queued main runs are kept")
            return result
        kept = defer_lib.keep_branches(edge, keep) if keep else []
        for number in from_file:
            if number in typed:
                continue
            try:
                kept += defer_lib.keep_branches(edge, number)
            except defer_lib.Stop as no:
                notes.append(f"keep file: {no.message.removeprefix('refusing: ')}; not kept")
        if if_main_starved:
            waited = defer_lib.main_queued_minutes(edge)
            if waited is None or waited < (threshold or 0):
                notes.append("no queued main CI" if waited is None else f"main CI queued {waited} min < {threshold}: leave PR CI alone")
                return Deferral(state="not_starved", live=cancel, keep=kept, main_queued_minutes=waited, starved_minutes=threshold, notes=notes)
            notes.append(f"main CI queued {waited} min >= {threshold}: {'cancelling' if cancel else 'would cancel'} PR CI (never main, release/* or bump/*)")
        rows = defer_lib.cancel(edge, kept, force_release, cancel, "--if-main-starved" if if_main_starved else "--cancel")
        return Deferral.from_decisions(rows, cancel, notes, force_release=force_release, keep=kept, main_queued_minutes=waited,
                                       starved_minutes=threshold)

    try:
        if not cancel:
            return work()
        with providers_of(rt).store.lock(deferred.LOCK):
            return work()
    except defer_lib.Stop as no:
        raise Refuse(no.message, help_cmd if no.code == 2 else "sm ci defer", code=no.code) from None
    except Busy:
        raise Wait("another deferral or re-run is changing the list of deferred runs", "that command") from None


def deferred_runs() -> list[dict[str, str]]:
    """The deferred runs, oldest first: [{"id", "branch", "workflow"}]. For other areas."""
    rt = runtime()
    return deferred.runs(providers_of(rt).store, path_of(rt, "ci_ops_legacy_deferred_file"))


def deferred_ids() -> set[str]:
    """The ids of the deferred runs. For the merge queue: a cancelled check of one of them is not a failure."""
    rt = runtime()
    return deferred.ids(providers_of(rt).store, path_of(rt, "ci_ops_legacy_deferred_file"))


@native("ci defer", writes=True, replaces=("hz-ci-defer",), help=H(
    "Free hosted runners: cancel queued and running CI of PR branches and record each run for its re-run.",
    "Use when main CI or a release candidate waits behind PR runs at the 20-job ceiling; never to stop a red run.",
    "nothing without --cancel (a dry run); with --cancel it cancels the allowlisted PR runs and records them in the list of deferred runs.",
    "sm ci defer --cancel --if-main-starved --keep 603,598",
    "sm ci rerun   (when main CI runs again)",
    "Only an allowlist is cancelled: pull-request runs of CI, CodeQL, Claude review, Merge gate and Docs (Merge gate also when\n"
    "triggered by workflow_run on the head branch of an open PR). Never main, release/*, bump/*, a tag, a workflow_dispatch,\n"
    "schedule or push run, or a run younger than 2 minutes. A run with a started macOS job is spared; a run whose jobs\n"
    "cannot be read is spared. --keep refuses (exit 2) an unknown or non-open PR before anything is cancelled.\n"
    "--keep holds for this call only; PRs that every call keeps go in the keep file (setting ci_ops_keep_file), where a closed\n"
    "or unknown PR is skipped with a note.\n"
    "--if-main-starved does nothing unless a queued main CI run has waited --minutes (default 15).\n"
    "--force-release is for the release train only: it also cancels runs with started macOS jobs. Every attempt is logged.\n"
    "--supersede-main: when more than two push runs of main CI are queued, cancel the older never-started ones and keep the newest\n"
    "two (main's tip is what gates); never a run in progress, never release/*; not recorded for a re-run; logged.\n"
    "Exit 1 when any attempt fails, including when GitHub reports that a run has not been queued yet."))
def ci_defer(cancel: bool = typer.Option(False, "--cancel", help="Cancel for real. Without it nothing is cancelled: the command shows what it would do."),
             if_main_starved: bool = typer.Option(False, "--if-main-starved", help="Act only when a queued main CI run has waited --minutes or longer."),
             minutes: int | None = typer.Option(None, "--minutes", metavar="MIN", min=0, help="The wait that counts as starved (default 15); needs --if-main-starved."),
             keep: str | None = typer.Option(None, "--keep", metavar="PR[,PR...]", help="Leave the runs of these PRs' head branches alone (fix PRs get the runners first)."),
             force_release: bool = typer.Option(False, "--force-release", help="Release train only: also cancel runs whose macOS jobs have started."),
             supersede_main: bool = typer.Option(False, "--supersede-main", help="Cancel queued main CI runs older than the newest two (never started, never release/*).")) -> Deferral:
    return defer(cancel, if_main_starved, minutes, keep, force_release, supersede_main)


@native("ci deferred", writes=False, replaces=("hz-ci-defer",), help=H(
    "The runs `sm ci defer` cancelled that still wait for their re-run: id, branch, workflow.",
    "Use before `sm ci rerun`, or when a PR shows cancelled checks and you want to know whether that was a deferral.",
    "nothing (read-only).",
    "sm ci deferred",
    "sm ci rerun --dry-run"))
def ci_deferred() -> Deferred:
    return Deferred(runs=[DeferredRun(**row) for row in deferred_runs()])


# ============================================================================= rerun
class RerunRun(Model):
    id: str
    workflow: str = ""
    state: str = ""  # rerun | would_rerun | failed


class RerunPick(Model):
    """One PR (or, with --all, one recorded run) whose cancelled CI is re-run."""

    pr: int | None = None
    branch: str
    why: str  # queued-for-merge | oldest | all
    runs: list[RerunRun] = []


class RerunSkip(Model):
    what: str  # "#12", or a branch without an open PR
    why: str


class Rerun(Model):
    """What `sm ci rerun` re-ran, or would re-run, and what stays on the list."""

    state: str  # empty | waiting | planned | done
    live: bool
    all_runs: bool = False
    capacity: int | None = None  # jobs queued or in progress on the hosted runners, when it was measured
    waiting_on: str = ""
    picked: list[RerunPick] = []
    skipped: list[RerunSkip] = []
    rerun: list[str] = []  # the run ids whose re-run was started
    failed: list[str] = []  # the run ids whose re-run GitHub refused
    lines: list[str] = []

    @property
    def exit_code(self) -> int:
        return WAITING if self.state == "waiting" else 0

    def render(self) -> list[str]:
        return self.lines


def rerun(n: int | None = None, dry_run: bool = False, all_runs: bool = False, force: bool = False) -> Rerun:
    """The body of `sm ci rerun`; the release train calls it with `all_runs=True` after a tag."""
    rt = runtime()
    if force and not all_runs:
        raise Refuse("--force only with --all", "sm ci rerun --help", code=2)
    if n is not None and all_runs:
        raise Refuse("--n does not apply to --all", "sm ci rerun --help", code=2)
    lines: list[str] = []
    edge = rerun_lib.Edge(commands=providers_of(rt).commands, clock=providers_of(rt).clock, store=providers_of(rt).store, repo=repo_of(rt),
                          legacy_file=path_of(rt, "ci_ops_legacy_deferred_file"), queue_items=lambda: legacy.queue_items(path_of(rt, "ci_ops_queue_state_file")),
                          out=lambda text: lines.extend(text.split("\n")), journal=lambda text: journal(rt, text))

    def work() -> None:
        if all_runs:
            rerun_lib.resume_all(edge, force, dry_run)
        else:
            rerun_lib.batch(edge, n, dry_run)

    try:
        if dry_run:
            work()
        else:
            with providers_of(rt).store.lock(deferred.LOCK):
                work()
    except rerun_lib.Stop as no:
        raise Refuse(str(no).removeprefix("refusing: "), "sm ci rerun --dry-run") from None
    except GhError as no:
        raise Refuse(f"cannot read GitHub: {no}", "sm ci rerun --dry-run") from None
    except Busy:
        raise Wait("another deferral or re-run is changing the list of deferred runs", "that command") from None
    found = edge.result
    done, failed = [str(i) for i in found.get("done", [])], [str(i) for i in found.get("failed", [])]

    def state_of(run_id: str) -> str:
        return "would_rerun" if dry_run else "rerun" if run_id in done else "failed" if run_id in failed else ""

    picked = [RerunPick(pr=p["pr"], branch=p["branch"], why=p["why"], runs=[RerunRun(id=str(r["id"]), workflow=r["workflow"], state=state_of(str(r["id"])))
                                                                           for r in p["runs"]]) for p in found.get("picked", [])]
    return Rerun(state=found.get("state", "empty"), live=not dry_run, all_runs=all_runs, capacity=found.get("capacity"), waiting_on=found.get("waiting_on", ""),
                 picked=picked, skipped=[RerunSkip(**s) for s in found.get("skipped", [])], rerun=done, failed=failed, lines=lines)


@native("ci rerun", writes=True, replaces=("hz-rerun-batch",), help=H(
    "Re-run the PR CI that `sm ci defer` cancelled: a few PRs at a time, queued-for-merge first, then oldest.",
    "Use after main CI is running again, from the hourly tick, until `sm ci deferred` is empty.",
    "starts a new attempt of the recorded runs of the picked PRs and removes them from the list of deferred runs; --dry-run changes nothing.",
    "sm ci rerun --dry-run",
    "sm ci deferred   (what still waits)",
    "As many PRs as fit the free hosted capacity (about 14 of the 20 jobs, 4 jobs per PR); --n N caps the PRs in flight instead.\n"
    "Exit 75 and nothing re-run while a release/* run is active, main CI is queued, a main run has queued jobs, or more than 2 macOS\n"
    "jobs are busy. PRs with real failures, drafts and closed PRs are skipped. A run with a green job is re-run with --failed only.\n"
    "--all re-runs EVERY recorded run at once and empties the list (after a release); it refuses while a release/* run is\n"
    "active unless --force. A re-run is never retried by itself."))
def ci_rerun(n: int | None = typer.Option(None, "--n", metavar="N", min=0, help="Cap on PRs with CI in flight (default: adaptive from the free runner capacity)."),
             dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan, change nothing."),
             all_runs: bool = typer.Option(False, "--all", help="Re-run every recorded run and empty the list."),
             force: bool = typer.Option(False, "--force", help="With --all: although a release/* run is active.")) -> Rerun:
    return rerun(n, dry_run, all_runs, force)


# ============================================================================= quarantine
class QuarantineEntry(Model):
    """One entry of the quarantine registry on main (tests/quarantine.toml of the core repository)."""

    test: str
    bead: str
    added: str
    until: str
    reason: str = ""
    age_days: int
    days_left: int
    last_lane: str = ""  # the newest lane result per OS, when a report exists


class QuarantineList(Model):
    """The quarantine registry as read from main, in one of three views."""

    view: str  # status | line | digest
    code: int = 0
    max_entries: int = quarantine_lib.MAX_ENTRIES
    entries: list[QuarantineEntry] = []
    lines: list[str] = []

    @property
    def exit_code(self) -> int:
        return self.code

    def render(self) -> list[str]:
        return self.lines


class BeadNote(Model):
    bead: str
    test: str
    day: str


class QuarantineFlag(Model):
    """The digest lines, and the bead notes written (or, in a dry run, due) for tests failing three days in a row."""

    live: bool
    lines: list[str] = []
    notes: list[BeadNote] = []
    failed: list[str] = []  # notes the tracker refused (bead, test, day, what it said), and a registry that could not be read

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0

    def render(self) -> list[str]:
        word = "bead note written" if self.live else "bead note due (dry run, nothing written)"
        return self.lines + [f"{word}: {note.bead} {note.test}" for note in self.notes] + [f"bead note NOT written: {row}" for row in self.failed
                                                                                          if not row.startswith("QUARANTINE-")]


def bind_quarantine(rt: NativeRuntime) -> None:
    quarantine_lib.bind(quarantine_lib.Edge(commands=providers_of(rt).commands, clock=providers_of(rt).clock, store=providers_of(rt).store, tracker=providers_of(rt).tracker,
                                            repo=repo_of(rt), beads_dir=path_of(rt, "ci_ops_beads_dir"), bead_prefix=str(setting(rt, "ci_ops_bead_prefix"))))


def quarantine_digest() -> list[str]:
    """The quarantine digest lines, for a read command's digest. Never writes a note: notes are `sm ci quarantine flag` only."""
    bind_quarantine(runtime())
    return quarantine_lib.digest(flag=False)[0]


def quarantine_release_line() -> str:
    """One line for the release status: count and ages from main's registry only."""
    bind_quarantine(runtime())
    return quarantine_lib.release_line()


CI_QUARANTINE = Group("ci quarantine", H(
    "ci quarantine: the quarantine lane of the core repository, seen from the coordinator's side.",
    "Use when a quarantined test nears its date or keeps failing in the lane.",
    "list only reads; flag writes one note a day on the bead of a test that failed three days in a row.",
    "sm ci quarantine list",
    "sm ci quarantine flag --dry-run",
    "The registry is tests/quarantine.toml on main of the core repository. An entry is added or removed there, by a\n"
    "reviewed pull request; no command here changes it."))


@native("ci quarantine list", writes=False, replaces=("hz-quarantine",), help=H(
    "Every quarantined test on main: bead, age, days left and the last lane result per OS.",
    "Use when CI names the `Quarantine registry` job, or before a release, to see what expires.",
    "nothing outside this machine; remembers the lane reports of main's CI runs in the store.",
    "sm ci quarantine list",
    "sm ci quarantine flag --dry-run",
    "--line prints one line (the count and ages); --digest prints the anomaly lines: QUARANTINE n/8, QUARANTINE-EXPIRING (at most\n"
    "1 day left), QUARANTINE-OVERDUE (the required `Quarantine registry` job fails now), QUARANTINE-STREAK (failed 3+ days in a row).\n"
    "Exit 1 when the registry on main cannot be parsed."))
def ci_quarantine_list(line: bool = typer.Option(False, "--line", help="One line: the count and ages."),
                       digest: bool = typer.Option(False, "--digest", help="The anomaly lines of the digest (writes no bead note).")) -> QuarantineList:
    rt = runtime()
    bind_quarantine(rt)
    if line or digest:
        lines = quarantine_lib.digest_lines()
        bad = bool(lines) and lines[0].startswith(("QUARANTINE-INVALID", "QUARANTINE-UNREADABLE"))
        return QuarantineList(view="line" if line else "digest", code=1 if bad else 0, lines=lines[:1] if line else lines)
    code, lines, entries, last, today = quarantine_lib.status()
    rows = [QuarantineEntry(test=str(e["test"]), bead=str(e["bead"]), added=str(e["added"]), until=str(e["until"]), reason=str(e["reason"] or ""),
                            age_days=(today - e["added"]).days, days_left=(e["until"] - today).days, last_lane=last.get(e["test"], "")) for e in entries]
    return QuarantineList(view="status", code=code, entries=rows, lines=lines)


@native("ci quarantine flag", writes=True, replaces=("hz-quarantine",), help=H(
    "The quarantine digest, and one note on the bead of every test that failed in the lane three days in a row.",
    "Use at every tick, as part of the digest; a bead and test are noted at most once a day.",
    "appends a note to the bead of each streaking test (tracker) and remembers it for the day; --dry-run writes nothing.",
    "sm ci quarantine flag --dry-run",
    "fix the cause of the test the QUARANTINE-STREAK line names, before its `until` date",
    "A note the tracker refuses is reported as NOT written (exit 1), is not remembered and is tried again at the next call.\n"
    "A full bead id is used as it is; a short one gets the setting ci_ops_bead_prefix."))
def ci_quarantine_flag(dry_run: bool = typer.Option(False, "--dry-run", help="Print the digest and the notes that are due, write nothing.")) -> QuarantineFlag:
    rt = runtime()
    if not dry_run and providers_of(rt).tracker is None and path_of(rt, "ci_ops_beads_dir") is None:
        raise Refuse("no tracker provider and no setting ci_ops_beads_dir: there is nowhere to write a bead note", "sm ci quarantine flag --dry-run")
    bind_quarantine(rt)
    failed: list[str] = []
    lines, noted = quarantine_lib.digest(flag=True, dry=dry_run, failed=failed)
    notes = []
    for key in noted:
        bead, _, rest = key.partition(" ")
        test, _, day = rest.rpartition(" ")
        notes.append(BeadNote(bead=bead, test=test, day=day))
    return QuarantineFlag(live=not dry_run, lines=lines, notes=notes, failed=failed)


# ============================================================================= sonar
class Closure(Model):
    """One reviewed finding and what happened to it."""

    issue: str
    file: str
    line: int | None = None
    rule: str
    transition: str
    reason: str
    state: str  # closed | would_close | commented | planned


class SonarClose(Model):
    """What `sm ci sonar-close` closed in Sonar, or would close."""

    live: bool
    project: str
    pull_request: int | None = None
    open_before: int
    open_after: int | None = None
    closures: list[Closure] = []
    calls: list[str] = []  # the write calls, in order: made when live, due otherwise
    lines: list[str] = []
    rejudge: list[str] = []  # after a live closure on a PR: what `ci merge-gate rejudge` did, or why it did not run
    rejudge_failed: bool = False  # the re-judge raised: the closures above stand, the command exits 1

    @property
    def exit_code(self) -> int:
        return 1 if self.rejudge_failed else 0

    def render(self) -> list[str]:
        return self.lines + self.rejudge


def closures_of(edge: sonar_close.Edge, live: bool) -> tuple[list[Closure], list[str]]:
    written = set(edge.written)
    rows, calls = [], []
    for row, issue in edge.plan:
        key, transition = str(issue["key"]), str(row.get("transition", "falsepositive"))
        state = "would_close" if not live else "closed" if (key, "transition") in written else "commented" if (key, "comment") in written else "planned"
        line = issue.get("line")
        rows.append(Closure(issue=key, file=str(row["file"]), line=line if isinstance(line, int) else None, rule=str(row["rule"]), transition=transition,
                            reason=str(row["reason"]), state=state))
        calls += [f"sonar api post /api/issues/add_comment (issue {key}, the reviewed reason)", f"sonar api post /api/issues/do_transition (issue {key}, {transition})"]
    return rows, calls


def rejudge_after(rt: NativeRuntime, pr: int) -> tuple[list[str], bool]:
    """`ci merge-gate rejudge PR` after a closure: (its lines or why it did not run, whether it failed). A refusal or a
    wait never fails the closure; any other error is reported after the closures and fails the command."""
    if not setting(rt, "ci_ops_rejudge_after_close"):
        return [f"re-judge: off (setting ci_ops_rejudge_after_close); run sm ci merge-gate rejudge {pr}"], False
    try:
        return [f"re-judge: {line}" for line in rejudge_lib.rejudge(pr).render()], False
    except Wait as later:
        return [f"re-judge: not yet: {later.message}; run sm ci merge-gate rejudge {pr} again"], False
    except Refuse as no:
        return [f"re-judge: not run: {no.message}", f"re-judge: NEXT {no.next_cmd or f'sm ci merge-gate rejudge {pr}'}"], False
    except Exception as error:  # the closures are written: report them, then fail
        return [f"re-judge: FAILED: {type(error).__name__}: {error}", f"re-judge: NEXT sm ci merge-gate rejudge {pr}"], True


@native("ci sonar-close", writes=True, replaces=("hz-sonar-close",), help=H(
    "Close reviewed Sonar findings with their written reason: main's file/rule rows, or exact reviewed issues of one PR.",
    "Use after a finding was reviewed as a false positive or accepted and its row is in the reviewed manifest.",
    "nothing without --apply (a preview); with --apply it comments the reason on each planned issue and transitions it in Sonar.",
    "sm ci sonar-close --pull-request 485 --issue AZexample1",
    "sm ci sonar-close --pull-request 485 --issue AZexample1 --apply   (after reading the preview)",
    "Only bugs and vulnerabilities; a code smell needs a new head. The manifest (setting ci_ops_sonar_closures_file, or --config)\n"
    "names the project and one [[closure]] row per finding: file, rule, reason, and for a PR issue also pull_request, issue,\n"
    "the full rule (python:S5332) and transition (falsepositive or accept). The whole plan is checked before the first write:\n"
    "every requested key must be open, in that PR, in that project, file and rule. Any mismatch refuses (exit 1) with no write.\n"
    "The `sonar` CLI must be signed in; no token passes through this command. After a live closure on a PR it runs\n"
    "`sm ci merge-gate rejudge PR` (setting ci_ops_rejudge_after_close): the gate checks are judged again only then."))
def ci_sonar_close(config: Path | None = typer.Option(None, "--config", metavar="FILE", help="Another reviewed manifest (default: setting ci_ops_sonar_closures_file)."),
                   apply: bool = typer.Option(False, "--apply", help="Change Sonar. Without it the command only shows what it would close."),
                   pull_request: int | None = typer.Option(None, "--pull-request", metavar="NUMBER", help="Close exact reviewed issues of this pull request."),
                   issue: list[str] | None = typer.Option(None, "--issue", metavar="KEY", help="An exact issue key (repeatable); needs --pull-request.")) -> SonarClose:
    rt = runtime()
    lines: list[str] = []
    edge = sonar_close.Edge(commands=providers_of(rt).commands, out=lines.append)
    sonar_close.bind(edge)
    manifest = config or path_of(rt, "ci_ops_sonar_closures_file")
    if manifest is None:
        raise Refuse("no reviewed manifest: set ci_ops_sonar_closures_file or pass --config", "sm ci sonar-close --help", code=2)
    args = SimpleNamespace(config=manifest, apply=apply, pull_request=pull_request, issue=list(issue or []))
    try:
        sonar_close.close_reviewed(args)
    except sonar_close.REFUSALS as exc:
        done = f" (already written to Sonar, in this order: {', '.join(f'{step} on {key}' for key, step in edge.written)})" if edge.written else ""
        raise Refuse(f"{exc}{done}", "sm ci sonar-close --help") from None
    rows, calls = closures_of(edge, apply)
    closed = any(row.state == "closed" for row in rows)
    judged, failed = rejudge_after(rt, pull_request) if apply and closed and pull_request else ([], False)
    return SonarClose(live=apply, project=edge.project, pull_request=pull_request, open_before=edge.open_before or 0, open_after=edge.open_after,
                      closures=rows, calls=calls, lines=lines, rejudge=judged, rejudge_failed=failed)
