"""The machine-wide run lock: one heavy local command at a time (a few, while the machine is idle), none above a load
bar, and a capped number of test workers for whatever runs under it.

Why it exists: several agents on one machine each start a test run, a type check and a build. Without a lock the
load passes the number of CPUs several times over and whatever else the machine serves stops answering.

    RunLock(policy, commands, clock).run(Request(argv=["pytest", "tests/unit/test_x.py"], environ=dict(os.environ)))

Mechanics (all of it is `flock` on files in `policy.directory`, so it holds across processes and users of one account):
- heavy slots: slot 0 always; further slots only while the 1-minute load is below `extra_slot_load`
- no admission at or above `max_load`, at any priority
- priority 1 goes ahead of the normal queue, priority 2 ahead of 1; neither passes the slot limit or the load bar
- an exclusive request (a critical-path push whose hook runs a timed test session) queues as priority 2 and starts
  only when no heavy slot is in use; while it runs no other heavy command is admitted, so its session gets the machine
- a fresh hold file stops every admission except a request that is hold exempt
- light lane: commands the policy calls light skip the heavy slots and the hold, at most `light_slots` at a time.
  A command is light when the caller says so, when the policy says so, when it matches `policy.light_commands`
  (ruff, the lint target, read-only git), or when its p50 over the last green runs in the history is at most
  `policy.light_p50_seconds` (30 s; at least three green runs). Both lanes write the history, so the p50 is learned
- no rerun of a failure: a command that failed on this exact tree (`HEAD^{tree}` plus a hash of the dirty state,
  `policy.tree_state`) is refused until the tree changes, or the caller gives `force_rerun` with a reason. Failures
  of class infra-timeout and usage, and pushes, are exempt. The history line records tree, caller (`SM_CALLER`),
  lane and failure class
- a command started under a holder (the environment variable `policy.held_env` names a real ancestor that still
  holds a locked slot) runs at once: a pre-push hook inside a locked push must not wait for its own parent
- worker caps: the variables in `policy.worker_caps` are exported to the command, lowered to this run's share of
  `cpu_budget`; a caller's own lower value wins, a higher one does not
- `max_wait_seconds`: give up with exit 75 and nothing run

A project supplies a `Policy`: the numbers, the directory and file prefix, which commands it refuses outright
(`refusals`), which are light (`is_light`), and what else the command's environment needs (`child_env`).
The file names are `<prefix>.lock`, `<prefix>.heavy.N.lock`, `<prefix>.light.N.lock`, `<prefix>.hold` and the two
waiting directories; a project that replaces an older lock keeps its prefix, so both exclude each other.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Protocol

from . import USAGE, WAITING
from .models import Model
from .protocols import Clock, Result

TIMED_OUT = 124  # exit code of a command stopped by `timeout_seconds`
PARENT_HOPS = 64
PID = re.compile(r"[0-9]{1,10}")
# Whole-argv patterns (`re.fullmatch` on `shlex.join(argv)`): `make lint test` is not `make lint`, and an argument with a space is quoted.
LIGHT_COMMANDS = (r"(uv run )?ruff (check|format)( [^ ]+)*", r"make lint", r"git (status|diff|log|show|rev-parse|ls-files)( [^ ]+)*")
SCOPE_ENV = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "NODE_OPTIONS", "TEST_FILTER")  # variables that change what a test command runs
MIN_REASON = 8
NO_RERUN_BLOCK = re.compile(r"^(git push|make pr|gh )")
RERUNNABLE = ("infra-timeout", "usage")  # failures of these classes say nothing about the tree
LEARNED_MIN_RUNS = 3
HISTORY_TAIL = 5000  # rows read for the p50 and the rerun check
UNTRACKED_HASHED = 200



EXCLUSIVE_DEFAULT_WAIT_SECONDS = 1800  # `--exclusive` with no --max-wait gives up after 30 minutes (exit 75, nothing ran)

def bad_reason(reason: str) -> str:
    """Why `reason` is not a reason to rerun a failed command; empty when it is one. A word that starts with `-` is an option
    that swallowed the command line (`--force-rerun -- pytest`)."""
    if reason.strip().startswith("-") or len(reason.strip()) < MIN_REASON:
        return f"--force-rerun needs a reason of at least {MIN_REASON} characters that does not start with `-`, put before the `--`."
    return ""


def failure_class(argv: Sequence[str], code: int) -> str:
    """The class of a failed run, from the command and its exit code; empty for success. Exit 2 and 4 are usage errors.
    One of: infra-timeout, usage, lint, type, test, other."""
    if code == 0:
        return ""
    if code in (TIMED_OUT, 137, 143, -9, -15):
        return "infra-timeout"
    if code in (2, 4, 126, 127):  # argparse and pytest usage errors, not a command found
        return "usage"
    text = " ".join(argv)
    for name, pattern in (("lint", r"\b(ruff|typos|eslint|biome|lint)\b"), ("type", r"\b(mypy|pyright|tsc)\b"),
                          ("test", r"\b(pytest|vitest|playwright|jest|check)\b")):
        if re.search(pattern, text):
            return name
    return "other"


def git_tree_state(commands: Runner) -> Callable[[str], str | None]:
    """A `Policy.tree_state`: `HEAD^{tree}`, plus a short hash of the dirty state when there is one; None outside Git."""
    import hashlib

    def state(cwd: str) -> str | None:
        where = cwd or None
        tree = commands.run(["git", "rev-parse", "HEAD^{tree}"], cwd=where)
        if tree.rc != 0 or not tree.out.strip():
            return None
        diff = commands.run(["git", "diff", "HEAD"], cwd=where)
        untracked = commands.run(["git", "ls-files", "-o", "--exclude-standard"], cwd=where)
        if diff.rc != 0 or untracked.rc != 0:
            return None
        names = [n for n in untracked.out.split("\n")[:UNTRACKED_HASHED] if n]
        if not diff.out and not names:
            return tree.out.strip()
        contents = commands.run(["git", "hash-object", "--", *names], cwd=where).out if names else ""
        dirty = diff.out + "\0" + "\n".join(names) + "\0" + contents
        return f"{tree.out.strip()}+{hashlib.sha256(dirty.encode()).hexdigest()[:12]}"

    return state


class Runner(Protocol):
    """The part of the `Commands` provider the lock needs."""

    def run(self, argv: list[str], cwd: str | None = None, timeout: float | None = None) -> Result: ...

    def passthrough(self, argv: list[str], cwd: str | None = None, env: dict[str, str] | None = None, timeout: float | None = None) -> int: ...


def sandbox_load(value: float | None) -> Callable[[], float] | None:
    """A fixed load for tests. Honoured only under a HOME other than the account's real one (a sandbox, whose lock
    files are separate anyway), so it opens no way around the shared lock.

    Compatibility: the run lock itself no longer uses it; kept, unchanged, for callers that still import it."""
    import pwd

    if value is None or Path.home() == Path(pwd.getpwuid(os.getuid()).pw_dir):
        return None
    return lambda: float(value)


@dataclass(frozen=True)
class Refusal:
    """A command the policy does not run at all. `code` 2: never like this; 75: not now."""

    code: int
    text: str


Refuser = Callable[[Sequence[str], Mapping[str, str]], Refusal | None]


@dataclass(frozen=True)
class Policy:
    directory: Path
    prefix: str = "runlock"
    label: str = "sm gate"
    max_load: float = 40.0
    heavy_slots: int = 3
    heavy_slot_limit: int = 0  # slots an older configuration may still hold; 0: the same as heavy_slots
    extra_slot_load: float = 20.0
    cpu_budget: int = 9
    worker_caps: Mapping[str, int] = field(default_factory=dict)
    light_slots: int = 4
    light_commands: Sequence[str] = LIGHT_COMMANDS
    light_p50_seconds: float = 30.0
    tree_state: Callable[[str], str | None] | None = None  # none: no rerun refusal
    caller_env: str = "SM_CALLER"
    hold_seconds: float = 900.0
    held_env: str = "SM_GATE_HELD"
    history: Path | None = None
    refusals: Sequence[Refuser] = ()
    is_light: Callable[[Sequence[str]], bool] | None = None
    child_env: Callable[[dict[str, str]], None] | None = None
    status_hint: str = "sm gate status"

    def slot_file(self, number: int) -> Path:
        return self.directory / (f"{self.prefix}.lock" if number == 0 else f"{self.prefix}.heavy.{number}.lock")

    def light_slot_file(self, number: int) -> Path:
        return self.directory / f"{self.prefix}.light.{number}.lock"

    @property
    def waiting(self) -> Path:
        return self.directory / f"{self.prefix}.priority-waiting"

    @property
    def urgent(self) -> Path:
        return self.directory / f"{self.prefix}.urgent-waiting"

    @property
    def hold(self) -> Path:
        return self.directory / f"{self.prefix}.hold"

    @property
    def exclusive(self) -> Path:
        return self.directory / f"{self.prefix}.exclusive.lock"


@dataclass(frozen=True)
class Request:
    argv: Sequence[str]
    environ: Mapping[str, str] = field(default_factory=dict)  # the caller's environment; the command inherits it
    timeout_seconds: int | None = None  # bounds the command after admission
    priority: int = 0  # 0 normal, 1 ahead of normal, 2 ahead of 1
    max_wait_seconds: int = 0  # 0: wait as long as it takes
    light: bool = False  # the caller declares the command light
    hold_exempt: bool = False
    cwd: str = ""
    force_rerun: str = ""  # the reason to rerun a command that failed on this very tree
    exclusive: bool = False  # start only on an idle machine (no heavy slot in use) and admit no other heavy run meanwhile


@dataclass(frozen=True)
class Outcome:
    status: str  # ran | refused | gave_up
    exit_code: int
    lane: str = ""  # heavy | light | nested
    waited_seconds: float = 0.0
    ran_seconds: float = 0.0
    reason: str = ""


class Status(Model):
    """Who holds the run lock, and what the next command would get."""

    slots_in_use: int
    heavy_slots: int
    workers_per_gate: int
    cpu_budget: int
    extra_slot_load: float
    max_load: float
    load: float
    hold: bool = False
    holders: list[str] = []
    waiting_priority: int = 0
    waiting_urgent: int = 0
    exclusive: bool = False
    light_slots_in_use: int = 0
    light_slots: int = 0
    light_holders: list[str] = []

    def render(self) -> list[str]:
        lines = [f"heavy slots in use {self.slots_in_use}/{self.heavy_slots}, workers per gate {self.workers_per_gate} "
                 f"(budget {self.cpu_budget}; second gate only below load {self.extra_slot_load:.0f}, none at or above {self.max_load:.0f}; "
                 f"load {self.load:.0f})"]
        lines += [f"  held by {holder}" for holder in self.holders]
        lines.append(f"light slots in use {self.light_slots_in_use}/{self.light_slots} (short commands: no heavy slot, no load bar)")
        lines += [f"  light: {holder}" for holder in self.light_holders]
        if self.hold:
            lines.append("  a hold is set: only hold-exempt commands start")
        if self.exclusive:
            lines.append("  an exclusive run (a critical-path push) holds the machine: no other heavy command starts")
        if self.waiting_priority or self.waiting_urgent:
            lines.append(f"  waiting ahead of the normal queue: {self.waiting_urgent} urgent, {self.waiting_priority} priority")
        return lines


def stricter(value: object, default: int) -> int:
    """A caller's number may only lower a cap, never raise it."""
    try:
        return max(1, min(default, int(str(value))))
    except ValueError:
        return default


