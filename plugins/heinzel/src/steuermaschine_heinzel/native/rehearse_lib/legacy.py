"""State files of the scripted era that the rehearsal still reads, READ-ONLY. One function per file. Nothing here writes.

The rehearsal's own former state file is not adopted: the rehearsal keeps its state in the store.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

QUEUE_STATE = "queue.json"  # the merge queue's state file, as its loop writes it


def queue_state_file(root: Path) -> Path:
    return root / QUEUE_STATE


def queue_items(path: Path) -> list[Any]:
    """The items of the merge queue's state file. Raises FileNotFoundError when there is no such file (no queue was
    ever kept here), and OSError, ValueError or AttributeError when it cannot be read: the caller refuses then."""
    items = json.loads(path.read_text()).get("items", [])
    if not isinstance(items, list):
        raise ValueError("the merge queue's items are not a list")
    return items
