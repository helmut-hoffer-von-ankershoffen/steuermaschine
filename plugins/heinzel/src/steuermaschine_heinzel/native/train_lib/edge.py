"""The edges of the moved release train: this program as a child process, the run lock, notes, the owner
notification, a command line of the batch view. Nothing here holds train logic and nothing here starts a script of
the old tool set."""

from __future__ import annotations

import shlex
from typing import Any

from ..queue_lib import edge as _base
from ..queue_lib.edge import note, records_dir, run  # noqa: F401 - the moved code's edge
from ..release_lib.edge import sm
from . import moved_map

_base.MAPS.setdefault("train_lib", moved_map)  # a test asks for the moved train by its old file name

DIGEST = "sm go --view tick"  # the tick digest the batch view scans for blockers


def gate() -> list[str]:
    """The run lock in front of the train's own heavy command (the version probe in the release checkout): a release
    goes ahead of builders and past a hold, as options of `sm gate run` (the lock reads no environment)."""
    return [*sm(), "gate", "run", "--priority", "2", "--hold-exempt", "--"]


def train_run(version: str) -> list[str]:
    """The detached runner of one release: `sm daemon train --run X.Y.Z`."""
    return [*sm(), "daemon", "train", "--run", version]


def argv(command: str) -> list[str]:
    """A command line of the batch view as an argument list; a leading `sm` is this program."""
    words = shlex.split(command)
    return [*sm(), *words[1:]] if words[:1] == ["sm"] else words


def digest(sh) -> str:
    """The tick digest through the view's own runner `sh`. Nothing at all (the view is not native yet, or it failed)
    is an UNKNOWN blocker line, so the batch view never says `blockers: none` without having read them."""
    text = sh(DIGEST, timeout=180)
    if text.strip() and not text.lstrip().startswith(("Usage:", "Error")):
        return text
    return f"UNKNOWN: the tick digest could not be read (`{DIGEST}` gave {'nothing' if not text.strip() else 'an error'})"


def notify_owner(text: str) -> bool:
    """Tell the owner through the notifier provider. False outside a native command or when the provider fails."""
    from steuermaschine.native import runtime

    try:
        runtime().providers.notifier.notify("info", "release-train", text)
    except Exception:  # noqa: BLE001 - as before: a missing or failing notifier is noted, it never stops the train
        return False
    return True


def setting(name: str) -> Any:
    """A value of the plugin's settings for the moved train (see queue_lib.edge.setting)."""
    return _base.setting(name)
