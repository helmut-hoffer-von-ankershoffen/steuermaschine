"""`sm head validate` and `sm ci window`: hosted validation of a head on a throwaway commit, one head at a time.

The line of heads, the per-branch rule and the cap are the core's (`steuermaschine.window`); the runs are started by
the single-lane library (`dispatch_lib/focused.py`) with all of its guards; each lane runs on its own throwaway child
ref (the focused workflow's concurrency group is per ref, and GitHub keeps one pending run per group), and the one
atomic push of those refs goes through the run lock.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import typer
from pydantic import BaseModel, Field

from steuermaschine import WAITING
from steuermaschine.config import child_environment
from steuermaschine.engine import Refuse, Wait
from steuermaschine.github import Gh, GitHub
from steuermaschine.helpdoc import H
from steuermaschine.models import Error, Model
from steuermaschine.native import Group, native, runtime
from steuermaschine.runlock import Request
from steuermaschine.window import Released, Rules, Turn, Window, Withdrawn, process_alive, throwaway_branch

from . import dispatch, lock, release_window
from .dispatch_lib import focused
from .dispatch_lib import rules as c

NAMESPACE = "heinzel.validation"
TRAP = ("TRAP: while a validation run is active, focused admission refuses (prepare and apply); an admission in progress holds the line, so no new\n"
        "validation starts and a waiting `sm head validate` waits (the runs already going finish): `sm ci window status` shows the hold.\n"
        "TRAP: checks attach by SHA. The runs are on a throwaway child commit, never on the head: a PR head would sit BLOCKED.")


class WindowSettings(BaseModel):
    """Settings of the validation window, a mixin of the plugin's table."""

    window_lease_minutes: float = Field(180.0, description="A head that never gave the validation window back is passed after this long.")
    window_waiting_minutes: float = Field(30.0, description="A waiting head whose command stopped asking is dropped after this long.")
    release_window_minutes: float = Field(60.0, description="How long a release admit prepare or apply holds the release-window lock "
                                                            "(hosted validations wait meanwhile); renewed by each, given back by a successful apply.")


class LaneResult(Model):
    lane: str
    branch: str = ""  # its own throwaway child ref: lanes on one ref would share the workflow's concurrency group
    commit: str = ""
    run: int | None = None
    status: str = ""
    conclusion: str = ""
    fail_log: str = ""
    problem: str = ""  # why the lane did not start
    cancelled: str = ""  # queued | running: the run was cancelled by someone else (not by this command), so no verdict
    unread: str = ""  # why the result of a started run could not be read (a GitHub read error): no verdict either way

    @property
    def failed(self) -> bool:
        """The run finished and said no: the only lane state that is a verdict against the head."""
        return self.status == "completed" and self.conclusion not in ("success", "cancelled") and not self.unread


class Validation(Model):
    """The hosted validation of one head."""

    worktree: str
    branch: str
    head: str
    status: str  # validated | not_validated | incomplete | plan | detached
    validation_branches: list[str] = []
    operating_system: str = ""
    lanes: list[LaneResult] = []
    notes: list[str] = []
    handle: str = ""  # detached: what `sm head validate --wait HANDLE` takes
    finished: bool = False  # read back by --wait: the detached run is over, so nothing changes by asking again

    @property
    def exit_code(self) -> int:
        if self.finished and self.status == "incomplete":
            return 1  # a finished background run stays incomplete: exit 75 ("the same command again") would loop forever
        return {"not_validated": 1, "incomplete": WAITING}.get(self.status, 0)

    def render(self) -> list[str]:
        if self.status == "detached":
            return [*self.notes, f"DETACHED {self.branch} {self.head[:9]}: lanes {', '.join(row.lane for row in self.lanes)} run in the background. "
                    f"Next: sm head validate --wait {self.handle}   (returns within --max-wait minutes; exit 75 = still running: the same command again)"]
        if self.status == "incomplete" and any(row.cancelled for row in self.lanes):
            return [*self.notes, *(f"  {row.lane}: run {row.run or 'not started'} {row.status or '-'}/{row.conclusion or '-'}"
                                   + (f"  cancelled while {row.cancelled}, not by this command" if row.cancelled else "") for row in self.lanes),
                    "INCOMPLETE: a lane's run was cancelled before it gave a result (a concurrency group or by hand), so no verdict. "
                    f"Nothing is wrong with the head: run the same command again (sm head validate {self.worktree})."]
        if self.status == "incomplete" and any(row.unread for row in self.lanes):
            return [*self.notes, *(f"  {row.lane}: run {row.run or 'not started'} {row.status or '-'}/{row.conclusion or '-'}"
                                   + (f"  result unread: {row.unread}" if row.unread else "") for row in self.lanes),
                    "INCOMPLETE: GitHub did not answer for a lane's run after bounded retries, so its result is unknown and there is no verdict. "
                    f"Nothing is known to be wrong with the head: look at the run (gh run view RUN) or run the same command again (sm head validate {self.worktree})."]
        if self.status == "incomplete":
            kept = any(row.run and row.conclusion != "cancelled" for row in self.lanes)
            return [*self.notes, *(f"  {row.lane}: " + (("run " + str(row.run) + (f" kept {row.status or '-'}/{row.conclusion or '-'}" if kept else " cancelled"))
                                                        if row.run else "not started") + (f"  {row.problem}" if row.problem else "")
                                   for row in self.lanes),
                    ("INCOMPLETE: a lane did not start because GitHub's run inventory could not be read; the started runs were kept and watched, "
                     "but half a lane set is no verdict. " if kept else
                     "INCOMPLETE: not every lane could start, so none counts and the started runs were cancelled. ")
                    + f"Nothing is wrong with the head: run the same command again (sm head validate {self.worktree})."]
        if self.status == "plan":
            return [f"sm head validate: {self.branch} {self.head[:9]}: would push one throwaway child commit per lane through the run lock, "
                    f"then run one hosted job per lane on its own branch on {self.operating_system}:",
                    *(f"  {row.lane}: {row.branch}" for row in self.lanes),
                    "  and delete those branches afterwards. Nothing was pushed or dispatched."]
        lines = list(self.notes)
        red = [row for row in self.lanes if row.failed]
        for row in self.lanes:
            lines.append(f"  {row.lane}: run {row.run or 'not started'} {row.status or '-'}/{row.conclusion or '-'}" + (f"  {row.problem}" if row.problem else ""))
            if row in red:
                lines += [f"      {line[:220]}" for line in row.fail_log.strip().splitlines()[-15:]]
        if red:
            return [*lines, f"NOT VALIDATED: {len(red)} of {len(self.lanes)} lanes. Fix every named failure in ONE new commit, then validate again; " "a rerun is never the fix."]
        return [*lines, f"VALIDATED {self.head[:9]}: {', '.join(row.lane for row in self.lanes)} green on {self.operating_system}. " f"Next: sm head admit {self.worktree}"]


