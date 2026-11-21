"""The push check as functions: information on CI traffic before a push that starts PR CI; it never refuses.

    wait_reason("801")      -> ""  (always: kept for its callers)
    check("801", minimum=5) -> (0, printed lines): main CI queue age and an active release candidate as info lines

The decision is the moved push gate (push_gate_cli.py, push_gate_rules.py). It fails open: when GitHub cannot be read the
answer is OK (unchecked). A release candidate is protected by the merge freeze on main merges, not by pushes or PRs.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..queue_lib import edge
from . import deps
from .push_gate_rules import OK, WAIT

__all__ = ["OK", "WAIT", "check", "wait_reason"]


def check(target: str, minimum: int | None = None, keep_file: Path | str | None = None) -> tuple[int, list[str]]:
    """Run the moved push gate for a PR number or a branch without a PR. `minimum` is the main CI queue age in minutes
    that makes the info line appear (default 15); `keep_file` is accepted and ignored."""
    deps.configure()
    module = edge.fresh(f"{edge.PACKAGE}.ship_lib.push_gate_cli")
    argv = ["push-gate", str(target)]
    if minimum is not None:
        argv += ["--min", str(minimum)]
    before, sys.argv = sys.argv, argv  # the moved gate reads its options from the command line
    try:
        done = deps.captured(lambda: module.main() or OK)
    finally:
        sys.argv = before
    return done.returncode, [*done.stdout.splitlines(), *done.stderr.splitlines()]


def wait_reason(target: str, minimum: int | None = None, keep_file: Path | str | None = None) -> str:
    """"" when the push may go now; otherwise why it must wait (one or two lines)."""
    code, lines = check(target, minimum, keep_file)
    return "\n".join(line for line in lines if line.startswith("WAIT")) if code == WAIT else ""
