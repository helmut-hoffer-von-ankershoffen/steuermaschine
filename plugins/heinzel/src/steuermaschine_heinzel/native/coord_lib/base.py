"""What every coordinator module needs: the settings mixin, a setting with its default, the child environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from steuermaschine import USAGE
from steuermaschine.config import child_environment, search_path
from steuermaschine.engine import Refuse


class CoordSettings(BaseModel):
    """Settings of the coordinator tools (mixin of the plugin's settings table). No path has a default: the tracking
    document and what belongs to it are named in the configuration."""

    coord_document: Path | None = Field(None, description="The one tracking document: asks table, checkpoints, work instructions, and the log at its end. Required by what reads or writes it.")
    coord_journal_lock_file: Path | None = Field(None, description="Lock held for one journal append; none: <coord_document>.lock. Name the former note tool's lock file while that tool still runs.")
    coord_log_dir: Path | None = Field(None, description="The log: one Markdown file per day (YYYY-MM-DD.md) that `sm note` appends to and "
                                                         "`sm status --write` moves old text into; none: research/log in the tracker's repository.")
    coord_tracker_dir: Path | None = Field(None, description="The repository whose issue tracker (bd) holds the beads; none: the directory of coord_document.")
    coord_bead_prefix: str = Field("heinzel-management-", description="Prefix of a full bead id; a short id gets it prepended.")
    coord_tool_path: list[str] = Field([], description="Directories put in front of PATH for the child commands bd, gh and git. Empty: PATH as it is.")


def setting(settings: Mapping[str, Any] | None, name: str, model: type[BaseModel] = CoordSettings) -> Any:
    value = settings.get(name) if settings else None
    return model.model_fields[name].default if value is None else value


def optional_path(settings: Mapping[str, Any] | None, name: str, model: type[BaseModel] = CoordSettings) -> Path | None:
    value = setting(settings, name, model)
    return None if value is None or str(value) == "" else Path(os.path.expanduser(str(value)))


def document(settings: Mapping[str, Any] | None) -> Path:
    """The tracking document. Refuses (usage) when the configuration does not name it."""
    found = optional_path(settings, "coord_document")
    if found is None:
        raise Refuse("the tracking document is not configured: set coord_document in the plugin's settings table", "sm self config", code=USAGE)
    return found


def tracker_dir(settings: Mapping[str, Any] | None) -> Path:
    return optional_path(settings, "coord_tracker_dir") or document(settings).parent


def log_file(settings: Mapping[str, Any] | None, now: float) -> Path:
    """The log file of the day of `now` (local time): `sm note` lines and the text the size cap moved out."""
    import time

    folder = optional_path(settings, "coord_log_dir") or tracker_dir(settings) / "research" / "log"
    return folder / f"{time.strftime('%Y-%m-%d', time.localtime(now))}.md"


def journal_lock(settings: Mapping[str, Any] | None) -> Path:
    found = optional_path(settings, "coord_journal_lock_file")
    return found or document(settings).with_name(document(settings).name + ".lock")


def child_env(settings: Mapping[str, Any] | None) -> dict[str, str]:
    """The environment of bd, gh and git: this process's, with the tool directories in front of PATH."""
    front = [os.path.expanduser(str(d)) for d in setting(settings, "coord_tool_path")]
    return child_environment(PATH=os.pathsep.join([*front, search_path()]))
