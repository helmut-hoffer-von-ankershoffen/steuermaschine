"""The edges of the moved queue code: where its records are, how a moved module loads another moved tool, how a
process is started, and the three things that used to be scripts of their own (note, bead stamp, alarm). Nothing here
holds queue logic, and nothing here starts a script of the old tool set."""

from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import io
import itertools
import subprocess
import time
from pathlib import Path
from types import ModuleType
from typing import Any

from ... import state as statedir  # the plugin's state paths
from . import moved_map

PACKAGE = __name__.rsplit(".", 2)[0]  # steuermaschine_heinzel.native
MAPS: dict[str, object] = {"queue_lib": moved_map}  # lib directory -> its table of moved tools; another lib adds its own
_serial = itertools.count(1)
_records: list[Path] = []


def configure(records: Path | str | None) -> None:
    """Set where the records checkout is (setting `queue_records_dir`). A native command calls this before it loads
    a moved module, because the moved modules compute their file paths when they are loaded."""
    _records.clear()
    if records:
        _records.append(Path(records).expanduser())


def setting(name: str) -> Any:
    """One value of the plugin's settings table [heinzel], for moved code (None when unset). The moved tools read
    environment variables; the plugin reads typed settings only: files, SM_HEINZEL__<FIELD> and the old variable a
    setting names as its compatibility source (`sm self config` shows which). Inside a native command: the command's
    settings, and where they leave a value unset, the environment layer as it resolves now (SM_HEINZEL__<FIELD> and
    the old variables; a test sets one after its runtime was made); a moved function called directly (its own tests):
    that environment layer alone."""
    try:
        from steuermaschine.native import runtime

        values = runtime().settings
    except RuntimeError:
        return _resolved().get(name)
    values = values.of("heinzel") if hasattr(values, "of") else values
    table = values if isinstance(values, dict) else dict(values)
    value = table.get(name)
    return value if value is not None else _resolved().get(name)


def _resolved() -> dict[str, Any]:
    from steuermaschine.plugins import stack_of

    # The plugin object is looked up by name, not imported: it is the registry (every area hangs off it), not code
    # this file runs. An `import` here put the whole plugin into the review digest of every unit that imports this file.
    plugin = importlib.import_module(PACKAGE.rsplit(".", 1)[0] + ".plugin").plugin
    global _stack
    _stack = _stack or stack_of(plugin)
    settings, _ = _stack.validate({}, Path.cwd())
    return dict(settings.of("heinzel"))


_stack: Any = None


def records_dir() -> Path:
    """The checkout with the queue's records: `research/` (known failures, queue order), the notes document and the
    bead database. Data only: nothing in it is executed."""
    if not _records:
        from steuermaschine.engine import Refuse

        raise Refuse("the records checkout is not set: set queue_records_dir (research/, the notes document, the beads)", "sm self config")
    return _records[0]


def code_changed_at(module_file: str) -> float:
    """Newest change of the code a running loop executes: every file next to the given module and, when that is this
    package, the native commands above it and the core. The loop restarts itself when this value moves."""
    here = Path(module_file).resolve().parent
    files = [p for p in here.iterdir() if p.is_file()]
    if here == Path(__file__).resolve().parent:
        import steuermaschine

        files += [p for p in here.parent.glob("*.py")] + [p for p in Path(steuermaschine.__file__).resolve().parent.rglob("*.py")]
    return max(p.stat().st_mtime for p in files)


# ----------------------------------------------------------------------------- moved tools loading each other
def module_of(tool: str) -> str | None:
    """The dotted module of a moved tool, by the module it moved to (`merge_train`), or by its old file name with or
    without `.py` (the moved tests ask by that)."""
    for lib, table in MAPS.items():
        for names in (table.MOVED, {k: v[0] for k, v in table.EXTRACTS.items()}):
            if tool in names.values():
                return f"{PACKAGE}.{lib}.{tool}"
            if module := names.get(tool) or names.get(f"{tool}.py"):
                return f"{PACKAGE}.{lib}.{module}"
    return None


def whole(tool: str) -> bool:
    """Whether the tool moved as a whole (not only the definitions another tool needs from it)."""
    return any(tool in table.MOVED or f"{tool}.py" in table.MOVED for table in MAPS.values())


