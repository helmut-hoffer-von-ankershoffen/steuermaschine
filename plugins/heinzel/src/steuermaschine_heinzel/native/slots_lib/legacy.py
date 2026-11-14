"""Read-only adoption of an old state directory: the release reservations among the focused admission receipts
(`focused-admission-receipts/*.json`). A release reservation is a receipt whose `release_main` proof names the
version and the tree of a release that was reserved in the registry."""

from __future__ import annotations

import json
from pathlib import Path

MAX_RECEIPT_BYTES = 1024 * 1024


def reservations(directory: Path) -> list[tuple[str, str]]:
    """(version, tree) of every release reservation receipt in `directory`; unreadable files are skipped."""
    found: list[tuple[str, str]] = []
    try:
        files = sorted(directory.glob("*.json"))
    except OSError:
        return found
    for path in files:
        try:
            if path.stat().st_size > MAX_RECEIPT_BYTES:
                continue
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        proof = data.get("release_main") if isinstance(data, dict) else None
        if isinstance(proof, dict) and isinstance(proof.get("tree"), str) and proof["tree"]:
            found.append((str(proof.get("version", "")).removeprefix("v"), proof["tree"]))
    return found
