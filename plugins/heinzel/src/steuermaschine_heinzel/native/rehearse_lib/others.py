"""The two guards of the rehearsal that look at other areas. Each is one small function, so the area that owns the
fact can take it over with one changed line here. Neither guesses: what cannot be decided refuses.

- the merge queue holds a PR that is not merged or needs work: `queue_items` (the queue area's state file);
- a focused admission prepare or apply runs on this machine: `admission_running` (the process table).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ... import state
from ..go import GoSettings
from . import legacy

# A focused admission at work, in both spellings: the admission tool of the scripted era (its name ends in
# focused-admit or focused_admit, with the word prepare or apply), and the native commands that prepare or apply a
# registry transaction (`head prepare`, `head apply`, `head admit`, `release admit`).
FORMER_ADMISSION = re.compile(r"[a-z]+[-_]focused[-_]admit")
# Options before the command words (each with or without a value) are skipped, so no spelling of the call escapes.
NATIVE_ADMISSION = re.compile(r"(?:^|[/\s])(?:sm|steuermaschine|sm-dev)\s+(?:-\S+\s+(?:(?!head\s|release\s)[^-\s]\S*\s+)?)*"
                              r"(?:head\s+(?:prepare|apply|admit)|release\s+admit)(?:\s|$)")


def queue_file(settings: Mapping[str, Any], home: Path | None = None) -> Path:
    """Where the merge queue keeps its state: the queue area's setting when it has one, else queue.json in the
    directory of the adopted state files (legacy_state_dir), else in the project's state directory, as the queue's loop
    writes it (HOME: that directory, the calling command's project home)."""
    named = settings.get("queue_state_file")
    if named:
        return Path(str(named)).expanduser()
    root = settings.get("legacy_state_dir") or GoSettings.model_fields["legacy_state_dir"].default
    if root:
        return legacy.queue_state_file(Path(str(root)).expanduser())
    return state.path(legacy.QUEUE_STATE, under=home)  # the file the queue's loop writes; never a relative path


def queue_items(path: Path) -> list[Any]:
    """Every item of the merge queue. Raises FileNotFoundError when no queue was ever kept (nothing waits), and
    OSError, ValueError or AttributeError when the state cannot be read (the caller refuses)."""
    return legacy.queue_items(path)


def admission_running(commands: Any) -> bool:
    """Whether a focused admission prepare or apply runs on this machine. True as well when the process table cannot
    be read: unreadable refuses."""
    try:
        done = commands.run(["ps", "-ww", "-axo", "pid=,command="], timeout=10)
    except OSError:
        return True
    if done.rc:
        return True
    for row in done.out.splitlines():
        words = row.split()
        if FORMER_ADMISSION.search(row) and any(word in words for word in ("prepare", "apply")):
            return True
        if NATIVE_ADMISSION.search(row):
            return True
    return False
