"""The coordinator commands: journal, bead stamps, owner asks, briefs, evidence, the tracking document, role skills.

Each command is a thin function here; the logic and the result models are in `coord_lib/`.
"""

from __future__ import annotations

import typer

from steuermaschine import USAGE
from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.native import Group, native

from .coord_lib import journal
from .coord_lib.base import CoordSettings

__all__ = ["CoordSettings"]

BEADS = Group("beads", H(
    "beads: the work items of the tracker: stamps, evidence sweep, tasks, refill, briefs.",
    "Use when a bead must be stamped, checked for evidence, listed or handed to a builder.",
    "nothing by itself; see each command.",
    "sm beads sweep",
    "sm go"))


@native("note", writes=True, replaces=("hz-note",), help=H(
    "Append one dated line to the log at the end of the tracking document.",
    "Use after every event another session must know: a merge, a decision, a state reset, a forced step.",
    "appends one line to the tracking document (setting coord_document); nothing else. Not committed, not pushed.",
    'sm note "0.9.40 candidate dropped: web-2 e2e failed, PR 794 closed"',
    "sm doc handoff   (commits and pushes the document)",
    "Safe when several agents write at once. Spaces and line breaks in the text are squeezed to single spaces."))
def note(text: list[str] = typer.Argument(..., metavar="TEXT", help="What happened, and the PR or file it concerns.")) -> journal.Noted:
    try:
        return journal.note(" ".join(text))
    except ValueError:
        raise Refuse('give the text: sm note "what happened, and the PR or file it concerns"', "sm note TEXT", code=USAGE) from None


@native("beads stamp", writes=True, replaces=("hz-stamp",), help=H(
    "Release stamp on one bead: merged_in after its pull request merged, released_in once the release is published.",
    "Use after a pull request merged or a release was published, so the bead can be closed with evidence.",
    "appends a note to the bead and adds the label merged-N or released-vX.Y.Z (bd); GitHub is only read.",
    "sm beads stamp ligy --merged 796",
    "sm beads sweep   (the evidence table)",
    "Refuses (exit 1) while the pull request is not merged or the release is not published; nothing is written then.\n"
    "Idempotent: a bead that carries the label is left alone. The id may be the short suffix."))
def beads_stamp(bead: str = typer.Argument(..., metavar="BEAD", help="The bead id, full or the short suffix."),
                merged: int | None = typer.Option(None, "--merged", metavar="PR", help="The merged pull request."),
                released: str | None = typer.Option(None, "--released", metavar="VERSION", help="The published version, with or without the leading v."),
                repo: str | None = typer.Option(None, "--repo", metavar="OWNER/NAME", help="Repository of the pull request; default: the core repository."),
                dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be stamped; write nothing.")) -> journal.Stamped:
    if not merged and not released:
        raise Refuse("give --merged PR and/or --released VERSION", f"sm beads stamp {bead} --merged PR", code=USAGE)
    result = journal.stamp(bead, merged, released, repo, dry_run)
    if result.refused:
        raise Refuse(result.refused, "sm go")
    return result


@native("beads stamp-released", writes=True, replaces=("hz-stamp",), help=H(
    "After a publish: stamp released_in on every open bead whose merged pull request is in a published release.",
    "Use after a release is published, before the bead sweep.",
    "appends a note and the label released-vX.Y.Z to each such bead (bd); GitHub and git tags are only read.",
    "sm beads stamp-released --version 0.9.40 --dry-run",
    "sm beads sweep",
    "A bead qualifies when it is open, labelled merged-N, not labelled released-*, and the merge commit of N is\n"
    "contained in a release tag vX.Y.Z whose release is published; the earliest such tag is stamped."))
def beads_stamp_released(version: str | None = typer.Option(None, "--version", metavar="VERSION", help="Only beads whose earliest release is this version."),
                         dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be stamped; write nothing.")) -> journal.StampSweep:
    return journal.stamp_released(version, dry_run)