class WindowHead(Model):
    worktree: str
    result: str  # VALIDATED | NOT VALIDATED | WAIT | REFUSED | not started
    detail: str = ""


class WindowRun(Model):
    """Several heads through hosted validation, in the order given."""

    lanes: list[str]
    ran: bool
    heads: list[WindowHead] = []
    code: int = 0

    @property
    def exit_code(self) -> int:
        return self.code

    def render(self) -> list[str]:
        lines = [f"validation window: {len(self.heads)} head(s), one at a time, lanes {', '.join(self.lanes)}"]
        if not self.ran:
            lines += [f"  {n}. sm head validate {head.worktree} --lanes {','.join(self.lanes)}" for n, head in enumerate(self.heads, 1)]
            return [*lines, "Nothing was started (plan only). Add --run to execute them in this order.", TRAP]
        lines += [f"  {Path(head.worktree).name}: {head.result}" + (f"  {head.detail}" if head.detail else "") for head in self.heads]
        return lines


def the_window(rt) -> Window:
    s = rt.settings
    rules = Rules(cap=c.MAX_FOCUSED, lease_minutes=float(s.get("window_lease_minutes") or 180.0), waiting_minutes=float(s.get("window_waiting_minutes") or 30.0))
    return Window(rt.providers.store, rt.providers.clock, rules, NAMESPACE, alive=liveness)


def liveness(turn: Turn) -> bool | None:
    """Whether the command of a turn still runs (the core's check; a seam for the tests)."""
    return process_alive(turn)


def in_flight(lanes: int = 0, wants_arm: bool = False) -> int:
    """Focused runs in flight, counted as the start itself counts them: GitHub's own inventory of active runs of the
    focused workflow plus the tracked receipts, whoever started them. Refuses (SystemExit, as the library does) when
    the inventory is uncertain, when a start in doubt is unreconciled, or when the whole lane set could not start now
    for another reason than the cap (a release run, the job ceiling while main waits)."""
    with focused.admission_lock():
        pending = focused.STATE.with_suffix(".pending")
        if pending.exists():
            raise SystemExit(f"refusing: reconcile the uncertain focused start in {pending} before validating")
        runs = {}
        for status in c.ACTIVE:
            for run in focused.inventory(f"runs?status={status}&per_page=100", "workflow_runs"):
                if not isinstance(run.get("head_branch"), str) or not isinstance(run.get("path"), str):
                    raise SystemExit("refusing: uncertain GitHub active run state")
                runs[run["id"]] = {**run, "headBranch": run["head_branch"]}
        tracked = {row["run"] for row in focused.rows(strict=True) if row.get("result") is None}
        count = len(tracked | {rid for rid, run in runs.items() if focused.workflow_path(run) == f".github/workflows/{c.WORKFLOW}"})
        if lanes and (why := c.capacity_refusal(runs.values(), [], 0, wants_arm)):  # a release run: no lane would start
            raise SystemExit(why)
        return count


def lane_args(branch: str, lane: str, os_name: str, wait: bool) -> SimpleNamespace:
    """One lane as the single-lane library takes it. A lane always waits for a slot (up to the library's 30 minutes);
    `wait` says whether it also waits for its result before the next lane starts."""
    return SimpleNamespace(branch=branch, suite=lane, test=[], kind=None, os=os_name, repeat=1, stress=False, reviewed_head=None, throwaway=False,
                           cleanup=False, wait=wait, wait_for_slot=True, json=False, force=False)


LANE_NAME_MAX = 80  # the name part of a throwaway branch (steuermaschine.window.throwaway_branch cuts there)


