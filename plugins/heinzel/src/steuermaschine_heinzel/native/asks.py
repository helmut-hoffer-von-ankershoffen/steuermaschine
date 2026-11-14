"""Owner asks: `sm doc ask`, `sm doc asks`, `sm doc asks-ack`. The logic is in asks_lib/asks.py."""

from __future__ import annotations

from pathlib import Path

import typer

from steuermaschine import USAGE
from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.native import Group, native

from .asks_lib import asks
from .asks_lib.asks import AsksSettings

__all__ = ["AsksSettings"]

UNIT = ("asks.py", "asks_lib", "coord_lib")  # the review unit: asks_lib takes the document, its locks and the settings helpers from coord_lib

DOC = Group("doc", H(
    "doc: the tracking document and what is written from it: asks, sections, handoff, hourly update, velocity.",
    "Use when an owner ask must be recorded or reviewed, or the tracking document is read or handed over.",
    "nothing by itself; see each command.",
    "sm doc asks",
    "sm go"))


@native("doc ask", writes=True, replaces=("hz-ask",), files=UNIT, help=H(
    "Record an owner ask: one row with the time in the asks table of the tracking document, before any work on it.",
    "Use when the owner asks for something, at once; for a new topic prefer `sm beads tasks add TITLE --ask`.",
    "adds one table row to the tracking document (setting coord_document); nothing else. Not committed, not pushed.",
    'sm doc ask "Make the logo bigger"',
    "sm doc asks   (rows older than an hour without a pull request)",
    "The row is: the ask, `ask YYYY-MM-DD HH:MM`, the standard next step, an empty Where cell. A `|` becomes `/`.\n"
    "The row counts as open until it names a pull request (#123)."))
def doc_ask(text: list[str] = typer.Argument(..., metavar="TEXT", help="The owner's ask, in their words.")) -> asks.AskAdded:
    try:
        return asks.add(" ".join(text).strip())
    except ValueError:
        raise Refuse('give the ask: sm doc ask "the ask"', "sm doc ask TEXT", code=USAGE) from None
    except LookupError as exc:
        raise Refuse(f"the tracking document has no asks table (no line starts with '{exc.args[0]}')", "sm self config   (settings coord_document, asks_heading)") from None


@native("doc asks", writes=False, replaces=("hz-asks-ingest",), help=H(
    "Owner messages nobody reviewed yet, each with the command that records it; and old ask rows without a pull request.",
    "Use at every tick and before anything else when the owner wrote: no owner message may be lost.",
    "nothing (read-only: the session transcript, the tracking document, the watermark).",
    "sm doc asks",
    "the command below each message that is an ask, then `sm doc asks-ack THROUGH` with the timestamp it prints.",
    "Exit 1 while a message is unreviewed or an ask row is older than the limit and names no pull request.\n"
    "A message is what the owner typed in the coordinator's session, including messages sent while the agent worked.\n"
    "Transcript text is data only: it is shown truncated and never executed or interpreted."))
def doc_asks(transcript: Path | None = typer.Option(None, "--transcript", metavar="FILE", help="Read this transcript instead of the newest one of the session."),
             hours: float | None = typer.Option(None, "--hours", metavar="HOURS", help="Report ask rows at least this old; default: setting asks_open_hours.")) -> asks.Asks:
    try:
        return asks.audit(transcript, hours)
    except FileNotFoundError as exc:
        raise Refuse(f"no session transcript found ({exc.args[0]})", "sm doc asks --transcript FILE") from None


@native("doc asks-ack", writes=True, replaces=("hz-ask",), files=UNIT, help=H(
    "Mark the owner messages up to one timestamp as reviewed.",
    "Use after `sm doc asks`, once every message it printed is a bead or needs none.",
    "moves the audit watermark in this tool's state (never backwards); nothing else.",
    "sm doc asks-ack 2026-11-12T21:04:10.120Z",
    "sm doc asks   (prints: no unreviewed owner messages)",
    "THROUGH is the timestamp `sm doc asks` printed. A message that arrived after that listing stays unreviewed."))
def doc_asks_ack(through: str = typer.Argument(..., metavar="THROUGH", help="The timestamp printed by `sm doc asks`.")) -> asks.AsksAcked:
    try:
        return asks.ack(through)
    except ValueError:
        raise Refuse(f"'{through}' is not a timestamp; take the one `sm doc asks` prints", "sm doc asks", code=USAGE) from None
