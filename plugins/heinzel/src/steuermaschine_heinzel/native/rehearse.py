"""`sm release rehearse ...` and `sm daemon rehearse`: the release candidate's e2e lanes and plugin Consumer checks
against main's tip, without a release.

The logic is `rehearse_lib/rehearsal.py` (moved); its two guards on other areas are `rehearse_lib/others.py`. This
module is the edge: typed options, the result models, and the binding of the library to the providers and settings
of this call. The hosted lanes start through the single-lane library in process (`dispatch_lib/focused.py`, with all
of its guards). The Consumer lock checks (the plugins repository's `validate_lock` for every plugin on plugins main)
are part of every rehearsal; the full Consumer checks (`make check` per plugin) run under the run lock (`lock.lock_of`).

Other areas read the outcome through `last_verdict(store)` and `open_rehearsal(store)`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, Field

from steuermaschine import REFUSED, USAGE, WAITING
from steuermaschine.config import child_environment
from steuermaschine.engine import Refuse, Wait
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import Group, NativeRuntime, native, runtime
from steuermaschine.protocols import Busy
from steuermaschine.runlock import Request

from . import dispatch
from .dispatch_lib import focused
from .lock import lock_of
from .rehearse_lib import others
from .rehearse_lib import rehearsal as lib
from .window import lane_args

STATUS = "sm release rehearse status"
ERRORS = (RuntimeError, KeyError, TypeError, ValueError, OSError)  # GitHub or a state that is not what the library expects


class RehearseSettings(BaseModel):
    """Settings of the release rehearsal, a mixin of the plugin's table."""

    rehearse_plugins_clone: Path | None = Field(None, description="A clone of the plugins repository; the Consumer checks fetch its origin's main and run "
                                                                  "in a temporary detached worktree of it. Not set: the Consumer checks are unavailable.")
    rehearse_scratch_dir: Path | None = Field(None, description="Where the Consumer checks keep the wheel, their temporary worktree and one log per plugin. "
                                                                "Default: rehearse/ in the project's state home.")
    rehearse_every_minutes: int = Field(120, description="The tick starts a new rehearsal at the earliest this long after the last start; never below 30.")
    rehearse_max_open_minutes: int = Field(180, description="A rehearsal still open after this long is closed and its own runs are cancelled; never above 180.")


# ----------------------------------------------------------------------------- result models
class Lane(Model):
    """One e2e lane on one runner: its rehearsal branch and its hosted run."""

    lane: str
    operating_system: str
    branch: str
    run: int | None = None
    status: str  # pending | running | done | dropped
    result: str = ""  # the run's conclusion once done; cancelled when the rehearsal stopped it
    url: str = ""


class ConsumerChecks(Model):
    """The plugin Consumer checks of one main: the edge wheel against every plugin on plugins main."""

    status: str  # pending | done | unavailable
    plugins_main: str = ""  # the commit of plugins main that was checked
    wheel: str = ""
    release: str = ""  # the core release the wheel was judged as (its .dev suffix dropped)
    stale: list[str] = []  # "name: why" for each plugin whose lock compose's validate_lock refuses
    locked: list[str] = []  # the plugins whose lock it accepts
    full: bool = False  # `make check` per plugin was asked for
    failed: list[str] = []
    passed: list[str] = []
    logs: str = ""
    detail: str = ""

    @property
    def exit_code(self) -> int:
        return int(bool(self.failed) or bool(self.stale) or self.status != "done")

    def render(self) -> list[str]:
        where = f": plugins main {self.plugins_main[:10]}, core wheel {self.wheel}" if self.plugins_main else ""
        lines = [f"Consumer checks {self.status}" + where + (f" (as core {self.release})" if self.release else "") + (f": {self.detail}" if self.detail else "")]
        lines += [f"  locks ok: {', '.join(self.locked)}"] if self.locked else []
        lines += [f"  STALE LOCK: {row}" for row in self.stale]
        lines += [f"  passed: {', '.join(self.passed)}"] if self.passed else []
        lines += [f"  FAILED: {', '.join(self.failed)}"] if self.failed else []
        return lines + ([f"  logs: {self.logs}"] if self.logs else [])