def lane_branch(branch: str, lane: str, head: str) -> str:
    """validation/<name>-<lane>-<sha8>: one ref per lane. The focused workflow's concurrency group is per ref and GitHub
    keeps one pending run per group, so lanes sharing a ref cancel each other while queued. A long work branch is cut
    before the lane is appended, so the lane always survives the cut and two lanes never share a ref."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", branch.removeprefix("validation/")).strip("-.") or "branch"
    return throwaway_branch(f"{name[:LANE_NAME_MAX - len(lane) - 1].rstrip('-.')}-{lane}", head)


def left_branches(data: dict) -> list[str]:
    """The validation branches a turn recorded (space separated; one per lane)."""
    return str(data.get("validation_branch") or "").split()


def cancelled_while(found: dict) -> str:
    """queued when the cancelled run never got a job that ran, running otherwise."""
    ran = [job for job in found.get("jobs") or [] if job.get("conclusion") not in (None, "", "cancelled", "skipped")]
    return "running" if ran else "queued"


def started_run(lines: list[str]) -> int | None:
    return next((int(m[1]) for line in reversed(lines) if (m := re.search(r": run (\d+)$", line))), None)


def branch_state(rt, name: str) -> str:
    """exists | absent | unknown: <why>. Only a clear 404 is `absent`."""
    done = Gh(rt.providers.commands, repo=focused.R, timeout=60, clock=rt.providers.clock).run(["api", f"repos/{focused.R}/git/ref/heads/{name}"])
    if done.rc == 0:
        return "exists"
    return "absent" if "HTTP 404" in (done.err or "") else f"unknown: {Gh.why(done)}"


class Terminated(BaseException):
    """The command was told to stop (SIGTERM). Raised so that the cleanup of `validate` runs."""


class Stopper:
    """What SIGTERM does inside `stoppable`: raise Terminated, or, inside `shielded`, only remember it. The push and
    the cleanup are shielded: a stop in the middle of either would leave refs on the remote that nobody removes."""

    def __init__(self) -> None:
        self.shields, self.pending = 0, False

    def stop(self, signum, frame) -> None:
        if self.shields:
            self.pending = True
            return
        raise Terminated()

    @contextmanager
    def shielded(self, then_stop: bool) -> Iterator[None]:
        """Defer SIGTERM to the end of the block; `then_stop` raises it there when the block ended normally."""
        self.shields += 1
        try:
            yield
        finally:
            self.shields -= 1
        if then_stop and self.pending and not self.shields:
            self.pending = False
            raise Terminated()


@contextmanager
def stoppable() -> Iterator[Stopper]:
    """Turn SIGTERM into an exception for the time of the block: a killed validation still removes its branch and
    gives the window back. Outside the main thread it changes nothing."""
    stopper = Stopper()
    try:
        before = signal.signal(signal.SIGTERM, stopper.stop)
    except ValueError:
        yield stopper
        return
    try:
        yield stopper
    finally:
        signal.signal(signal.SIGTERM, before)


def cancel_runs(rt, rows: list[LaneResult]) -> None:
    hub = GitHub(rt.providers.commands, repo=focused.R, timeout=60, clock=rt.providers.clock)
    for row in rows:
        if row.run is not None and row.status != "completed":  # a finished run (--serial) keeps its result, red or green
            found = hub.cancel_run(str(row.run), live=True)
            row.status, row.conclusion = "cancelled" if found.state == "cancelled" else f"not cancelled ({found.state})", "cancelled"


def make_commits(commands, wt: str, head: str, rows: list[LaneResult]) -> None:
    """One empty child commit of the head per lane (the lane in the message keeps the SHAs apart). Refuses when one fails."""
    for row in rows:
        made = commands.run(["git", "-C", wt, "commit-tree", f"{head}^{{tree}}", "-p", head, "-m", f"ci: hosted validation of {head[:8]} ({row.lane})"],
                            timeout=60)
        commit = (made.out.strip().splitlines() or [""])[-1]
        if made.rc or not re.fullmatch(r"[0-9a-f]{40,64}", commit) or commit == head or commit in {other.commit for other in rows}:
            raise Refuse("could not create the throwaway commits; nothing was pushed", f"sm head validate {wt}")
        row.commit = commit


def remove_lane_branches(rows: list[LaneResult], notes: list[str]) -> None:
    for row in rows:
        try:
            removed = focused.delete_throwaway(row.branch, row.commit)
            notes.append(f"sm head validate: removed {row.branch}" if removed else
                         f"sm head validate: did NOT remove {row.branch}: it no longer points at {row.commit[:9]}")
        except SystemExit as stop:
            notes.append(f"sm head validate: could NOT delete {row.branch} ({stop.code}): sm ci window release --remove-branch {row.branch}")


def validate(rt, worktree: str, lanes: list[str], os_name: str, serial: bool, dry_run: bool, max_wait_minutes: float) -> Validation:
    wt = str(Path(worktree).expanduser().resolve())
    unknown = [lane for lane in lanes if lane not in c.LANES]
    if not lanes or unknown:
        raise Refuse(f"--lanes takes a comma list of {', '.join(c.LANES)}" + (f"; unknown: {', '.join(unknown)}" if unknown else ""), "sm head validate --help", code=2)
    if len(lanes) > c.MAX_FOCUSED:
        raise Refuse(f"at most {c.MAX_FOCUSED} lanes: that is the cap of focused runs in flight", "sm head validate --help", code=2)
    if os_name not in c.OSES:
        raise Refuse(f"--os is one of {', '.join(c.OSES)}", "sm head validate --help", code=2)
    vcs, clock, commands = rt.providers.vcs, rt.providers.clock, rt.providers.commands
    head, branch = vcs.head(wt), vcs.branch(wt)
    if branch in ("main", "master", "HEAD", "") or branch.startswith(("release/", "validation/")):
        raise Refuse(f"branch is '{branch}': validate works on a work branch, never on main, release/* or a validation branch", "sm head validate WORKTREE", code=2)
    if dirty := vcs.dirty(wt):
        raise Refuse("dirty tree: the throwaway commit carries only the committed tree, so commit first:\n" + dirty[:300], f"sm head validate {wt}")
    rows = [LaneResult(lane=lane, branch=lane_branch(branch, lane, head)) for lane in lanes]
    names = [row.branch for row in rows]
    if len(set(names)) != len(names):  # one ref per lane is the point; a clash would push one lane's commit only
        raise Refuse(f"two lanes would share one validation ref ({', '.join(names)}); this is a bug of sm head validate", "sm head validate --help")
    if dry_run:
        return Validation(worktree=wt, branch=branch, head=head, status="plan", validation_branches=names, operating_system=os_name, lanes=rows)
    lines: list[str] = []
    edge = dispatch.bound(rt, lines, [])
    window, start, arm = the_window(rt), clock.now(), os_name == "macos-latest"
    owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"  # this command's turn: nobody else resumes or ends it
    remember_waiter(rt, owner, wt, os.getpid())
    notes: list[str] = []
    started: list[LaneResult] = []
    pushed, incomplete, held, unreadable = False, False, False, False
    with stoppable() as stopper:
        try:
            try:
                while True:  # one head at a time, one request per branch, and the cap: wait, never force
                    release_hold(rt)
                    decision = window.enter(branch, head, len(lanes), in_flight(len(lanes), arm), owner=owner, pid=os.getpid(), host=socket.gethostname())
                    if decision.passed is not None:
                        left = left_branches(decision.passed.data)
                        notes.append(f"sm head validate: the window was held by a command that is gone ({decision.passed.branch} {decision.passed.head[:9]})"
                                     + (f"; it left {', '.join(left)}: " + "; ".join(f"sm ci window release --remove-branch {name}" for name in left) if left else ""))
                        rt.say(notes[-1])
                    if decision.state == "go":
                        held = True
                        break
                    if (clock.now() - start) / 60 >= max_wait_minutes:
                        raise Wait(f"the validation window is not free: {decision.reason}. Nothing was pushed", "the validation window (sm ci window status)")
                    rt.say(f"sm head validate: waiting for the validation window: {decision.reason}")
                    clock.sleep(60)
            except SystemExit as stop:
                reason = str(stop.code).removeprefix("refusing: ")
                if "release/*" in reason or "ceiling" in reason or "macOS/ARM" in reason:
                    raise Wait(f"hosted runners are not free for a whole lane set: {reason}. Nothing was pushed", "hosted runner capacity (sm ci one status)") from None
                raise Refuse(f"cannot count the focused runs in flight: {reason}. Nothing was pushed", "sm ci one status") from None
            for name in names:
                there = branch_state(rt, name)
                if there != "absent":
                    raise Refuse(f"{name} already exists on the remote: a validation of this head runs elsewhere or was left behind" if there == "exists"
                                 else f"cannot tell whether {name} exists on the remote ({there.removeprefix('unknown: ')})",
                                 f"sm ci window status   (a leftover branch with no run on it: sm ci window release --remove-branch {name})")
            # The tree and the parent are the head read above, not HEAD: a commit made meanwhile does not change what is validated.
            make_commits(commands, wt, head, rows)
            window.note(owner, {"validation_branch": " ".join(names), "commit": " ".join(row.commit for row in rows)})
            notes.append(f"sm head validate: {branch} {head[:9]} -> one throwaway child per lane on {os_name}: "
                         + ", ".join(f"{row.lane} {row.commit[:9]} on {row.branch}" for row in rows))
            rt.say(notes[-1])
            rt.say(TRAP)
            # one atomic push: every lane branch lands, or none does. A stop during the push waits for its end, so a ref
            # that landed is known and removed below.
            with stopper.shielded(then_stop=True):
                done = lock.lock_of().run(Request(argv=["git", "-C", wt, "push", "--atomic", "origin", *(f"{row.commit}:refs/heads/{row.branch}" for row in rows)],
                                                  environ=child_environment(), cwd=wt, timeout_seconds=7200))
                pushed = done.exit_code == 0  # from here on the branches are this command's to remove
            if done.exit_code == WAITING:
                raise Wait(f"the push of {', '.join(names)} must wait: {done.reason or 'the pre-push check said WAIT'}", "the run lock or the push check")
            if done.exit_code:
                raise Refuse(f"push of {', '.join(names)} failed (exit {done.exit_code}). {done.reason} "
                             "git's own message above names the cause: a ref the remote refused, or the pre-push hook (fix what it names, never --no-verify)",
                             f"sm head validate {wt}")
            for row in rows:  # every lane waits for its slot; serial: also for its result. A lane that cannot start ends the set
                started.append(row)
                del lines[:]
                edge.result = None
                read_error = False
                try:
                    focused.cmd_dispatch(lane_args(row.branch, row.lane, os_name, wait=serial))
                except SystemExit as stop:
                    row.problem = str(stop.code).removeprefix("refusing: ") if not isinstance(stop.code, int) else "refused by the SHA guard"
                    read_error = isinstance(stop, focused.Unreadable)
                row.run = started_run(lines)
                if row.run is None:
                    incomplete, unreadable = True, read_error
                    break
                if serial and edge.result:
                    take_result(row, edge.result)
                elif serial:  # started, but its result could not be read: no verdict, and no reason to stop the other lanes
                    row.unread, row.problem = row.problem or "the watch gave no result", ""
            if incomplete and not unreadable:  # all or nothing: half a lane set says nothing about the head
                cancel_runs(rt, started)
            if not incomplete or unreadable:  # a read error refuses the next start, never the runs already going: watch them
                for row in started:
                    if serial or row.run is None:
                        continue
                    edge.result = None
                    try:
                        focused.wait(row.run, None, False, f"validate {branch} {row.lane}")
                    except SystemExit as stop:
                        row.unread = str(stop.code).removeprefix("refusing: ")
                    if edge.result:
                        take_result(row, edge.result)
                    elif not row.unread:
                        row.unread = "the watch gave no result"
        except Terminated:
            cancel_runs(rt, started)
            raise Refuse(f"stopped by a signal: the started runs were cancelled, {', '.join(names) if pushed else 'nothing'} removed, the window is free",
                         f"sm head validate {wt}", code=143) from None
        finally:
            with stopper.shielded(then_stop=False):  # a stop during the cleanup lets it finish: no ref and no turn is left behind
                try:
                    if pushed:
                        remove_lane_branches(rows, notes)
                finally:
                    if held:  # a command that only waited keeps its place in the line for its next call
                        window.leave(branch, head, owner=owner)
                        rt.providers.store.save(WAITER_KIND, owner, {})
                if stopper.pending:
                    notes.append("sm head validate: a stop signal came during the cleanup; the cleanup finished first")
    return Validation(worktree=wt, branch=branch, head=head, status=verdict(rows, incomplete), validation_branches=names,
                      operating_system=os_name, lanes=rows, notes=notes)


def release_hold(rt: Any) -> None:
    """Exit 75 while a release prepare or apply holds the release-window lock: a validation's runs would stand in the
    way of the cut (its prepare and apply refuse while an uncovered Actions run is active)."""
    if hold := release_window.holder(rt):
        raise Wait(f"{hold.describe()}: validations wait so they do not block the cut. Nothing was pushed",
                   "the release window (a successful sm release admit apply gives it back)")


def take_result(row: LaneResult, found: dict) -> None:
    row.status, row.conclusion, row.fail_log = found.get("status") or "", found.get("conclusion") or "", found.get("faillog") or ""
    if row.conclusion == "cancelled":  # this command cancels only after the watch; a cancelled run here was someone else's doing
        row.cancelled, row.fail_log = cancelled_while(found), ""


def verdict(rows: list[LaneResult], incomplete: bool) -> str:
    """A lane that ran and failed is a verdict; a lane that did not start, was cancelled, or whose result could not be
    read is none. A start refused by a read error needs no case of its own: the runs it kept were watched."""
    if any(row.failed for row in rows):
        return "not_validated"
    if incomplete or any(row.conclusion != "success" for row in rows):
        return "incomplete"
    return "validated"


# Core's merge-tier lanes (ADR 0149): e2e-web-1/2 are the same whole browser suites as web-1/2. desktop and consumers
# run in the PR's own CI only: the focused job workflow has no node or consumer kind. managed is gone.
ALIASES = {"e2e-web-1": "web-1", "e2e-web-2": "web-2"}
NOT_HOSTED = {"desktop": "the desktop unit suite", "consumers": "the plugin consumers lane", "managed": "the removed managed lane"}


def lanes_of(text: str) -> list[str]:
    lanes: list[str] = []
    for lane in (word.strip() for word in text.split(",")):
        if lane in NOT_HOSTED:
            raise Refuse(f"{lane} is {NOT_HOSTED[lane]}: no focused job runs it on a throwaway commit; hosted PR CI is the validation",
                         "sm head validate --help", code=2)
        if lane and ALIASES.get(lane, lane) not in lanes:
            lanes.append(ALIASES.get(lane, lane))
    return lanes


@native("head validate", writes=True, replaces=("hz-ci-one",), files=("dispatch.py", "dispatch_lib", "lock.py", "lock_lib"), review="head-validate.md", help=H(
    "Run full hosted e2e lanes for a head on a throwaway child commit, never on the head SHA itself.",
    "Use before a packet is built, when a head touches web, e2e drivers or journeys (the candidate runs every journey on Linux).",
    "pushes one validation/<branch>-<lane>-<sha8> per lane through the run lock, dispatches one hosted run on each, deletes them afterwards.",
    "sm head validate ~/Code/heinzel-e2e-details --lanes web-1,web-2",
    "sm head admit WORKTREE   (when it prints VALIDATED); on a failure fix all of it in ONE new commit and validate again",
    "The validation window: one head at a time, one request per branch (a newer head replaces a waiting older one), and at most\n"
    "5 focused runs in flight; it waits (--max-wait, then exit 75: the same command again) and never forces. `sm ci window status`\n"
    "shows the line. Lanes run in parallel, each on its own branch: the focused workflow keeps one pending run per ref\n"
    "(--serial: one after another).\n" + TRAP + "\n"
    "Detached (an agent's tool call ends after 10 minutes): --detach starts the same validation in the background and returns\n"
    "a HANDLE at once; `sm head validate --wait HANDLE` waits up to --max-wait minutes (default 8) and prints the result, or\n"
    "exits 75 while it still runs (the same command again).\n"
    "Exit 0 VALIDATED (or DETACHED), 1 a lane failed or a refusal, 2 usage, 75 still waiting (same command again)."))
def head_validate(worktree: Path | None = typer.Argument(None, metavar="WORKTREE", help="Core worktree at the head to validate (clean tree); not with --wait."),
                  lanes: str = typer.Option("web-1,web-2", "--lanes", metavar="A,B", help="Comma list of web-1, web-2, cli, supervised (e2e-web-1, e2e-web-2: the same suites)."),
                  os_name: str = typer.Option("ubuntu-24.04", "--os", help="Hosted runner: ubuntu-24.04 or macos-latest."),
                  serial: bool = typer.Option(False, "--serial", help="One lane after another instead of in parallel."),
                  max_wait: float | None = typer.Option(None, "--max-wait", metavar="MIN",
                                                        help="Minutes to wait before exit 75: for the validation window (default 60), with --wait for the result (default 8)."),
                  dry_run: bool = typer.Option(False, "--dry-run", help="Print the branch and the lanes; push and start nothing."),
                  detach: bool = typer.Option(False, "--detach", help="Start the validation in the background and print its HANDLE at once."),
                  wait_handle: str | None = typer.Option(None, "--wait", metavar="HANDLE", help="Wait for a detached validation and print its result.")) -> Validation:
    rt = runtime()
    if wait_handle is not None:
        if worktree is not None or detach or dry_run:
            raise Refuse("--wait HANDLE takes no WORKTREE, --detach or --dry-run", "sm head validate --wait HANDLE", code=2)
        return await_detached(rt, wait_handle, WAIT_DEFAULT_MINUTES if max_wait is None else max_wait)
    if worktree is None:
        raise Refuse("WORKTREE is missing (or give --wait HANDLE)", "sm head validate WORKTREE", code=2)
    minutes = RUN_DEFAULT_MINUTES if max_wait is None else max_wait
    if detach:
        if dry_run:
            raise Refuse("--detach and --dry-run exclude each other", "sm head validate WORKTREE --dry-run", code=2)
        return start_detached(rt, str(worktree), lanes_of(lanes), os_name, serial, minutes)
    return validate(rt, str(worktree), lanes_of(lanes), os_name, serial, dry_run, minutes)


# ----------------------------------------------------------------------------- detached validations (bead p0hc)
RUN_DEFAULT_MINUTES, WAIT_DEFAULT_MINUTES = 60.0, 8.0
HANDLE = re.compile(r"[A-Za-z0-9._-]{1,120}")
# `sh -c` starts the validation in the background, its stdout (one JSON document) and stderr in files, and prints its pid.
SPAWN = 'out="$1"; shift; nohup "$@" >"$out.json" 2>"$out.log" </dev/null & echo $!'


class DetachedMeta(Model):
    """What `--detach` recorded next to the output of the background validation."""

    handle: str
    pid: int
    host: str
    worktree: str
    branch: str
    head: str
    lanes: list[str]
    started: float
    pid_started: str = ""  # `ps -o lstart=` of the child: a reused pid is not taken for it (an older meta has none)


def detached_dir(rt: Any) -> Path:
    """Where detached validations keep their output: `validations/` in the project's state directory."""
    return Path(rt.project.home) / "validations"


