"""The guards: the Claude Code hooks on Bash (`sm guard git`, `sm guard heavy`), the uv guard (`sm guard uv`), the load guard
tick (`sm daemon loadguard`), the agent load governor (`sm mini governor`, `sm daemon governor`). The output limiter
`sm lim` is the core's (`steuermaschine.commands.lim`); plugin.py hands it the setting `guards_lim_dir`. The leak sampler is `sm mini leakwatch sample` (native/leakwatch.py): a second sampler here
(`sm daemon leakwatch`, an unbounded store no command read) was retired after its review.

The logic is `guards_lib/` (moved, decisions unchanged). This module is the edge: typed options, result models, and the
binding of the library to the providers (processes, clock, store) and the settings of the call.

Exported for the read slice (no command here): `loadguard_paused` and `governor_digest` below, and
`guards_lib.legacy` (the former files, read-only).
"""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, Field

from steuermaschine import smprocs
from steuermaschine.config import child_environment
from steuermaschine.engine import Refuse, Wait
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import Group, native, runtime
from steuermaschine.protocols import Busy

from .. import guard_floor
from .. import state as statedir  # the plugin's state paths
from .guards_lib import governor, hooks, legacy, loadguard, uvguard

LOADGUARD_KIND = "heinzel.guards.loadguard"
GOVERNOR_KIND = "heinzel.guards.governor"
LIVE_UV_KIND = "heinzel.guards.live_uv"


class GuardsSettings(BaseModel):
    """Settings of the guards, a mixin of the plugin's table."""

    guards_management_marker: str = Field("HLD.md", description="A file whose presence in the working directory or above it marks the management checkout (bare heavy runs allowed).")
    guards_real_uv: Path | None = Field(None, description="The real uv that `sm guard uv` hands a passing command to; none: the first uv on PATH that is not the shim (`--shim`).")
    guards_lim_dir: Path | None = Field(None, description="Where `sm lim` writes the full output of a command whose output it cut (one file per call); none: `lim` in the project's state directory.")
    guards_governor_high: float = Field(45.0, description="Above this 1-minute load the governor stops builder processes.")
    guards_governor_low: float = Field(25.0, description="Below this 1-minute load the governor continues what it stopped, 6 per 20 s.")
    guards_governor_off_file: Path | None = Field(None, description="While this file exists the governor continues what it stopped and stops nothing; none: guards/governor.off in the state directory.")
    guards_sm_install_roots: list[Path] = Field([], description="Install roots of sm (the stable install's directory): every process run from below one is sm, which the load guard"
                                                " and the governor never pause; empty: the root of the running sm. No root at all: they pause nothing.")
    guards_loadguard_legacy_dir: Path | None = Field(None, description="The former load guard's directory; its stopped.json is read, never written; none: not adopted.")


# --- the host: what a test replaces ------------------------------------------------------------------------------------

def load_now() -> float:
    """The 1-minute load average of this machine."""
    return os.getloadavg()[0]


def user_id() -> int:
    return os.getuid()


def setting(name: str) -> Any:
    value = runtime().settings.get(name)
    return GuardsSettings.model_fields[name].default if value is None else value


def path_setting(name: str, fallback: str = "") -> Path | None:
    """A path setting, expanded. Without a value: `fallback` below the project's state directory, or None."""
    value = setting(name)
    if value is not None:
        return Path(str(value)).expanduser()
    rt: Any = runtime()
    return Path(rt.project.home) / fallback if fallback else None


def stamp(now: float) -> str:
    return datetime.fromtimestamp(now).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def sm_shapes() -> tuple[smprocs.Shapes | None, str]:
    """The process shapes of sm, or (None, why) when they cannot be established (then nothing is paused or stopped)."""
    try:
        return smprocs.establish(setting("guards_sm_install_roots") or [], sys.executable), ""
    except smprocs.Unestablished as err:
        return None, str(err)


def killer(commands: Any) -> Callable[[int, int], None]:
    """`kill(pid, signal)` through the Commands provider: a vanished or foreign pid is a failed `kill`, never an error."""
    def kill(pid: int, sig: int) -> None:
        commands.run(["kill", f"-{signal.Signals(sig).name.removeprefix('SIG')}", str(pid)], timeout=10)

    return kill