class Rehearsal(Model):
    """One rehearsal, open or finished."""

    main: str
    commit: str  # the throwaway commit every lane runs on
    started: str
    finished: str = ""
    lanes: list[Lane] = []
    consumers: ConsumerChecks | None = None
    yielded: str = ""  # why it gave way before every lane ran
    preempted: bool = False  # it cancelled its own running lanes for the merge queue or an admission
    waiting: str = ""  # why the last lane did not start
    verdict: str = ""  # green | red | incomplete; empty while open
    failures: list[str] = []
    suspects: list[str] = []  # the merges on main since the last green rehearsal


class RehearsalStatus(Model):
    """The open rehearsal and the last verdict."""

    line: str
    current: Rehearsal | None = None
    last: Rehearsal | None = None
    last_green: str = ""

    @property
    def exit_code(self) -> int:
        return REFUSED if self.last is not None and self.last.verdict == "red" and self.current is None else 0

    def render(self) -> list[str]:
        return [self.line or "no rehearsal yet"]


class PlannedRun(Model):
    lane: str
    operating_system: str


class RehearsalPlan(Model):
    """What a rehearsal would run now, and the guard that refuses it."""

    runs: list[PlannedRun]
    consumers: bool
    refusal: str = ""

    def render(self) -> list[str]:
        return [f"{len(self.runs)} focused-job runs: " + ", ".join(f"{row.lane} ({row.operating_system})" for row in self.runs),
                "Consumer lock checks of every plugin on plugins main: always",
                f"Full Consumer checks (make check per plugin) on this machine: {'yes' if self.consumers else 'no (--consumers)'}",
                self.refusal or "no guard refuses a start now"]


class RehearsalRun(Model):
    """What `start`, `step`, `cancel` or one tick did."""

    action: str  # start | step | cancel | tick
    ran: bool = True  # False: a tick that had nothing to do, or could not work
    message: str = ""
    code: int = 0
    current: Rehearsal | None = None
    last: Rehearsal | None = None

    @property
    def exit_code(self) -> int:
        return self.code

    def render(self) -> list[str]:
        return [self.message] if self.message else []


# ----------------------------------------------------------------------------- for other areas
def last_verdict(store: Any) -> dict[str, Any] | None:
    """The last finished rehearsal, or None when none finished yet: `verdict` (green | red | incomplete), `main`,
    `failures`, `suspects`, `yielded`, `preempted` (only when it gave way mid-flight), `started` and `finished`
    (epoch seconds), `lanes`, `consumers`. A red verdict counts only while no rehearsal is open (`open_rehearsal`)."""
    last = (store.load(lib.KIND, lib.KEY) or {}).get("last")
    return last if isinstance(last, dict) else None


def open_rehearsal(store: Any) -> dict[str, Any] | None:
    """The open rehearsal (`main`, `commit`, `started`, `lanes`, `yielded`), or None."""
    current = (store.load(lib.KIND, lib.KEY) or {}).get("current")
    return current if isinstance(current, dict) else None


# ----------------------------------------------------------------------------- binding
def setting(rt: NativeRuntime, name: str) -> Any:
    value = (rt.settings or {}).get(name)
    return RehearseSettings.model_fields[name].default if value in (None, "") else value


def start_lane(rt: NativeRuntime, branch: str, lane: str, os_name: str) -> tuple[int, str, str]:
    """One lane through the single-lane library, in process. -> (exit code, what it printed, what it said when it
    refused). Every guard of that library applies; the rehearsal lifts none."""
    lines: list[str] = []
    problems: list[str] = []
    dispatch.bound(rt, lines, problems)
    start: Callable[[Any], Any] = focused.cmd_dispatch  # the moved single-lane library is not annotated
    args = lane_args(branch, lane, os_name, wait=False)
    args.wait_for_slot = False  # no slot: the lane stays pending for the next tick instead of holding the tick (and its lock) up to 30 minutes
    try:
        code = start(args)
    except SystemExit as stop:
        if isinstance(stop.code, int):  # the SHA guard: its reasons were said line by line
            return stop.code or REFUSED, "\n".join(lines), "\n".join(problems) or f"refused (exit {stop.code})"
        return REFUSED, "\n".join(lines), str(stop.code)
    return int(code or 0), "\n".join(lines), "\n".join(problems)


