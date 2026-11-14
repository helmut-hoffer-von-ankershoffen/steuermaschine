"""Which write paths a change reaches. Read-only.

    reach(units)                  one `Reach` per write path: the unit's own files and the first-party files it imports
    reached_by(reaches, changed)  write path -> the changed files its code covers (`sm self reviews`)
    parsed_once()                 while open, the import walk parses each file once (for a listing of every unit)

A unit is the module of a write command with its `<module>_lib` package (`native.NativeCommand.code_files`). The walk
follows every `import` and `from ... import` in the source (also inside functions) into core (`steuermaschine`) and the
plugin's own package, and takes a `*_lib` package whole once one of its files is reached (`_closure`). No command declares its files. Files are named by package path
(`steuermaschine/engine.py`), the same in a checkout and in an install.
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
    own: tuple[str, ...]  # the unit: the module and its `<module>_lib`
    imported: tuple[str, ...]  # every first-party file the unit reaches outside itself (`_closure`)

    @property
    def files(self) -> tuple[str, ...]:
        """Every file the write path covers."""
        return (*self.own, *self.imported)


def reach(units: Iterable[Any]) -> list[Reach]:
    """One `Reach` per unit that has code (a `NativeCommand`, or what `native.unit` returns), in the given order."""
    cache: _Cache = {}
    out: list[Reach] = []
    for cmd in units:
        own = [Path(p).resolve() for p in cmd.code_files()]
        if not own:
            continue
        roots = native._roots(cmd.fn.__module__)
        imported = _closure(own, roots, cache)
        out.append(Reach(cmd.path, tuple(_name(p, roots) for p in own), tuple(sorted(_name(p, roots) for p in imported))))
    return out


def _closure(own: list[Path], roots: dict[str, Path], cache: _Cache) -> list[Path]:
    """The files `own` reaches, transitively, that are not in it. Two rules:
    - every import in the source is followed (`native._imports_of`);
    - one file of a `*_lib` package brings the whole package, data files included: such a package loads its modules
      and reads its files by name at run time, which no import shows. On the wide side on purpose: a file left out
      is a change the reviewer is not pointed to.
    Not taken: the `__init__.py` of the packages above a module outside a `*_lib`. The plugin's registry is one, and
    it imports every area, so every write path would reach the whole plugin."""
    tops = frozenset(roots)
    seen = set(own)
    queue = list(own)
    extra: list[Path] = []
    libs: set[Path] = set()
    while queue:
        path = queue.pop()
        hits = _whole_lib(path, roots, libs)
        if path.suffix == ".py":
            key = (path, tops)
            if key not in cache:
                cache[key] = native._imports_of(path, roots)
            hits += cache[key]
        for hit in hits:
            if hit not in seen:
                seen.add(hit)
                extra.append(hit)
                queue.append(hit)
    return extra


def _below(path: Path, roots: dict[str, Path]) -> list[Path]:
    """The directories from the first-party package that holds `path` down to the file's own, outermost first."""
    for root in roots.values():
        if path.is_relative_to(root):
            return [root, *(root.joinpath(*path.relative_to(root).parts[:i]) for i in range(1, len(path.relative_to(root).parts)))]
    return []


def _whole_lib(path: Path, roots: dict[str, Path], done: set[Path]) -> list[Path]:
    """Every file of the outermost `*_lib` package that holds `path`, once per package; caches and hidden files are not code."""
    lib = next((d for d in _below(path, roots)[1:] if d.name.endswith("_lib")), None)
    if lib is None or lib in done:
        return []
    done.add(lib)
    return sorted(f.resolve() for f in lib.rglob("*") if f.is_file() and f.suffix != ".pyc"
                  and not any(part.startswith(".") or part == "__pycache__" for part in f.relative_to(lib).parts))


def _name(path: Path, roots: dict[str, Path]) -> str:
    """The package path of a file (`steuermaschine/engine.py`); a data file is named below its package as well."""
    for root in roots.values():
        if path.is_relative_to(root):
            return path.relative_to(root.parent).as_posix()
    return Path(native._package_path(path)).as_posix()


def reached_by(reaches: Iterable[Reach], changed: Iterable[str]) -> dict[str, list[str]]:
    """Write path -> the files of `changed` its code covers, for every write path the change reaches, in the order of
    `reaches`. A changed file is a package path or any path that ends with one (`src/steuermaschine/engine.py`, as
    `git diff --name-only` prints it)."""
    names = [Path(c).as_posix() for c in changed]
    out: dict[str, list[str]] = {}
    for unit in reaches:
        hit = sorted({c for c in names for f in unit.files if c == f or c.endswith("/" + f)})
        if hit:
            out[unit.path] = hit
    return out


@contextlib.contextmanager
def parsed_once() -> Iterator[None]:
    """While open, `native._imports_of` answers from memory after the first parse of a file, so the reach of many
    units costs one parse per file instead of one per unit and call (`sm self reviews`: 103 s without, 3 s with, on 77
    write paths). Only for a listing: the files must not change while it is open."""
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
