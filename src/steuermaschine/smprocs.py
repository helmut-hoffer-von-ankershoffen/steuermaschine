"""The process shapes of steuermaschine itself, for the load guard and the governor: which processes in `ps` output are
`sm` (and what it runs), so the load guard never pauses a test tree that holds the run lock and the governor never stops
sm's own daemons.

Why not a regex on `sm gate run`: the program is never seen like that. The stable launcher (`bin/sm`) and the console
scripts exec the venv's Python, so a real process reads `/…/venv/bin/python /…/venv/bin/steuermaschine gate run -- …`
(or `…/bin/sm …`, or `/bin/sh /…/bin/sm …` for the instant before the launcher execs). The shapes are taken from the live
process table by a test of the project plugin that uses them, which starts a real `sm gate run` and a real
`sm daemon … --loop 1` and reads `ps`.

What counts as an sm process (`Shapes.argv_of`): the first word, or the script word after an interpreter (python*, sh,
bash, zsh, with its options; `python -m steuermaschine`), is an entry (basename `steuermaschine` or `sm`); or that word
lies below an install root (the stable install: `<root>/releases/<id>/venv/...`, `<root>/current/venv/...`).

Fail closed: `establish` raises `Unestablished` when no install root is known (no setting, and the running program is
not inside a venv). The callers then pause nothing and stop nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

ENTRIES = ("steuermaschine", "sm")
INTERPRETER = re.compile(r"^(python[0-9.]*|sh|bash|zsh|dash)$")
VALUE_OPTIONS = {"--format"}  # root options of sm that take a separate value
PY_VALUE_OPTIONS = {"-W", "-X", "-Q"}  # interpreter options that take a separate value


class Unestablished(Exception):
    """The pattern set cannot be established; nothing may be paused or stopped."""


def base(word: str) -> str:
    return word.rstrip("/").rsplit("/", 1)[-1]


def entry(word: str) -> bool:
    """An sm entry word: a path or name `steuermaschine` / `sm`, or a module `steuermaschine[.x]` after -m."""
    return base(word) in ENTRIES or word.split(".")[0] in ENTRIES


@dataclass(frozen=True)
class Shapes:
    roots: tuple[str, ...] = ()

    def under_root(self, word: str) -> bool:
        return any(word.startswith(root.rstrip("/") + "/") for root in self.roots)

    def program(self, cmd: str) -> tuple[list[str], int] | None:
        """(words, index of the program word) for an sm process, else None. The program word is the entry itself, or the
        script an interpreter runs."""
        words = cmd.split()
        if not words:
            return None
        if entry(words[0]):
            return words, 0
        script = self.script(words) if INTERPRETER.match(base(words[0])) else None
        if script is not None and (entry(words[script]) or self.under_root(words[script])):
            return words, script
        if self.under_root(words[0]):  # anything the install's own interpreter or scripts run is sm
            return words, script if script is not None else 0
        return None

    @staticmethod
    def script(words: list[str]) -> int | None:
        """The index of the script (or of the module after -m) an interpreter runs; None for -c or none at all."""
        i = 1
        while i < len(words) and words[i].startswith("-"):
            if words[i] == "-m":
                return i + 1 if i + 1 < len(words) else None
            if words[i] == "-c":
                return None
            i += 2 if words[i] in PY_VALUE_OPTIONS else 1
        return i if i < len(words) else None

    def is_sm(self, cmd: str) -> bool:
        return self.program(cmd) is not None

    def verbs(self, cmd: str) -> list[str] | None:
        """The command path words after the program (root options skipped), or None for a process that is not sm."""
        found = self.program(cmd)
        if found is None:
            return None
        words, i = found
        rest, j = words[i + 1:], 0
        while j < len(rest) and rest[j].startswith("-") and rest[j] != "--":  # root options (`--format json`) come first
            j += 2 if rest[j] in VALUE_OPTIONS else 1
        out = []
        while j < len(rest) and not rest[j].startswith("-"):  # the command path ends at its first option
            out.append(rest[j])
            j += 1
        return out

    def runs(self, cmd: str, paths: Iterable[tuple[str, ...]]) -> bool:
        """An sm process whose command path starts with one of `paths` (("gate", "run"), ("release",), ...)."""
        verbs = self.verbs(cmd)
        return verbs is not None and any(tuple(verbs[:len(p)]) == tuple(p) for p in paths)


ENTRY_SHAPES = Shapes()  # the entries alone; what the library functions use when the caller names no install root


def install_root(program: str) -> Path | None:
    """The install root of a program inside a venv: `<root>/venv/bin/x` or `<root>/.venv/bin/x`, and for the stable layout
    (`<stable>/releases/<id>/venv`, `<stable>/current/venv`) the stable directory itself. None outside a venv."""
    p = Path(program)
    if p.parent.name != "bin" or p.parent.parent.name not in ("venv", ".venv"):
        return None
    root = p.parent.parent.parent
    if root.name == "current":
        return root.parent
    if root.parent.name == "releases":
        return root.parent.parent
    return root


def establish(configured: Iterable[str | Path], running: str) -> Shapes:
    """The shapes with the install roots of the setting, or else the one of the running program (`sys.executable`).
    Raises Unestablished when there is none, or a configured one does not exist."""
    roots = [Path(str(r)).expanduser() for r in configured]
    if not roots:
        derived = install_root(running)
        roots = [derived] if derived is not None else []
    if not roots:
        raise Unestablished(f"no install root of sm: none is configured and {running or 'the running program'} is not in a venv")
    missing = [str(r) for r in roots if not r.is_dir()]
    if missing:
        raise Unestablished(f"install root(s) of sm not found: {', '.join(missing)}")
    names: list[str] = []
    for r in roots:
        for form in (str(r.absolute()), str(r.resolve())):
            if form not in names:
                names.append(form)
    return Shapes(tuple(names))