class Logged:
    """The process runner of the run lock for one Consumer check: the check's output goes to its log file."""

    def __init__(self, inner: Any, log: Path):
        self.inner, self.log = inner, log

    def run(self, *args: Any, **kwargs: Any) -> Any:
        return self.inner.run(*args, **kwargs)

    def passthrough(self, argv: list[str], cwd: str | None = None, env: dict[str, str] | None = None, timeout: float | None = None) -> int:
        return int(self.inner.passthrough(["sh", "-c", 'exec "$@" >"$0" 2>&1', str(self.log), *argv], cwd=cwd, env=env, timeout=timeout))


def gated(rt: NativeRuntime, allow_full: bool, argv: list[str], cwd: Path, log: Path) -> tuple[int, str]:
    """One `make` of a Consumer check under the run lock, in `cwd`, its output in `log`. -> (exit code, why the run
    lock did not run it, or ""). The lock refuses `make check` as a full suite unless the caller allows it."""
    gate = lock_of(allow_full)
    providers: Any = rt.providers
    gate.commands = Logged(providers.commands, log)
    outcome = gate.run(Request(argv=[argv[0], "-C", str(cwd), *argv[1:]], environ=child_environment(), cwd=str(cwd)))
    return outcome.exit_code, "" if outcome.status == "ran" else outcome.reason or outcome.status


def bind_to(rt: NativeRuntime, edge: lib.Edge) -> lib.Edge:
    table = rt.settings or {}
    repo = str(table.get("repo") or "")
    clone, scratch = table.get("rehearse_plugins_clone"), table.get("rehearse_scratch_dir")
    project: Any = rt.project
    lib.bind(edge, repo, Path(str(clone)).expanduser() if clone else Path("plugins-clone-not-set"),  # the Consumer checks say so and stay unavailable
             Path(str(scratch)).expanduser() if scratch else Path(project.home) / "rehearse",
             others.queue_file(table, Path(project.home)), int(setting(rt, "rehearse_max_open_minutes")))
    return edge


def bound_to_read(rt: NativeRuntime) -> lib.Edge:
    """The library bound for a command that only reads: it has no way to start a lane or a gated command."""
    providers: Any = rt.providers
    return bind_to(rt, lib.Edge(commands=providers.commands, clock=providers.clock, store=providers.store, say=rt.say))


def bound(rt: NativeRuntime, allow_full: bool = False) -> lib.Edge:
    """Bind the moved library to this call's providers and settings."""
    providers: Any = rt.providers
    edge = lib.Edge(commands=providers.commands, clock=providers.clock, store=providers.store, journal=lambda text: dispatch.journal(rt, text),
                    lane=lambda branch, lane, os_name: start_lane(rt, branch, lane, os_name),
                    gated=lambda argv, cwd, log: gated(rt, allow_full, argv, cwd, log), say=rt.say)
    return bind_to(rt, edge)


@contextmanager
def working(rt: NativeRuntime, allow_full: bool = False) -> Iterator[None]:
    """The library bound, and one rehearsal command at a time on this machine."""
    bound(rt, allow_full)
    providers: Any = rt.providers
    with ExitStack() as stack:
        try:
            stack.enter_context(providers.store.lock(lib.LOCK))
        except Busy:
            raise Wait("another rehearsal command is working on this machine", "that command to finish") from None
        yield


def iso(seconds: Any) -> str:
    return datetime.fromtimestamp(float(seconds)).astimezone().isoformat(timespec="seconds") if seconds else ""


def checks_of(raw: dict[str, Any]) -> ConsumerChecks:
    return ConsumerChecks(status=str(raw.get("status") or ""), plugins_main=raw.get("plugins") or "", wheel=raw.get("wheel") or "", release=raw.get("release") or "",
                          stale=list(raw.get("stale") or []), locked=list(raw.get("locked") or []), full=bool(raw.get("full")),
                          failed=list(raw.get("failed") or []), passed=list(raw.get("passed") or []), logs=raw.get("logs") or "", detail=raw.get("detail") or "")


