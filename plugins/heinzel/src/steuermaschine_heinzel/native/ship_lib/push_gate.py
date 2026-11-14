"""The push check as functions: may a push that starts PR CI go now, or must it wait?

    wait_reason("801")      -> ""  (push), or the WAIT line(s): main CI starved, or a release candidate owns the runners
    check("801", minimum=5) -> (exit code, printed lines): 0 OK, 75 WAIT

The decision is the moved push gate (push_gate_cli.py, push_gate_rules.py), unchanged. It fails open: when GitHub
cannot be read the answer is OK (unchecked), so a network error never blocks a fix.

On top of it (bead 7et): while a merge freeze is on (a release's owned hold or the incident freeze), a push that the
moved gate let through WAITs unless it is a fix: a PR on the keep list (research/ci-keep.txt, `fix PR #N` in
research/known-failures.toml) or a push to release/*, bump/* or main. The coordinator's waiver (`minimum`) lifts it,
as it lifts the release-candidate hold. The freeze is a local file: no network, so this part never fails open.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..queue_lib import edge, hzlib
from . import deps
from .push_gate_rules import OK, WAIT, fix_prs, never_held, parse_keep

__all__ = ["OK", "WAIT", "check", "wait_reason"]


def check(target: str, minimum: int | None = None, keep_file: Path | str | None = None) -> tuple[int, list[str]]:
    """Run the moved push gate for a PR number or a branch without a PR. `minimum`: the main CI queue age in minutes
    that triggers WAIT (default 15); giving it is also the coordinator's waiver of the release-candidate hold, as the
    old `--min` was."""
    deps.configure()
    module = edge.fresh(f"{edge.PACKAGE}.ship_lib.push_gate_cli")
    argv = ["push-gate", str(target)]
    if minimum is not None:
        argv += ["--min", str(minimum)]
    if keep_file is not None:
        argv += ["--keep-file", str(keep_file)]
    before, sys.argv = sys.argv, argv  # the moved gate reads its options, and the waiver, from the command line
    try:
        done = deps.captured(lambda: module.main() or OK)
    finally:
        sys.argv = before
    lines = [*done.stdout.splitlines(), *done.stderr.splitlines()]
    if done.returncode == OK and minimum is None and hzlib.merge_frozen() and not is_fix(str(target), keep_file):
        who = f"PR #{target}" if str(target).isdigit() else f"branch {target}"
        return WAIT, [*lines, f"WAIT: a merge freeze is on (release or incident hold); {who} is not a fix PR, so its CI waits for the release. "
                              "Commit locally and retry after the freeze lifts; a fix PR goes on the keep list (research/ci-keep.txt)"]
    return done.returncode, lines


def is_fix(target: str, keep_file: Path | str | None = None) -> bool:
    """Whether a push goes on during a merge freeze: release/*, bump/* or main, or a PR on the keep list. A branch is
    looked up as its open PR; a branch without one is not a fix."""
    if never_held(target):
        return True
    records = edge.records_dir()
    keep = Path(keep_file) if keep_file is not None else records / "research/ci-keep.txt"
    known = records / "research/known-failures.toml"
    numbers, _ = parse_keep(keep.read_text() if keep.exists() else None)
    numbers |= fix_prs(known.read_text() if known.exists() else "")
    if target.isdigit():
        return int(target) in numbers
    try:
        found = edge.run(["gh", "pr", "list", "-R", hzlib.CORE, "--head", target, "--state", "open", "--json", "number", "-q", ".[].number"],
                         capture_output=True, text=True, timeout=60).stdout.split()
    except (OSError, ValueError, edge.subprocess.SubprocessError):
        return False
    return any(n.isdigit() and int(n) in numbers for n in found)


def wait_reason(target: str, minimum: int | None = None, keep_file: Path | str | None = None) -> str:
    """"" when the push may go now; otherwise why it must wait (one or two lines)."""
    code, lines = check(target, minimum, keep_file)
    return "\n".join(line for line in lines if line.startswith("WAIT")) if code == WAIT else ""
