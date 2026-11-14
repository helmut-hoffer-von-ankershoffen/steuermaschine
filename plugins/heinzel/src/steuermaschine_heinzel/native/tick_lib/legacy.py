"""Marker files of the scripted era, adopted READ-ONLY, so the first native tick does not report again what was
acknowledged the old way. One function per file; `root` is the setting `legacy_state_dir`. Nothing here writes.
"""

from __future__ import annotations

from pathlib import Path


def crash_ack(root: Path) -> float:
    """hz-crash-ack.ts: its modification time is when crash reports were last acknowledged. Missing: never (0)."""
    try:
        return (root / "crash-ack.ts").stat().st_mtime
    except OSError:
        return 0.0


def venv_ack(root: Path) -> Path:
    """hz-uv-ack.ts: its modification time is when the live tool environment was last acknowledged."""
    return root / "uv-ack.ts"


def venv_allowed(root: Path) -> Path:
    """hz-live-uv.log: the PATH shim appends a line for every allowed change of the live tool environment."""
    return root / "live-uv.log"
