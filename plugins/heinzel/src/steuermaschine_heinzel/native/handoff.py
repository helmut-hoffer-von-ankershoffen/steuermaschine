"""`sm doc handoff` and `sm doc hour`: publish the tracking files with the open-work snapshot; the hourly update draft."""

from __future__ import annotations

from typing import Any

import typer

from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.native import native, runtime

from .handoff_lib import hour, snapshot
from .handoff_lib.snapshot import HandoffSettings

__all__ = ["HandoffSettings"]

UNIT = ("handoff.py", "handoff_lib", "coord_lib")  # the review unit: the tracker directory and the child environment come from coord_lib

PUBLISHES = ("writes the open-work snapshot file, commits ONLY that file (and paths named in handoff_files) on branch main of the tracker's repository, "
             "and PUSHES main to origin, fast-forward only.")
LOCAL = typer.Option(False, "--include-local-commits", help="Also publish earlier local commits of the tracker's repository that the remote lacks.")
MORE = ("It never stashes, rebases, merges or stages anything else: uncommitted work in the tracker's checkout stays as it is.\n"
        "It refuses before writing anything when the checkout is not on main, when origin/main has commits the checkout\n"
        "lacks (fast-forward only), and when the checkout has local commits the remote lacks, because a push would\n"
        "publish them all; --include-local-commits allows that. A git step that fails stops it with exit 1 and names the step.")


@native("doc handoff", writes=True, replaces=("hz-handoff",), files=UNIT, help=H(
    "Publish a fresh open-work snapshot to the remote of the tracker's repository, so another agent can take over from there.",
    "Use at a handoff, before a context reset, and after a checkpoint was written to the tracking document.",
    PUBLISHES,
    "sm doc handoff --dry-run",
    "sm go",
    "The snapshot lists the open pull requests, the last CI runs on main, the latest releases and local work that is on\n"
    "no remote (worktrees changed in the last days with unpushed commits or uncommitted files).\n" + MORE + "\n"
    "--dry-run prints the snapshot and the paths it would commit; it writes and fetches nothing."))
def doc_handoff(dry_run: bool = typer.Option(False, "--dry-run", help="Print the snapshot; write, commit and push nothing."), include_local_commits: bool = LOCAL) -> snapshot.Handoff:
    try:
        return snapshot.handoff(runtime(), dry_run, include_local_commits)
    except snapshot.StepFailed as stop:
        raise Refuse(str(stop), "git -C <the tracker's repository> status") from None


@native("doc hour", writes=True, replaces=("hz-hour",), files=UNIT, help=H(
    "The facts of this hour and a draft of the owner update; then a handoff, unless --no-handoff.",
    "Use at every full hour between 07:00 and 23:00, and when something went live or a decision waits for the owner.",
    "without --no-handoff it " + PUBLISHES + " With --no-handoff: nothing.",
    "sm doc hour --no-handoff",
    "write the ETAs and decisions into the draft, then send it to the owner.",
    "Facts are the text of the read-only commands named in the setting hour_facts (a command that does not exist yet\n"
    "is shown as missing), the last CI runs on main, the latest releases and the load. The draft has four parts:\n"
    "live now, open pull requests, ETAs with confidence, decisions for the owner; the last two are yours to write.\n"
    "It changes no ruleset and acknowledges no owner message. With --no-handoff no file is written and no git command\n"
    "runs in the tracker's repository. The handoff: " + MORE))
def doc_hour(no_handoff: bool = typer.Option(False, "--no-handoff", help="Gather and draft only; write, commit and push nothing."), include_local_commits: bool = LOCAL) -> hour.Hour:
    rt: Any = runtime()
    try:  # the registry comes from the runtime: importing the plugin's registry would put every module into this unit
        return hour.build(rt, rt.commands(), no_handoff, include_local_commits=include_local_commits)
    except snapshot.StepFailed as stop:
        raise Refuse(str(stop), "sm doc hour --no-handoff") from None
