"""The merge queue, native: `sm queue add | list | why | drop`.

The queue's logic is the moved module `queue_lib/queue_daemon.py` (verbatim; `port/move.py verify` proves it). This
module is its edge: typed options instead of the argument parser, a result model instead of printed lines, and the
plugin's settings instead of fixed paths. The state file stays the one the running loop writes, in its exact format,
so the switch from the old loop loses nothing.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import typer
from pydantic import BaseModel, Field

from steuermaschine.engine import Refuse, Wait
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import Group, call, native, runtime
from steuermaschine.plugins import old_variable

from .. import environment
from . import queue_hold
from .journal_view import mirrored
from .queue_lib import auto_arm, edge, main_plan

GROUP = Group("queue", H(
    "queue: the merge queue; what is in it, why it does not merge, add and drop.",
    "Use when a PR should merge, or did not.",
    "list and why only read; add and drop edit the queue's state file. The loop (`sm daemon queue`) arms GitHub auto-merge; GitHub merges on green.",
    "sm queue why", "sm queue list"))


class QueueSettings(BaseModel):
    """Settings of the queue area (mixin of the plugin's table)."""

    queue_state_file: Path | None = Field(None, description="The queue's state file. Default: queue.json in the project's state directory "
                                                                "(lock and log beside it: queue.lock, queue.log).",
                                          json_schema_extra=old_variable("HZ_QUEUE_STATE"))
    queue_known_failures_file: Path | None = Field(None, description="The registry of known failures the queue's digest names beads from. Default: research/known-failures.toml "
                                                                     "in queue_records_dir.", json_schema_extra=old_variable("HZ_KNOWN_FAILURES"))
    queue_main_batch_publish: bool = Field(False, description="Publish the intent of a batch of two or more core PRs before it merges (the main batch admission).",
                                           json_schema_extra=old_variable("HZ_MAIN_BATCH_PUBLISH", on="1"))
    queue_interventions_log: Path | None = Field(None, description="The FILE the merge train appends its advisory interventions to. Default: the interventions log of queue_records_dir.",
                                                 json_schema_extra=old_variable("HZ_INTERVENTIONS_LOG"))
    queue_release_flow_dir: Path | None = Field(None, description="The directory of the release flow records and the tag-on-green-main flag file. "
                                                                  "Default: release-flow/ and release-tag-on-main in the state directory. "
                                                                  "Tests set it; a real release never does.", json_schema_extra=old_variable("HZ_RELEASE_FLOW_DIR"))
    queue_release_tag_on_main: bool = Field(False, description="Tag new versions on green main (the coordinator's switch); the flag file in queue_release_flow_dir "
                                                               "turns it on too.", json_schema_extra=old_variable("HZ_RELEASE_TAG_ON_MAIN", on="1"))
    alarm_state_file: Path | None = Field(None, description="The FILE with the alarm de-duplication state the queue and the watch share. Default: alarm.json in the "
                                                                "project's state directory (the loops share it).",
                                          json_schema_extra=old_variable("HZ_ALARM_STATE"))
    alarm_notify: str | None = Field(None, description="The command that pushes an alarm to the owner (it gets the text as its argument); off: none. "
                                                       "Default: release_train_notify, else the Telegram bridge's send script when it is installed.", json_schema_extra=old_variable("HZ_ALARM_NOTIFY"))
    alarm_renotify_minutes: float = Field(120.0, gt=0, description="Minutes before an alarm that is still on is pushed again.", json_schema_extra=old_variable("HZ_ALARM_RENOTIFY_MIN"))
    queue_records_dir: Path | None = Field(None, description="The checkout with the queue's records: research/ (known failures, queue order), the notes document, the bead database. Data only; nothing in it is executed. Unset: the queue, train and release commands that read it refuse, naming this key.")
    queue_core_repo: Path | None = Field(None, description=(
        "A Core checkout (bare is fine) the main plan check computes merge trees from, in a disposable clone; never changed. "
        "Unset: every core merge and `queue add` of a core PR is refused, naming this key."))


class QueueEntry(Model):
    """One row of the queue, with the fields of the state file that explain where it stands."""

    key: str
    repo: str
    pr: str
    status: str
    detail: str = ""
    after: list[str] = []
    reviewed_head: str = ""
    fixes_main: bool = False
    fix_forward: bool = False
    exception: str = Field("", description="diagnostics, safety fix, mitigation or reviewed progress: the reason given")
    bead: str = ""
    added: float | None = None
    waiting_since: float | None = None
    ready_since: float | None = None
    wait_reason: str = Field("", description="What the last tick recorded for a queued PR that did not merge, e.g. BLOCKED with green checks.")
    merge_sha: str = ""


class RedMain(Model):
    """Main's red state as the queue holds it."""

    pinned_run: int | None = Field(None, description="The pinned failed main run; merges wait for a fix or a restoring pass.")
    pinned_head: str = ""
    fixed_by: list[str] = []
    red_batch: list[str] = Field([], description="The PRs of the batch after which main went red.")
    restored_run: int | None = Field(None, description="A rerun that restored merging (merges only, never releases).")
    restored_label: str = ""


class QueueList(Model):
    entries: list[QueueEntry]
    hidden: int = 0
    last_batch: float = 0
    blocked: str = Field("", description="What the last tick waited on at queue level.")
    red_main: RedMain = RedMain()
    lines: list[str] = Field([], description="The text form, exactly as the moved listing prints it.")

    def render(self) -> list[str]:
        return self.lines


class QueueWhy(Model):
    queued: list[str]
    merge_freeze: bool
    freeze_holder: str = ""
    held: str = Field("", description="The reason of `sm queue hold` while the queue is held: nothing merges.")
    blocked: str = ""
    stalls: list[str] = Field([], description="Queued PRs that wait although nothing at queue level blocks them, each with the recorded reason.")
    stalled_with_green_checks: list[str] = Field([], description="Keys of queued PRs the forge reports BLOCKED while their checks are green: a required check is missing or attached to another suite.")
    red_main: RedMain = RedMain()
    verdict: str = ""
    lines: list[str] = []

    def render(self) -> list[str]:
        return self.lines


class QueueChanged(Model):
    """What `add` or `drop` did."""

    action: str
    prs: list[str]
    queued: list[str] = Field([], description="Keys that are queued after the command.")
    lines: list[str] = []
    errors: list[str] = Field([], description="What the command wrote to stderr: guard findings and refusals, one line each.")
    exit: int = 0

    def render(self) -> list[str]:
        return self.lines

    @property
    def exit_code(self) -> int:
        return self.exit


def _settings() -> dict:
    values = runtime().settings
    values = values.of("heinzel") if hasattr(values, "of") else values
    return values if isinstance(values, dict) else dict(values)


def _queue():
    """A fresh copy of the moved queue module, bound to the configured state file and management checkout."""
    conf = _settings()
    edge.configure(conf.get("queue_records_dir"))
    main_plan.configure(conf.get("queue_core_repo"))
    module = edge.fresh(f"{edge.PACKAGE}.queue_lib.queue_daemon")
    module.hzlib.MERGE_TIER_MODE = str(conf.get("merge_tier_mode") or "auto")  # Core ADR 0149: read from main unless forced
    if conf.get("queue_state_file"):
        state = Path(str(conf["queue_state_file"])).expanduser()
        old = environment.state_overrides().queue_state  # a session that isolates itself with it never writes another file
        if old and Path(old).expanduser() != state:
            raise Refuse(f"two state files are named: the setting queue_state_file ({state}) and HZ_QUEUE_STATE ({old})", "sm self config", code=2)
        module.state_path = lambda: state
    return module


def _load(module) -> dict:
    """The queue's state; a file that exists but cannot be read refuses (exit 1, alarm raised), never an empty queue."""
    try:
        return module.load()
    except module.StateUnreadable as error:
        raise Refuse(str(error), "repair or move the queue state file, then sm queue list", code=1) from None


def _run(module, argv: list[str]) -> tuple[int, list[str], list[str]]:
    """Run the moved command line in process and return exit code, stdout lines and stderr lines."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = module.main(argv)
        except SystemExit as stop:
            code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
            if not isinstance(stop.code, int) and stop.code is not None:
                err.write(str(stop.code) + "\n")
    return int(code or 0), out.getvalue().splitlines(), err.getvalue().splitlines()


def _entry(item: dict) -> QueueEntry:
    exception = item.get("safety_fix_main") or item.get("mitigation_main") or item.get("diagnostics_main") or item.get("reviewed_progress_main") or ""
    return QueueEntry(key=item["key"], repo=item.get("repo", ""), pr=str(item.get("pr", "")), status=item["status"], detail=item.get("detail", "") or "",
                      after=list(item.get("after") or []), reviewed_head=item.get("reviewed_head") or "", fixes_main=bool(item.get("fixes_main")),
                      fix_forward=bool(item.get("fix_forward")), exception=exception, bead=item.get("bead") or "", added=item.get("added"),
                      waiting_since=item.get("waiting_since"), ready_since=item.get("ready_since"), wait_reason=item.get("wait_reason") or "",
                      merge_sha=item.get("merge_sha") or "")


def _red_main(data: dict) -> RedMain:
    pinned, restored = data.get("main_failure") or {}, data.get("main_restored") or {}
    return RedMain(pinned_run=pinned.get("databaseId"), pinned_head=pinned.get("headSha") or "", fixed_by=list(pinned.get("fixed_by") or []),
                   red_batch=list((data.get("red_batch") or {}).get("prs") or []), restored_run=restored.get("run"), restored_label=restored.get("label") or "")


def _translate(lines: list[str]) -> list[str]:
    """The lines of the moved code's output (it names the sm commands itself)."""
    return list(lines)


@native("queue list", replaces=("hz-queue",), help=H(
    "What is in the merge queue, in order, and what the last tick waited on.",
    "Use when you need the queued PRs and their holds.",
    "nothing (read-only).",
    "sm queue list --active", "sm queue why"))
def queue_list(active: bool = typer.Option(False, "--active", help="Hide merged and needs-work rows (history); says how many were hidden.")) -> QueueList:
    module = _queue()
    data = _load(module)
    code, lines, errors = _run(module, ["list", *(["--active"] if active else [])])
    if code:
        raise Refuse("; ".join(_translate(errors)) or "the queue's state could not be listed", "sm queue why", code=code)
    shown = [i for i in data["items"] if not (active and i["status"] in module.DONE)]
    return QueueList(entries=[_entry(i) for i in shown], hidden=len(data["items"]) - len(shown), last_batch=data.get("last_batch") or 0,
                     blocked=data.get("blocked") or "", red_main=_red_main(data), lines=_translate(lines))


@native("queue why", replaces=("hz-queue",), help=H(
    "One-call diagnosis of why the queue is not merging: freeze, main's runs, pinned failure, stalls, verdict.",
    "Use when a queued PR did not merge within two ticks.",
    "nothing (read-only); it reads main's last five CI runs.",
    "sm queue why", "the command its verdict names; sm queue list"))
def queue_why() -> QueueWhy:
    module = _queue()
    data = _load(module)
    code, lines, errors = _run(module, ["why"])
    if code:
        raise Refuse("; ".join(_translate(errors)) or "the queue could not be diagnosed", "sm queue list", code=code)
    queued = [i["key"] for i in data["items"] if i["status"] == "queued"]
    frozen = any(line.startswith("merge freeze: ON") for line in lines)
    holder = next((line.split("[", 1)[1].rstrip("]") for line in lines if line.startswith("merge freeze: ON [")), "")
    stalls = [line for line in lines if line.startswith("item ")]
    green = [i["key"] for i in data["items"] if i["status"] == "queued" and "BLOCKED with green checks" in (i.get("wait_reason") or "")]
    verdict = next((line.removeprefix("verdict: ") for line in lines if line.startswith("verdict: ")), "")
    hold = queue_hold.current()
    if hold:
        lines = [f"queue HOLD: {hold.describe()} (sm queue unhold)", *lines]
        verdict = f"held: {hold.reason}"
    return QueueWhy(queued=queued, merge_freeze=frozen, freeze_holder=holder, held=hold.reason if hold else "", blocked=data.get("blocked") or "",
                    stalls=stalls, stalled_with_green_checks=green, red_main=_red_main(data), verdict=verdict, lines=_translate(lines))


def _require_prs(module, groups: list[list[str]] | list[str], what: str) -> None:
    """Refuse a word that names no PR (a usage error, exit 2) before the moved code turns it into a traceback."""
    for words in groups:
        try:
            module.hzlib.split_key(words)
        except ValueError:
            shown = words if isinstance(words, str) else " ".join(words)
            raise Refuse(f"not a {what}: {shown!r}; give a PR number, or `plugins 38` for a PR of another repository",
                         "sm queue list", code=2) from None


def _changed(action: str, argv: list[str], prs: list[str], after: list[str] | None = None) -> QueueChanged:
    """Run the moved command line. What it printed stays on its stream: stdout lines are the result, stderr lines
    (guard findings, refusals) go to stderr, the exit code is the moved one. Nothing is rephrased."""
    module = _queue()
    _require_prs(module, module.pr_words(prs), "PR")
    _require_prs(module, after or [], "PR for --after")  # the moved add reads each --after value as one PR
    before = {i["key"] for i in _load(module)["items"]}
    code, lines, errors = _run(module, argv)
    for line in _translate(errors):
        runtime().say(line)
    items = _load(module)["items"]
    if action == "drop":  # the moved drop is silent, also about a key that was never queued; say what happened
        keys = [module.hzlib.split_key(words)[2] for words in module.pr_words(prs)]
        lines = [f"dropped {key}" if key in before else f"not in the queue: {key}" for key in keys]
    return QueueChanged(action=action, prs=prs, queued=[i["key"] for i in items if i["status"] == "queued"], lines=_translate(lines),
                        errors=_translate(errors), exit=code)


@native("queue add", writes=True, replaces=("hz-queue",), help=H(
    "Queue PRs for merge; the loop then updates, waits for green checks and squash-merges them.",
    "Use after the diff guard passed or an independent review gave a written reason (`sm head ship` does both).",
    "adds entries to the queue's state file; with --reviewed it also writes a note. No merge happens here.",
    "sm queue add --reviewed \"review: research/review-pr801.md\" --reviewed-head e9c943810ea75cfad155a431a2f168b2a9559828 801",
    "sm queue list"))
@mirrored  # it runs a program that writes the working files: the subject journals follow (journal_view.py)
def queue_add(
    pr: list[str] = typer.Argument(..., metavar="PR...", help="PR numbers; `plugins 38` names a PR of another repository."),
    after: list[str] = typer.Option([], "--after", metavar="PR[,PR...]", help="Merge only after this PR merged (repeatable)."),
    bead: str | None = typer.Option(None, "--bead", help="The bead this PR closes; recorded at the merge."),
    reviewed: str | None = typer.Option(None, "--reviewed", metavar="REASON", help="Queue a PR the diff guard flagged; the reason of the independent review."),
    reviewed_head: str | None = typer.Option(None, "--reviewed-head", metavar="SHA", help="Pin one PR (any repository) to its reviewed full SHA; needs --reviewed."),
    fixes_main: bool = typer.Option(False, "--fixes-main", help="The PR that makes a red main green: it may merge while main is red, alone."),
    diagnostics_main: str | None = typer.Option(None, "--diagnostics-main", metavar="REASON", help="One reviewed core diagnostic, not a fix; needs --reviewed and --diagnostic-head."),
    safety_fix_main: str | None = typer.Option(None, "--safety-fix-main", metavar="REASON", help="One reviewed core safety fix, not a main CI fix; needs --reviewed and --safety-head."),
    mitigation_main: str | None = typer.Option(None, "--mitigation-main", metavar="REASON", help="One reviewed measured mitigation; needs --reviewed, --mitigation-head, --mitigation-evidence."),
    reviewed_progress_main: str | None = typer.Option(None, "--reviewed-progress-main", metavar="REASON", help="Owner-approved reviewed progress past a known main failure."),
    fix_forward: bool = typer.Option(False, "--fix-forward", help="One fix|test|ci core PR with green PR CI may merge while main is red; needs --for."),
    fix_for: list[str] = typer.Option([], "--for", metavar="JOB=BEAD[@PR]", help="Fix-forward: attribute each failing main job to a bead this PR addresses."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fix-forward: print every check and the verdict, queue nothing."),
    progress_head: str | None = typer.Option(None, "--progress-head", metavar="SHA", help="The reviewed progress head; must match the PR head."),
    mitigation_head: str | None = typer.Option(None, "--mitigation-head", metavar="SHA", help="The reviewed mitigation commit; must match the PR head."),
    mitigation_evidence: str | None = typer.Option(None, "--mitigation-evidence", metavar="EVIDENCE", help="Observed improvement supporting the mitigation."),
    safety_head: str | None = typer.Option(None, "--safety-head", metavar="SHA", help="The reviewed safety-fix commit; must match the PR head."),
    diagnostic_head: str | None = typer.Option(None, "--diagnostic-head", metavar="SHA", help="The reviewed diagnostic commit; must match the PR head."),
) -> QueueChanged:
    # "given" is not "non-empty": the moved code refuses an option given with an empty value, so every given value is passed on
    kinds = [fixes_main, diagnostics_main is not None, safety_fix_main is not None, mitigation_main is not None, reviewed_progress_main is not None, fix_forward]
    if sum(kinds) > 1:
        raise Refuse("only one of --fixes-main, --diagnostics-main, --safety-fix-main, --mitigation-main, --reviewed-progress-main, --fix-forward", "sm queue add --help", code=2)
    argv = ["add"]
    for value in after:
        argv += ["--after", value]
    for flag, value in (("--bead", bead), ("--reviewed", reviewed), ("--reviewed-head", reviewed_head), ("--diagnostics-main", diagnostics_main),
                        ("--safety-fix-main", safety_fix_main), ("--mitigation-main", mitigation_main), ("--reviewed-progress-main", reviewed_progress_main),
                        ("--progress-head", progress_head), ("--mitigation-head", mitigation_head), ("--mitigation-evidence", mitigation_evidence),
                        ("--safety-head", safety_head), ("--diagnostic-head", diagnostic_head)):
        if value is not None:
            argv += [flag, value]
    for value in fix_for:
        argv += ["--for", value]
    argv += [flag for flag, on in (("--fixes-main", fixes_main), ("--fix-forward", fix_forward), ("--dry-run", dry_run)) if on]
    return _changed("add", [*argv, *pr], list(pr), list(after))


@native("queue drop", writes=True, replaces=("hz-queue",), help=H(
    "Remove PRs from the merge queue.",
    "Use when a queued PR must not merge (wrong head, superseded).",
    "removes entries from the queue's state file; the PR itself is untouched.",
    "sm queue drop 795", "sm queue list"))
@mirrored  # it runs a program that writes the working files: the subject journals follow (journal_view.py)
def queue_drop(pr: list[str] = typer.Argument(..., metavar="PR...", help="PR numbers; `plugins 38` names a PR of another repository.")) -> QueueChanged:
    return _changed("drop", ["drop", *pr], list(pr))


# ----------------------------------------------------------------------------- the loop: `sm daemon queue`
# Registered from this module on purpose: the review digest of a command covers its module and `queue_lib/`, and the
# tick is queue code. The loop itself is the core's (`--loop SECONDS`). A call decides whether a tick is due: the
# first call, the interval since the last tick, or a local event (the queue's state file changed, a merge hold was
# released). So `sm daemon queue --loop 1` behaves as the old loop did: a tick every `--interval` seconds, and within
# a second after `sm queue add` or the end of a hold. The tick, the lock and the log are the moved code, unchanged.
DAEMON = Group("daemon", H(
    "daemon: one tick of a loop the service manager keeps alive (merge queue, release train).",
    "Use for a launchd job; by hand only to run a single tick while the job is stopped.",
    "depends on the loop: `queue` merges ready PRs onto protected main.",
    "sm daemon queue --loop 1", "sm queue why"))
_LOOP: dict = {}  # this process's loop: the moved module, the held lock, when the next tick is due, what was seen last


def reset() -> None:
    """Forget this process's loop and release its lock (tests; a real loop ends with its process)."""
    if held := _LOOP.get("lock"):
        held.close()
    _LOOP.clear()


class QueueTick(Model):
    """One call of the queue loop."""

    ran: bool = Field(description="False: no tick was due and nothing local changed, or the queue is held.")
    held: str = Field(default="", description="The reason of `sm queue hold` when the queue is held: no tick ran.")
    wake: str = Field("", description="Why the tick ran before its interval: queue changed, merge hold released.")
    seconds: float = 0
    queued: list[str] = []
    blocked: str = Field("", description="What the tick waited on at queue level, as the state file records it.")
    red_main: RedMain = RedMain()
    lines: list[str] = Field([], description="The tick's log lines, as written to the queue log.")

    def render(self) -> list[str]:
        return self.lines


DETACH = "import os, sys\ntry:\n    os.setsid()\nexcept OSError:\n    pass\nos.execv(sys.argv[1], sys.argv[1:])"  # its own session: it outlives this loop


def _main_proof(module: Any) -> None:
    """With the merge tier at main (the queue's own check, hzlib.merge_tier), give the newest green main commit its
    hz/main-proof without a manual call, and never let the tick wait for the judge.

    Once per tick: `sm queue main-proof --check` (a few reads) says whether the judge's inputs are complete: main's push
    run finished green with the aggregate and its commit is in the Core clone. Not complete, already proven, or no main proof at
    this Core: a log line, and a later tick looks again. Complete: ONE attempt per main commit, recorded in the queue state (`main_proof`) before
    `sm queue main-proof --sha SHA` starts detached in its own session (its own judge lock, its own review gate, output
    in main-proof.log next to the queue state); the tick returns at once. A judge that fails is not started again for
    that commit by the tick (by hand: `sm queue main-proof`); the next green main commit gets its own attempt. An older
    Core, or merge_tier_mode off: nothing runs.

    The command is looked up in the registry by path, not imported: it is its own review unit and its own gate decides
    (`native.call`). A refusal or a wait is a log line; it never ends the queue's tick."""
    try:
        if not module.hzlib.merge_tier():
            return
        registry = importlib.import_module(__package__ or "steuermaschine_heinzel.native")
        command = next(c for c in registry.commands() if getattr(c, "path", None) == "queue main-proof")
        ready = call(command, check=True)
        if ready.state != "ready":
            module.log(f"main proof {ready.sha[:9] or '-'}: {ready.state}" + (f": {ready.reason}" if ready.reason else ""))
            return
        if module.load().get("main_proof", {}).get("sha") == ready.sha:
            module.log(f"main proof {ready.sha[:9]}: attempted already (run {ready.run}); not repeated")
            return
        rt = runtime()
        module.update(main_proof={"sha": ready.sha, "run": ready.run, "at": str(rt.providers.clock.now())})
        log = module.state_path().with_name("main-proof.log")
        child = [sys.executable, *sys.argv[: _program_words()], "queue", "main-proof", "--sha", ready.sha]
        started = rt.providers.commands.run(["sh", "-c", 'nohup "$@" >>"$0" 2>&1 </dev/null &', str(log), sys.executable, "-c", DETACH, *child], timeout=30)
        if started.rc != 0:  # nothing ran: no attempt was made, a later tick tries again
            with module.state_lock():
                data = module.load()
                data.pop("main_proof", None)
                module.save(data)
            module.log(f"main proof {ready.sha[:9]}: could not start the judge (exit {started.rc}); a later tick tries again")
            return
        module.log(f"main proof {ready.sha[:9]}: judge started detached (run {ready.run}; log {log.name})")
    except (Refuse, Wait) as error:
        module.log(f"main proof: {error}")
    except Exception as error:  # as the tick: a failed proof must not end the loop
        module.log(f"main proof failed: {error!r}")


def _program_words() -> int:
    """How many words of this process's argv name the program and its root options (everything before `daemon`)."""
    return sys.argv.index("daemon") if "daemon" in sys.argv else 1


@native("daemon queue", writes=True, tick=True, replaces=("hz-queue",), help=H(
    "One tick of the merge queue: arm GitHub auto-merge on queued PRs, revert a red main; no tick while `sm queue hold` holds.",
    "Use for the launchd job (`--loop 1`); by hand for a single tick only while that job is stopped.",
    "arms or disarms auto-merge (squash, pinned head), may rerun main's KNOWN failed jobs once, opens a revert PR when main turned red "
    "on a merge with an unknown failure, writes queue state, log, notes, alarms; "
    "with the merge tier at main starts `sm queue main-proof` detached, once per green main commit (posts hz/main-proof).",
    "sm daemon queue --loop 1 --interval 300", "sm queue why"))
@mirrored  # it runs a program that writes the working files: the subject journals follow (journal_view.py)
def daemon_queue(
    interval: float = typer.Option(300.0, "--interval", metavar="SECONDS", help="Seconds between two ticks when nothing local changed."),
) -> QueueTick | None:
    if interval < 0:
        raise Refuse("--interval must be non-negative", "sm daemon queue --help", code=2)
    first = "module" not in _LOOP
    if first:
        module = _queue()
        held = module.acquire_lock(block=False)
        if held is None:
            raise Wait(f"another queue loop holds {module.lock_path()}", "that loop to exit (the service manager starts this one again)")
        _LOOP.update(module=module, lock=held, started=module.tools_changed_at(), due=None, stamp=None, frozen=False)
    module = _LOOP["module"]
    if module.tools_changed_at() != _LOOP["started"]:  # as before: stale code in a running loop broke a batch once
        os.execv(sys.executable, [sys.executable, *sys.argv])  # this same program, its own argv
    wake = ""
    if not first:
        now_frozen = module.hzlib.merge_frozen()
        if module.queue_stamp() != _LOOP["stamp"]:
            wake = "queue changed"
        elif _LOOP["frozen"] and not now_frozen:
            wake = "merge hold released"
        _LOOP["frozen"] = now_frozen
        if not wake and module.time.monotonic() < _LOOP["due"]:
            return None
    out = io.StringIO()
    hold = queue_hold.current()
    if hold and hold.lets([queue_hold.queued_key(i) for i in _load(module)["items"] if i["status"] == "queued"]):
        with contextlib.redirect_stdout(out):
            module.log(f"queue held, but only excepted PRs are queued: tick for {', '.join(hold.excepted)}")
        hold = None
    if hold:  # sm queue hold: no tick at all, so nothing merges in any repository
        with contextlib.redirect_stdout(out):
            module.log(f"queue held, no tick: {hold.reason} (sm queue unhold lifts it)")
            auto_arm.hold(SimpleNamespace(**vars(module)), hold.reason, lambda i: queue_hold.queued_key(i) in hold.excepted)  # an armed PR would still merge on GitHub
        _LOOP.update(due=module.time.monotonic() + interval, stamp=module.queue_stamp(), frozen=module.hzlib.merge_frozen())
        data = _load(module)
        return QueueTick(ran=False, wake=wake, held=hold.reason, queued=[i["key"] for i in data["items"] if i["status"] == "queued"],
                         blocked=data.get("blocked") or "", red_main=_red_main(data), lines=_translate(out.getvalue().splitlines()))
    with contextlib.redirect_stdout(out):
        if first:
            module.log(f"queue run started, pid {os.getpid()}")
        if wake:
            module.log(f"idle wake: {wake}")
        started = module.time.monotonic()
        module.log("tick started")
        try:
            try:
                module.tick()
            except Exception as error:  # noqa: BLE001 - as before: a bad tick must not end the loop
                module.log(f"tick failed: {error!r}")
            _main_proof(module)
        finally:
            seconds = module.time.monotonic() - started
            module.log(f"tick finished in {seconds:.1f}s")
    _LOOP.update(due=module.time.monotonic() + interval, stamp=module.queue_stamp(), frozen=module.hzlib.merge_frozen())
    data = _load(module)
    return QueueTick(ran=True, wake=wake, seconds=round(seconds, 1), queued=[i["key"] for i in data["items"] if i["status"] == "queued"],
                     blocked=data.get("blocked") or "", red_main=_red_main(data), lines=_translate(out.getvalue().splitlines()))
