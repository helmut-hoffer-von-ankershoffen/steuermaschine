"""The help contract, and its lint.

Every command and group starts its help with five lines, in this order:
    purpose                         what it is, in one line of at most 140 characters
    Use when|before|after|for|at    when to type it
    Changes: ...                    "nothing (read-only)" or exactly what it writes, pushes or dispatches
    Example: ...                    one command that can be copied
    Next: ...                       the command that usually follows
Strict: every argument and option says what it is (its own help text); the lint names the ones that do not (letter A).
The lines come in the order above (letter O; a long line may wrap onto the next), and the example types the program,
or feeds it from a pipe (`... | sm ...`) (letter X).
Hidden commands follow the contract too; the root of a declared surface has the `sm -h` contract instead (surface.py).
The lint walks the built command tree (it never runs a command). Output format is not part of a command's help:
the root option --format text|json applies to every command.
"""

from __future__ import annotations

import re
from typing import Any

FIRST_LINE_MAX = 140
WHEN = re.compile(r"(?im)^\s*use (it |this )?(when|before|after|for|at)\b")
CHANGES = re.compile(r"(?m)^\s*Changes:\s*\S")
EXAMPLE = re.compile(r"(?m)^\s*Example:\s*\S")
NEXT = re.compile(r"(?m)^\s*Next:\s*\S")
READ_ONLY = re.compile(r"(?m)^\s*Changes:\s*nothing\b")


def H(purpose: str, when: str, changes: str, example: str, next_: str, more: str = "") -> str:
    """Help text in contract form. The \\b marker keeps the five lines from being re-wrapped."""
    text = f"\b\n{purpose}\n{when}\nChanges: {changes}\nExample: {example}\nNext: {next_}"
    return text + (f"\n\n{more}" if more else "")


def missing(text: str) -> str:
    """Letters of the contract a help text lacks: P purpose, W when, C changes, E example, N next."""
    lines = [ln for ln in (text or "").replace("\b", "").splitlines() if ln.strip()]
    out = ""
    if not lines or len(lines[0]) > FIRST_LINE_MAX or lines[0].lstrip().lower().startswith(("use ", "changes:", "example:", "next:")):
        out += "P"
    for letter, pattern in (("W", WHEN), ("C", CHANGES), ("E", EXAMPLE), ("N", NEXT)):
        if not pattern.search(text or ""):
            out += letter
    return out


def disorder(text: str, prog: str) -> str:
    """Letters for a help text whose contract lines are all there: O when they are not in the order purpose, Use,
    Changes, Example, Next; X when the example does not type `prog` (directly or after a pipe)."""
    text = text or ""
    found = [pattern.search(text) for pattern in (WHEN, CHANGES, EXAMPLE, NEXT)]
    if not all(found):
        return ""  # missing() names the absent line
    starts = [m.start() for m in found if m]
    out = "" if starts == sorted(starts) else "O"
    example = text[found[2].end() - 1 :].splitlines()[0] if found[2] else ""
    return out + ("" if re.search(rf"(^|\| ){re.escape(prog)} ", example) else "X")


def undocumented(command: Any) -> list[str]:
    """Arguments and options of one command without their own help text (`--help` is Click's own)."""
    out = []
    for param in getattr(command, "params", []):
        opts = list(getattr(param, "opts", []))
        if "--help" in opts or getattr(param, "help", None):
            continue
        longest = max((o for o in opts if o.startswith("-")), key=len, default="")
        out.append(longest or (getattr(param, "metavar", None) or param.name or "?").upper())
    return out


def walk(command: Any, path: str) -> Any:
    """Yield (path, command, is_group) for a built command tree, depth first. Works on Typer's own Click objects."""
    children = getattr(command, "commands", None)
    yield path, command, children is not None
    for name, child in sorted((children or {}).items()):
        yield from walk(child, f"{path} {name}")


def hidden_paths(command: Any, path: str, below: bool = False) -> set[str]:
    """The paths that are hidden themselves or sit below a hidden group."""
    hidden = below or bool(getattr(command, "hidden", False))
    out = {path} if hidden else set()
    for name, child in (getattr(command, "commands", None) or {}).items():
        out |= hidden_paths(child, f"{path} {name}", hidden)
    return out


def tree(root: Any, prog: str, writes: dict[str, bool] | None = None) -> list[dict[str, Any]]:
    """The command tree as data: the one source for help lint, and later for MCP tools and generated skills.
    `writes` (path -> bool) is what a command declares (`writes=` of a native command, a pipeline, a core writer); it
    decides `read_only`. A command that declares nothing is read-only when its Changes line says nothing (read-only)."""
    rows, writes = [], writes or {}
    hidden = hidden_paths(root, prog)
    for path, command, is_group in walk(root, prog):
        text = (command.help or "").replace("\b", "").strip()
        opts = sorted(o for p in command.params for o in getattr(p, "opts", []) if o.startswith("--"))
        args = [p.name for p in command.params if not any(o.startswith("-") for o in getattr(p, "opts", []))]
        rows.append({"path": path, "group": is_group, "hidden": path in hidden, "purpose": text.splitlines()[0] if text else "",
                     "help": text, "options": opts, "arguments": args, "declared": path in writes,
                     "read_only": (not writes[path]) if path in writes and not is_group else bool(READ_ONLY.search(text)),
                     "missing": "" if getattr(command, "sm_surface", False) else missing(text) + disorder(text, prog) + ("A" if undocumented(command) else ""),
                     "undocumented": undocumented(command)})
    return rows


def finding(row: dict[str, Any]) -> str:
    """One lint line for a row of `tree`; empty when the row passes."""
    if not row["missing"]:
        return ""
    return f"{row['path']}: missing {row['missing']}" + (f" (no help: {', '.join(row['undocumented'])})" if row["undocumented"] else "")


def disagrees(row: dict[str, Any]) -> str:
    """A declared write flag and the Changes line must tell the same story: a command declared read-only says
    `Changes: nothing ...`; a write command does not say `nothing (read-only ...)`."""
    if not row.get("declared") or row["group"]:
        return ""
    says_nothing = bool(re.search(r"(?m)^\s*Changes:\s*nothing\b", row["help"]))
    says_read_only = bool(re.search(r"(?m)^\s*Changes:\s*nothing \(read-only", row["help"]))
    if row["read_only"] and not says_nothing:
        return "declared read-only (writes=False) but its Changes line names a write: register it with writes=True"
    if not row["read_only"] and says_read_only:
        return "declared as writing but its Changes line says read-only"
    return ""


def lint(root: Any, prog: str) -> list[str]:
    return [line for row in tree(root, prog) if (line := finding(row))]


def markdown(rows: list[dict[str, Any]], prog: str) -> str:
    """A Markdown command reference from `tree` rows: one section per command with its purpose and the Use, Changes and
    Example lines of its help. Groups are headings of their own; the order is the tree's."""
    out = [f"# `{prog}` command reference", "", f"Generated from the command tree by `{prog} self doc`; do not edit by hand.", ""]
    for row in rows:
        depth = len(row["path"].split())
        lines = [ln.strip() for ln in row["help"].splitlines() if ln.strip()]
        keep = [ln for ln in lines[1:6] if WHEN.match(ln) or ln.startswith(("Changes:", "Example:"))]
        out += [f"{'#' * min(depth + 1, 4)} `{row['path']}`" + (" (writes)" if not row["read_only"] else ""), "", row["purpose"], ""]
        out += [f"- {ln}" for ln in keep] + ([""] if keep else [])
    return "\n".join(out).rstrip() + "\n"
