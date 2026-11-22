"""The queue of `sm check`: one run at a time per machine, the others wait their turn.

Ten builders that finish at once must not start ten sets of checks on one busy machine: each run takes an exclusive
file lock (kernel-held, so a crashed run frees it) and waits for it. A wait that outlasts the ceiling ends as
`Wait` (exit 75: run the same command again), never as a silent pass or a bypass.
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from steuermaschine.engine import Wait

LOCK_NAME = "sm-check.queue"
WAIT_SECONDS = 1200.0  # the ceiling of one wait: about eight full checks ahead
POLL_SECONDS = 1.0


@contextmanager
def queued(
    directory: Path,
    say: Callable[[str], None] = lambda text: None,
    wait: float = WAIT_SECONDS,
    poll: float = POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[None]:
    """Hold the check queue lock for the body; wait up to `wait` seconds for it, saying so once."""
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        started, told = clock(), False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if clock() - started >= wait:
                    raise Wait(f"another sm check has held the queue for {int(wait)} s", "the sm check run ahead of this one") from None
                if not told:
                    say("sm check: another check is running on this machine; waiting for my turn")
                    told = True
                sleep(poll)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