def this_program() -> list[str]:
    """This program, as a child process starts it."""
    here = Path(sys.argv[0])
    return [str(here)] if here.name in ("sm", "steuermaschine") and here.exists() else [shutil.which("sm") or "sm"]


def start_detached(rt: Any, worktree: str, lanes: list[str], os_name: str, serial: bool, max_wait: float) -> Validation:
    """The same checks as --dry-run, then the validation as a background process of its own; returns its handle."""
    plan = validate(rt, worktree, lanes, os_name, serial, True, max_wait)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", plan.branch).strip("-.")[:60] or "branch"
    handle = f"{name}-{plan.head[:8]}-{uuid.uuid4().hex[:6]}"
    folder = detached_dir(rt)
    folder.mkdir(parents=True, exist_ok=True)
    argv = [*this_program(), "--format", "json", "head", "validate", plan.worktree, "--lanes", ",".join(lanes), "--os", os_name,
            "--max-wait", f"{max_wait:g}", *(["--serial"] if serial else [])]
    done = rt.providers.commands.run(["/bin/sh", "-c", SPAWN, "sh", str(folder / handle), *argv], cwd=os.getcwd(), timeout=30, env=child_environment())
    pid = (done.out.strip().splitlines() or [""])[-1]
    if done.rc or not pid.isdigit():
        raise Refuse(f"could not start the detached validation: {(done.err or done.out).strip()[:200] or 'no pid'}", f"sm head validate {plan.worktree}")
    meta = DetachedMeta(handle=handle, pid=int(pid), host=socket.gethostname(), worktree=plan.worktree, branch=plan.branch, head=plan.head,
                        lanes=lanes, started=rt.providers.clock.now(), pid_started=process_start(rt, int(pid)))
    (folder / f"{handle}.meta.json").write_text(meta.model_dump_json() + "\n")
    return plan.model_copy(update={"status": "detached", "handle": handle,
                                   "notes": [f"sm head validate: started in the background as pid {pid}; output {folder / handle}.json, log {folder / handle}.log"]})


