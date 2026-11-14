"""`sm doc section`: read a Markdown file by section (the reader is `steuermaschine.docsection`). Read-only.

A plugin may name the project's default document through `sm_provider_extras` under the key `DOCUMENT`: a callable
that returns its path (or raises `Refuse` when the project does not configure it). Without one FILE is required.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from .. import USAGE, docsection
from ..engine import Refuse
from ..helpdoc import H
from ..models import Model
from ..native import Group, native, runtime

DOCUMENT = "doc.document"  # key in `Providers.extra`: () -> Path of the project's default document


class Section(Model):
    """Lines of a Markdown file: its table of contents, sections or matches."""

    file: str
    lines: list[str]

    def render(self) -> list[str]:
        return self.lines


DOC = Group("doc", H(
    "doc: read Markdown files by section.",
    "Use before reading a long document: take the table of contents first, then one section.",
    "nothing (read-only).",
    "sm doc section FILE --toc",
    "sm doc section FILE --sec NUMBER"))


def default_document() -> Callable[[], Path] | None:
    rt: Any = runtime()
    found: Callable[[], Path] | None = (rt.providers.extra or {}).get(DOCUMENT)
    return found


def resolve(file: Path | None) -> Path:
    """FILE as given; a relative FILE that does not exist here is looked up next to the default document."""
    default = default_document()
    if file is None:
        if default is None:
            raise Refuse("give FILE: this project names no default document", "sm doc section FILE --toc", code=USAGE)
        return default()
    path = file.expanduser()
    if not path.is_absolute() and not path.exists() and default is not None:
        try:
            return default().parent / file
        except Refuse:  # the project does not configure its default document: FILE stays as given
            return path
    return path


@native("doc section", writes=False, help=H(
    "Read a Markdown file by section, not by line range: table of contents, one section, or matching lines.",
    "Use before reading the tracking document or a research file: take the table of contents first, then one section.",
    "nothing (read-only).",
    'sm doc section --sec 6            (another file: sm doc section research/log/2026-11-12.md --grep "candidate")',
    "sm doc section --sec NUMBER   (the section you need)",
    "FILE defaults to the project's default document (when its plugin names one); a relative FILE is looked up in the current\n"
    "directory, then next to that document. Without --sec and --grep it prints the headings with line numbers and section sizes.\n"
    "--sec KEY prints the section whose heading starts with `KEY.` or `KEY ` or contains KEY, down to the next heading\n"
    "of the same or a higher level. --grep REGEX prints matching lines with line numbers and the heading above them.\n"
    "Every mode stops at --max-lines and says what was cut."))
def doc_section(file: Path | None = typer.Argument(None, metavar="[FILE]", help="A Markdown file; default: the project's default document."),
                toc: bool = typer.Option(False, "--toc", help="The headings with line numbers and section sizes."),
                level: int = typer.Option(3, "--level", metavar="N", help="Deepest heading level in the table of contents."),
                sec: list[str] | None = typer.Option(None, "--sec", metavar="KEY", help="A section, by number or by text of its heading; may be repeated."),
                grep: str | None = typer.Option(None, "--grep", metavar="REGEX", help="Lines that match, case-insensitive."),
                context: int = typer.Option(0, "-C", "--context", metavar="N", help="Lines of context around each match."),
                matches: int = typer.Option(20, "-m", "--matches", metavar="N", help="Stop after this many matches."),
                max_lines: int = typer.Option(120, "--max-lines", metavar="N", help="Stop after this many lines.")) -> Section:
    path = resolve(file)
    try:
        lines = path.read_text(errors="replace").split("\n")
        return Section(file=str(path), lines=docsection.read(lines, str(path), toc, level, sec or [], grep, context, matches, max_lines))
    except OSError as exc:
        raise Refuse(f"cannot read {path}: {exc.strerror or exc}", "sm doc section FILE --toc", code=USAGE) from None
    except re.error as exc:
        raise Refuse(f"--grep is not a regular expression: {exc}", "sm doc section --grep REGEX", code=USAGE) from None
