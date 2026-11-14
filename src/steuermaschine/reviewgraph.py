"""What the digest of a review unit reaches, and which verdicts a change voids. Read-only.

    reach(units)                  one `Reach` per write path: the unit's own files and the first-party files it imports
    fan_in(reaches)               file -> the review files whose digest covers it
    kernel(reaches)               the files in every digest: a change to one of them voids every verdict
    voided_by(reaches, changed)   the review files a change to these files voids
    reached_by(reaches, changed)  write path -> the changed files its code covers (`sm self reviews`)
    parsed_once()                 while open, the digest walk parses each file once (for a listing of every unit)

The digest itself stays `native.NativeCommand.code_digest`; this module answers the questions around it with the same
import walk (`native._imports_of`), parsing every file once for all units instead of once per unit. Files are named
by package path (`steuermaschine/engine.py`), the same in a checkout and in an install.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import native

_Cache = dict[tuple[Path, frozenset[str]], list[Path]]


@dataclass(frozen=True)
class Reach:
    path: str  # the command path of the write: "queue add"
    review: str  # its review file name: "queue-add.md"
    own: tuple[str, ...]  # the declared unit: the module and its `<module>_lib`, or `files=`
    imported: tuple[str, ...]  # every first-party file the unit imports, transitively, outside the unit

    @property
    def files(self) -> tuple[str, ...]:
        """Every file the digest covers."""
        return (*self.own, *self.imported)


def reach(units: Iterable[Any]) -> list[Reach]:
    """One `Reach` per unit that has code (a `NativeCommand`, or what `native.unit` returns), in the given order."""
    cache: _Cache = {}
    out: list[Reach] = []
    for cmd in units:
        own = [Path(p).resolve() for p in cmd.code_files()]
        if not own:
            continue
        imported = _closure(own, native._roots(cmd.fn.__module__), cache)
        out.append(Reach(cmd.path, cmd.review_file, tuple(_name(p) for p in own), tuple(sorted(_name(p) for p in imported))))
    return out


def _closure(own: list[Path], roots: dict[str, Path], cache: _Cache) -> list[Path]:
    """The files `own` imports, transitively, that are not in it: the walk of `native._imported` with a cache."""
    tops = frozenset(roots)
    seen = set(own)
    queue = [p for p in own if p.suffix == ".py"]
    extra: list[Path] = []
    while queue:
        key = (queue.pop(), tops)
        if key not in cache:
            cache[key] = native._imports_of(key[0], roots)
        for hit in cache[key]:
            if hit not in seen:
                seen.add(hit)
                extra.append(hit)
                queue.append(hit)
    return extra


def _name(path: Path) -> str:
    return Path(native._package_path(path)).as_posix()


def fan_in(reaches: Iterable[Reach]) -> dict[str, list[str]]:
    """File -> the review files whose digest covers it (sorted), the file in the most digests first."""
    table: dict[str, set[str]] = {}
    for unit in reaches:
        for name in unit.files:
            table.setdefault(name, set()).add(unit.review)
    return {name: sorted(table[name]) for name in sorted(table, key=lambda n: (-len(table[n]), n))}


def kernel(reaches: Iterable[Reach]) -> list[str]:
    """The files every unit's digest covers. Empty without units."""
    units = list(reaches)
    if not units:
        return []
    return sorted(set.intersection(*(set(unit.files) for unit in units)))


def voided_by(reaches: Iterable[Reach], changed: Iterable[str]) -> list[str]:
    """The review files whose verdict a change to `changed` voids. A changed file is a package path or any path that
    ends with one (`src/steuermaschine/engine.py`, as `git diff --name-only` prints it)."""
    names = [Path(c).as_posix() for c in changed]
    hit = {unit.review for unit in reaches if any(c == f or c.endswith("/" + f) for f in unit.files for c in names)}
    return sorted(hit)


def reached_by(reaches: Iterable[Reach], changed: Iterable[str]) -> dict[str, list[str]]:
    """Write path -> the files of `changed` its code covers, for every write path the change reaches, in the order of
    `reaches`. A changed file matches as in `voided_by`: a package path, or a path that ends with one."""
    names = [Path(c).as_posix() for c in changed]
    out: dict[str, list[str]] = {}
    for unit in reaches:
        hit = sorted({c for c in names for f in unit.files if c == f or c.endswith("/" + f)})
        if hit:
            out[unit.path] = hit
    return out


@contextlib.contextmanager
def parsed_once() -> Iterator[None]:
    """While open, `native._imports_of` answers from memory after the first parse of a file, so the digests of many
    units cost one parse per file instead of one per unit and call (`sm self reviews`: 103 s without, 3 s with, on 77
    write paths). The digest and its files are the same; only for a listing: the files must not change while it is open."""
    real, cache = native._imports_of, _Cache()

    def cached(path: Path, roots: dict[str, Path]) -> list[Path]:
        key = (path, frozenset(roots))
        if key not in cache:
            cache[key] = real(path, roots)
        return cache[key]

    native._imports_of = cached
    try:
        yield
    finally:
        native._imports_of = real
