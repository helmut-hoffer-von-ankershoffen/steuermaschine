"""`sm doc checkpoint`, and the tracking document as the default of the core's `sm doc section`
(`steuermaschine.commands.doc`; the plugin hands `default_document` to it in plugin.py, `sm_provider_extras`). Read-only."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime

from .coord_lib.base import document


class DocsSettings(BaseModel):
    """Anchors of the tracking document (mixin of the plugin's settings table)."""

    doc_checkpoint_section: str = Field("## 5.", description="A line of the tracking document that starts with this opens the section whose first `### ` block is the latest checkpoint.")
    doc_checkpoint_end: str = Field("## 6.", description="A line that starts with this ends the checkpoint section.")
    doc_checkpoint_lines: int = Field(14, description="Lines of the latest checkpoint that are shown.")


class Checkpoint(Model):
    """The latest checkpoint of the tracking document: history, not live state."""

    found: bool
    lines: list[str] = []
    file: str = ""

    def render(self) -> list[str]:
        return ["Latest checkpoint (historical; reconcile with `sm go` before acting):", *self.lines] if self.found else self.lines


@native("doc checkpoint", writes=False, replaces=("hz-start",), help=H(
    "The latest checkpoint written to the tracking document: where the last session stopped.",
    "Use at a cold start or after a context reset, before `sm go`.",
    "nothing (read-only).",
    "sm doc checkpoint",
    "sm go   (a checkpoint is history; the live state decides)",
    "The checkpoint is the first `### ` block of the section named by the setting doc_checkpoint_section, up to the\n"
    "line named by doc_checkpoint_end; its first lines are shown (setting doc_checkpoint_lines)."))
def doc_checkpoint() -> Checkpoint:
    settings = runtime().settings or {}
    path = document(settings)
    fields = DocsSettings.model_fields
    start, end, count = (settings.get(name) or fields[name].default for name in ("doc_checkpoint_section", "doc_checkpoint_end", "doc_checkpoint_lines"))
    try:
        text = path.read_text()
    except (OSError, UnicodeError):
        return Checkpoint(found=False, lines=["Checkpoint unavailable: the tracking document cannot be read. Reconcile live state with `sm go`."], file=str(path))
    section = ("\n" + text).partition(f"\n{start}")[2].partition(f"\n{end}")[0]
    checkpoints = re.split(r"^### ", section, flags=re.MULTILINE)
    if len(checkpoints) < 2:
        return Checkpoint(found=False, lines=["No dated checkpoint found. Reconcile live state with `sm go`."], file=str(path))
    return Checkpoint(found=True, lines=checkpoints[1].strip().splitlines()[:int(count)], file=str(path))


def default_document(settings) -> Path:
    """The tracking document: the default FILE of `sm doc section`. Refuses (usage) when it is not configured."""
    return document(settings)
