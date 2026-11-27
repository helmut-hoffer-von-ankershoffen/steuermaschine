"""The budget of a local check: local is a signal, CI is the gate.

A local `sm check` looks at the changed files only and has `BUDGET_SECONDS` (90) of wall time in total, however many
commands that takes. Extended focused mode allows up to 600 s for those same commands.

If the wall budget expires before a step starts or stops a command, the remaining steps do not run. The check
exits 75 and writes no receipt unless a previous step failed, in which case it reports failure. A command that hits
its own limit (124) or cannot get a slot (75) fails (sm #57, #59). A complete check can reach READY while leaving a
large test selection (more than `MAX_TEST_FILES`) to PR CI.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

BUDGET_SECONDS = 90.0
EXTENDED_SECONDS = 600.0
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
