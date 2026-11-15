"""`sm alarm asks`: record the one combined alarm for open beads that ask for a human look (rules in `alarm_lib/asks.py`)."""

from __future__ import annotations

from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime

from .alarm_lib import asks
from .alarm_lib.asks import AskAlarmSettings

__all__ = ["AskAlarmSettings"]

UNIT = ("alarm_asks.py", "alarm_lib", "queue_lib/stall.py", "coord_lib")


class AskAlarm(Model):
    holds: bool
    text: str = ""

    def render(self) -> list[str]:
        return [f"ALERT {self.text} [key {asks.KEY}]" if self.holds else "no open ask bead"]


@native("alarm asks", writes=True, files=UNIT, help=H(
    "Record one alarm for the open beads labelled owner-ask or heinzel-issues, so bare `sm` shows it and `sm alarm ack` works.",
    "Use after bead asks or in-app reports arrive; the queue daemon runs it on every tick.",
    "writes the one recorded alarm asks:open to the alarm state file (and drops an acknowledgement when beads are new since it).",
    "sm alarm asks", "sm alarm ack asks:open   (the ack is the baseline: only beads created after it raise the alarm again)",
    "The labels are the setting alarm_ask_labels. One alarm, not one per bead: `N open ... beads, M new since the last ack`."))
def alarm_asks() -> AskAlarm:
    rt = runtime()
    labels = list(rt.settings.get("alarm_ask_labels") or AskAlarmSettings.model_fields["alarm_ask_labels"].default)
    try:
        found = asks.record(rt.providers.commands, rt.providers.store, rt.settings, labels, rt.providers.clock.now())
    except RuntimeError as error:
        raise Refuse(f"the tracker cannot be read: {error}", "sm self config   (settings coord_tracker_dir, coord_document)") from None
    return AskAlarm(holds=found is not None, text=found["text"] if found else "")