# --- the Claude Code hooks ---------------------------------------------------------------------------------------------

class HookVerdict(Model):
    """What a hook guard decided for one Bash command. A refusal is not this model: it is exit 2 with the reason on stderr."""

    allowed: bool = True

    def render(self) -> list[str]:
        return []


def read_hook(input_file: str) -> str:
    import sys

    return sys.stdin.read() if input_file == "-" else Path(input_file).read_text()


@native("guard git", writes=False, replaces=("hz-guard",), help=H(
    "Claude Code PreToolUse hook on Bash: refuse git push or commit that skips hooks or forces.",
    "Use for the command of the Bash PreToolUse hook; it reads the hook JSON on stdin.",
    "nothing (read-only).",
    "echo '{\"tool_input\": {\"command\": \"git push --no-verify\"}}' | sm guard git",
    "sm guard heavy",
    "Refuses (exit 2, the reason on stderr, which blocks the call): --no-verify, --force, --force-with-lease, -f and -n on a git push\n"
    "or commit (-f on a commit is allowed); and the rest of the floor: a script of the retired tools directory run by hand, `make pr`,\n"
    "a workflow dispatch on a PR head SHA. Fails CLOSED: input that is not the hook JSON blocks the call too (exit 2). For the hook\n"
    "itself use the console script sm-guard (standard library only, the same rules, safe when sm cannot load)."))
def guard_git(input_file: str = typer.Option("-", "--input", metavar="FILE", help="The hook JSON; - reads stdin.")) -> HookVerdict:
    try:
        command = hooks.read_command(read_hook(input_file))
    except (hooks.Unreadable, OSError) as err:
        raise Refuse(f"Blocked: the hook input could not be read ({err}).", code=2) from None
    why = hooks.git_refusal(command) or guard_floor.refusal(command)
    if why:
        raise Refuse(why, code=2)
    return HookVerdict()


@native("guard heavy", writes=False, replaces=("hz-testguard",), help=H(
    "Claude Code PreToolUse hook on Bash: refuse bare heavy runs (pytest, vitest, tsc, mypy, playwright, make) and the banned desktop tests.",
    "Use for the command of a Bash PreToolUse hook; it reads the hook JSON on stdin.",
    "nothing (read-only).",
    "echo '{\"tool_input\": {\"command\": \"pytest tests\"}, \"cwd\": \"/tmp\"}' | sm guard heavy",
    "sm gate run -- COMMAND",
    "Refuses (exit 2, the reason on stderr): the two desktop tests anywhere, even through the gate; heavy runs (also behind uv, npx,\n"
    "pnpm, python -m ...) that do not start with `sm gate run`. Bare heavy runs are allowed inside the management checkout (a directory\n"
    "at or above the cwd that holds the file of the setting guards_management_marker). Fails OPEN: input that is not the hook JSON passes."))
def guard_heavy(input_file: str = typer.Option("-", "--input", metavar="FILE", help="The hook JSON; - reads stdin.")) -> HookVerdict:
    try:
        text = read_hook(input_file)
    except OSError:
        return HookVerdict()
    found = hooks.read_heavy_input(text)
    if found is None:
        return HookVerdict()
    why = hooks.heavy_refusal(found[0], found[1], str(setting("guards_management_marker")))
    if why:
        raise Refuse(why, code=2)
    return HookVerdict()


# --- the uv guard ------------------------------------------------------------------------------------------------------

class UvRun(Model):
    """What the uv guard did with one uv command. uv's own output went to the terminal."""

    decision: str  # pass | isolated | allowed
    command: list[str]
    code: int

    @property
    def exit_code(self) -> int:
        return self.code

    def render(self) -> list[str]:
        return []


