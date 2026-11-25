"""Native commands: a function with typed parameters that returns a result model, registered with `@native`.

    @native("queue list", help=H(...))                      # reads only: on as soon as it is merged
    def queue_list(all_: bool = typer.Option(False, "--all")) -> QueueList: ...

    @native("queue add", writes=True, help=H(...))          # writes: takes the writers' lock; reviewed once per promote
    def queue_add(pr: int) -> QueueAdded: ...

    @native("daemon queue", writes=True, tick=True, help=H(...))   # one tick; the core adds --loop SECONDS
    def daemon_queue() -> QueueTick: ...

A plugin returns its commands from the hook `sm_native` (use `collect(module, ...)`). The core builds the command at
`path`, sets `runtime()` for the call, renders the returned model (text: `model.render()`, json: one document) and
exits with `model.exit_code`. A function fails by raising `engine.Refuse` or `engine.Wait`; it never exits or prints
errors itself. Progress goes through `runtime().say(text)` to stderr.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import typing
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any, TypeVar

import typer

from . import WAITING
from .engine import Refuse, Wait
from .helpdoc import H
from .protocols import Providers
from .surface import Default, Renamed, Surface, Verb  # the visible surface, declared next to Group and Alias

F = TypeVar("F", bound=Callable[..., Any])

@dataclass(frozen=True)
class NativeCommand:
    path: str  # "queue list": the command path without the program name
    fn: Callable[..., Any]
    help: str
    writes: bool = False  # True: writes state other commands depend on, or anything outside this machine
    tick: bool = False  # True: one tick of a loop; the core adds --loop SECONDS
    replaces: tuple[str, ...] = ()  # old tool names this command takes over, for the tool map

    @property
    def model(self) -> Any:
        hint = typing.get_type_hints(self.fn).get("return")
        args = typing.get_args(hint)
        return args[0] if args else hint

    @property
    def models(self) -> list[Any]:
        """Every result model the command can print: the return annotation, through `X | Y`, `X | None` and `list[X]`.
        A command that returns None prints `models.Nothing`."""
        from .models import Model

        found, todo = [], [typing.get_type_hints(self.fn).get("return")]
        while todo:
            hint = todo.pop(0)
            if isinstance(hint, type) and issubclass(hint, Model):
                found.append(hint)
            else:
                todo += [a for a in typing.get_args(hint) if a is not type(None)]
        return found

    def code_files(self) -> list[Any]:
        """The code of this command: the module that defines it and, next to it, the package `<module>_lib` with the
        logic it moved in. `reviewgraph.reach` follows the imports from here (`sm self reviews`)."""
        import sys
        from pathlib import Path

        module = sys.modules.get(self.fn.__module__)
        source = Path(getattr(module, "__file__", "") or "")
        if not source.is_file():
            return []
        lib = source.with_name(source.stem + "_lib")
        return [source, *sorted(p for p in lib.rglob("*.py"))] if lib.is_dir() else [source]


CORE = "steuermaschine"


def _roots(module: str) -> dict[str, Path]:
    """Top-level package name -> its directory, for the first-party packages: core and the command's own package."""
    import sys

    out: dict[str, Path] = {}
    for top in {CORE, module.split(".")[0]}:
        source = getattr(sys.modules.get(top), "__file__", None)
        if source:
            out[top] = Path(source).resolve().parent
    return out


def _module_of(path: Path, roots: dict[str, Path]) -> tuple[str, bool]:
    """(dotted module name, is a package) of a file below one of the roots; ("", False) outside them."""
    for top, root in roots.items():
        try:
            rel = path.resolve().relative_to(root.parent)
        except ValueError:
            continue
        parts = list(rel.with_suffix("").parts)
        if parts[0] != top:
            continue
        if parts[-1] == "__init__":
            return ".".join(parts[:-1]), True
        return ".".join(parts), False
    return "", False


