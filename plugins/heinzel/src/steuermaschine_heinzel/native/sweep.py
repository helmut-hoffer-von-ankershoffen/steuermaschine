"""`sm beads sweep`: the evidence table of a bead sweep. The logic is in sweep_lib/evidence.py. It never closes a bead."""

from __future__ import annotations

import typer

from steuermaschine import USAGE
from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.native import native

from .sweep_lib import evidence


@native("beads sweep", writes=False, replaces=("hz-bead-sweep",), help=H(
    "Evidence table for a bead sweep: per open bead its pull requests, the release that contains each, age, assignee, due.",
    "Use at each learn pass and after every release's LIVE-OK, before any bead is closed.",
    "nothing outside this tool (keeps the pull request list for 10 minutes in its own state); it never closes a bead.",
    "sm beads sweep --all --propose-close --prs 796,795",
    "verify each proposed bead's behaviour live, then run the printed `bd close` yourself.",
    "Hints: A? needs live and behaviour verification, B? shipped but not live-verified, C? in flight, D? not started.\n"
    "Evidence per bead: pull request numbers in its notes, pull requests that name the bead id, pull requests that\n"
    "share three or more title keywords (kw), and the release tag that contains each merged one.\n"
    "--propose-close prints `bd close` lines only after a fresh live verification says LIVE-OK for exactly the version\n"
    "the installed CLI reports; otherwise exit 1 and no proposal. A proposal is not a verification of the bead's behaviour."))
def beads_sweep(p0: bool = typer.Option(False, "--p0", help="Only priority 0."), p1: bool = typer.Option(False, "--p1", help="Only priority 1."),
                p2: bool = typer.Option(False, "--p2", help="Only priority 2."), p3: bool = typer.Option(False, "--p3", help="Only priority 3."),
                all_: bool = typer.Option(False, "--all", help="Every open, in-progress and blocked bead."),
                stale_hours: int | None = typer.Option(None, "--stale-hours", metavar="N", help="Only beads untouched for N hours."),
                prs: str | None = typer.Option(None, "--prs", metavar="N,N", help="Only beads whose evidence includes these pull requests (after a release's LIVE-OK)."),
                propose_close: bool = typer.Option(False, "--propose-close", help="Print close commands for released, live-verified evidence; never runs them."),
                summary: bool = typer.Option(False, "--summary", help="One line: open count, P0, with merged and released pull requests, untouched over 48 h.")) -> evidence.Sweep:
    priority = next((n for n, chosen in enumerate((p0, p1, p2, p3)) if chosen), None)
    try:
        only = {int(x) for x in prs.split(",") if x.strip()} if prs else None
        return evidence.sweep(priority, all_, stale_hours, only, propose_close, summary)
    except ValueError:
        raise Refuse("choose a slice: --p0 --p1 --p2 --p3 --all (and --prs takes numbers: 796,795)", "sm beads sweep --all", code=USAGE) from None
