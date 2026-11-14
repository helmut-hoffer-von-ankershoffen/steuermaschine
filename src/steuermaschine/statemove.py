"""Moving state files to new names, in one explicit step, and back.

A plugin names its moves (`sm_state_moves`): an old path that an earlier release used and the new path this release
uses. `sm self migrate-state` (bin/promote runs it while no write step runs, before the switch) moves every one in
one pass; `--back` reverses it (bin/rollback runs it before it switches to a release that knows the old paths).
Nothing moves at run time: code of this release reads the new path only, and refuses while an old one still holds
the state (`require_migrated`).

How one move goes, so that a process of the older release that is still running keeps working:
- a file gets a second name first (a hard link: the same inode, so a lock held on it still excludes), then the old
  name is replaced by a symbolic link to the new one in one rename. At no moment is the old name missing.
- a directory is renamed and the old name becomes a symbolic link to it; writes inside it follow the link.
- old and new must be on the same file system (a rename across devices is a copy, and a lock would split): refused.
Before anything moves, every move is checked; one problem (both names hold state, another device, a link that
points elsewhere) refuses the whole pass and changes nothing."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import INTERNAL
from .engine import Refuse


@dataclass(frozen=True)
class Move:
    """One state path of an earlier release (`old`) and its name in this release (`new`); both absolute."""

    old: Path
    new: Path


class MigrationError(Refuse):
    """A move cannot run, or code of this release meets state that is not migrated (exit 70, names the command)."""

    def __init__(self, message: str):
        super().__init__(message, "sm self migrate-state --check", INTERNAL)


def _links_to(old: Path, new: Path) -> bool:
    return old.is_symlink() and Path(os.path.realpath(old)) == Path(os.path.realpath(new))


def _real(path: Path) -> bool:
    return os.path.lexists(path) and not path.is_symlink()


def pending(moves: Sequence[Move]) -> list[Move]:
    """The moves whose old path still holds the state (a real file or directory, not yet a link)."""
    return [m for m in moves if _real(m.old)]


def problems(moves: Sequence[Move], back: bool = False) -> list[str]:
    """Why a pass in this direction would refuse; empty: it can run."""
    out = []
    for m in moves:
        if back:
            if _real(m.old) and os.path.lexists(m.new):
                out.append(f"{m.old} and {m.new} both hold state")
            elif m.old.is_symlink() and not _links_to(m.old, m.new):
                out.append(f"{m.old} is a link to {os.readlink(m.old)}, not to {m.new}")
            continue
        if not _real(m.old):
            if m.old.is_symlink() and not _links_to(m.old, m.new):
                out.append(f"{m.old} is a link to {os.readlink(m.old)}, not to {m.new}")
            continue
        if os.path.lexists(m.new):
            out.append(f"{m.old} and {m.new} both hold state")
            continue
        parent = _existing_parent(m.new)
        if os.stat(m.old, follow_symlinks=False).st_dev != os.stat(parent).st_dev:
            out.append(f"{m.old} and {m.new} are on different file systems; an atomic rename is not possible")
    return out


def _existing_parent(path: Path) -> Path:
    parent = path.parent
    while not parent.exists():
        parent = parent.parent
    return parent


def _link_over(target: Path, at: Path) -> None:
    """Make `at` a symbolic link to `target` in one rename (whatever `at` was is replaced)."""
    tmp = at.with_name(f".{at.name}.sm-move-{os.getpid()}")
    tmp.unlink(missing_ok=True)
    os.symlink(target, tmp)
    os.replace(tmp, at)


def forward(moves: Sequence[Move], log: Callable[[str], None]) -> list[str]:
    """Move every old path that still holds state to its new name. Returns the lines it logged."""
    if found := problems(moves):
        raise MigrationError("; ".join(found))
    done = []
    for m in pending(moves):
        m.new.parent.mkdir(parents=True, exist_ok=True)
        if m.old.is_dir():
            os.rename(m.old, m.new)
            try:
                os.symlink(m.new, m.old)
            except FileExistsError:  # a running older process made the directory again in between: take its entries
                for entry in list(m.old.iterdir()):
                    if os.path.lexists(m.new / entry.name):
                        raise MigrationError(f"{entry} was written during the move and {m.new / entry.name} exists") from None
                    os.rename(entry, m.new / entry.name)
                m.old.rmdir()
                os.symlink(m.new, m.old)
            line = f"moved directory {m.old} -> {m.new} (the old name links to it)"
        else:
            os.link(m.old, m.new, follow_symlinks=False)
            _link_over(m.new, m.old)
            line = f"moved file {m.old} -> {m.new} (the old name links to it)"
        log(line)
        done.append(line)
    return done


def back(moves: Sequence[Move], log: Callable[[str], None]) -> list[str]:
    """Reverse `forward`: every new path goes back to its old name (for a release that knows only the old names)."""
    if found := problems(moves, back=True):
        raise MigrationError("; ".join(found))
    done = []
    for m in moves:
        if not os.path.lexists(m.new) or _real(m.old):
            continue
        if m.new.is_dir() and not m.new.is_symlink():
            if m.old.is_symlink():
                m.old.unlink()
            m.old.parent.mkdir(parents=True, exist_ok=True)
            os.rename(m.new, m.old)
            line = f"moved directory {m.new} -> {m.old}"
        else:
            m.old.parent.mkdir(parents=True, exist_ok=True)
            tmp = m.old.with_name(f".{m.old.name}.sm-move-{os.getpid()}")
            tmp.unlink(missing_ok=True)
            os.link(m.new, tmp, follow_symlinks=False)
            os.replace(tmp, m.old)
            m.new.unlink()
            line = f"moved file {m.new} -> {m.old}"
        log(line)
        done.append(line)
    return done


def require_migrated(move: Move) -> Path:
    """The new path of a move, for code of this release. Refuses (MigrationError) while the old path still holds the
    state and the new one does not exist: this release never reads or moves the old path itself."""
    if _real(move.old) and not os.path.lexists(move.new):
        raise MigrationError(f"the state at {move.old} is not migrated to {move.new} yet: run `sm self migrate-state` (bin/promote runs it)")
    if _real(move.old) and not _same_file(move.old, move.new):
        raise MigrationError(f"{move.old} and {move.new} both hold state (an older release wrote the old path after the move): "
                             "nothing is read until one is chosen by hand; `sm self migrate-state --check` lists them")
    return move.new


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False
