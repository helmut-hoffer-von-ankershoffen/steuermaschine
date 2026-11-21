"""`sm beads tasks ...`: the beads with this project's conventions. The logic is tasks_lib/beads.py."""

from __future__ import annotations

import typer

from steuermaschine import USAGE
from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.native import Group, native, runtime

from .tasks_lib import beads

TASKS = Group("beads tasks", H(
    "beads tasks: create, note, claim, schedule, close and list beads with this project's conventions.",
    "Use for every owner message that is an ask (add --ask), and whenever a bead's state, owner or pull request changes.",
    "add, note, claim, due-set, pr and done write to the issue tracker; due, ls and show only read.",
    'sm beads tasks add "Logo is cut off on phones" --ask -a logo-builder --due 10:30',
    "sm beads brief TOPIC BRANCH --bead ID"))
ID = typer.Argument(..., metavar="ID", help="The bead id, full or the short suffix.")


@native("beads tasks add", writes=True, replaces=("hz-tasks",), help=H(
    "Create a bead; with --ask also record the owner ask in the tracking document.",
    "Use for every actionable owner message and every new piece of work, after searching for an existing bead.",
    "creates one bead in the issue tracker (bd create); with --ask also adds one ask row to the tracking document.",
    'sm beads tasks add "Logo is cut off on phones" --ask -a logo-builder --due 10:30',
    "sm beads brief TOPIC BRANCH --bead ID",
    "Prints the new bead's short id. --due takes HH:MM (today) or `YYYY-MM-DD HH:MM`; it is stored as a label\n"
    "due-YYYYMMDD-HHMM in local time."))
def beads_tasks_add(title: str = typer.Argument(..., metavar="TITLE", help="The bead title, one line."), priority: str = typer.Option("2", "-p", "--priority", metavar="N", help="0 (highest) to 4."),
                    agent: str = typer.Option("", "-a", "--assignee", metavar="AGENT", help="Who works on it."),
                    labels: list[str] | None = typer.Option(None, "-l", "--labels", metavar="LABEL", help="A label."),
                    due: str | None = typer.Option(None, "--due", metavar="WHEN", help="HH:MM today, or YYYY-MM-DD HH:MM."),
                    description: str = typer.Option("", "-d", "--description", metavar="TEXT", help="The owner's words."),
                    kind: str = typer.Option("task", "--type", metavar="TYPE", help="task, bug, feature, ..."),
                    ask: bool = typer.Option(False, "--ask", help="Also record the owner ask in the tracking document.")) -> beads.TaskDone:
    if not title.strip():
        raise Refuse("give a title", 'sm beads tasks add "title"', code=USAGE)
    try:
        return beads.Tracker(runtime()).add(title, priority, agent, ",".join(labels or []), due, description, kind, ask)
    except LookupError as exc:
        raise Refuse(f"the bead exists, but the tracking document has no asks table (no line starts with '{exc.args[0]}'): add the row by hand", "sm doc ask TEXT") from None


@native("beads tasks note", writes=True, replaces=("hz-tasks",), help=H(
    "Append a dated note to a bead; history is never replaced.",
    "Use when something about the bead changed: a finding, a repeat of the ask, a decision.",
    "appends one note to the bead (bd update --append-notes).",
    'sm beads tasks note ligy "root cause: the helper retried without an end-state check"',
    "sm beads tasks show ID"))
def beads_tasks_note(bead: str = ID, text: str = typer.Argument(..., metavar="TEXT", help="The note, one line.")) -> beads.TaskDone:
    return beads.Tracker(runtime()).note(bead, text)


@native("beads tasks claim", writes=True, replaces=("hz-tasks",), help=H(
    "Give a bead to an agent: status in progress, assignee, optionally a deadline.",
    "Use when an agent starts on a bead.",
    "sets status and assignee of the bead; with --due replaces its due label (bd update, bd label).",
    "sm beads tasks claim ligy details-builder --due 10:30",
    "sm beads agents add BEAD PR AGENT_ID MODEL"))
