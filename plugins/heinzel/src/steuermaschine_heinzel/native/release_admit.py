"""Native release-main admission: `sm release admit prepare|apply` (one reviewed release main manifest, fresh or
paused recovery). Both are write commands under one review file: prepare proves stuck-queued receipts from the
active-run list rows (no cancel probe) and stores the transaction apply depends on; apply performs the one registry
write, after one full per-run proof with cancel probe directly before it.
"""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import typer

from steuermaschine.engine import Refuse, Wait
from steuermaschine.helpdoc import H
from steuermaschine.native import native, runtime

from . import admission as _admission
from . import release_window
from .admission_lib import ops as admission
from .admission_lib import wait
from .admission_lib.hold import admission_hold
from .release_admit_lib import release_ops

ERRORS = release_ops.ERRORS
UNIT = ("release_admit.py", "release_admit_lib", "admission_lib")  # the route runs the PR admission's modules too
REVIEW = "release-admit-apply.md"
WAIT_HELP = "Wait while a validation run (or another uncovered run) is active; the admission hold keeps new validations from starting."


def _refuse(error: Exception) -> NoReturn:
    raise Refuse(f"release-main admission refused: {error}", "") from error


@native("release admit prepare", writes=True, review=REVIEW, files=UNIT, replaces=("hz-focused-admit",), help=H(
    "Prepare the registry transaction for one reviewed release main manifest; stores it and its arguments next to the packet.",
    "Use before the release branch is pushed and before its PR is opened, on current main.",
    "prepare.out and prepare.args.json next to the packet; takes the release-window lock; nothing on GitHub but reads. No variable is written.",
    "sm release admit prepare PACKET.json --core-repo WORKTREE --review-root ROOT",
    "sm head hash PACKET_DIR/prepare.out, then an independent review of that file",
    "The release-window lock (release_window_minutes, default 60, renewed by every prepare and apply): while it holds,\n"
    "`sm head validate` waits (exit 75) and starts no hosted run that would stand in the way of the cut."))
def release_admit_prepare(
    packet: Path = typer.Argument(..., metavar="PACKET", help="The reviewed release packet."),
    core_repo: Path = typer.Option(..., "--core-repo", help="The owned Core checkout at the release head."),
    review_root: Path = typer.Option(..., "--review-root", help="Root the packet's review paths are relative to."),
    nonconsumers: Path | None = typer.Option(None, "--nonconsumers", help="Reviewed stuck-queued receipts file."),
    wait_: bool = typer.Option(False, "--wait", help=WAIT_HELP),
) -> release_ops.ReleasePrepareResult:
    rt = runtime()
    try:
        # Order: the release-window lock first (the whole prepare-to-apply stretch: `sm head validate` waits while it holds), the
        # admission hold inside it (atomic in the validation line: a validation that passed the lock check a moment ago still cannot start).
        with release_window.held(rt, "prepare", str(packet), give_back_on_success=False), admission_hold(rt, f"release admit prepare {packet}"):
            def once() -> release_ops.ReleasePrepareResult:
                prepared = release_ops.prepare(packet, core_repo, review_root, nonconsumers=nonconsumers)
                _admission.register_prepared(prepared.sha256, "release admit prepare")  # every path that stores a prepare.out registers it
                return prepared

            done: release_ops.ReleasePrepareResult = wait.retry_when_clear(once, step="prepare", **wait.limits(rt)) if wait_ else once()
            return done
    except wait.StillWaiting as waiting:
        raise Wait(waiting.message, waiting.waiting_on) from waiting
    except ERRORS as error:
        _refuse(error)


@native("release admit apply", writes=True, review=REVIEW, files=UNIT, replaces=("hz-focused-admit",), help=H(
    "Apply one approved release-main transaction: the single registry slot write, with journal and receipt.",
    "Use after an independent reviewer approved the sha256 of prepare.out.",
    "one repository variable (a focused plan slot); pauses and restores the merge queue; writes the attempt journal and a receipt; "
    "holds the release-window lock while it runs and gives it back when the write succeeded.",
    "sm release admit apply PACKET.json --approved SHA256",
    "sm release cut VERSION"))
def release_admit_apply(
    packet: Path = typer.Argument(..., metavar="PACKET", help="The release packet that was prepared."),
    approved: str = typer.Option(..., "--approved", help="The sha256 of prepare.out the reviewer approved."),
    wait_: bool = typer.Option(False, "--wait", help=WAIT_HELP),
) -> admission.ApplyResult:
    rt = runtime()
    try:
        _admission.require_verdict(approved)  # before the window lock and the hold: a missing verdict never holds either
        # Order: the release-window lock first (the whole prepare-to-apply stretch: `sm head validate` waits while it holds), the
        # admission hold inside it (atomic in the validation line: a validation that passed the lock check a moment ago still cannot start).
        with release_window.held(rt, "apply", str(packet), give_back_on_success=True), admission_hold(rt, f"release admit apply {packet}"):
            def once() -> admission.ApplyResult:
                return release_ops.apply(packet, approved)

            done: admission.ApplyResult = wait.retry_when_clear(once, **wait.limits(rt)) if wait_ else once()
            return done
    except wait.StillWaiting as waiting:
        raise Wait(waiting.message, waiting.waiting_on) from waiting
    except ERRORS as error:
        _refuse(error)