def detached_result(folder: Path, handle: str, worktree: str = "WORKTREE") -> Validation | None:
    """The finished validation's result, or None while its output is not complete. A refusal of the background command
    is raised as the same refusal here; a background command that ended waiting is over too: exit 1, start it again."""
    try:
        text = (folder / f"{handle}.json").read_text().strip()
    except FileNotFoundError:
        return None
    if not text:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        return None  # still being written
    if isinstance(doc, dict) and "error" in doc:
        found = Error.model_validate(doc)
        if found.code == WAITING:
            raise Refuse(f"the detached validation {handle} ended waiting, nothing was validated: {found.error}. It does not run any more: "
                         "start it again", f"sm head validate {worktree} --detach")
        raise Refuse(f"the detached validation {handle} was refused: {found.error}", found.hint or f"sm head validate {handle}", code=found.code or 1)
    return Validation.model_validate(doc)


def await_detached(rt: Any, handle: str, max_wait: float) -> Validation:
    """Wait up to `max_wait` minutes for a detached validation; its result, or exit 75 while it still runs."""
    if not HANDLE.fullmatch(handle):
        raise Refuse(f"{handle!r} is not a handle of `sm head validate --detach`", "sm head validate --help", code=2)
    folder, clock = detached_dir(rt), rt.providers.clock
    try:
        meta = DetachedMeta.model_validate_json((folder / f"{handle}.meta.json").read_text())
    except (OSError, ValueError):
        raise Refuse(f"no detached validation {handle} on this machine", f"ls {folder}", code=2) from None
    start = clock.now()
    while True:
        result = detached_result(folder, handle, meta.worktree)
        if result is not None:
            again = [f"sm head validate: it ended incomplete; start it again: sm head validate {meta.worktree} --detach"] if result.status == "incomplete" else []
            return result.model_copy(update={"finished": True, "notes": [*result.notes, f"sm head validate: detached validation {handle} finished", *again]})
        alive = liveness(Turn(branch=meta.branch, head=meta.head, runs=0, requested=meta.started, asked=meta.started, pid=meta.pid, host=meta.host))
        if alive and meta.pid_started and process_start(rt, meta.pid) != meta.pid_started:
            alive = False  # S5: the pid runs, but it is another process now (its start time differs): the child is gone
        if alive is False and detached_result(folder, handle, meta.worktree) is None:
            raise Refuse(f"the detached validation {handle} (pid {meta.pid}) ended without a result: read {folder / handle}.log",
                         f"sm head validate {meta.worktree}")
        if (clock.now() - start) / 60 >= max_wait:
            raise Wait(f"the detached validation {handle} of {meta.branch} {meta.head[:9]} still runs (pid {meta.pid}, "
                       f"{(clock.now() - meta.started) / 60:.0f} min)", f"sm head validate --wait {handle}")
        clock.sleep(15)