def beads_tasks_claim(bead: str = ID, agent: str = typer.Argument(..., metavar="AGENT", help="The agent that works on it."),
                      due: str | None = typer.Option(None, "--due", metavar="WHEN", help="HH:MM today, or YYYY-MM-DD HH:MM.")) -> beads.TaskDone:
    return beads.Tracker(runtime()).claim(bead, agent, due)


@native("beads tasks due-set", writes=True, replaces=("hz-tasks",), help=H(
    "Set or replace the deadline of a bead.",
    "Use when a deadline is agreed or moves.",
    "removes the bead's due-* labels and adds the new one (bd label).",
    "sm beads tasks due-set ligy 10:30",
    "sm beads tasks due"))
def beads_tasks_due_set(bead: str = ID, when: str = typer.Argument(..., metavar="WHEN", help="HH:MM today, or YYYY-MM-DD HH:MM.")) -> beads.TaskDone:
    return beads.Tracker(runtime()).due_set(bead, when)


@native("beads tasks pr", writes=True, replaces=("hz-tasks",), help=H(
    "Note a pull request on its bead.",
    "Use when a pull request is opened, merged or closed for a bead.",
    "appends one note `PR #N STATE` to the bead (bd update --append-notes).",
    "sm beads tasks pr ligy 796 merged",
    "sm beads stamp ID --merged PR   (after the merge)"))
def beads_tasks_pr(bead: str = ID, number: str = typer.Argument(..., metavar="PR", help="The pull request number."),
                   state: str = typer.Argument("open", metavar="[STATE]", help="open, merged or closed.")) -> beads.TaskDone:
    return beads.Tracker(runtime()).pr(bead, number, state)


@native("beads tasks done", writes=True, replaces=("hz-tasks",), help=H(
    "Close a bead with its evidence.",
    "Use after the bead's behaviour was verified live, never before; `sm beads sweep` shows the evidence.",
    "CLOSES the bead with the reason (bd close --force).",
    'sm beads tasks done ligy "live in 0.9.40: details open on first tap (screenshot in the bead)"',
    "sm beads sweep --prs N",
    "The id must name exactly one bead: the tracker matches partial ids, so it is resolved with bd show first and\n"
    "a partial one (abc for abcd) is refused with exit 2 before anything is closed."))
def beads_tasks_done(bead: str = ID, evidence: str = typer.Argument(..., metavar="EVIDENCE", help="What was verified, and where.")) -> beads.TaskDone:
    if not evidence.strip():
        raise Refuse("give the evidence", 'sm beads tasks done ID "what was verified"', code=USAGE)
    return beads.Tracker(runtime()).done(bead, evidence)


@native("beads tasks due", writes=False, replaces=("hz-tasks",), help=H(
    "Beads in progress that are due soon or overdue.",
    "Use at every tick and before the hourly update.",
    "nothing (read-only).",
    "sm beads tasks due 120",
    "message the owner of each OVERDUE bead, or move its deadline with `sm beads tasks due-set`."))
def beads_tasks_due(minutes: int = typer.Argument(60, metavar="[MINUTES]", help="Due within this many minutes.")) -> beads.TaskList:
    return beads.Tracker(runtime()).due(minutes)


@native("beads tasks ls", writes=False, replaces=("hz-tasks",), help=H(
    "A compact list of beads: id, priority, assignee, deadline, title.",
    "Use when you need the beads of one state at a glance.",
    "nothing (read-only).",
    "sm beads tasks ls ready",
    "sm beads tasks show ID"))
def beads_tasks_ls(status: str = typer.Argument("in_progress", metavar="[STATUS]", help="A tracker status, or ready, or all.")) -> beads.TaskList:
    return beads.Tracker(runtime()).ls(status)


@native("beads tasks show", writes=False, replaces=("hz-tasks",), help=H(
    "One bead in full (capped at 1800 characters).",
    "Use before working on, noting or closing a bead.",
    "nothing (read-only).",
    "sm beads tasks show ligy",
    "sm beads tasks note ID TEXT"))
def beads_tasks_show(bead: str = ID) -> beads.TaskShown:
    return beads.Tracker(runtime()).show(bead)
