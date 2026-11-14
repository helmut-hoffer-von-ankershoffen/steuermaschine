"""Old state files this area still reads. Read-only: nothing here writes, and nothing else in the area opens them.

- the former list of deferred runs (one line per run: `id branch workflow name`): read until the first deferral or
  re-run here has written the list to the store; after that it is never read again;
- the merge queue's state file (`{"items": [{"pr": 12, "status": "queued"}, ...]}`): read for the order of re-runs
  until the queue area offers the same through its own function.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def deferred_runs(path: Path | None) -> list[dict[str, Any]]:
    """The rows of the former deferred list: [{"id", "branch", "workflow"}]. A missing or unreadable file is empty."""
    try:
        text = Path(path).read_text() if path else ""
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit():
            rows.append({"id": parts[0], "branch": parts[1], "workflow": parts[2] if len(parts) > 2 else ""})
    return rows


def queue_items(path: Path | None) -> list[dict[str, Any]]:
    """The entries of the merge queue's state file; a missing or unreadable file is an empty queue."""
    try:
        found = json.loads(Path(path).read_text()).get("items", []) if path else []
    except (OSError, ValueError, AttributeError):
        return []
    return found if isinstance(found, list) else []