CI_WINDOW = Group("ci window", H(
    "ci window: the hosted validation window; who holds it, who waits, and several heads through it in order.",
    "Use when two or more heads need hosted lanes before admission.",
    "status only reads; run pushes and deletes one validation branch per lane of each head and starts hosted runs; release ends a dead holder's turn.",
    "sm ci window status",
    "sm head validate WORKTREE"))


@native("ci window run", writes=True, replaces=("hz-ci-one",), files=("dispatch.py", "dispatch_lib", "lock.py", "lock_lib"), review="head-validate.md", help=H(
    "Several heads through `sm head validate`, one head at a time, in the order given.",
    "Use when two or more heads need hosted lanes before admission and you want them serialised, not racing for runners.",
    "nothing without --run (prints the order); with --run each head pushes and deletes its validation branch and starts hosted runs.",
    "sm ci window run ~/Code/heinzel-e2e-details ~/Code/heinzel-chat-composer --lanes web-1,web-2",
    "sm head admit WORKTREE for every head it reports VALIDATED, one at a time",
    "With --run it goes on after a failed head and stops at the first WAIT (exit 75): the rest would wait as well.\n" + TRAP))
def ci_window_run(worktrees: list[Path] = typer.Argument(..., metavar="WORKTREE...", help="Core worktrees, in the order to validate."),
                  lanes: str = typer.Option("web-1,web-2", "--lanes", metavar="A,B", help="Comma list of web-1, web-2, cli, supervised (e2e-web-1, e2e-web-2: the same suites)."),
                  os_name: str = typer.Option("ubuntu-24.04", "--os", help="Hosted runner."),
                  run: bool = typer.Option(False, "--run", help="Execute the window instead of printing it.")) -> WindowRun:
    rt, wanted = runtime(), lanes_of(lanes)
    if not run:
        return WindowRun(lanes=wanted, ran=False, heads=[WindowHead(worktree=str(wt), result="not started") for wt in worktrees])
    heads, code = [], 0
    for wt in worktrees:
        if code == WAITING:
            heads.append(WindowHead(worktree=str(wt), result="not started"))
            continue
        try:
            found = validate(rt, str(wt), wanted, os_name, False, False, 0.0)
            heads.append(WindowHead(worktree=str(wt), result="VALIDATED" if found.status == "validated" else "NOT VALIDATED"))
            code = max(code, found.exit_code)
        except Wait as wait:
            heads.append(WindowHead(worktree=str(wt), result="WAIT", detail=wait.message))
            code = WAITING
        except Refuse as no:
            heads.append(WindowHead(worktree=str(wt), result="REFUSED", detail=no.message))
            code = max(code, no.code)
    return WindowRun(lanes=wanted, ran=True, heads=heads, code=code)