def rehearsal_of(raw: Any) -> Rehearsal | None:
    if not isinstance(raw, dict):
        return None
    lanes = [Lane(lane=x["lane"], operating_system=x["os"], branch=x["branch"], run=x.get("run"), status=x["status"], result=x.get("result") or "",
                  url=x.get("url") or "") for x in raw.get("lanes", [])]
    consumers = raw.get("consumers")
    return Rehearsal(main=raw["main"], commit=raw["commit"], started=iso(raw.get("started")), finished=iso(raw.get("finished")), lanes=lanes,
                     consumers=checks_of(consumers) if isinstance(consumers, dict) else None, yielded=raw.get("yielded") or "",
                     preempted=bool(raw.get("preempted")), waiting=raw.get("waiting") or "", verdict=raw.get("verdict") or "",
                     failures=list(raw.get("failures") or []), suspects=list(raw.get("suspects") or []))


def picked(lanes: str, os_names: str, help_cmd: str) -> tuple[list[str], list[str]]:
    try:
        return lib.chosen(lanes, lib.LANES), lib.chosen(os_names, lib.OSES)
    except SystemExit as stop:
        raise Refuse(f"--lanes and --os: {stop.code}", help_cmd, code=USAGE) from None


def done(action: str, work: Callable[[], tuple[str, int]], allow_full: bool = False) -> RehearsalRun:
    """Run one library command under the rehearsal's lock and turn what it said into the result model."""
    rt = runtime()
    try:
        with working(rt, allow_full):
            message, code = work()
            state = lib.load()
        if code == WAITING:
            raise Wait(message.removeprefix("refusing: "), "the guard to clear (sm release rehearse plan)")
        return RehearsalRun(action=action, message=message, code=code, current=rehearsal_of(state.get("current")), last=rehearsal_of(state.get("last")))
    except ERRORS as err:
        raise Refuse(f"the rehearsal stopped: {err}", STATUS) from err


# ----------------------------------------------------------------------------- commands
REHEARSE = Group("release rehearse", H(
    "release rehearse: the candidate's e2e lanes and plugin Consumer checks against main's tip, without a release.",
    "Use before a cut, and after a fix for a red rehearsal is on main.",
    "plan and status only read; start, step and cancel push and delete rehearsal/* branches and start or cancel hosted runs.",
    "sm release rehearse start --consumers",
    "sm release rehearse status"))

LANES_HELP = "Comma list of cli, supervised, web-1, web-2."
OS_HELP = "Comma list of ubuntu-24.04, macos-latest."
CONSUMERS_HELP = "Also run each plugin's `make check` on this machine (about 10 minutes); the lock check of every plugin always runs."
ALLOW_FULL_HELP = "Let the Consumer checks run `make check`, which the run lock refuses as a full suite; logged."
GUARDS = ("Guards (start, and every dispatch in step): refuses while a release is in flight (an open release/* PR or an active release/* run),\n"
          "while the merge queue holds a PR that is not merged or needs work, while a focused admission prepare or apply runs on this\n"
          "machine, and while GitHub or the queue state cannot be read. A rehearsal that meets a guard midway yields: pending lanes are\n"
          "dropped, its own running lanes are cancelled when the queue or an admission needs the window, and the verdict is incomplete\n"
          "(preempted); a release (candidate) that starts midway preempts it the same way. A rehearsal still open after 180 minutes is\n"
          "closed the same way. Every rehearsal checks the lock of EVERY plugin on plugins main against main's edge wheel with the\n"
          "plugins repository's own validate_lock, as compose does for a candidate: a stale lock, or no plugin at all, is red.\n"
          "It gates nothing and is no release evidence.")


@native("release rehearse plan", writes=False, replaces=("hz-rehearse",), help=H(
    "What a rehearsal would run now, and whether a guard refuses it.",
    "Use before `sm release rehearse start`.",
    "nothing (read-only).",
    "sm release rehearse plan --consumers",
    "sm release rehearse start --consumers"))
def rehearse_plan(lanes: str = typer.Option(",".join(lib.LANES), "--lanes", metavar="A,B", help=LANES_HELP),
                  os_names: str = typer.Option(",".join(lib.OSES), "--os", metavar="A,B", help=OS_HELP),
                  consumers: bool = typer.Option(False, "--consumers", help=CONSUMERS_HELP)) -> RehearsalPlan:
    bound_to_read(runtime())
    wanted, runners = picked(lanes, os_names, "sm release rehearse plan --help")
    rows = lib.lanes_for("0" * 40, wanted, runners)
    return RehearsalPlan(runs=[PlannedRun(lane=row["lane"], operating_system=row["os"]) for row in rows], consumers=consumers, refusal=lib.refusal())