@native("guard uv", writes=False, replaces=("hz-uv-guard",), help=H(
    "Run uv unless the command would change the LIVE heinzel tool environment: then refuse it (exit 3).",
    "Use for the uv on PATH of agent and builder shells (a shim that calls `sm guard uv -- \"$@\"`).",
    "nothing by itself; a passing command is handed to the real uv, which does what it was asked; an allowed live change is recorded.",
    "sm guard uv -- tool list",
    "sm gate run -- COMMAND",
    "Refuses `uv tool install|upgrade|uninstall|reinstall` that names heinzel or heinzel-*, a local source whose pyproject names it, or\n"
    "`upgrade --all` while the live tool exists. It passes when UV_TOOL_DIR and UV_TOOL_BIN_DIR are both set outside the live directories,\n"
    "or when HZ_ALLOW_LIVE_UV=1 (recorded in the guard state, kind heinzel.guards.live_uv). Everything else, `uv tool run` included,\n"
    "passes untouched. The options of uv come after the `--`."))
def guard_uv(args: list[str] = typer.Argument(None, metavar="UV-ARGS...", help="The uv command line, after `--`.")) -> UvRun:
    rt: Any = runtime()
    args = list(args or [])
    environ = child_environment()  # the decision reads UV_TOOL_DIR, UV_TOOL_BIN_DIR and the live opt-in
    decision = uvguard.decide(args, environ, Path.home())
    if decision == "refused":
        raise Refuse(uvguard.refusal(args), "sm guard uv -- --help", code=3)
    if decision == "allowed":
        now = rt.providers.clock.now()
        rt.providers.store.save(LIVE_UV_KIND, f"{stamp(now)}-{os.getpid()}", {"at": stamp(now), "pid": os.getpid(), "ppid": os.getppid(), "cwd": os.getcwd(), "args": args})
    real = path_setting("guards_real_uv")
    if real is None:
        raise Refuse("the real uv is not configured: set heinzel.guards_real_uv", "sm self config", code=2)
    command = [str(real), *args]
    return UvRun(decision=decision, command=command, code=rt.providers.commands.passthrough(command))


# --- the load guard ----------------------------------------------------------------------------------------------------

class LoadguardTick(Model):
    """One decision of the load guard: pause one ungated test tree, continue one, or nothing."""

    action: str  # stop | resume | none
    pid: int | None = None
    load: float = 0.0
    readable: bool = True
    lines: list[str] = []

    @property
    def exit_code(self) -> int:
        return 0 if self.readable else 1

    def render(self) -> list[str]:
        return [*self.lines, f"{self.action} {self.pid or ''}".strip()] if self.readable else [*self.lines]


def loadguard_paused(store: Any, legacy_dir: Path | None = None) -> list[dict[str, Any]]:
    """The process trees the load guard paused: the store's record, else the former file's (read-only). For the status read."""
    record = store.load(LOADGUARD_KIND, "stopped")
    if record is not None:
        return list(record.get("stopped", []))
    return legacy.loadguard_stopped(legacy_dir) if legacy_dir else []


@native("daemon loadguard", writes=True, tick=True, replaces=("hz-loadguard",), help=H(
    "One tick of the load guard: above load 100 pause (SIGSTOP) the newest ungated pytest tree; below load 40 continue (SIGCONT) the paused ones.",
    "Use for the launchd job that protects the Mini from heavy runs started outside the run lock (--loop 20).",
    "sends SIGSTOP / SIGCONT to test process trees of this user and records which it paused; it never kills.",
    "sm daemon loadguard --loop 20",
    "sm gate status",
    "One tree per tick, never one that has the run lock, a release tool or a train among its ancestors, never another user's process, never\n"
    "the live service, never a process it did not pause (a paused pid that now runs something else is forgotten, not continued).\n"
    "One tick at a time: while another tick holds the guard's store lock this one does nothing (exit 75).\n"
    "If the process table cannot be read nothing is signalled and the paused list is kept (exit 1). sm processes are recognised by their\n"
    "real shape (the venv's python running the steuermaschine or sm entry, or anything under the install root of the setting\n"
    "guards_sm_install_roots, else of the running sm); if no install root can be established nothing is paused (exit 1)."))
