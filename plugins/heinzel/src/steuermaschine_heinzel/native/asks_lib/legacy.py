"""State of the scripted era that the asks adopt, read-only: one function per file."""

from __future__ import annotations

import json
from pathlib import Path


def watermark(state_dir: Path) -> str:
    """The timestamp through which the former audit was acknowledged (`hz-ask-watermark.json`, key `ts`), or ''."""
    try:
        return str(json.loads((state_dir / "ask-watermark.json").read_text()).get("ts", "") or "")
    except (OSError, ValueError, AttributeError):
        return ""