@native("release rehearse status", writes=False, replaces=("hz-rehearse",), help=H(
    "The open rehearsal and the last verdict (green, red or incomplete), in one line.",
    "Use before a cut, and when the scheduled tick reported a verdict.",
    "nothing (read-only).",
    "sm release rehearse status",
    "sm release rehearse start --consumers   (red: fix forward on main first)",
    "Exit 1 when the last verdict is red and no rehearsal is open. A red rehearsal is the candidate failing in advance: it names the\n"
    "failed runs and the merges since the last green rehearsal. Incomplete proves nothing either way."))
def rehearse_status(line: bool = typer.Option(False, "--line", help="The one-line form (the text output is that line).")) -> RehearsalStatus:
    bound_to_read(runtime())
    try:
        state = lib.load()
        return RehearsalStatus(line=lib.line(state), current=rehearsal_of(state.get("current")), last=rehearsal_of(state.get("last")),
                               last_green=state.get("last_green") or "")
    except ERRORS as err:
        raise Refuse(f"the rehearsal state cannot be read: {err}", STATUS) from err


@native("release rehearse start", writes=True, replaces=("hz-rehearse",), files=("rehearse_lib", "dispatch.py", "dispatch_lib", "window.py", "lock.py", "lock_lib"), review="release-rehearse-start.md", help=H(
    "Start a rehearsal of main's tip: one throwaway commit, one rehearsal/* branch and one hosted run per lane and OS.",
    "Use before a cut when main moved since the last green rehearsal.",
    "creates one commit and the rehearsal/* branches on the remote, starts hosted runs; later steps cancel runs and delete the branches.",
    "sm release rehearse start --consumers",
    "sm release rehearse step   (until it prints the verdict; the scheduled tick does the same)",
    "The commit has main's tree and main as its parent; it is no PR's head and is never merged. Lanes start through the single-lane\n"
    "library: its caps apply (5 focused runs in flight), and lanes it refuses stay pending for the next step. With a rehearsal already\n"
    "open this is `step`. Exit 75: a guard refuses the start, nothing was pushed. Exit 1: the verdict is red.\n" + GUARDS))
def rehearse_start(if_moved: bool = typer.Option(False, "--if-moved", help="Do nothing when main is still the commit of the last rehearsal."),
                   lanes: str = typer.Option(",".join(lib.LANES), "--lanes", metavar="A,B", help=LANES_HELP),
                   os_names: str = typer.Option(",".join(lib.OSES), "--os", metavar="A,B", help=OS_HELP),
                   consumers: bool = typer.Option(False, "--consumers", help=CONSUMERS_HELP),
                   allow_full: bool = typer.Option(False, "--allow-full", help=ALLOW_FULL_HELP)) -> RehearsalRun:
    wanted, runners = picked(lanes, os_names, "sm release rehearse start --help")
    return done("start", lambda: lib.command("start", wanted, runners, consumers, if_moved), allow_full)


@native("release rehearse step", writes=True, replaces=("hz-rehearse",), files=("rehearse_lib", "dispatch.py", "dispatch_lib", "window.py", "lock.py", "lock_lib"), review="release-rehearse-start.md", help=H(
    "Advance the open rehearsal: start pending lanes, read finished runs, write the verdict once all are done.",
    "Use after `sm release rehearse start`, until it prints the verdict.",
    "starts hosted runs for pending lanes; cancels the rehearsal's own runs when it must give way; deletes its branches at the verdict.",
    "sm release rehearse step",
    "sm release rehearse status",
    "Exit 1: the verdict is red.\n" + GUARDS))
def rehearse_step(allow_full: bool = typer.Option(False, "--allow-full", help=ALLOW_FULL_HELP)) -> RehearsalRun:
    return done("step", lambda: lib.command("step"), allow_full)