def daemon_loadguard() -> LoadguardTick:
    store = runtime().providers.store
    try:
        with store.lock(LOADGUARD_KIND):  # one tick at a time: two ticks that load and save the paused list would forget a tree
            return loadguard_tick()
    except Busy:
        raise Wait("another load guard tick is running (the launchd loop or a manual tick); its paused list is not touched", "that tick") from None


def loadguard_tick() -> LoadguardTick:
    """One tick of the load guard, under its store lock."""
    rt: Any = runtime()
    commands, clock, store = rt.providers.commands, rt.providers.clock, rt.providers.store
    legacy_dir = path_setting("guards_loadguard_legacy_dir")
    lines: list[str] = []
    listing = commands.run(["ps", "-axo", loadguard.PS_FIELDS], timeout=30)
    if listing.rc != 0 or not listing.out.strip():
        return LoadguardTick(action="none", readable=False, lines=[f"{stamp(clock.now())} the process table could not be read: nothing signalled"])
    load = load_now()

    def load_state() -> list[dict[str, Any]]:
        return loadguard_paused(store, legacy_dir)

    def save_state(stopped: list[dict[str, Any]]) -> None:
        store.save(LOADGUARD_KIND, "stopped", {"stopped": stopped})

    shapes, why = sm_shapes()
    if shapes is None:
        lines.append(f"{stamp(clock.now())} the process shapes of sm could not be established ({why}): nothing is paused")
    edge = loadguard.Edge(kill=killer(commands), now=clock.now, load_state=load_state, save_state=save_state,
                          log=lambda line: lines.append(f"{stamp(clock.now())} {line}"))
    done = loadguard.tick(listing.out, load, edge, str(Path("~").expanduser()), user_id(), shapes)
    word, _, pid = done.partition(" ")
    return LoadguardTick(action=word, pid=int(pid) if pid else None, load=round(load, 1), readable=shapes is not None, lines=lines)


# --- the governor ------------------------------------------------------------------------------------------------------

class GovernedPid(Model):
    pid: int
    args: str


class GovernorStatus(Model):
    """The governor: load, thresholds, the kill switch, and the pids it holds stopped."""

    load: float
    high: float
    low: float
    off: bool
    stopped: list[GovernedPid] = []

    def render(self) -> list[str]:
        return [f"load {self.load:.1f} high={self.high:g} low={self.low:g} off={self.off} stopped={len(self.stopped)}",
                *[f"  {row.pid} {row.args}" for row in self.stopped]]


def governor_off_file() -> Path:
    found = path_setting("guards_governor_off_file", "guards/governor.off")
    assert found is not None
    return found


def governor_state(store: Any) -> dict[str, Any]:
    """The pids the governor holds stopped: the store's record, else the former file's (read-only, in the state directory)."""
    record: dict[str, Any] | None = store.load(GOVERNOR_KIND, "state")
    if record is not None:
        return record
    return legacy.governor_state(statedir.dashboard_dir(runtime().settings or {}))


def governor_digest(store: Any, load: float, now: float | None = None) -> str:
    """`GOV: stopped N pids, load L` for the digest, or nothing; with `now`, plus an ALARM for pids held stopped > 30 min. For the coordinator's digest."""
    governor.bind(float(setting("guards_governor_high")), float(setting("guards_governor_low")))
    return governor.digest_line(governor_state(store), load, now)


@native("mini governor", writes=False, replaces=("hz-governor",), help=H(
    "The agent load governor: load, thresholds, the kill switch and the builder pids it holds stopped.",
    "Use when builders seem stuck under load, or to see what the governor holds.",
    "nothing (read-only).",
    "sm mini governor",
    "sm daemon governor --dry-run",
    "To continue everything it holds create the file of the setting guards_governor_off_file and run one `sm daemon governor`."))
def mini_governor() -> GovernorStatus:
    rt: Any = runtime()
    st = governor_state(rt.providers.store)
    return GovernorStatus(load=round(load_now(), 1), high=float(setting("guards_governor_high")), low=float(setting("guards_governor_low")),
                          off=governor_off_file().exists(),
                          stopped=[GovernedPid(pid=int(k), args=v["args"]) for k, v in st["stopped"].items()])


