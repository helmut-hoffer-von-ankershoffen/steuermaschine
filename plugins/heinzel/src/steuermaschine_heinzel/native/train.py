"""The release train: the dispatcher tick a service manager calls, the detached runner of one release, status, clear,
the N+1 batch view at the cut (next), the candidate table (plan) and the selective resume of deferred CI after a tag.

The logic is the moved train in `train_lib/` (see `port/move.py verify`): its conditions for a cut, its state files
and locks, its phases are unchanged, and so are the files in its state directory (one JSON file, lock and log per
version, the cadence file train.json, the batch records next-vX.json). One release is `sm release resume X --intro ...
--snapshot-acceptance --candidate-gate` as a child process whose lines the train reads, as before. Nothing here starts
an old script.

The moved code reads settings, not the environment (state directory, checkout, cadence, gate hold, resume); the old
variables (HZ_TRAIN_STATE_DIR, HZ_TRAIN_CHECKOUT, HZ_TRAIN_EVERY ...) still set a setting that nothing else sets, and
`sm self config` names them as its source. A setting from a file wins; when its old variable is set and differs, the
command says so on stderr. The same holds for HZ_RELEASE_DIR: a release of the train is prepared in the train's
checkout, which the child gets as the setting release_dir.

Review unit of the write commands here: this file, `train_lib/`, `queue_lib/` (the main gate, the freeze and the flow
flag are read through the queue's shared library) and `release_lib/` (the edge, the wording of old names).
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, Field

from steuermaschine.engine import Refuse, Wait
from steuermaschine.github import Gh
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime
from steuermaschine.plugins import old_variable
from steuermaschine.protocols import Busy

from .. import environment
from .journal_view import mirrored
from .queue_lib import edge as base
from .queue_lib import hzlib
from .train_lib import edge, legacy, resume_ci
from .train_lib.names import translate

VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
DEFERRED = "heinzel.ci_ops.deferred"  # the list of deferred runs `sm ci defer` records (key `runs`) and `sm ci deferred` prints


class TrainSettings(BaseModel):
    """Settings of the release train (mixin of the plugin's table)."""

    release_train_checkout: Path | None = Field(None, description="The dedicated clean checkout in which the train prepares release PRs; passed to the step runner as its "
                                                                   "release_dir. Default: ~/Code/heinzel-release-train.", json_schema_extra=old_variable("HZ_TRAIN_CHECKOUT"))
    release_train_every_minutes: int | None = Field(None, description="Opt-in floor in minutes between two cuts; unset or 0: the train paces itself.",
                                                    json_schema_extra=old_variable("HZ_TRAIN_EVERY"))
    release_train_gate_hold: bool = Field(False, description="Hold the local run lock while a candidate runs (off: builders keep working; hosted CI does not compete "
                                                             "with local heavy runs).", json_schema_extra=old_variable("HZ_TRAIN_GATE_HOLD"))
    release_train_resume_all: bool = Field(False, description="After a tag, re-run every deferred CI run (`sm ci rerun --all`) instead of only those of open queued "
                                                              "and combine PRs.", json_schema_extra=old_variable("HZ_TRAIN_RESUME_ALL"))
    release_train_auto_cut: bool = Field(False, description="Let a tick cut the next release by itself. Off: ticks drive the active trains to LIVE-OK, and only "
                                                            "`sm daemon train --now` cuts.")
    release_train_notify: str | None = Field(None, description="A command that pushes a text to the owner (the text is its one argument); the alarm's fallback "
                                                               "when alarm_notify is unset.", json_schema_extra=old_variable("HZ_TRAIN_NOTIFY"))


def conf() -> dict:
    values = runtime().settings
    values = values.of("heinzel") if hasattr(values, "of") else values
    return values if isinstance(values, dict) else dict(values)


# setting -> (its old variable, the module global it sets, how a value is read)
BOUND: dict[str, tuple[str, str, Any]] = {
    "release_train_state_dir": ("HZ_TRAIN_STATE_DIR", "STATE_DIR", lambda v: Path(str(v)).expanduser()),
    "release_train_checkout": ("HZ_TRAIN_CHECKOUT", "CHECKOUT", lambda v: Path(str(v)).expanduser()),
    "release_train_every_minutes": ("HZ_TRAIN_EVERY", "EVERY", int),
}


def differs(raw: str | None, value: object, read: Any) -> bool:
    """An old variable is set and names something else than `value`."""
    if not raw:
        return False
    try:
        return bool(read(raw) != value)
    except ValueError:
        return True


def train(warn: bool = False) -> Any:
    """A fresh copy of the moved train, bound to the settings: state directory, checkout, cadence. A setting from a
    file wins over its old variable; `warn`: say on stderr when such a variable is set and differs, and when
    HZ_RELEASE_DIR differs from the train's checkout (a release of the train is never prepared there)."""
    values = conf()
    base.configure(values.get("queue_records_dir"))
    module = base.fresh(f"{base.PACKAGE}.train_lib.train")
    old = environment.state_overrides()
    for key, (variable, name, read) in BOUND.items():
        if values.get(key) is None:
            continue
        setattr(module, name, read(values[key]))
        if warn and differs(raw := old.named(variable), getattr(module, name), read):
            runtime().say(f"warning: {variable}={raw} is ignored; the setting heinzel.{key} ({getattr(module, name)}) wins")
    if warn and differs(old.release_dir, module.CHECKOUT, lambda v: Path(v).expanduser()):
        runtime().say(f"warning: HZ_RELEASE_DIR={old.release_dir} is ignored; a release of the train is prepared in the "
                      f"train's checkout {module.CHECKOUT} (setting heinzel.release_train_checkout), passed to it as its release_dir")
    return module


class Lines(io.TextIOBase):
    """Standard output of the moved train: every finished line is shown at once (a runner lives for an hour) and kept."""

    def __init__(self, show, state_dir=None):
        self.show, self.lines, self.rest, self.state_dir = show, [], "", state_dir

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        *done, self.rest = (self.rest + text).split("\n")
        for line in done:
            self.lines.append(translate(line, self.state_dir))
            self.show(self.lines[-1])
        return len(text)


def capture(call, state_dir=None, live: bool = False) -> tuple[int, list[str]]:
    """Run moved code in process: (exit code, its lines, worded for this program). `live`: show each line as printed."""
    say, shown_on = runtime().say, sys.stderr

    def show(text: str) -> None:
        if live:
            with contextlib.redirect_stderr(shown_on):
                say(text)

    out = Lines(show, state_dir)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = int(call() or 0)
    if out.rest:
        out.write("\n")
    return code, out.lines


def run(module, argv: list[str], live: bool = False) -> tuple[int, list[str]]:
    """Run the moved command line of the train in process: (exit code, its lines)."""
    return capture(lambda: module.main(argv), module.STATE_DIR, live)


# ----------------------------------------------------------------------------- the tick and the runner
class TrainTick(Model):
    """One tick of the release train's dispatcher, or the run of one release."""

    action: str = Field(description="tick: start idle runners, then decide on a cut. run: take one release as far as it goes.")
    version: str = ""
    exit: int = Field(description="0 done or nothing to do, 1 a step failed (the next tick resumes), 3 blocked or refused.")
    lines: list[str] = []

    def render(self) -> list[str]:
        return [] if self.action == "run" else self.lines  # a runner's lines were shown as they happened

    @property
    def exit_code(self) -> int:
        return self.exit


@native("daemon train", writes=True, tick=True, replaces=("hz-train",), help=H(
    "One tick of the release train: start a runner for every active release that has none, then cut when every condition holds.",
    "Use for the launchd job (every 10 minutes); by hand for a single tick only while that job is stopped.",
    "may start detached release runners (`--run`), which freeze merges, merge the release PR, tag and publish; writes the train's state, logs and notes.",
    "sm daemon train", "sm release train status",
    "A cut needs: no train before its tag or blocked, no foreign unfinished release, the previous tail at least at its\n"
    "acceptance dispatch, no open release PR, no merge freeze, releasable commits on main, the main gate ok, and\n"
    "auto-cut on (setting release_train_auto_cut; off by default) or --now. A runner takes its release to LIVE-OK:\n"
    "`sm release resume X --live`, then `sm mini update --minimum X` every tick until the Mini proves it.\n"
    "--run X.Y.Z is the detached runner of one release (started by the tick; it takes that version's lock itself).\n"
    "Exit 0: done or nothing to do. 1: a step failed, the next tick resumes. 3: blocked or refused."))
@mirrored  # it runs a program that writes the working files: the subject journals follow (journal_view.py)
def daemon_train(dry_run: bool = typer.Option(False, "--dry-run", help="Say what the tick would do; start and write nothing."),
                 now: bool = typer.Option(False, "--now", help="Cut in this tick even with auto-cut off; skips the cadence floor. Every other condition still holds."),
                 run_version: str | None = typer.Option(None, "--run", metavar="X.Y.Z", help="Run one release under its own lock (what the tick starts detached).")) -> TrainTick:
    if run_version is not None:
        if not VERSION.fullmatch(run_version) or dry_run or now:
            raise Refuse("--run takes one exact version X.Y.Z and no other option", "sm daemon train --help", code=2)
        code, lines = run(train(warn=True), ["run", run_version], live=True)
        return TrainTick(action="run", version=run_version, exit=code, lines=lines)
    code, lines = run(train(warn=True), ["tick", *(["--dry-run"] if dry_run else []), *(["--now"] if now else [])])
    return TrainTick(action="tick", exit=code, lines=lines)


# ----------------------------------------------------------------------------- status, clear
class TrainRow(Model):
    version: str
    phase: str
    running: bool = Field(description="A runner holds this version's lock.")
    attempts: int = 0
    reason: str = ""


class TrainStatus(Model):
    """The active trains, the release tails and the candidate receipt."""

    trains: list[TrainRow] = []
    tails: list[str] = Field([], description="One line per release past its tag: its verdict and acceptance run.")
    candidate: str = Field("", description="The version of an unfinished release receipt before its handoff.")
    state: dict = Field({}, description="The oldest active train's state file, else the cadence file, as it is on disk.")
    lines: list[str] = []

    def render(self) -> list[str]:
        return self.lines


@native("release train status", replaces=("hz-train",), help=H(
    "The release train: every active train with its phase, every release tail with its verdict, the oldest train's state.",
    "Use when a release should have been cut or does not move.",
    "nothing (read-only).",
    "sm release train status", "sm release status   (the candidate's PR and CI)"))
def release_train_status() -> TrainStatus:
    from . import train_status

    return train_status.status(train())


class TrainCleared(Model):
    """A blocked or stuck train, marked cleared so that the next tick may cut again."""

    version: str = ""
    cleared: bool
    exit: int
    lines: list[str] = []

    def render(self) -> list[str]:
        return self.lines

    @property
    def exit_code(self) -> int:
        return self.exit


def clear_refusal(module: Any, version: str) -> str:
    """Why train `version` must not be cleared now, or "". A clear abandons what the train still owns, so it is refused
    while that version's candidate lives: a run of its release/vX or bump/vX branch, an open PR of that branch, or the
    release receipt naming that version unfinished. Any read that fails refuses too (fail closed)."""
    rt = runtime()
    repo = str(conf().get("repo") or hzlib.CORE)
    client, branches = Gh(rt.providers.commands, repo=repo, clock=rt.providers.clock), (f"release/v{version}", f"bump/v{version}")
    drop = f"sm release drop {version} --reason ... (--close-pr when its PR is open), then clear"
    try:
        receipt = json.loads(module.RECEIPT.read_text())
    except FileNotFoundError:
        receipt = {}
    except (OSError, ValueError) as exc:
        return f"the release receipt is unreadable ({exc}); inspect it before clearing"
    if not isinstance(receipt, dict):
        return "the release receipt is not a JSON object; inspect it before clearing"
    if receipt.get("version") == version and not receipt.get("finished"):
        return f"the release receipt shows {version} unfinished: {drop}"
    prs, why = client.try_json(["pr", "list", "-R", repo, "--state", "open", "--limit", "200", "--json", "number,headRefName"])
    if why or not isinstance(prs, list | type(None)):
        return f"cannot read the open PRs ({why or 'not a list'}); nothing cleared"
    open_prs = [f"#{row.get('number')}" for row in prs or [] if row.get("headRefName") in branches]
    if open_prs:
        return f"the release PR of {version} is open ({', '.join(open_prs)}): {drop}"
    for branch in branches:
        runs, why = client.try_json(["run", "list", "-R", repo, "--branch", branch, "--limit", "20", "--json", "status,databaseId"])
        if why or not isinstance(runs, list | type(None)):
            return f"cannot read the runs of {branch} ({why or 'not a list'}); nothing cleared"
        live = [str(row.get("databaseId")) for row in runs or [] if row.get("status") != "completed"]
        if live:
            return f"the candidate run of {version} is still active ({branch}: run {', '.join(live)}): wait for it or cancel it, then clear"
    return ""


@native("release train clear", writes=True, replaces=("hz-train",), help=H(
    "Mark one named train cleared after its cause is fixed, so that the next tick may cut again.",
    "Use after a train stopped as blocked (three failed attempts or NOT ACCEPTED) and the cause is resolved, or after its candidate was dropped.",
    "sets that version's state file to cleared and writes a note; nothing else. Refused while that train's runner holds its lock.",
    "sm release train clear 0.9.40", "sm daemon train --dry-run   (what the next tick would do)",
    "The version is required: no other train is ever cleared. Refused (exit 1, nothing written) while that version's\n"
    "candidate lives: a run of release/vX or bump/vX still active, its PR open, or the release receipt naming it unfinished.\n"
    "Dropping a candidate first: `sm release drop X --reason ... --close-pr`, then clear. A failed GitHub read refuses too."))
def release_train_clear(version: str = typer.Argument(..., metavar="X.Y.Z", help="The train to clear (required).")) -> TrainCleared:
    if not VERSION.fullmatch(version):
        raise Refuse(f"{version!r} is not a version X.Y.Z", "sm release train clear --help", code=2)
    module = train()
    if not any(t.get("version") == version for t in module.active_trains()):  # read-only; GitHub is not asked
        return TrainCleared(version=version, cleared=False, exit=1, lines=[f"refused: no active train v{version}"])
    why = clear_refusal(module, version)
    if why:
        line = f"refused: train v{version} is not cleared: {why}"
        return TrainCleared(version=version, cleared=False, exit=1, lines=[line])
    code, lines = run(module, ["clear", version])
    cleared = any(line.startswith("train state cleared (was ") and f" v{version}:" in line for line in lines)
    return TrainCleared(version=version, cleared=cleared, exit=code, lines=lines)


# ----------------------------------------------------------------------------- the N+1 batch, the candidate table
class TrainNext(Model):
    """The N+1 batch at the cut: merged since the cut, queued PRs and whether they are green on current main,
    blockers, builders to nudge. With --start also: the green PRs queued and the batch recorded."""

    version: str = Field("", description="The cut train the batch follows (N); empty when no train has been cut.")
    next_version: str = Field("", description="The version the batch goes into (N+1).")
    started: bool = Field(description="--start was given: green PRs queued and the record next-vX.json written.")
    exit: int
    lines: list[str] = []

    def render(self) -> list[str]:
        return self.lines

    @property
    def exit_code(self) -> int:
        return self.exit


@native("release train next", writes=True, replaces=("hz-train",), help=H(
    "The N+1 batch at the cut: what merged since train N was cut, which queued PRs are green on current main, blockers, builders to nudge.",
    "Use after a cut, in the same tick; with --start to queue the green PRs and record the batch.",
    "without --start nothing; with --start queues the green PRs (`sm queue add`) and writes the batch record next-vX.json in the train's state directory.",
    "sm release train next", "sm release train next --start   (queue the green PRs, record the batch)",
    "Without a version: the newest train that has been cut. It never touches a train's state file or the release.\n"
    "Blockers come from `sm go --view tick`; when that gives nothing the view lists an UNKNOWN blocker. The GO messages of\n"
    "the old view are retired."))
def release_train_next(version: str | None = typer.Argument(None, metavar="[X.Y.Z]", help="The cut train; default: the newest one."),
                       start: bool = typer.Option(False, "--start", help="Queue the green PRs and record the batch.")) -> TrainNext:
    if version is not None and not VERSION.fullmatch(version):
        raise Refuse(f"{version!r} is not a version X.Y.Z", "sm release train next --help", code=2)
    code, lines = run(train(), ["next", *(["--start"] if start else []), *([version] if version else [])])
    found = next((m for line in lines if (m := re.match(r"sm release train next: v(\S+) is cut; v(\S+) batch is OPEN", line))), None)
    return TrainNext(version=found[1] if found else "", next_version=found[2] if found else "", started=start and found is not None, exit=code, lines=lines)


class TrainPlan(Model):
    """The release candidate list as one table: per PR its state, merge state, CI, CI minutes and the next action."""

    exit: int = Field(description="0 the table, 2 no PRs given and none listed.")
    lines: list[str] = []

    def render(self) -> list[str]:
        return self.lines

    @property
    def exit_code(self) -> int:
        return self.exit


@native("release train plan", replaces=("hz-train",), help=H(
    "The release candidate list as one table: state, merge state, CI, CI minutes and the next action per PR.",
    "Use when a release should take a known set of PRs and you need to see which are ready.",
    "nothing (read-only).",
    "sm release train plan 701 702", "sm queue list   (what the queue holds)",
    "Without PR numbers: the list research/release-a.txt in the records checkout (setting queue_records_dir).\n"
    "Exit 2: no PR given and none listed."))
def release_train_plan(prs: list[str] | None = typer.Argument(None, metavar="[PR...]", help="PR numbers, with or without #.")) -> TrainPlan:
    words = [str(pr) for pr in prs or []]
    bad = [pr for pr in words if not pr.lstrip("#").isdigit()]
    if bad:
        raise Refuse(f"not a PR number: {', '.join(bad)}", "sm release train plan --help", code=2)
    base.configure(conf().get("queue_records_dir"))
    table = base.fresh(f"{base.PACKAGE}.train_lib.plan")  # the moved table alone: it reads GitHub and the queue file, nothing else
    code, lines = capture(lambda: table.plan(words))
    return TrainPlan(exit=code, lines=lines)


# ----------------------------------------------------------------------------- resume deferred CI after a tag
class ResumedCI(Model):
    """What happened to each deferred run after a tag."""

    dry_run: bool
    exit: int
    rerun: list[str] = Field([], description="Runs re-run now (or that would be, with --dry-run).")
    dropped: list[str] = Field([], description="Runs whose PR is closed or merged: removed from the list, not re-run.")
    later: list[str] = Field([], description="Runs of open PRs that are neither queued nor combine branches: they stay in the list.")
    failed: list[str] = Field([], description="Runs whose re-run failed: they stay in the list.")
    lines: list[str] = []

    def render(self) -> list[str]:
        return self.lines

    @property
    def exit_code(self) -> int:
        return self.exit


@native("release train resume-ci", writes=True, replaces=("hz-ci-resume-after-tag",), help=H(
    "Right after a release tag: re-run the deferred CI of open PRs that are queued or combine branches; drop runs of closed PRs.",
    "Use after a tag when the candidate window cancelled PR CI; the train calls it by itself.",
    "re-runs workflow runs on GitHub and rewrites the list of deferred runs (the one `sm ci defer` records and `sm ci deferred` prints).",
    "sm release train resume-ci --dry-run", "sm ci rerun   (the rest, when main CI runs)",
    "Refuses while a run of a release branch is active, unless --force. Runs of other open PRs stay listed.\n"
    "A failed read of the runs or the open PRs refuses (exit 1): nothing re-run, the list unchanged."))
def release_train_resume_ci(dry_run: bool = typer.Option(False, "--dry-run", help="Say what would be re-run; re-run and rewrite nothing."),
                            force: bool = typer.Option(False, "--force", help="Resume although a run of a release branch is active.")) -> ResumedCI:
    rt = runtime()
    repo = str(conf().get("repo") or hzlib.CORE)
    client = Gh(rt.providers.commands, repo=repo, clock=rt.providers.clock)

    def gh(*args: str) -> tuple[int, str]:
        done = client.run(list(args), retry=False)
        return done.rc, done.out

    listed = rt.providers.commands.run([*edge.sm(), "--format", "json", "queue", "list", "--active"])
    try:
        queued = {str(entry["pr"]) for entry in json.loads(listed.out)["entries"] if entry.get("status") == "queued"} if listed.rc == 0 else set()
    except (ValueError, KeyError, TypeError):
        queued = set()
    store = rt.providers.store

    def rows() -> list[dict[str, Any]]:
        kept = store.load(DEFERRED, "runs")
        if kept is not None:
            return [dict(row) for row in kept.get("runs", [])]
        old_file = conf().get("ci_ops_legacy_deferred_file")  # read-only, until `sm ci defer` has written the list once
        return legacy.deferred_rows(Path(str(old_file)).expanduser() if old_file else None)

    def save(keep: list[dict[str, Any]]) -> None:
        store.save(DEFERRED, "runs", {"runs": [{"id": str(r["id"]), "branch": r["branch"], "workflow": r.get("workflow", "")} for r in keep]})

    try:
        with store.lock(DEFERRED):  # the lock `sm ci defer` and `sm ci rerun` take around the same list
            code, lines = resume_ci.resume(gh, queued, repo, rows(), save, dry=dry_run, force=force)
    except Busy:
        raise Wait("another deferral or re-run is changing the list of deferred runs", "that command") from None

    def runs(prefix: str) -> list[str]:
        return [line.split()[len(prefix.split())] for line in lines if line.startswith(prefix + " ")]

    return ResumedCI(dry_run=dry_run, exit=code, rerun=runs("would rerun") if dry_run else runs("rerun"), dropped=runs("drop"), later=runs("later"),
                     failed=runs("FAILED rerun"), lines=lines)