def _resolve(dotted: str, roots: dict[str, Path]) -> Path | None:
    root = roots.get(dotted.split(".")[0])
    if root is None:
        return None
    base = root.parent.joinpath(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate.resolve()
    return None


def _imports_of(path: Path, roots: dict[str, Path]) -> list[Path]:
    import ast

    name, package = _module_of(path, roots)
    found: list[Path] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found += [hit for alias in node.names if (hit := _resolve(alias.name, roots))]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                here = name.split(".") if package else name.split(".")[:-1]
                if node.level - 1 > len(here) - 1 or not here[0]:
                    continue
                base = ".".join(here[:len(here) - (node.level - 1)] + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            for alias in node.names:
                hit = _resolve(f"{base}.{alias.name}", roots) if alias.name != "*" else None
                hit = hit or _resolve(base, roots)
                if hit:
                    found.append(hit)
    return found


def _package_path(path: Path) -> str:
    """`steuermaschine/engine.py`: the path below the directory that holds the top-level package."""
    path = Path(path).resolve()
    for parent in path.parents:
        if not (parent / "__init__.py").is_file():
            return str(path.relative_to(parent))
    return path.name


@dataclass(frozen=True)
class Group:
    """Help of a command group ("queue"), in contract form. Without one the core writes a plain one."""

    path: str
    help: str


@dataclass(frozen=True)
class Alias:
    """A second name for a command group: `Alias("work", "beads")` mounts the group `beads` as `work` as well. The
    commands are the same objects, so the writers' lock and the help are the original's.
    `Alias("work focus", "focus")` mounts a group below an alias that was declared before it."""

    path: str
    target: str
    help: str = ""


def native(path: str, *, help: str, writes: bool = False, tick: bool = False, replaces: tuple[str, ...] = ()) -> Callable[[F], F]:
    def mark(fn: F) -> F:
        setattr(fn, "__sm_native__", NativeCommand(" ".join(path.split()), fn, help, writes, tick, tuple(replaces)))  # noqa: B010
        return fn

    return mark


def collect(*modules: Any) -> list[Any]:
    """Every `@native` function and every `Group`, `Alias` and surface object (or list of renames) in the given modules."""
    out = []
    for module in modules:
        for value in vars(module).values():
            if isinstance(value, Group | Alias | Surface | Verb | Renamed | Default):
                out.append(value)
            elif isinstance(value, list) and value and all(isinstance(v, Renamed | Verb) for v in value):
                out += value
            elif getattr(value, "__sm_native__", None) is not None and getattr(value, "__module__", None) == module.__name__:
                out.append(value.__sm_native__)
    return out


# ----------------------------------------------------------------------------- the runtime of one call
@dataclass
class NativeRuntime:
    providers: Providers
    settings: Any  # plugins.Settings: a mapping of every loaded plugin's settings
    project: Any
    say: Callable[[str], None] = field(default=lambda text: typer.echo(text, err=True))
    # every native command of the loaded plugin stack; lets a command look up others (a digest of read commands)
    # without importing the plugin's whole registry, which would put every module into its reach (`sm self reviews`)
    commands: Callable[[], list[Any]] = field(default=list)


_CURRENT: contextvars.ContextVar[NativeRuntime] = contextvars.ContextVar("steuermaschine_native_runtime")


def runtime() -> NativeRuntime:
    """What a native function works with: `.providers`, `.settings`, `.project`, `.say(text)`."""
    try:
        return _CURRENT.get()
    except LookupError:
        raise RuntimeError("runtime() is only available inside a native command; in a test use `with using(...)` or `invoke`") from None


class using:
    """`with using(NativeRuntime(providers, settings, project)):` for calling a native function directly in a test."""

    def __init__(self, rt: NativeRuntime):
        self.rt = rt

    def __enter__(self) -> Any:
        self.token = _CURRENT.set(self.rt)
        return self.rt

    def __exit__(self, *exc: Any) -> Any:
        _CURRENT.reset(self.token)
        return False


# ----------------------------------------------------------------------------- write units
def unit(path: str, anchor: Callable[..., Any]) -> NativeCommand:
    """A write path that is not a `@native` function: a core command, a pipeline. `anchor` is any function of the
    module that holds the code (its module and `<module>_lib` are the unit, imports followed by `reviewgraph.reach`)."""
    return NativeCommand(" ".join(path.split()), anchor, "", writes=True)


# The writers that do not take the writers' lock, by command path: the run lock (`gate run`), which every test run goes
# through. The core alone decides this; a plugin cannot exempt a command of its own.
EXEMPT_PATHS: frozenset[str] = frozenset({"gate run"})


def is_exempt(cmd: Any) -> bool:
    """True when the path of `cmd` is on the core's allowlist (`EXEMPT_PATHS`)."""
    return getattr(cmd, "path", "") in EXEMPT_PATHS


def call(cmd: NativeCommand, **kwargs: Any) -> Any:
    """Run a native command from code (a pipeline step) as on the command line. Options not given take the
    command's defaults; the current `runtime()` is used."""
    for name, param in inspect.signature(cmd.fn).parameters.items():
        default = param.default
        if name in kwargs or default is inspect.Parameter.empty:
            continue
        value = default.default if type(default).__module__.startswith("typer") else default
        if value is ...:
            raise TypeError(f"sm {cmd.path}: {name} is required")
        kwargs[name] = value
    return cmd.fn(**kwargs)


# ----------------------------------------------------------------------------- building the commands
def build(commands: list[Any], rt: Any, emit: Callable[..., Any], fail: Callable[..., Any], ctx_settings: dict[str, Any]) -> typer.Typer | None:
    """One Typer app with every native command at its path. `rt` is the app's Runtime (providers built on first use)."""
    natives = [c for c in commands if isinstance(c, NativeCommand)]
    if not natives:
        return None
    helps = {g.path: g.help for g in commands if isinstance(g, Group)}
    app = typer.Typer(add_completion=False, rich_markup_mode=None)

    @app.callback()
    def root() -> None:
        pass

    groups: dict[tuple[Any, ...], typer.Typer] = {(): app}
    # A path that is a command and also has commands below it (`ci merge-gate`, `ci merge-gate rejudge`): the command
    # runs when the group is called without a subcommand.
    prefixes = {tuple(c.path.split()[:n]) for c in natives for n in range(1, len(c.path.split()))}
    own = {tuple(c.path.split()): c.help for c in natives}
    both = set(own) & prefixes  # such a group shows the command's own help

    def group_for(words: tuple[Any, ...]) -> typer.Typer:
        if words not in groups:
            path = " ".join(words)
            first = next(c.path for c in natives if tuple(c.path.split()[:len(words)]) == words)
            text = (own.get(words) if words in both else None) or helps.get(path) or H(f"{words[-1]}: {len([c for c in natives if tuple(c.path.split()[:len(words)]) == words])} command(s).",
                                        "Use when one of the commands below is what you need.", "nothing by itself; see each command.", f"sm {first}", "sm go")
            sub = typer.Typer(help=text, short_help=text.replace("\b", "").strip().splitlines()[0], no_args_is_help=words not in both,
                              context_settings=ctx_settings, rich_markup_mode=None, add_completion=False)
            group_for(words[:-1]).add_typer(sub, name=words[-1])
            groups[words] = sub
        return groups[words]

    for cmd in natives:
        words = tuple(cmd.path.split())
        if words in both:
            group_for(words).callback(invoke_without_command=True)(_group_command(_command(cmd, rt, emit, fail)))
            continue
        group_for(words[:-1]).command(words[-1], help=cmd.help, short_help=cmd.help.replace("\b", "").strip().splitlines()[0],
                                      context_settings=ctx_settings)(_command(cmd, rt, emit, fail))
    return app


def mount_aliases(root: Any, commands: list[Any]) -> None:
    """Mount every `Alias` in `commands` on the Click group `root`, in the order given. A target that is missing is skipped."""
    import copy

    for alias in (c for c in commands if isinstance(c, Alias)):
        target = root.commands.get(alias.target)
        if target is None or not hasattr(target, "commands"):
            continue
        *parents, name = alias.path.split()
        parent = root
        for word in parents:
            parent = parent.commands.get(word)
            if parent is None or not hasattr(parent, "commands"):
                break
        else:
            twin = copy.copy(target)
            twin.name, twin.commands = name, dict(target.commands)
            if alias.help:
                twin.help = alias.help
                twin.short_help = alias.help.replace("\b", "").strip().splitlines()[0]
            parent.commands[name] = twin


def _group_command(run: Callable[..., Any]) -> Callable[..., Any]:
    """The callback of a group that is also a command: it runs the command only when no subcommand was named."""

    def callback(ctx: typer.Context, **kwargs: Any) -> None:
        if ctx.invoked_subcommand is None:
            run(**kwargs)

    params = list(inspect.signature(run).parameters.values())
    ctx_param = inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=typer.Context)
    callback.__signature__ = inspect.Signature([ctx_param, *[q.replace(kind=inspect.Parameter.KEYWORD_ONLY) for q in params]])  # type: ignore[attr-defined]
    return callback


WRITERS_LOCK = "writers.lock"  # in the project's state directory


@contextlib.contextmanager
def writers_lock(rt: Any, cmd: Any, exclusive: bool = False) -> Iterator[None]:
    """Held by every run of a write command (shared) and by `sm self migrate-state` (exclusive): state files never
    move under a writer, and no writer starts while they move. Not held by a reader, by an exempt writer (the run
    lock: its files keep their inode when they move), or without a project."""
    import fcntl

    project = getattr(rt, "project", None)
    if project is None or not (exclusive or (getattr(cmd, "writes", False) and not is_exempt(cmd))):
        yield
        return
    path = Path(project.home) / WRITERS_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield


@contextlib.contextmanager
def writing(rt: Any, cmd: Any) -> Iterator[int | None]:
    """One write run: the writers' lock (shared) for its whole length, and the pid of a pending switch (None: none, or
    `cmd` does not write). The native wrapper and every pipeline run go through here, so a promote or rollback and
    `sm self migrate-state` see a pipeline (`sm release cut`, `sm head admit`) the same as a typed write command."""
    with writers_lock(rt, cmd):
        yield switch_pending(rt.providers.commands, rt.providers.clock.now()) if getattr(cmd, "writes", False) else None


SWITCH_PENDING = (".cache", "sm-promote-switch-pending")  # under the home directory; bin/promote writes its pid there


def switch_pending(commands: Any, now: float) -> int | None:
    """The pid of a promote that is switching the live sm right now (bin/promote writes "PID UTC-TIME" before it waits
    for running steps and removes it after the switch), or None. Stale and ignored: a file whose pid is gone, and one
    whose pid was reused: the process with that pid started after the file was written (`ps -o etime=` through the
    process runner, now minus the elapsed time). When the start cannot be read the file holds (fail closed)."""
    import os
    from pathlib import Path

    try:
        words = Path.home().joinpath(*SWITCH_PENDING).read_text().split()
        pid = int(words[0])
        os.kill(pid, 0)
    except (OSError, ValueError, IndexError):
        return None
    written = _utc_seconds(words[1]) if len(words) > 1 else None
    done = commands.run(["ps", "-o", "etime=", "-p", str(pid)], timeout=10) if written is not None else None
    elapsed = _elapsed_seconds(done.out) if done is not None and done.rc == 0 else None
    if written is not None and elapsed is not None and now - elapsed > written + SWITCH_START_SLACK_SECONDS:
        return None  # the pid now belongs to a process that started after the promote wrote the file
    return pid


SWITCH_START_SLACK_SECONDS = 2.0  # etime and the file's time have whole seconds; the promote starts before it writes


def _utc_seconds(stamp: str) -> float | None:
    """`2026-11-13T16:00:00Z` (the file's time) as epoch seconds; None when it is not that form."""
    from datetime import datetime

    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


def _elapsed_seconds(text: str) -> float | None:
    """`ps -o etime=` (`[[DD-]HH:]MM:SS`) as seconds; None when it is not that form."""
    value = text.strip()
    days, _, clock = value.rpartition("-")
    parts = clock.split(":")
    if not value or len(parts) not in (2, 3) or not all(p.isdigit() for p in parts) or (days and not days.isdigit()):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return float(seconds + int(days or 0) * 86400)


def _release() -> str:
    """Which release the stable pointer names right now ($STEUERMASCHINE_RELEASE is the pointer, set by the launcher)."""
    import os

    from .config import environment

    pointer = environment().steuermaschine_release or ""
    return os.path.realpath(pointer) if pointer else ""


def _running_release() -> str:
    """The release this process runs: the launcher's pin when there is one, else what the pointer names now."""
    import os

    from .config import environment

    pinned = environment().steuermaschine_pinned or ""
    return os.path.realpath(pinned) if pinned else _release()


def registered(rt: Any) -> list[Any]:
    """Every native command of `rt`'s plugin stack (an app `Runtime`); none without a plugin."""
    plugin = getattr(rt, "plugin", None)
    return list(plugin.native(rt.settings)) if plugin else []


def _command(cmd: NativeCommand, rt: Any, emit: Callable[..., Any], fail: Callable[..., Any]) -> Callable[..., Any]:
    def once(args: Any, kwargs: Any) -> int:
        token = _CURRENT.set(NativeRuntime(rt.providers, rt.settings, rt.project, commands=lambda: registered(rt)))
        try:
            result = cmd.fn(*args, **kwargs)
        except Refuse as no:
            fail(no.message, no.code, hint=no.next_cmd)
        except Wait as wait:
            retry = f"waiting on {wait.waiting_on}; run the same command again"
            fail(wait.message, WAITING, hint=wait.next_cmd or retry)
        finally:
            _CURRENT.reset(token)
        emit(result)
        many = result if isinstance(result, list) else [result]
        return max((getattr(r, "exit_code", 0) or 0 for r in many), default=0)

    @functools.wraps(cmd.fn)
    def command(*args: Any, **kwargs: Any) -> Any:
        loop = kwargs.pop("loop", 0.0) if cmd.tick else 0.0
        release = _running_release()  # before the first tick: a switch during it must end the loop too

        def guarded(first: bool) -> int | None:
            """One run under the writers' lock (shared; `sm self migrate-state` takes it exclusively), unless a switch
            is pending: then a loop skips the tick (None) and a single run waits (exit 75)."""
            with writing(rt, cmd) as pending:
                if pending is not None:
                    if first and not loop:
                        fail(f"a promote (pid {pending}) is switching the live sm: `sm {cmd.path}` writes and waits until it is done", WAITING,
                             hint="nothing was run; run the same command again in a minute")
                    return None
                return once(args, kwargs)

        code = guarded(True) or 0  # a loop skips its ticks while the switch is pending
        while loop > 0:  # a daemon: the same tick again, until the process is stopped
            rt.providers.clock.sleep(loop)
            if _release() != release:  # a promote or rollback switched the install under a running loop
                typer.echo(f"sm {cmd.path}: another release is live now; this loop ends so the service manager starts it on the new one", err=True)
                break
            ran = guarded(False)
            code = code if ran is None else ran
        raise typer.Exit(code)

    if cmd.tick:
        sig = inspect.signature(cmd.fn)
        extra = inspect.Parameter("loop", inspect.Parameter.KEYWORD_ONLY, annotation=float,
                                  default=typer.Option(0.0, "--loop", metavar="SECONDS", help="Repeat the tick every SECONDS until stopped (for a service manager)."))
        setattr(command, "__signature__", sig.replace(parameters=[*sig.parameters.values(), extra]))  # noqa: B010
        command.__annotations__ = {**getattr(cmd.fn, "__annotations__", {}), "loop": float}
    return command