class GovernorTick(Model):
    """One decision of the governor: what it stopped, what it continued."""

    load: float
    dry_run: bool
    readable: bool = True
    actions: list[str] = []
    lines: list[str] = []

    @property
    def exit_code(self) -> int:
        return 0 if self.readable else 1

    def render(self) -> list[str]:
        return [*self.lines, " ".join(self.actions) or "no action"] if self.readable else [*self.lines]


@native("daemon governor", writes=True, tick=True, replaces=("hz-governor",), help=H(
    "One tick of the agent load governor: stop builder processes above the high load, continue them below the low load.",
    "Use for the launchd job that keeps agent builds from starving the live service (--loop 15).",
    "sends SIGSTOP / SIGCONT to builder processes of this user and records which it stopped; --dry-run signals nothing.",
    "sm daemon governor --loop 15",
    "sm mini governor",
    "Above the high load (setting guards_governor_high, 45) it stops up to 6 builder processes per tick in descending CPU order; below the low\n"
    "load (25) it continues 6 per 20 s. Only test runners (pytest, vitest, playwright, jest, coverage run) whose args or cwd sit under\n"
    "builder worktrees, and their child processes, are eligible; the coordinator's checkout (outside its .worktrees), its commands,\n"
    "the live service, Claude.app, Chrome and the governor itself never are. One tick at a time (exit 75 while another runs). While the file of the setting guards_governor_off_file\n"
    "exists it continues everything and stops nothing. If the process table cannot be read nothing is signalled (exit 1). No sm process\n"
    "is ever eligible (recognised by its real shape and the install root, setting guards_sm_install_roots); if no install root can be\n"
    "established nothing is stopped (exit 1)."))
def daemon_governor(dry_run: bool = typer.Option(False, "--dry-run", help="Log the decisions, signal nothing, record nothing.")) -> GovernorTick:
    store = runtime().providers.store
    try:
        with store.lock(GOVERNOR_KIND):  # one tick at a time: two ticks that load and save the state would forget a stopped pid
            return governor_tick(dry_run)
    except Busy:
        raise Wait("another governor tick is running; its state is not touched", "that tick") from None


def governor_tick(dry_run: bool) -> GovernorTick:
    """One tick of the governor, under its store lock."""
    rt: Any = runtime()
    commands, clock, store = rt.providers.commands, rt.providers.clock, rt.providers.store
    governor.bind(float(setting("guards_governor_high")), float(setting("guards_governor_low")))
    listing = commands.run(governor.PS_ARGS, timeout=30)
    load = load_now()
    if listing.rc != 0 or not listing.out.strip():
        return GovernorTick(load=round(load, 1), dry_run=dry_run, readable=False, lines=[f"{stamp(clock.now())} the process table could not be read: nothing signalled"])
    lines: list[str] = []
    st = governor_state(store)

    def cwd(pid: int) -> str:
        return governor.cwd_of(commands.run(["lsof", "-a", "-d", "cwd", "-Fn", "-p", str(pid)], timeout=30).out)

    shapes, why = sm_shapes()
    if shapes is None:
        lines.append(f"{stamp(clock.now())} the process shapes of sm could not be established ({why}): nothing is stopped")
    acts = governor.step(load, st, governor.ps_table(listing.out), killer(commands), clock.now(), dry=dry_run, cwd=cwd,
                         off=governor_off_file().exists(), logf=lambda msg: lines.append(f"{stamp(clock.now())} {msg}"), shapes=shapes)
    if not dry_run:
        store.save(GOVERNOR_KIND, "state", st)
    return GovernorTick(load=round(load, 1), dry_run=dry_run, readable=shapes is not None, actions=[f"{kind}:{pid}" for kind, pid in acts], lines=lines)


GUARD = Group("guard", H(
    "guard: the hooks and shims that refuse what must not run on this machine.",
    "Use for the command of a Claude Code hook (guard git, guard heavy) or as the uv on PATH (guard uv).",
    "nothing by itself; see each command.",
    "sm guard git",
    "sm guard heavy"))
