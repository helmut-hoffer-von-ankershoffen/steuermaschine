"""A journal: one dated line per event, appended to the end of a text document. Generic; a plugin says which document.

    line = journal.append(Path("NOTES.md"), "queue: #12 merged", now, lock_file=Path("~/.cache/notes.lock"))

    row = journal.add_table_row(Path("NOTES.md"), "### Now", ["Make the logo bigger", "ask 2026-11-12 07:00", "", ""], locks)

Several processes may write at once: each append holds an exclusive lock on `lock_file` (any file all writers agree
on) for the one write. The text is squeezed to one line. A document that does not end with a newline gets one first,
so a line never continues another. `add_table_row` rewrites the document, so it holds every lock it is given (its
own and the one of `append`) from the read to the write, and it replaces the file by an atomic rename: the document is
never truncated in place.
"""

from __future__ import annotations

import fcntl
import os
import stat
import time
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path

STAMP = "%m-%d %H:%M"


def line_of(text: str, now: float, stamp: str = STAMP) -> str:
    """The journal line for `text`: a list item with the local time, whitespace squeezed."""
    return f"- {time.strftime(stamp, time.localtime(now))} {' '.join(text.split())}"


def append(document: Path, text: str, now: float, lock_file: Path | None = None, stamp: str = STAMP) -> str:
    """Append one dated line and return it (without the newline). Raises ValueError for empty text."""
    if not text.strip():
        raise ValueError("no text")
    line = line_of(text, now, stamp)
    lock_path = lock_file or document.with_name(document.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lead = ""
        if document.is_file() and document.stat().st_size:
            with document.open("rb") as handle:
                handle.seek(-1, 2)
                lead = "" if handle.read(1) == b"\n" else "\n"
        with document.open("a") as handle:
            handle.write(f"{lead}{line}\n")
    return line


def table_end(lines: Sequence[str], heading: str) -> int | None:
    """The index after the last row of the table that follows the first line starting with `heading`. None when no
    line starts with it, or when the next heading (or the end) comes before any table row: a row is never put into
    another section's table."""
    start = next((i for i, line in enumerate(lines) if line.startswith(heading)), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and not lines[end].startswith("|"):
        if lines[end].startswith("#"):
            return None
        end += 1
    if end >= len(lines):
        return None
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    return end


def replace_text(document: Path, text: str) -> None:
    """Replace the document's content without ever truncating it in place: the bytes are built first (UTF-8; text that
    cannot be encoded raises before anything is written), written to a temporary file next to the document, synced,
    given the document's mode, and renamed over it. A reader sees the old or the new content, never a part."""
    data = text.encode("utf-8")
    temporary = document.with_name(f".{document.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(document.stat().st_mode))
        os.replace(temporary, document)
    finally:
        temporary.unlink(missing_ok=True)


def add_table_row(document: Path, heading: str, cells: Sequence[str], lock_files: Sequence[Path] = ()) -> str:
    """Add one row at the end of the Markdown table below `heading` and return the row. A `|` inside a cell becomes
    `/`; whitespace is squeezed; an empty cell is `| |`. Raises LookupError when no line starts with `heading` or no
    table follows it before the next heading, and UnicodeEncodeError (a ValueError) for text that is not valid
    UTF-8; in both cases the document is untouched."""
    clean = [" ".join(str(cell).split()).replace("|", "/") for cell in cells]
    row = "|" + "".join(f" {cell} |" if cell else " |" for cell in clean)
    row.encode("utf-8")
    with ExitStack() as stack:
        for lock_path in lock_files:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock = stack.enter_context(lock_path.open("a+"))
            fcntl.flock(lock, fcntl.LOCK_EX)
        lines = document.read_text(encoding="utf-8").splitlines()
        end = table_end(lines, heading)
        if end is None:
            raise LookupError(heading)
        lines.insert(end, row)
        replace_text(document, "\n".join(lines) + "\n")
    return row
