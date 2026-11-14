"""The Mini's updater hold: which published Core release the installed product's automatic updates hold back.

The product's updater writes its state file (`core_held`: a newer release it does not install because the release has
no positive acceptance marker yet). sm only reads that file, never writes it: `sm go` names the hold in PHASE and
`sm mini verify` says `held: no acceptance marker` instead of a bare NOT-LIVE. No command of its own lives here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
HELD = "held: no acceptance marker"


class UpdaterHoldSettings(BaseModel):
    mini_update_state: Path | None = Field(
        None, description="The installed product's updater state file (it records core_held); read only. Unset: no hold is reported.")


def held_version(settings: dict[str, Any] | None) -> str | None:
    """The exact version the updater holds back, or None (setting unset, file missing, unreadable or no hold)."""
    path = (settings or {}).get("mini_update_state")
    if not path:
        return None
    try:
        data = json.loads(Path(str(path)).expanduser().read_text())
    except (OSError, ValueError):
        return None
    held = data.get("core_held") if isinstance(data, dict) else None
    return held if isinstance(held, str) and VERSION.fullmatch(held) else None


def held_reason(version: str, installed: str) -> str:
    """The NOT-LIVE reason when the updater holds `version` on a machine still on `installed`."""
    return (f"{HELD}: v{version} is published, the updater holds it until its acceptance.json says accepted "
            f"(installed v{installed}); sm release status")
