"""The registry writer lock, the same one `sm head apply` and `sm release admit apply` take: one path, one implementation.

`admission_lib.focused_admit._lock` is the moved implementation (a checked, flocked file with a 15 s wait); this module
only names it, so a second writer of the focused registry cannot invent another lock that excludes nothing."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..admission_lib import focused_admit

Busy = focused_admit.AdmissionError


@contextmanager
def writer_lock(path: Path) -> Iterator[None]:
    """Hold the registry writer lock at `path` (the state directory's `focused-pr-registry.lock`); `Busy` after the wait."""
    with focused_admit._lock(path):
        yield
