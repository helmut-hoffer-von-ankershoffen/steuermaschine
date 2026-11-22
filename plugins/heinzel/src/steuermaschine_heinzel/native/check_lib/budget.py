"""The budget of a local check: local is a signal, CI is the gate.

A local `sm check` looks at the changed files only and has `BUDGET_SECONDS` (90) of wall time in total, however many
commands that takes. What does not fit is DEFERRED TO CI, never failed and never dropped without a word:

  over budget      the time ran out before a step started (or a command was stopped at the budget's end): that step
                   and the ones after it are deferred; failures found before stay failures
  too many tests   a selection of more than `MAX_TEST_FILES` (40) test files is not run here: PR CI runs them

The check still ends with exit 0 when nothing it ran failed, and its receipt line names what CI must run. A type check
that a command's own limit stops (exit 124) or that gets no slot (75) still FAILS: that is a check that did not run
for its own reasons, not a decision of the budget (sm #57, #59). Only the budget's end defers.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

BUDGET_SECONDS = 90.0
MAX_TEST_FILES = 40
TIMED_OUT = 124


class OverBudget(Exception):
    """The wall budget of the local check is spent: the step that was about to run is deferred to CI."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Budget:
    """`seconds` of wall time from the moment it is made, read from `clock`."""

    clock: Callable[[], float]
    seconds: float = BUDGET_SECONDS
    started: float = field(init=False)

    def __post_init__(self) -> None:
        self.started = self.clock()

    def left(self) -> float:
        return self.seconds - (self.clock() - self.started)

    def spent(self) -> bool:
        return self.left() <= 0

    def reason(self) -> str:
        return f"the local budget of {self.seconds:g} s is spent"

    def ensure(self) -> None:
        if self.spent():
            raise OverBudget(self.reason())


def gated(budget: Budget, make: Callable[[int], Callable[..., tuple[int, str]]], limit: int) -> Callable[..., tuple[int, str]]:
    """A heavy command through the run lock, inside the budget: it never starts after the budget's end, is stopped at
    it, and a stop at the budget's end is a deferral (a stop at the command's own limit stays exit 124, a failure).
    `make(seconds)` builds the lock call with that time limit."""

    def run(cmd: Sequence[object], cwd: object = None, env: dict[str, str] | None = None) -> tuple[int, str]:
        budget.ensure()
        rc, out = make(max(1, min(limit, int(budget.left()) + 1)))(cmd, cwd=cwd, env=env)
        if rc == TIMED_OUT and budget.spent():
            raise OverBudget(budget.reason())
        return rc, out

    return run


def too_many(files: Sequence[Any]) -> str:
    """Why a selection of test files is left to CI, or an empty string."""
    return f"{len(files)} test files are selected, over the local limit of {MAX_TEST_FILES}" if len(files) > MAX_TEST_FILES else ""
