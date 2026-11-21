"""Waiting for the release admission: wait while an uncovered Actions run is active, and retry a truncated inventory.

A prepare that meets an active run without a reviewed nonconsumer receipt refuses; so does one that GitHub answered
with a short page ("truncated inventory"). Both pass by themselves. Waiting is done with the read-only run inventory
(`blocking`), not with prepare itself: a prepare recomputes the whole guard and transaction, so it runs again only
when the inventory says it can succeed. Prepare sends no cancel probe (`probe=False`; only apply probes before its
write). The identical prepare is retried; the packet is never rebuilt here.

Time comes from the injected clock. Nothing in this module writes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from . import focused_admit as a

TRUNCATED = "truncated inventory"
DRIFT = "active run membership drift"
UNCOVERED = "unknown active Actions run; admission refused: "
SHOWN = 5


class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class StillWaiting(Exception):
    """The wait ended without a prepared transaction. Nothing is wrong: the same command continues."""

    def __init__(self, message: str, waiting_on: str):
        super().__init__(message)
        self.message, self.waiting_on = message, waiting_on


class WrittenWithoutReceipt(a.AdmissionError):
    """The registry slot was written, then a guard refused: there is no receipt. Never a wait, never retried."""


def transient(error: Exception) -> bool:
    text = str(error)
    return TRUNCATED in text or DRIFT in text


def name_runs(rows: list[Any]) -> str:
    """`ID status branch (event)` for the first few runs, and how many more."""
    shown = ", ".join(f"{row.id} {row.status} {row.head_branch} ({row.event})" for row in rows[:SHOWN])
    return shown + (f", and {len(rows) - SHOWN} more" if len(rows) > SHOWN else "")


def prepare_when_clear(prepare_once: Callable[[], Any], blocking_once: Callable[[], list[Any]], *, clock: Clock,
                       say: Callable[[str], None], max_minutes: float, poll_seconds: float, step: str = "prepare") -> Any:
    """Poll the run inventory until no uncovered run is active, then prepare. Returns what `prepare_once` returns.

    Retried: a truncated or drifting inventory (in the poll or in prepare), and a run that appeared between the poll
    and prepare. Every other refusal is raised unchanged. After `max_minutes` raises StillWaiting, naming the run.
    """
    start = clock.now()

    def pause(reason: str) -> None:
        waited = (clock.now() - start) / 60
        if waited >= max_minutes:
            raise StillWaiting(f"{step} did not get a clear window in {max_minutes:g} min: {reason}", reason)
        say(f"waiting ({waited:.0f} min) for {reason}; next look in {poll_seconds:g} s")
        clock.sleep(poll_seconds)

    while True:
        try:
            rows = blocking_once()
        except a.AdmissionError as error:
            if not transient(error):
                raise
            pause(f"a stable run inventory ({error})")
            continue
        if rows:
            pause("uncovered active run " + name_runs(rows))
            continue
        try:
            return prepare_once()
        except a.AdmissionError as error:
            if isinstance(error, WrittenWithoutReceipt):
                raise
            text = str(error)
            if text.startswith(UNCOVERED):
                pause("uncovered active run " + text[len(UNCOVERED):])
            elif transient(error):
                pause(f"a complete inventory ({text}); the identical prepare is retried")
            else:
                raise


def watch_write(rt: Any) -> list[bool]:
    """Remember whether the runtime's registry writer was called (one flag, set before the write is attempted)."""
    flag = [False]
    write = getattr(rt, "set_variable", None)
    if write is None:  # a runtime with no writer (a test double) cannot write
        return flag

    def set_variable(name: str, value: str) -> Any:
        flag[0] = True
        return write(name, value)

    rt.set_variable = set_variable
    return flag


def tracked(rt: Any, run: Callable[[], Any]) -> Any:
    """Run an apply; a refusal raised after the registry write becomes `WrittenWithoutReceipt`, which no wait retries."""
    flag = watch_write(rt)
    try:
        return run()
    except WrittenWithoutReceipt:
        raise
    except a.AdmissionError as error:
        if flag[0]:
            raise WrittenWithoutReceipt(
                f"the registry slot WAS WRITTEN but there is no receipt: a check after the write refused ({error}). Do not run apply again and do not wait: "
                "tell the coordinator; once no run is active, the slot holds this head's plan, and the receipt must be recovered by hand "
                "(the retired blockers read, then reprepare, which reads the written slot)") from error
        raise


def retry_when_clear(once: Callable[[], Any], *, clock: Clock, say: Callable[[str], None], max_minutes: float, poll_seconds: float,
                     step: str = "apply") -> Any:
    """`prepare_when_clear` for a command with no read-only inventory of its own (apply, the release admission): run it,
    and when it refused for an uncovered active run or a truncated inventory, wait and run the identical command again.
    Only a refusal from BEFORE the registry write is retried: `once` must raise `WrittenWithoutReceipt` (see `tracked`) for a refusal
    after it, and that is raised at once."""
    return prepare_when_clear(once, list, clock=clock, say=say, max_minutes=max_minutes, poll_seconds=poll_seconds, step=step)


def limits(rt: Any) -> dict[str, Any]:
    """The clock, the voice and the two settings of every `--wait`."""
    return {"clock": rt.providers.clock, "say": rt.say, "max_minutes": float(rt.settings.get("admission_wait_max_minutes", 45.0)),
            "poll_seconds": float(rt.settings.get("admission_wait_poll_seconds", 90.0))}