class WindowRelease(Model):
    """What `sm ci window release` did."""

    released: bool
    reason: str
    branch_removed: str = ""  # space separated when several lane branches were removed
    notes: list[str] = []

    def render(self) -> list[str]:
        return [self.reason, *self.notes]


def remove_branch(rt, name: str) -> str:
    """Delete a leftover validation branch, only when no run is active on it. Returns what was done; refuses otherwise."""
    if not name.startswith("validation/"):
        raise Refuse(f"{name} is not a validation branch: only validation/* is ever removed here", "sm ci window status", code=2)
    try:
        with focused.admission_lock():
            busy = [row["run"] for row in focused.rows(strict=True) if row.get("result") is None and row.get("branch") == name]
            for status in c.ACTIVE:
                busy += [run["id"] for run in focused.inventory(f"runs?status={status}&per_page=100", "workflow_runs") if run.get("head_branch") == name]
        if busy:
            raise Refuse(f"{name} has active runs ({', '.join(str(run) for run in sorted(set(busy)))}): wait for them or cancel them first", "sm ci one status")
        removed = focused.delete_throwaway(name, focused.remote_tip(name))
    except SystemExit as stop:
        raise Refuse(f"cannot remove {name}: {str(stop.code).removeprefix('refusing: ')}", "sm ci window status") from None
    return f"removed {name}" if removed else f"did not remove {name}"


@native("ci window release", writes=True, replaces=("hz-ci-one",), files=("dispatch.py", "dispatch_lib", "lock.py", "lock_lib"), review="head-validate.md", help=H(
    "End the turn of a validation whose command is gone, and remove the validation branches it left behind.",
    "Use when `sm ci window status` shows a holder whose command is gone, or `sm head validate` names a leftover branch.",
    "the window's state on this machine; with --remove-branch deletes that validation/* branch on the remote when no run is active on it.",
    "sm ci window release --remove-branch validation/fix-clock-0a1b2c3d",
    "sm head validate WORKTREE",
    "Without --force it ends a turn only when the holder's command is gone or its lease ran out; a live holder is left alone.\n"
    "--force ends a live holder's turn too (its runs go on, under the cap): only after you checked that nobody waits for it.\n"
    "--remove-branch NAME works with or without a held window; without NAME it removes the branches the released holder recorded."))
def ci_window_release(force: bool = typer.Option(False, "--force", help="End the turn although the holder's command still runs."),
                      remove: bool = typer.Option(False, "--remove", help="Also remove the validation branch the released holder recorded."),
                      remove_branch_name: str = typer.Option("", "--remove-branch", metavar="NAME", help="Remove this leftover validation/* branch.")) -> WindowRelease:
    rt = runtime()
    dispatch.bound(rt, [], [])
    found: Released = the_window(rt).release(force=force)
    notes, removed = [], ""
    left = left_branches(found.turn.data) if found.released and found.turn else []
    targets = [remove_branch_name] if remove_branch_name else left if remove else []
    for target in targets:  # one branch per lane
        notes.append(remove_branch(rt, target))
        if notes[-1].startswith("removed"):
            removed = " ".join(filter(None, (removed, target)))
    if not targets and left:
        notes += [f"it left {name} on the remote: sm ci window release --remove-branch {name}" for name in left]
    return WindowRelease(released=found.released, reason=found.reason, branch_removed=removed, notes=notes)


@native("ci window release-hold", writes=True, replaces=("hz-ci-one",), files=("dispatch.py", "dispatch_lib", "lock.py", "lock_lib"), review="head-validate.md", help=H(
    "Remove a stale admission hold of the validation window by hand.",
    "Use when `sm ci window status` shows a hold whose admission is long gone (a reused pid keeps it looking alive) and no validation can start.",
    "the window's state on this machine: that one hold.",
    "sm ci window release-hold OWNER",
    "sm ci window status",
    "OWNER is the token `sm ci window status` prints after `hold`. A hold whose command still runs, or may (another machine), is left alone unless --force."))
