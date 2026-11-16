"""The edges of the moved release code: its records, its checkouts, how a process starts, and the commands that used
to be scripts of their own (the run lock, shipping a PR). Nothing here holds release logic and nothing here starts a
script of the old tool set. The queue's edge module is the base; this one adds what only the release code needs."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from ... import environment  # moved: the process context is a typed model
from ..queue_lib import edge as _base
from ..queue_lib.edge import PACKAGE, configure, fresh, module_of, moved, note, records_dir, setting  # noqa: F401 - the moved code's edge
from ..queue_lib.edge import run as _run
from . import moved_map

_base.MAPS.setdefault("release_lib", moved_map)  # the moved code loads a moved tool by its old file name

DEFAULT_RELEASE_DIR = "/private/tmp/heinzel-air-release"  # where the release PR has always been prepared
_conf: dict[str, Path] = {}


def configure_release(release_dir: Path | str | None = None, core_checkout: Path | str | None = None) -> None:
    """Set the release checkout (setting `release_dir`) and the read-only core checkout (`release_core_checkout`).
    A native command calls this before it loads a moved module."""
    _conf.clear()
    for key, value in (("release_dir", release_dir), ("core_checkout", core_checkout)):
        if value:
            _conf[key] = Path(value).expanduser()


def release_dir() -> str:
    """The checkout the release PR is prepared in. A native command sets it from its settings; a moved function
    called directly keeps the old default and the old variable its tests set."""
    if "release_dir" in _conf:
        return str(_conf["release_dir"])
    return str(setting("release_dir") or environment.state_overrides().release_dir or DEFAULT_RELEASE_DIR)


def core_checkout() -> str:
    """A checkout of the core repository that is only read (`git show`, `git log`, `git diff`): the setting
    release_core_checkout; empty when unset (no machine path as a default; the commands that need it refuse first)."""
    return str(_conf.get("core_checkout") or "")




def sm() -> list[str]:
    """This program, as a child process starts it."""
    here = Path(sys.argv[0])
    if here.name in ("sm", "steuermaschine") and here.exists():
        return [str(here)]
    return [shutil.which("sm") or "sm"]


def gate() -> list[str]:
    """The run lock in front of a heavy local command or a push: `sm gate run -- COMMAND...`."""
    return [*sm(), "gate", "run", "--"]


def ship(pr: int | str) -> list[str]:
    """Guard a PR's diff and queue it: `sm head ship PR`."""
    return [*sm(), "head", "ship", str(pr)]


def run(argv, *args, **kwargs):
    return _run(argv, *args, **kwargs)
