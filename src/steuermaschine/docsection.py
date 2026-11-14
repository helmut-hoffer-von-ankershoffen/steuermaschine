"""Read a Markdown file by section instead of by line range, so a large document costs a few hundred tokens.

    lines = docsection.read(text.split("\\n"), name, toc=True)                 headings with line numbers and section sizes
    lines = docsection.read(lines, name, sections=["6", "Tools on"])           the section whose heading starts with "6." / "6 " or contains the text
    lines = docsection.read(lines, name, grep="regex", context=1, matches=20)  matching lines with line numbers and the heading they sit under

A section is its heading and everything below it up to the next heading of the same or a higher level. Headings inside
a fenced code block do not count. Every mode stops at `max_lines` and says what was cut.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence


def headings(lines: Sequence[str]) -> list[tuple[int, int, str]]:
    out, fence = [], False
    for i, x in enumerate(lines):
        if x.startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"^(#{1,6})\s+(.*)", x)
        if m:
            out.append((i, len(m.group(1)), m.group(2).strip()))
    return out


def find_sections(lines: Sequence[str], key: str) -> list[tuple[int, int, str]]:
    hs = headings(lines)
    starts: Callable[[str], bool] = lambda t: t.startswith(key + ".") or t.startswith(key + " ") or t == key  # noqa: E731
    pick = next((k for k, h in enumerate(hs) if starts(h[2])), None)
    if pick is None:
        pick = next((k for k, h in enumerate(hs) if key.lower() in h[2].lower()), None)
    if pick is None:
        return []
    i, lvl, title = hs[pick]
    end = next((j for j, l2, _ in hs[pick + 1:] if l2 <= lvl), len(lines))
    return [(i, end, title)]


def cap(out: list[str], mx: int) -> list[str]:
    if len(out) > mx:
        return [*out[:mx], f"[{len(out) - mx} more lines cut; raise --max-lines or narrow with --sec/--grep]"]
    return out


def read(lines: Sequence[str], name: str, toc: bool = False, level: int = 3, sections: Sequence[str] = (), grep: str | None = None, context: int = 0,
         matches: int = 20, max_lines: int = 120) -> list[str]:
    """The lines to show. With neither sections nor grep: the table of contents."""
    out: list[str] = []
    hs = headings(lines)
    if toc or not (sections or grep):
        for k, (i, lvl, t) in enumerate(hs):
            if lvl > level:
                continue
            end = next((j for j, l2, _ in hs[k + 1:] if l2 <= lvl), len(lines))
            out.append(f"{i + 1:>6} {'#' * lvl} {t[:90]}  ({end - i} lines)")
        out = cap(out, max(max_lines, 200))
    for key in sections:
        found = find_sections(lines, key)
        if not found:
            out.append(f"[no heading matches {key!r}]")
        for i, end, _t in found:
            out.append(f"=== {name} lines {i + 1}-{end} ===")
            out += lines[i:end]
    if grep:
        rx, n = re.compile(grep, re.I), 0
        for i, x in enumerate(lines):
            if rx.search(x):
                n += 1
                if n > matches:
                    out.append(f"[more matches cut at -m {matches}]")
                    break
                under = next((t for j, _, t in reversed(hs) if j <= i), "")
                for c in range(max(0, i - context), min(len(lines), i + context + 1)):
                    out.append(f"{c + 1}: {lines[c][:300]}")
                out.append(f"   (under: {under[:60]})")
    return cap(out, max_lines) if not toc else out