def ci_window_release_hold(owner: str = typer.Argument(..., metavar="OWNER", help="The hold's owner token, as `sm ci window status` prints it."),
                           force: bool = typer.Option(False, "--force", help="Remove the hold although its command may still run.")) -> Withdrawn:
    return the_window(runtime()).release_hold(owner, force=force)


class WindowDrop(Model):
    """What `sm ci window drop` did."""

    dropped: list[Turn] = []
    stopped: list[int] = []  # the waiting commands this call stopped (their owner proved the head with --worktree)
    notes: list[str] = []

    def render(self) -> list[str]:
        return [*(f"dropped {turn.branch} {turn.head[:9]} from the line" + (f" (stopped its command, pid {turn.pid})" if turn.pid in self.stopped else "")
                  for turn in self.dropped), *self.notes]


WAITER_KIND = "heinzel.window-waiter"  # per turn owner: the worktree and the start time of the command that asked


def process_start(rt: Any, pid: int) -> str:
    """When process `pid` started (`ps -o lstart=`), or "" when it does not run or cannot be read: with the pid it names
    one process, so a reused pid is never taken for the command that asked."""
    done = rt.providers.commands.run(["ps", "-o", "lstart=", "-p", str(int(pid))], timeout=10)
    return done.out.strip() if done.rc == 0 else ""


def remember_waiter(rt: Any, owner: str, worktree: str, pid: int) -> None:
    """What `ci window drop` checks before it stops a waiting command: its worktree and its process start time."""
    rt.providers.store.save(WAITER_KIND, owner, {"worktree": worktree, "pid": pid, "started": process_start(rt, pid)})


def stop_process(pid: int) -> None:
    """End a waiting validation of the caller's own head: SIGTERM, which `validate` turns into its normal cleanup."""
    os.kill(pid, signal.SIGTERM)


def matches(turn: Turn, head: str) -> bool:
    """HEAD names a turn by its branch, its full SHA or a SHA prefix of at least 7 characters."""
    return turn.branch == head or (len(head) >= 7 and re.fullmatch(r"[0-9a-f]+", head) is not None and turn.head.startswith(head))


@native("ci window drop", writes=True, replaces=("hz-ci-one",), files=("dispatch.py", "dispatch_lib", "lock.py", "lock_lib"), review="ci-window-drop.md", help=H(
    "Take a waiting head out of the validation window's line: its command is gone, or you own the head and give up its turn.",
    "Use when `sm ci window status` lists a head that no longer needs hosted validation, or a waiter whose command died.",
    "the window's state on this machine; for a waiter whose command still runs here, stops that command (SIGTERM) first.",
    "sm ci window drop fix/clock --worktree ~/Code/heinzel-clock",
    "sm ci window status",
    "HEAD is the branch, the full SHA or a SHA prefix (7 or more). A waiter whose command is gone is dropped for anyone (the\n"
    "window also drops it by itself at the next look). A waiter whose command still runs is the owner's: --worktree must be the\n"
    "worktree that asked (at exactly that head), and the pid must still be that command (its recorded start time), then that\n"
    "command is stopped. A waiter on another machine is refused. The holder of the\n"
    "window is never dropped here: `sm ci window release` ends a gone holder's turn."))
def ci_window_drop(head: str = typer.Argument(..., metavar="HEAD", help="The branch, full SHA or SHA prefix (7+) of the waiting head."),
                   worktree: Path | None = typer.Option(None, "--worktree", metavar="DIR", help="Your worktree at that head: proves you own a waiter whose command still runs.")) -> WindowDrop:
    rt = runtime()
    window = the_window(rt)
    found = window.status()
    if found.active is not None and matches(found.active, head):
        raise Refuse(f"{found.active.branch} {found.active.head[:9]} holds the window, it does not wait: drop takes waiting heads only",
                     "sm ci window release   (ends the turn of a holder whose command is gone)")
    targets = [turn for turn in [*found.waiting, *found.gone] if matches(turn, head)]  # a gone waiter is still in the stored line until someone looks
    if not targets:
        raise Refuse(f"no waiting head matches {head!r}", "sm ci window status")
    mine, wt = "", ""
    if worktree is not None:
        wt = str(worktree.expanduser().resolve())
        mine = rt.providers.vcs.head(wt)
    result = WindowDrop()
    for turn in targets:
        alive = liveness(turn)
        if alive is None:
            raise Refuse(f"{turn.branch} {turn.head[:9]} waits from another machine ({turn.host or '?'}): its command cannot be checked or stopped from here",
                         "sm ci window status   (it leaves the line by itself when its command stops asking)")
        if alive:
            if mine != turn.head:
                raise Refuse(f"{turn.branch} {turn.head[:9]} waits and its command still runs (pid {turn.pid}): only its owner drops it, "
                             "with --worktree at exactly that head" + (f" (that worktree is at {mine[:9]})" if mine else ""),
                             f"sm ci window drop {head} --worktree YOUR_WORKTREE")
            asked = rt.providers.store.load(WAITER_KIND, turn.owner) or {}
            if not asked.get("worktree") or str(Path(str(asked["worktree"])).expanduser().resolve()) != wt:
                raise Refuse(f"{turn.branch} {turn.head[:9]} was asked for from " + (str(asked.get("worktree")) if asked.get("worktree") else "an unrecorded worktree")
                             + f", not {wt}: a worktree at the same head is not ownership, only the worktree that asked is", "sm ci window status")
            if not asked.get("started") or asked.get("pid") != turn.pid or process_start(rt, turn.pid) != asked["started"]:
                raise Refuse(f"pid {turn.pid} cannot be shown to be the command that asked (its start time differs or was not recorded; "
                             "the pid may be reused): nothing is stopped", "sm ci window status   (a gone command leaves the line by itself)")
            try:
                stop_process(turn.pid)
                result.stopped.append(turn.pid)
            except ProcessLookupError:
                result.notes.append(f"pid {turn.pid} ended before the stop")
        window.leave(turn.branch, turn.head, owner=turn.owner)
        rt.providers.store.save(WAITER_KIND, turn.owner, {})
        result.dropped.append(turn)
    return result
