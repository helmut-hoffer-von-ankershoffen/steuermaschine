"""`sm beads agents add|list`: the agent routing table."""

from __future__ import annotations

from pathlib import Path

import typer

from steuermaschine import USAGE
from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.native import Group, native, runtime

from .agents_lib import table
from .agents_lib.table import AgentsSettings

__all__ = ["AgentsSettings"]


AGENTS = Group("beads agents", H(
    "beads agents: the routing table of running agents (bead, pull request, agent id, model).",
    "Use when an agent is started (add its row) or must be messaged or resumed (find its id).",
    "add appends one row to the routing table; list only reads.",
    "sm beads agents list",
    "sm beads refill"))


def _file() -> Path:
    path = table.table_file(runtime().settings)
    if not path.is_file():
        raise Refuse(f"the routing table {path} does not exist", "sm self config   (settings agents_table, coord_tracker_dir)", code=USAGE)
    return path


@native("beads agents add", writes=True, replaces=("hz-agent",), help=H(
    "Add one agent to the routing table: bead, pull request, agent id, model, once per agent id.",
    "Use at the moment an agent is started, so it can be messaged and resumed after a context reset.",
    "adds one row at the end of the routing table file (setting agents_table), under a lock and by an atomic rename;\n"
    "nothing else. Not committed, not pushed.",
    "sm beads agents add ligy 796 a9b40bbe597ed25d8 sonnet reviewer",
    "sm beads agents list",
    "An agent id that is already a cell of a row is not added again. An unreadable table is refused, not rewritten.\n"
    "PR may be a number (written as `PR #N`), or text such as `core`, `plugins` or `-` when there is no pull request yet."))
def beads_agents_add(bead: str = typer.Argument(..., metavar="BEAD", help="The bead id the agent works on."), pr: str = typer.Argument(..., metavar="PR", help="A number, or core, plugins, -."),
                     agent_id: str = typer.Argument(..., metavar="AGENT_ID", help="The id to message and resume the agent with."),
                     model: str = typer.Argument(..., metavar="MODEL", help="The model the agent runs on."),
                     role: str = typer.Argument("", metavar="[ROLE]", help="builder, reviewer, ...")) -> table.AgentRow:
    path = _file()
    try:
        got = table.add(bead, pr, agent_id, model, role, doc=path, lock=table.lock_file(runtime().settings, path))
    except table.TableProblem as exc:
        raise Refuse(str(exc), "sm beads agents list") from None
    return table.AgentRow(row="" if got.startswith("already listed: ") else got, added=not got.startswith("already listed: "), agent=agent_id, file=str(path))


@native("beads agents list", writes=False, replaces=("hz-agent",), help=H(
    "The rows of the agent routing table.",
    "Use before messaging, resuming or replacing an agent.",
    "nothing (read-only).",
    "sm beads agents list",
    "sm beads refill   (owners that stopped, ready beads against free slots)"))
def beads_agents_list() -> table.AgentRows:
    path = _file()
    return table.AgentRows(rows=table.rows(path), file=str(path))
