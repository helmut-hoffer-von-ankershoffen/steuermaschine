"""Rows of the agent routing table (a Markdown table in the tracker's repository): one row per agent id.

Several agents are started at once, so `add` holds an exclusive lock (a file all writers agree on, setting
`agents_lock_file`) from the read through the duplicate check to the write, and replaces the file by an atomic rename
(the core's `journal.replace_text`: temporary file next to the table, fsync, rename). The table is never truncated in
place. A table that cannot be read as UTF-8 text, has no table row, or has a row that is not closed by `|` is refused
and left untouched.
"""

from __future__ import annotations

import fcntl
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from steuermaschine import journal
from steuermaschine.models import Model

from ..coord_lib.base import optional_path, tracker_dir


class AgentsSettings(BaseModel):
    """Settings of the agent routing table (mixin of the plugin's settings table)."""

    agents_table: Path | None = Field(None, description="The Markdown file with the agent routing table; none: research/agent-ids.md in the tracker's repository.")
    agents_lock_file: Path | None = Field(None, description="Lock held by every add from the read to the write; none: <agents_table>.lock.")


class AgentRow(Model):
    """One row added to the routing table, or the reason none was added."""

    row: str = ""
    added: bool
    agent: str
    file: str

    def render(self) -> list[str]:
        return [self.row if self.added else f"already listed: {self.agent}"]


class AgentRows(Model):
    rows: list[str]
    file: str

    def render(self) -> list[str]:
        return self.rows


class TableProblem(ValueError):
    """The table file or the arguments cannot be used; nothing was written."""


def table_file(settings: Mapping[str, Any] | None) -> Path:
    return optional_path(settings, "agents_table", AgentsSettings) or tracker_dir(settings) / "research" / "agent-ids.md"


def lock_file(settings: Mapping[str, Any] | None, doc: Path) -> Path:
    return optional_path(settings, "agents_lock_file", AgentsSettings) or doc.with_name(doc.name + ".lock")


def cell(text: str) -> str:
    """One table cell: whitespace (newlines included) squeezed, `|` becomes `/`."""
    return " ".join(text.split()).replace("|", "/")


def cells(line: str) -> list[str]:
    return [part.strip() for part in line.strip()[1:-1].split("|")]


def parse(text: str, doc: Path) -> list[str]:
    """The table rows of the file. Raises TableProblem when there is none or one is not closed by `|`."""
    found = [line for line in text.splitlines() if line.startswith("|")]
    if not found:
        raise TableProblem(f"{doc} has no table row: not the routing table?")
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("|") and (len(line.rstrip()) < 2 or not line.rstrip().endswith("|")):
            raise TableProblem(f"{doc} line {number} is not a closed table row: fix it by hand first")
    return found


def add(bead: str, pr: str, agent_id: str, model: str, role: str = "", *, doc: Path, lock: Path | None = None) -> str:
    """Append the row and return it, or return `already listed: ID` (then nothing is written). An agent id counts as
    listed when it is a whole cell of any row. Raises TableProblem (nothing written) for an unusable file or an agent
    id that is empty or contains whitespace or `|`."""
    if not agent_id or agent_id != "".join(agent_id.split()) or "|" in agent_id:
        raise TableProblem(f"not an agent id: {agent_id!r}")
    pr = pr if pr.startswith(("core", "plugins", "-")) or not pr.isdigit() else f"PR #{pr}"
    row = f"| {cell(bead)} | {cell(f'{pr} {role}')} | {agent_id} | {cell(model)} |"
    row.encode("utf-8")  # text that cannot be written raises before the lock is taken
    lock_path = lock or doc.with_name(doc.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        try:
            text = doc.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            raise TableProblem(f"{doc} is not UTF-8 text: fix it by hand first") from None
        if any(agent_id in cells(line) for line in parse(text, doc)):
            return f"already listed: {agent_id}"
        journal.replace_text(doc, (text if not text or text.endswith("\n") else text + "\n") + row + "\n")
    return row


def rows(doc: Path) -> list[str]:
    return [line for line in doc.read_text().splitlines() if line.startswith("| ")]
