"""The tool map: every old tool of a project and what became of it. Generic mechanics; the rows are a plugin's data
(hook `sm_tool_map`). It answers "what replaced X" and "what is not ported yet", from the command tree itself.

A row's status is computed, never written down:
    native    the row names a command path and that command exists; a row naming a group also lists in `requires`
              the command paths of every old subcommand, and all of them exist (a partial port stays missing)
    missing   the row names a command path and no such command exists yet (not ported)
    internal  no command of its own: its logic lives inside other commands or a library
    retired   gone, with the reason in the note
"""

from __future__ import annotations

from .models import ToolMapRow


def status(row: ToolMapRow, paths: set[str]) -> str:
    if row.kind in ("internal", "retired"):
        return row.kind
    if row.requires:  # a group: native only when the commands of every old subcommand exist
        return "native" if all(r in paths for r in row.requires) else "missing"
    return "native" if row.path in paths else "missing"


def resolve(rows: list[ToolMapRow], paths: set[str]) -> list[ToolMapRow]:
    """Rows with their status and, for `also`, only the paths that exist."""
    return [row.model_copy(update={"status": status(row, paths)}) for row in rows]


def where(rows: list[ToolMapRow], name: str) -> ToolMapRow | None:
    by_tool = {r.tool: r for r in rows}
    for candidate in (name, name + ".py", *(f"{prefix}{name}" for prefix in sorted({r.tool.split("-")[0] + "-" for r in rows if "-" in r.tool}))):
        if candidate in by_tool:
            return by_tool[candidate]
    return None


def describe(row: ToolMapRow, prog: str) -> str:
    also = f"   (also: {', '.join(prog + ' ' + a for a in row.also)})" if row.also else ""
    note = f"   # {row.note}" if row.note else ""
    if row.status == "native":
        return f"{row.tool} -> {prog} {row.path}{also}{note}"
    if row.status == "missing":
        return f"{row.tool}: not ported yet; it becomes `{prog} {row.path}`" + (f" (owner: {row.owner})" if row.owner else "") + note
    if row.status == "retired":
        return f"{row.tool}: retired, no command{note}"
    return f"{row.tool}: internal, no command of its own{note}"


def counts(rows: list[ToolMapRow]) -> dict[str, int]:
    return {k: sum(1 for r in rows if r.status == k) for k in ("native", "missing", "internal", "retired")}
