"""`sm head combine-close`: after a combine pull request merged, close its members and note their beads."""

from __future__ import annotations

import typer

from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.native import native

from .merges_lib import combine_close



@native("head combine-close", writes=True, replaces=("hz-merged-via",), help=H(
    "After a combine pull request merged: close its still-open members and note the beads that mention them.",
    "Use after a combine merged whose members were not closed by the merge queue, or after one member was moved out.",
    "with --apply: closes each open member pull request on GitHub with a comment, and adds one note per bead (bd). Without: nothing.",
    "sm head combine-close 717 --apply",
    "sm beads sweep --prs 717   (the beads stay open until released and verified)",
    "Members are the `Combines #N` lines of the combine's body. The comment is `merged via #X at <sha>`; the bead note\n"
    "adds `merged via, not yet released`. Refuses (exit 1, nothing written) when the combine is not merged. It never\n"
    "closes a bead. Dry run unless --apply."))
def head_combine_close(combine: int = typer.Argument(..., metavar="PR", help="The merged combine pull request."),
                       apply: bool = typer.Option(False, "--apply", help="Close the members and write the notes; without it, only list."),
                       repo: str | None = typer.Option(None, "--repo", metavar="OWNER/NAME", help="Repository; default: the core repository.")) -> combine_close.CombineClose:
    try:
        return combine_close.close(combine, apply, repo or "")
    except combine_close.NotMerged as stop:
        raise Refuse(str(stop), f"gh pr view {combine}") from None