@native("release rehearse consumers", writes=True, replaces=("hz-rehearse",), files=("rehearse_lib", "dispatch.py", "dispatch_lib", "window.py", "lock.py", "lock_lib"), review="release-rehearse-start.md", help=H(
    "The plugin Consumer checks only: main's edge wheel against every plugin on plugins main.",
    "Use when only the plugin contract is in doubt; every rehearsal runs the lock checks too.",
    "downloads the edge wheel (removed afterwards), fetches plugins main into the clone, adds and removes a detached worktree of it;\n"
    "with --full runs `make check` per plugin under the run lock.",
    "sm release rehearse consumers",
    "sm release rehearse start",
    "Every plugin on plugins main gets the plugins repository's own validate_lock against the wheel, as compose runs it for a\n"
    "candidate (the wheel judged as the release it rehearses: its .dev suffix dropped). Exit 1: a lock is stale, plugins main has no\n"
    "plugin, a check failed, or the checks were unavailable (no green Edge build of main yet, or the run lock refused `make check`\n"
    "without --allow-full). It writes no verdict."))
def rehearse_consumers(only: str | None = typer.Option(None, "--only", metavar="NAME", help="Only this plugin."),
                       full: bool = typer.Option(False, "--full", help="Also run each plugin's `make check` (about 10 minutes; needs --allow-full)."),
                       allow_full: bool = typer.Option(False, "--allow-full", help=ALLOW_FULL_HELP)) -> ConsumerChecks:
    rt = runtime()
    try:
        with working(rt, allow_full):
            return checks_of(lib.run_consumers(lib.api("git/ref/heads/main")["object"]["sha"], only, full))
    except ERRORS as err:
        raise Refuse(f"the Consumer checks stopped: {err}", STATUS) from err


@native("release rehearse cancel", writes=True, replaces=("hz-rehearse",), files=("rehearse_lib", "dispatch.py", "dispatch_lib", "window.py", "lock.py", "lock_lib"), review="release-rehearse-start.md", help=H(
    "Stop the open rehearsal: cancel its own runs, delete its rehearsal/* branches, verdict incomplete.",
    "Use when a rehearsal must not go on, for example before a cut that cannot wait.",
    "cancels the rehearsal's own hosted runs and deletes its rehearsal/* branches (only those still on its throwaway commit).",
    "sm release rehearse cancel",
    "sm release rehearse status"))
def rehearse_cancel() -> RehearsalRun:
    return done("cancel", lambda: (lib.cancel(), 0))


@native("daemon rehearse", writes=True, tick=True, replaces=("hz-rehearse",), files=("rehearse_lib", "dispatch.py", "dispatch_lib", "window.py", "lock.py", "lock_lib"), review="release-rehearse-start.md", help=H(
    "One tick of the rehearsal: step the open one, else start one when main moved and the last start is old enough.",
    "Use for the scheduled job; by hand use `sm release rehearse start` and `step`.",
    "as `sm release rehearse start` and `step`: rehearsal/* branches, hosted runs, cancels, the lock checks, with --consumers `make check`.",
    "sm daemon rehearse --consumers --allow-full",
    "sm release rehearse status",
    "A tick never stops a loop: a guard that refuses (exit 75) or an error (exit 1) is the tick's result, and the next tick tries again.\n"
    "Exit 1 as well when the last verdict is red.\n" + GUARDS))
def daemon_rehearse(consumers: bool = typer.Option(False, "--consumers", help=CONSUMERS_HELP),
                    allow_full: bool = typer.Option(False, "--allow-full", help=ALLOW_FULL_HELP),
                    every: int | None = typer.Option(None, "--every", metavar="MIN", help="Minutes between two starts, at least 30; default: the setting rehearse_every_minutes."),
                    lanes: str = typer.Option(",".join(lib.LANES), "--lanes", metavar="A,B", help=LANES_HELP),
                    os_names: str = typer.Option(",".join(lib.OSES), "--os", metavar="A,B", help=OS_HELP)) -> RehearsalRun:
    wanted, runners = picked(lanes, os_names, "sm daemon rehearse --help")
    minutes = int(setting(runtime(), "rehearse_every_minutes")) if every is None else every
    try:
        found = done("tick", lambda: lib.command("tick", wanted, runners, consumers, False, minutes), allow_full)
    except Wait as wait:
        return RehearsalRun(action="tick", ran=False, message=wait.message, code=WAITING)
    except Refuse as no:
        return RehearsalRun(action="tick", ran=False, message=no.message, code=no.code)
    return found.model_copy(update={"ran": bool(found.message)})
