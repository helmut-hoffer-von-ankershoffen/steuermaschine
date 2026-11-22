"""The machine-wide slot pool of the local checks: how many `sm check` runs share one machine.

The old queue let one check run at a time and made the others wait twenty minutes, then fail with 75. Fifteen builders
that finish at once either stalled for hours or, run without a lock, pushed the load past 500. The pool replaces it:

  check slots      `CHECK_SLOTS` (4) files `<dir>/sm-check.slot.N`, each an exclusive `flock` (kernel-held: a crashed run frees it)
  start bar        no check starts at a load over `START_MAX_LOAD` (30)
  wait             a run waits for a free slot at most `WAIT_SECONDS` (60)

A run that gets no slot, or meets the load bar, never times out and is never READY: `pool` yields a `Grant` whose
`deferred` holds the reason, and `sm check` ends with exit 75 (the same command again later). Only a selection of more than
40 test files is left to CI, and that receipt says so. There is no simulator slot: sm check starts none.
A granted run lowers its own priority to background (`taskpolicy -b` on macOS, `nice` elsewhere): the checks of builders
never starve the live services of the machine, and every child process inherits it.
"""

from __future__ import annotations

import fcntl
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from steuermaschine.config import environment

CHECK_SLOTS = 4
START_MAX_LOAD = 30.0  # no check starts above this 1-minute load
WAIT_SECONDS = 60.0
POLL_SECONDS = 1.0
KINDS = {"check": CHECK_SLOTS}


@dataclass(frozen=True)
class Grant:
    """The outcome of asking the pool: a slot number, or the reason the run is deferred to CI."""

    kind: str
    slot: int | None = None
    deferred: str = ""

    @property
    def granted(self) -> bool:
        return self.slot is not None


def lower_priority(commands: Any) -> None:
    """Background priority for this process and, by inheritance, everything it starts. Best effort: a failure changes nothing."""
    if environment().pytest_current_test:  # a test never demotes the pytest process itself (as the process runner refuses a push under pytest)
        return
    try:
        if sys.platform == "darwin":
            commands.run(["taskpolicy", "-b", "-p", str(os.getpid())], timeout=10)  # the one process runner; a failure changes nothing
        else:
            os.nice(10)
    except OSError:
        pass


def bar(kind: str) -> float:
    return START_MAX_LOAD


def over(kind: str, load: float) -> str:
    """Why the load forbids a start now, or an empty string: a check does not start over the bar."""
    limit = bar(kind)
    return f"the 1-minute load {load:.0f} is over {limit:g}" if load > limit else ""


def _try(directory: Path, kind: str) -> tuple[int, int] | None:
    for n in range(KINDS[kind]):
        fd = os.open(directory / f"sm-{kind}.slot.{n}", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        return n, fd
    return None


@contextmanager
def pool(
    directory: Path,
    kind: str = "check",
    say: Callable[[str], None] = lambda text: None,
    wait: float = WAIT_SECONDS,
    poll: float = POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    load: Callable[[], float] = lambda: os.getloadavg()[0],
    demote: Callable[[], None] | None = None,
) -> Iterator[Grant]:
    """Ask for a slot of `kind`; hold it for the body. A busy pool or a loaded machine yields a deferred Grant, never an error."""
    directory.mkdir(parents=True, exist_ok=True)
    started, told = clock(), False
    held: int | None = None
    grant: Grant
    while True:
        if why := over(kind, load()):
            grant = Grant(kind, deferred=f"{why}: a {kind} run starts only at or under {bar(kind):g}")
            break
        got = _try(directory, kind)
        if got is not None:
            grant, held = Grant(kind, slot=got[0]), got[1]
            break
        if clock() - started >= wait:
            grant = Grant(kind, deferred=f"no {kind} slot of {KINDS[kind]} was free within {wait:g} s")
            break
        if not told:
            say(f"sm check: all {KINDS[kind]} {kind} slots of this machine are busy; waiting up to {wait:g} s")
            told = True
        sleep(poll)
    try:
        if grant.granted and demote is not None:
            demote()
        yield grant
    finally:
        if held is not None:
            fcntl.flock(held, fcntl.LOCK_UN)
            os.close(held)
