"""`sm lim`: run a command and print only what matters, so long output does not land in an agent's context.

`condense` is the former limiter's, unchanged. Where the full output of a cut command goes: `-o FILE`, else a new file
in the directory a plugin hands in through `sm_provider_extras` under the key `DIRECTORY` (a path or None), else `lim`
below the project's state directory.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import typer

from ..engine import Refuse
from ..helpdoc import H
from ..models import Model
from ..native import native, runtime

DIRECTORY = "lim.directory"  # key in `Providers.extra`: where the full output goes (a path or None)
DEFAULT_G = r"error|fail|traceback|assert|denied|FLAG|NEEDS|exception|fatal"


def condense(lines: list[str], head: int = 12, tail: int = 25, grep: str = DEFAULT_G, width: int = 220, maxm: int = 15) -> tuple[list[str], bool]:
    """Return (lines to print, were_cut)."""
    def cut(s: str) -> str:
        return s if len(s) <= width else s[:width] + f" …(+{len(s) - width})"

    if len(lines) <= head + tail:
        return [cut(x) for x in lines], False
    rx = re.compile(grep, re.I)
    mid = [(i, x) for i, x in enumerate(lines[head:len(lines) - tail], head) if rx.search(x)]
    out = [cut(x) for x in lines[:head]]
    skipped = len(lines) - head - tail
    if mid:
        out.append(f"--- {len(mid)} matching of {skipped} hidden lines (first {min(len(mid), maxm)}) ---")
        out += [cut(f"{i + 1}: {x}") for i, x in mid[:maxm]]
    else:
        out.append(f"--- {skipped} lines hidden, none match /{grep[:30]}/ ---")
    out += [cut(x) for x in lines[-tail:]]
    return out, True


class Limited(Model):
    """A command's output, condensed. `log` holds the whole output when something was cut."""

    lines: list[str]
    cut: bool
    code: int
    total_lines: int
    total_bytes: int
    log: str = ""

    @property
    def exit_code(self) -> int:
        return self.code

    def render(self) -> list[str]:
        note = [f"[lim: full output {self.total_lines} lines, {self.total_bytes} bytes in {self.log}; exit {self.code}]"] if self.cut else []
        return ["\n".join(self.lines), *note] if self.lines or note else [""]


def log_directory() -> Path:
    """The plugin's directory for full outputs (`DIRECTORY`), expanded; else `lim` below the project's state directory."""
    rt: Any = runtime()
    value = (rt.providers.extra or {}).get(DIRECTORY)
    return Path(str(value)).expanduser() if value is not None else Path(rt.project.home) / "lim"


@native("lim", writes=True, help=H(
    "Run a command and print only what matters, so long output does not land in an agent's context.",
    "Use before any command whose output may be long (test runs, logs, listings).",
    "writes the full output of the command to a log file when it cut something; the command itself does what it does.",
    "sm lim -n 12 -t 25 -- pytest tests/unit",
    "grep -n ERROR LOG   (the log path is on the last line)",
    "When the output is longer than head+tail lines, the full output is kept in a log (default: a new file in the project's\n"
    "lim directory, `sm self config`; -o FILE overrides) and it prints the first -n lines, up to -m lines of the middle that match -g (default: error, fail, traceback, assert,\n"
    "denied, FLAG, NEEDS, exception, fatal), the last -t lines, and the log path with its line and byte counts. Short output passes through\n"
    "unchanged and writes no log. Lines are cut at -w characters. The exit code is the command's. -s runs a shell pipeline instead of COMMAND."))
def lim_command(command: list[str] = typer.Argument(None, metavar="COMMAND...", help="The command and its arguments, after `--`."),
                head: int = typer.Option(12, "-n", help="Head lines."),
                tail: int = typer.Option(25, "-t", help="Tail lines."),
                grep: str = typer.Option(DEFAULT_G, "-g", metavar="REGEX", help="Lines to keep from the middle."),
                max_matches: int = typer.Option(15, "-m", help="At most this many matching middle lines."),
                width: int = typer.Option(220, "-w", help="Maximum line width."),
                output: Path | None = typer.Option(None, "-o", metavar="FILE", help="Log file for the full output."),
                shell: str | None = typer.Option(None, "-s", metavar="PIPELINE", help="A shell command string instead of COMMAND.")) -> Limited:
    rt: Any = runtime()
    argv = ["/bin/sh", "-c", shell] if shell else list(command or [])
    if not argv:
        raise Refuse("give -s 'cmd' or -- COMMAND ARGS", "sm lim --help", code=2)
    done = rt.providers.commands.run(argv)
    text = done.out + (done.err if done.err else "")
    lines = text.splitlines()
    out, was_cut = condense(lines, head, tail, grep, width, max_matches)
    log = ""
    if was_cut:
        now = datetime.fromtimestamp(rt.providers.clock.now())
        target = output or log_directory() / f"{now.strftime('%H%M%S')}-{os.getpid()}.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        log = str(target)
    return Limited(lines=out, cut=was_cut, code=done.rc, total_lines=len(lines), total_bytes=len(text), log=log)
