"""What the release route needs from the former shared helper module: the repository identity and PR key shape of the
PR admission (imported), and the merge freeze test (`release_holds`, `release_holds_present`, `incident_frozen`,
`merge_frozen`, moved from the operator copy's `hzlib.py`; tests/admission_verbatim.py checks the four blocks).
The moved code refers to this module as `hzlib`.
"""
from __future__ import annotations

import stat
from pathlib import Path

from ... import state as statedir  # the plugin's state paths
from ..admission_lib.identity import CORE, OWNER, PLUGINS, SHORT, split_key  # noqa: F401


def release_holds() -> Path:
    """Each release owns a unique hold name; no release deletes a shared incident sentinel."""
    return statedir.path("release-holds")


def release_holds_present() -> bool:
    """Unknown, unreadable or malformed hold directories block merging just like an owned hold."""
    root = release_holds()
    try:
        mode = root.lstat().st_mode
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if not stat.S_ISDIR(mode):
        return True
    try:
        return any(root.iterdir())
    except OSError:
        return True


def incident_frozen(legacy: Path | None = None) -> bool:
    """Honor the shared sentinel, including dangling links and unreadable state."""
    try:
        (legacy or statedir.path("merge-freeze")).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def merge_frozen(legacy: Path | None = None) -> bool:
    """Honor the shared incident hold and every release-owned hold."""
    return incident_frozen(legacy) or release_holds_present()