def alive(pid: str) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return True


def _try_lock(path: Path) -> IO[str] | None:
    """The open, exclusively locked file, or None when another process holds it."""
    handle = path.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


class RunLock:
    def __init__(self, policy: Policy, commands: Runner, clock: Clock, load: Callable[[], float] | None = None,
                 say: Callable[[str], None] | None = None, pid: Callable[[], int] = os.getpid, parent_pid: Callable[[], int] = os.getppid):
        self.p, self.commands, self.clock = policy, commands, clock
        self.load = load or (lambda: os.getloadavg()[0])
        self.say = say or (lambda text: None)
        self.pid, self.parent_pid = pid, parent_pid
        self._exclusive_handle: IO[str] | None = None

    # ------------------------------------------------------------------ reads
    def slots_in_use(self) -> int:
        """How many heavy slots are locked right now (probed with a non-blocking flock, released at once)."""
        used = 0
        for number in range(self.p.heavy_slots):
            try:
                probe = self.p.slot_file(number).open("a+")
            except OSError:
                continue
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                used += 1
            finally:
                probe.close()
        return used

    def on_hold(self, exempt: bool = False) -> bool:
        try:
            fresh = self.clock.now() - self.p.hold.stat().st_mtime < self.p.hold_seconds
        except OSError:
            return False
        return fresh and not exempt

    def _holders(self) -> list[str]:
        out = []
        for number in range(max(self.p.heavy_slots, self.p.heavy_slot_limit)):
            path = self.p.slot_file(number)
            try:
                text = path.read_text().strip()
            except OSError:
                continue
            if text and self._owns_locked_slot(path, text.split(maxsplit=1)[0]):
                out.append(text[:160])
        return out

    def status(self) -> Status:
        self._prepare()
        used = self.slots_in_use()
        return Status(slots_in_use=used, heavy_slots=self.p.heavy_slots, workers_per_gate=self.p.cpu_budget // max(1, used), cpu_budget=self.p.cpu_budget,
                      extra_slot_load=self.p.extra_slot_load, max_load=self.p.max_load, load=self.load(), hold=self.on_hold(),
                      holders=self._holders(), waiting_priority=self._count_waiting(self.p.waiting), waiting_urgent=self._count_waiting(self.p.urgent),
                      exclusive=self.exclusive_held(), light_slots_in_use=sum(1 for n in range(self.p.light_slots) if self._locked(self.p.light_slot_file(n))),
                      light_slots=self.p.light_slots, light_holders=self._light_holders())

    def _light_holders(self) -> list[str]:
        out = []
        for number in range(self.p.light_slots):
            path = self.p.light_slot_file(number)
            try:
                text = path.read_text().strip()
            except OSError:
                continue
            if text and self._owns_locked_slot(path, text.split(maxsplit=1)[0]):
                out.append(f"pid {text[:160]}")
        return out

    def exclusive_held(self) -> bool:
        """Whether an exclusive run holds the machine (probed with a non-blocking flock, released at once)."""
        probe = _try_lock(self.p.exclusive)
        if probe is None:
            return True
        probe.close()
        return False

    def busy_slots(self) -> int:
        """Heavy slots locked right now, including those an older configuration may still hold."""
        return sum(1 for number in range(max(self.p.heavy_slots, self.p.heavy_slot_limit)) if self._locked(self.p.slot_file(number)))

    @staticmethod
    def _locked(path: Path) -> bool:
        if not path.exists():
            return False
        probe = _try_lock(path)
        if probe is None:
            return True
        probe.close()
        return False

    # ------------------------------------------------------------------ waiting markers
    def _prepare(self) -> None:
        self.p.waiting.mkdir(parents=True, exist_ok=True)
        self.p.urgent.mkdir(parents=True, exist_ok=True)

    def _someone_waiting(self, directory: Path) -> bool:
        for marker in directory.iterdir():
            if alive(marker.name):
                return True
            marker.unlink(missing_ok=True)
        return False

    def _count_waiting(self, directory: Path) -> int:
        return sum(1 for marker in directory.iterdir() if alive(marker.name))

    # ------------------------------------------------------------------ a holder among the ancestors
    def _parent_of(self, pid: int) -> int:
        done = self.commands.run(["ps", "-o", "ppid=", "-p", str(pid)])
        return int(done.out.strip()) if done.rc == 0 and done.out.strip().isdigit() else 0

    @staticmethod
    def _owns_locked_slot(path: Path, pid: str) -> bool:
        """A leftover record alone is insufficient: its slot must still be locked."""
        try:
            with path.open("r+") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return handle.read().split(maxsplit=1)[:1] == [pid]
        except OSError:
            return False
        return False

    def holder_lane(self, environ: Mapping[str, str]) -> str | None:
        """The lane of a genuine ancestor that holds a slot; a stale or made-up marker gives None."""
        held = environ.get(self.p.held_env, "")
        if not PID.fullmatch(held):
            return None
        pid, hops = self.parent_pid(), 0
        while pid > 1 and hops < PARENT_HOPS and str(pid) != held:
            pid, hops = self._parent_of(pid), hops + 1
        if str(pid) != held:
            return None
        if any(self._owns_locked_slot(self.p.slot_file(number), held) for number in range(max(self.p.heavy_slots, self.p.heavy_slot_limit))):
            return "heavy"
        if any(self._owns_locked_slot(self.p.light_slot_file(number), held) for number in range(self.p.light_slots)):
            return "light"
        return None

    # ------------------------------------------------------------------ the run
    def _record_holder(self, handle: IO[str], env: dict[str, str], details: str = "") -> None:
        pid = str(self.pid())
        handle.seek(0)
        handle.truncate()
        handle.write(f"{pid}{details}\n")
        handle.flush()
        env[self.p.held_env] = pid

    def _child(self, request: Request, env: dict[str, str]) -> int:
        if request.timeout_seconds is None:
            return self.commands.passthrough(list(request.argv), env=env)
        code = self.commands.passthrough(list(request.argv), env=env, timeout=request.timeout_seconds)
        if code == TIMED_OUT:
            self.say(f"{self.p.label}: command timed out after {request.timeout_seconds}s")
        return code

    def _why_waiting(self, request: Request) -> str:
        """One line on what this run waits for: the hold, the load bar or the current holder (from the lock file)."""
        if self.on_hold(request.hold_exempt):
            return f"a hold is set ({self.p.hold})"
        load = self.load()
        if load >= self.p.max_load:
            return f"load {load:.0f} is at or above {self.p.max_load:.0f}"
        if request.exclusive:
            return f"an exclusive run waits for the machine to be idle ({self.busy_slots()} heavy run(s) still going)"
        if self.exclusive_held():
            return "an exclusive run (a critical-path push) holds the machine"
        try:
            holder = self.p.slot_file(0).read_text().strip()
        except OSError:
            holder = ""
        ahead = self._someone_waiting(self.p.urgent) or (request.priority == 0 and self._someone_waiting(self.p.waiting))
        return (f"the heavy slot is held by {holder[:160]}" if holder else "the heavy slot is taken") + ("; priority gates are ahead" if ahead else "")

    def _light_slot(self, request: Request, start: float) -> IO[str] | None:
        limit = request.max_wait_seconds
        while not limit or self.clock.now() - start < limit:
            for number in range(self.p.light_slots):
                handle = _try_lock(self.p.light_slot_file(number))
                if handle is not None:
                    return handle
            elapsed = self.clock.now() - start
            self.clock.sleep(max(0.0, min(2.0, limit - elapsed)) if limit else 2.0)
        return None

    def _heavy_slot(self, request: Request, start: float) -> tuple[IO[str] | None, str]:
        """The locked slot, or (None, why) after `max_wait_seconds`. An exclusive request also holds the exclusive
        file, kept in `self._exclusive_handle` until the run ends."""
        level, limit = (2 if request.exclusive else request.priority), request.max_wait_seconds
        if request.exclusive and not limit:  # an exclusive request holds others off; it never waits without end
            limit = EXCLUSIVE_DEFAULT_WAIT_SECONDS
        marker = (self.p.urgent if level == 2 else self.p.waiting) / str(self.pid())
        if level in (1, 2):
            marker.touch()
        told = -1.0
        try:
            while True:
                waited = self.clock.now() - start
                if limit and waited >= limit:
                    return None, (f"gave up after {waited:.0f}s (max wait {limit}s): {self._why_waiting(request)}. Nothing ran; run it "
                                  f"again later (`{self.p.status_hint}` shows who holds it).")
                if level == 2:
                    turn = True
                elif level == 1:
                    turn = not self._someone_waiting(self.p.urgent)
                else:
                    turn = not self._someone_waiting(self.p.waiting) and not self._someone_waiting(self.p.urgent)
                turn = turn and self.load() < self.p.max_load
                if turn and request.exclusive and not self.on_hold(request.hold_exempt):
                    found = self._exclusive_slot()
                    if found is not None:
                        return found, ""
                elif turn and not request.exclusive and not self.on_hold(request.hold_exempt) and not self.exclusive_held():
                    for number in range(self.p.heavy_slots):
                        if number and self.load() >= self.p.extra_slot_load:
                            break
                        handle = _try_lock(self.p.slot_file(number))
                        if handle is not None:
                            return handle, ""
                waited = self.clock.now() - start
                if told < 0 or waited - told >= 60:
                    told = waited
                    self.say(f"{self.p.label}: waiting {waited:.0f}s: {self._why_waiting(request)}. One heavy run at a time, and none above "
                             "the load bar, keeps this machine answering; this starts by itself when it is its turn.")
                self.clock.sleep(max(0.0, min(5.0, limit - waited)) if limit else 5.0)
        finally:
            marker.unlink(missing_ok=True)

    def _exclusive_slot(self) -> IO[str] | None:
        """Slot 0 and the exclusive file, when no heavy slot is in use; else None and nothing held."""
        if self.busy_slots():
            return None
        gate = _try_lock(self.p.exclusive)
        if gate is None:
            return None
        if self.busy_slots():  # someone was admitted between the count and the lock: let it finish first
            gate.close()
            return None
        handle = _try_lock(self.p.slot_file(0))
        if handle is None:
            gate.close()
            return None
        self._exclusive_handle = gate
        return handle

    def _rows(self) -> list[dict[str, Any]]:
        """The last rows of the history (oldest first); none when there is no history."""
        if self.p.history is None:
            return []
        try:
            lines = self.p.history.read_text().splitlines()[-HISTORY_TAIL:]
        except OSError:
            return []
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    @staticmethod
    def _key(argv: Sequence[str]) -> str:
        return " ".join(argv)[:200]

    @staticmethod
    def history_key(argv: Sequence[str], cwd: str = "", environ: Mapping[str, str] | None = None) -> str:
        """What a history row is about: a hash of the full argv, the directory it ran in and the variables that change its scope."""
        scope = {name: (environ or {}).get(name, "") for name in SCOPE_ENV}
        return hashlib.sha256(json.dumps([list(argv), cwd, scope], sort_keys=True).encode()).hexdigest()[:32]

    def learned_light(self, argv: Sequence[str], cwd: str = "", environ: Mapping[str, str] | None = None) -> bool:
        """Whether the p50 of this exact command's green runs (same full argv, directory and scope) is at most `light_p50_seconds`
        (at least three runs). Rows of an older version carry no key and teach nothing."""
        key = self.history_key(argv, cwd, environ)
        seconds = [float(r.get("ran_s", 0)) for r in self._rows() if r.get("key") == key and r.get("exit") == 0]
        return len(seconds) >= LEARNED_MIN_RUNS and statistics.median(seconds[-20:]) <= self.p.light_p50_seconds

    def is_light_command(self, argv: Sequence[str], cwd: str = "", environ: Mapping[str, str] | None = None) -> bool:
        text = shlex.join(argv)
        return any(re.fullmatch(pattern, text) for pattern in self.p.light_commands) or self.learned_light(argv, cwd, environ)

    def _rerun_refusal(self, request: Request, tree: str | None) -> str:
        """Why this command must not run again on this tree; empty when it may."""
        argv = list(request.argv)
        if not tree or request.force_rerun or NO_RERUN_BLOCK.match(" ".join(argv)):
            return ""
        key = self.history_key(argv, request.cwd, request.environ)
        last = next((r for r in reversed(self._rows()) if r.get("key") == key and r.get("tree") == tree), None)
        if last is None or last.get("exit") == 0 or last.get("failure") in RERUNNABLE or not last.get("failure"):
            return ""
        return (f"this command failed (exit {last.get('exit')}, class {last.get('failure')}) at {last.get('at')} on this exact tree "
                f"({tree}); nothing changed since. Fix the cause or change the tree (a change outside Git, such as an ignored file, a sibling "
                "checkout or a variable, does not count); to rerun anyway: --force-rerun REASON.")

    def _log(self, request: Request, lane: str, waited: float, ran: float, code: int, tree: str | None) -> None:
        if self.p.history is None:
            return
        row = {"at": datetime.fromtimestamp(self.clock.now()).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"), "cwd": request.cwd,
               "cmd": self._key(request.argv), "key": self.history_key(request.argv, request.cwd, request.environ), "priority": str(request.priority),
               "waited_s": round(waited), "ran_s": round(ran), "exit": code,
               "lane": lane, "tree": tree or "", "caller": request.environ.get(self.p.caller_env, ""), "failure": failure_class(request.argv, code)}
        if request.force_rerun:
            row["forced"] = request.force_rerun[:200]
        try:
            self.p.history.parent.mkdir(parents=True, exist_ok=True)
            with self.p.history.open("a") as handle:
                handle.write(json.dumps(row) + "\n")
        except OSError:
            pass

    def run(self, request: Request) -> Outcome:
        argv = list(request.argv)
        if not argv:
            return Outcome("refused", USAGE, reason="no command given")
        self._prepare()
        env = dict(request.environ)
        if self.p.child_env is not None:
            self.p.child_env(env)
        for name, cap in self.p.worker_caps.items():
            env[name] = str(stricter(env.get(name, cap), cap))
        # A refused command stays refused under an admitted ancestor: the check comes before the nested shortcut.
        for refuser in self.p.refusals:
            refusal = refuser(argv, request.environ)
            if refusal is not None:
                return Outcome("refused", refusal.code, reason=refusal.text)
        if request.force_rerun and (bad := bad_reason(request.force_rerun)):
            return Outcome("refused", USAGE, reason=bad)
        tree = self.p.tree_state(request.cwd) if self.p.tree_state is not None and argv[0] != "git" else None  # Git's own commands change the tree
        why = self._rerun_refusal(request, tree)
        if why:
            return Outcome("refused", USAGE, reason=why)
        ancestor = self.holder_lane(request.environ)
        light = not request.exclusive and (request.light or bool(self.p.is_light and self.p.is_light(argv)) or self.is_light_command(argv, request.cwd, request.environ))
        if ancestor == "heavy" or (ancestor == "light" and light):
            began = self.clock.now()
            code = self._child(request, env)
            return Outcome("ran", code, "nested", 0.0, self.clock.now() - began)
        start = self.clock.now()
        if light:
            slot = self._light_slot(request, start)
            if slot is None:
                return Outcome("gave_up", WAITING, "light", self.clock.now() - start,
                               reason=f"gave up waiting for a light slot (max wait {request.max_wait_seconds}s). Nothing ran.")
            with slot:
                self._record_holder(slot, env)
                waited = self.clock.now() - start
                self.say(f"{self.p.label}: light lane, acquired after {waited:.0f}s")
                began = self.clock.now()
                code = self._child(request, env)
                ran = self.clock.now() - began
                self._log(request, "light", waited, ran, code, tree)
                return Outcome("ran", code, "light", waited, ran)
        self._exclusive_handle = None
        held, why = self._heavy_slot(request, start)
        if held is None:
            return Outcome("gave_up", WAITING, "heavy", self.clock.now() - start, reason=why)
        with held, (self._exclusive_handle or contextlib.nullcontext()):
            waited = self.clock.now() - start
            self.say(f"{self.p.label}: acquired after {waited:.0f}s" + (" (exclusive: no other heavy run starts until this ends)" if request.exclusive else ""))
            # Shrink the worker caps to this run's share of the CPU budget (held is locked, so it counts as running).
            share = self.p.cpu_budget // max(1, self.slots_in_use())
            for name, cap in self.p.worker_caps.items():
                env[name] = str(stricter(env[name], min(cap, share)))
            self._record_holder(held, env, f" {request.cwd} {' '.join(argv)}")
            began = self.clock.now()
            code = self._child(request, env)
            ran = self.clock.now() - began
            self._log(request, "heavy", waited, ran, code, tree)
            return Outcome("ran", code, "heavy", waited, ran)

