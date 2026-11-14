"""Old state files the release train still reads. Read-only: nothing here writes.

- the former list of deferred runs (one line per run: `id branch workflow name`), named by the setting
  `ci_ops_legacy_deferred_file`: read by `release train resume-ci` only until `sm ci defer` has written the list to
  the store, exactly as `sm ci deferred` reads it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def deferred_rows(path: Path | None) -> list[dict[str, Any]]:
    """The rows of the former deferred list: [{"id", "branch", "workflow"}]. A missing or unreadable file is empty."""
    try:
        text = path.read_text() if path else ""
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit():
            rows.append({"id": parts[0], "branch": parts[1], "workflow": parts[2] if len(parts) > 2 else ""})
    return rows