def fresh(dotted: str) -> ModuleType:
    """A new copy of a moved module, executed from its file. The moved code relies on that: a tool loaded another tool
    as a fresh module each time, so one caller's state never leaked into the next."""
    origin = importlib.util.find_spec(dotted)
    if origin is None or origin.origin is None:
        raise ImportError(dotted)
    spec = importlib.util.spec_from_file_location(f"{dotted}__{next(_serial)}", origin.origin)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    module.__package__ = dotted.rpartition(".")[0]
    spec.loader.exec_module(module)
    return module


def moved(tool: str) -> ModuleType:
    dotted = module_of(tool)
    if dotted is None:
        raise LookupError(f"{tool} is not part of this package; nothing is loaded from a directory")
    return fresh(dotted)


# ----------------------------------------------------------------------------- processes
_RAW = {"input", "stdin", "stdout", "stderr", "shell"}
ProcessTimeout = subprocess.TimeoutExpired


def run(argv, *args, **kwargs) -> subprocess.CompletedProcess:
    """`subprocess.run` for the moved code. Inside a native command a call that captures text goes through the core's
    one process runner (`providers.commands`), so tests fake it and record it; the moved code keeps its result shape
    and its exceptions (a timeout raises, a missing program raises, `check=True` raises). Calls the runner cannot
    express (stdin input, output into a file, bytes, lenient decoding) and calls outside a native command are the
    plain call."""
    from steuermaschine.native import runtime

    text = kwargs.get("text") or kwargs.get("errors") or kwargs.get("encoding") or kwargs.get("universal_newlines")
    simple = not args and kwargs.get("capture_output") and text and not _RAW & kwargs.keys()
    try:
        commands = runtime().providers.commands
    except RuntimeError:
        commands = None
    if commands is None or not simple:
        return subprocess.run(argv, *args, **kwargs)
    if kwargs.get("errors") or kwargs.get("encoding"):
        # The caller asked for lenient or special decoding (the merge call: errors="replace"). The core's runner decodes
        # strictly and would raise on a stray byte AFTER the command ran; a scripted runner returns text already.
        from steuermaschine.defaults import Subprocess

        if isinstance(commands, Subprocess):
            return subprocess.run(argv, *args, **kwargs)
    argv = [str(a) for a in argv]
    timeout = kwargs.get("timeout")
    done = commands.run(argv, cwd=kwargs.get("cwd"), timeout=timeout, env=kwargs.get("env"))
    if done.rc == 124 and timeout is not None and done.err.startswith("timed out after"):
        raise subprocess.TimeoutExpired(argv, timeout)
    if done.rc == 127 and not done.out and "No such file or directory" in done.err:
        raise FileNotFoundError(done.err)
    result = subprocess.CompletedProcess(argv, done.rc, done.out, done.err)
    if kwargs.get("check"):
        result.check_returncode()
    return result


# ----------------------------------------------------------------------------- what used to be scripts of their own
def notes_file() -> Path:
    return records_dir() / "AGENTS.md"


def note(text: str) -> bool:
    """Append one dated line to the notes document, one writer at a time: `- MM-DD HH:MM text`, whitespace collapsed.
    The same line, lock and document as the note tool wrote. The ops slice replaces this with its journal."""
    text = " ".join(str(text).split())
    if not text:
        return False
    try:
        lock_path = statedir.path("note.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with notes_file().open("a") as handle:
                handle.write(f"- {time.strftime('%m-%d %H:%M')} {text}\n")
    except OSError:  # the queue never failed on a note it could not write
        return False
    return True


def stamp_merged(bead: str, pr: str) -> int | None:
    """Stamp a bead with the PR that merged it. Output is swallowed and a failure never stops the queue, as when the
    stamp was a separate process whose result nobody read. Returns the stamp's code, None when it could not run."""
    from . import bead_stamp

    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            return bead_stamp.do_stamp(bead, int(pr))
    except Exception:  # noqa: BLE001 - as before: the merge is recorded whether or not the stamp worked
        return None


def raise_alarm(key: str, level: str, text: str, source: str = "queue") -> dict:
    """Record one alarm in the alarm state file (same format, lock and owner push as the alarm tool), without
    touching other alarms of the source. The read-only slice owns `alarm` and takes this function over."""
    from . import stall

    return stall.process([{"cls": level, "key": key, "text": text}], source, complete=False)[0]


def clear_alarm(key: str, source: str = "queue") -> bool:
    from . import stall

    return stall.clear(key, source)
