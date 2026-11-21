"""The size cap of the tracking document, for `sm status --write`: the document does not grow past the cap; the oldest
checkpoints, then the log lines, move into the dated log file instead. Pure: text in, (text, moved text) out."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Cap:
    limit: int = 16 * 1024  # bytes
    keep: int = 3  # newest checkpoints that stay
    checkpoints: str = "## 5."  # the section whose `### ` blocks are checkpoints, newest first
    log: str = "## 8."  # the log section: dated lines, oldest first
    keep_log: int = 20  # newest log lines that stay


def _section(lines: list[str], start: str) -> tuple[int, int] | None:
    """(first line after the heading, end) of the section whose heading starts with `start`; None when there is none."""
    begin = next((i for i, line in enumerate(lines) if line.startswith(start)), None)
    if begin is None:
        return None
    end = next((i for i in range(begin + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return begin + 1, end


def apply(text: str, cap: Cap) -> tuple[str, str]:
    """(the capped document, what moved out). Nothing moves while the document fits."""
    if len(text.encode()) <= cap.limit:
        return text, ""
    lines = text.split("\n")
    moved: list[str] = []
    if (found := _section(lines, cap.checkpoints)) is not None:
        begin, end = found
        starts = [i for i in range(begin, end) if lines[i].startswith("### ")]
        if len(starts) > cap.keep:
            cut = starts[cap.keep]
            moved += lines[cut:end]
            lines = lines[:cut] + lines[end:]
    if len("\n".join(lines).encode()) > cap.limit and (found := _section(lines, cap.log)) is not None:
        begin, end = found
        entries = [i for i in range(begin, end) if lines[i].strip()]
        if len(entries) > cap.keep_log:
            cut = entries[-cap.keep_log]
            moved += [line for line in lines[begin:cut] if line.strip()]
            lines = lines[:begin] + lines[cut:]
    return "\n".join(lines), "\n".join(moved)
