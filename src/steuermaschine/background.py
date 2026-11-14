"""Background runs: `sm gate run --bg -- COMMAND` starts the gated command detached and returns at once.

Why it exists: a shell that is not interactive starts a `&` job with SIGINT and SIGQUIT ignored (POSIX), and an ignored
signal stays ignored in every child. `nohup sh -c "pytest ... &"` therefore runs tests that stop their own children
with SIGINT against children that never notice: they hang, and it reads as a flaky test. A background run started
here has both signals at their default, a session of its own (no terminal sends it anything), its output in a log
file and its exit code in a file, and a handle to wait on:

    sm gate run --bg -- pytest tests/unit/test_x.py     ->  the job's id, pid and log
    sm gate wait ID                                      ->  waits, exits with the command's exit code

One job is three files in the directory: `<id>.json` (what was started), `<id>.log` (its output and the gate's own
lines) and `<id>.exit` (the exit code, written when it ended). A job whose process is gone without an exit file is
`lost`: it was killed. Nothing here needs a process to stay alive.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

from . import WAITING
from .models import Model
from .protocols import Clock

RESET = (signal.SIGINT, signal.SIGQUIT)  # what a non-interactive shell leaves ignored in a `&` job
LOST = 70  # the exit code reported for a job that ended without writing one
KEEP_SECONDS = 24 * 3600.0  # ended jobs older than this are removed by the next start
LISTED_SECONDS = 3600.0  # ended jobs `status` still shows
Spawn = Callable[[Sequence[str], Mapping[str, str], str, Path], int]


class Job(Model):
    """One background run."""

    id: str
    pid: int
    command: list[str]
    cwd: str = ""
    started: float = 0.0
    log: str = ""
    state: str = "running"  # running | done | lost
    code: int | None = None

    @property
    def exit_code(self) -> int:
        return WAITING if self.state == "running" else LOST if self.code is None else self.code

    def line(self) -> str:
        how = "running" if self.state == "running" else f"exit {self.code}" if self.state == "done" else "LOST (ended without an exit code: killed)"
        return f"{self.id} pid {self.pid} {how}: {' '.join(self.command)[:120]} (log {self.log})"

    def render(self) -> list[str]:
        if self.state == "running":
            return [f"background run {self.id} (pid {self.pid}) is running: {' '.join(self.command)[:160]}", f"  log:  {self.log}",
                    f"  wait: sm gate wait {self.id}   (exits with the command's exit code)"]
        return [self.line()]


@contextlib.contextmanager
def default_signals() -> Iterator[None]:
    """SIGINT and SIGQUIT at their default for the time of the block, so that a process started inside it does not
    inherit them ignored. Outside the main thread nothing can be changed, and nothing is."""
    before = {}
    for number in RESET:
        try:
            before[number] = signal.signal(number, signal.SIG_DFL)
        except ValueError:
            break
    try:
        yield
    finally:
        for number, handler in before.items():
            signal.signal(number, handler if handler is not None else signal.SIG_DFL)


def reset_signals() -> None:
    """In the background run itself: SIGINT and SIGQUIT back to their default, whatever it inherited."""
    for number in RESET:
        with contextlib.suppress(ValueError):
            signal.signal(number, signal.SIG_DFL)


def spawn_detached(argv: Sequence[str], env: Mapping[str, str], cwd: str, log: Path) -> int:
    """Start `argv` in a session of its own, output appended to `log`, no input; the pid. It is never waited for."""
    with log.open("ab") as out, default_signals():
        process = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, cwd=cwd or None, env=dict(env),
                                   start_new_session=True)
    return process.pid


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def new_id(directory: Path, clock: Clock, pid: int) -> str:
    base = f"{int(clock.now())}-{pid}"
    name, n = base, 1
    while (directory / f"{name}.json").exists():
        name, n = f"{base}-{n}", n + 1
    return name


def start(directory: Path, launcher: Callable[[str], Sequence[str]], command: Sequence[str], cwd: str, env: Mapping[str, str], clock: Clock,
          spawn: Spawn = spawn_detached, pid: Callable[[], int] = os.getpid) -> Job:
    """Start one background run. `launcher(id)` is the argv that runs the command and records its exit code under that id
    (`finish`); the job is recorded before this returns."""
    directory.mkdir(parents=True, exist_ok=True)
    sweep(directory, clock)
    name = new_id(directory, clock, pid())
    log = directory / f"{name}.log"
    child = spawn(launcher(name), env, cwd, log)
    job = Job(id=name, pid=child, command=list(command), cwd=cwd, started=clock.now(), log=str(log))
    (directory / f"{name}.json").write_text(json.dumps(job.model_dump()))
    return job


def finish(directory: Path, name: str, code: int) -> None:
    """Record the exit code of a background run; called by the run itself as its last act."""
    with contextlib.suppress(OSError):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.exit").write_text(f"{code}\n")


def read(directory: Path, name: str, is_alive: Callable[[int], bool] = alive) -> Job | None:
    """The job with its state now; None when no such job is recorded."""
    try:
        job = Job(**json.loads((directory / f"{name}.json").read_text()))
    except (OSError, ValueError, TypeError):
        return None
    try:
        code = int((directory / f"{name}.exit").read_text().strip())
    except (OSError, ValueError):
        return job.model_copy(update={"state": "running" if is_alive(job.pid) else "lost", "code": None})
    return job.model_copy(update={"state": "done", "code": code})


def jobs(directory: Path, clock: Clock, is_alive: Callable[[int], bool] = alive) -> list[Job]:
    """The running jobs and those that ended within the last hour, oldest first."""
    try:
        names = sorted(path.stem for path in directory.glob("*.json"))
    except OSError:
        return []
    found = [job for name in names if (job := read(directory, name, is_alive)) is not None]
    return sorted((job for job in found if job.state == "running" or clock.now() - job.started <= LISTED_SECONDS), key=lambda job: job.started)


def sweep(directory: Path, clock: Clock) -> None:
    """Remove the files of jobs that ended and were started more than a day ago."""
    for path in directory.glob("*.json"):
        job = read(directory, path.stem)
        if job is not None and job.state != "running" and clock.now() - job.started > KEEP_SECONDS:
            for suffix in (".json", ".log", ".exit"):
                with contextlib.suppress(OSError):
                    (directory / f"{path.stem}{suffix}").unlink(missing_ok=True)


def wait(directory: Path, name: str, clock: Clock, timeout_seconds: float = 0.0, is_alive: Callable[[int], bool] = alive, poll: float = 2.0) -> Job | None:
    """The job once it ended, or as it is after `timeout_seconds` (0: wait as long as it takes); None when unknown.
    A job whose process is gone gets one more look for its exit file, which it writes last."""
    start_time = clock.now()
    while True:
        job = read(directory, name, is_alive)
        if job is None or job.state == "done":
            return job
        if job.state == "lost":
            return read(directory, name, is_alive)
        if timeout_seconds and clock.now() - start_time >= timeout_seconds:
            return job
        clock.sleep(poll)
