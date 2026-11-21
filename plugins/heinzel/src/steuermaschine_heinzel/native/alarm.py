"""alarm: stall and failure alarms. `check` reads the world now, `list` prints what is recorded. Both read only:
nothing is written and nobody is notified (the loop that owns the alarm state records and notifies)."""

from __future__ import annotations

import datetime
from typing import Any

from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native

from .alarm_lib import rules, watch
from .go import tool_rows, world
from .go_lib import legacy


class AlarmRow(Model):
    level: str  # ALERT | WARN | INFO
    key: str
    text: str
    first_seen: str = ""  # ISO 8601 with offset; empty when not recorded
    acknowledged: bool = False
    new: bool = False  # holds now and is not in the recorded state yet

    def line(self) -> str:
        shown = "ACKED" if self.acknowledged and self.level != "INFO" else self.level
        since = "new" if self.new else f"since {self.first_seen[11:16] or '?'}"
        return f"{shown} {self.text} [{since}; key {self.key}]"


class Alarms(Model):
    """Alarms, most urgent first. Exit 3 while an unacknowledged ALERT holds."""

    checked: bool  # True: the world was read now; False: only the recorded state
    alarms: list[AlarmRow] = []

    def render(self) -> list[str]:
        return [a.line() for a in self.alarms] or ["OK"]

    @property
    def exit_code(self) -> int:
        return 3 if any(a.level == "ALERT" and not a.acknowledged for a in self.alarms) else 0


def iso(epoch: Any) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(epoch)).astimezone().isoformat(timespec="seconds") if epoch else ""
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def rows(items: list[dict[str, Any]]) -> list[AlarmRow]:
    return [AlarmRow(level=i["cls"], key=i["key"], text=legacy.sweep(i.get("text") or "", tool_rows()), first_seen=iso(i.get("first")),
                     acknowledged=bool(i.get("acked")), new=bool(i.get("new"))) for i in rules.order(items)]


def recorded(w: Any) -> dict[str, Any]:
    """The recorded alarms; one acknowledged with `sm alarm ack` counts as acknowledged."""
    try:
        data = legacy.alarm_state(w.state_dir)
    except (OSError, ValueError):
        data = {}
    return {k: ({**e, "acked": True} if k in w.acks and isinstance(e, dict) else e) for k, e in data.items()}


def recorded_alarms(providers: Any, settings: Any) -> list[Any]:
    """For the hook `sm_alarms`: the recorded ALERT and WARN alarms, without any network read. This is what makes
    `sm alarm ack KEY` accept the keys `go` and `alarm check` print."""
    from steuermaschine.plugins import Alarm

    from .go import world_of

    w = world_of(providers, settings)
    return [Alarm(row.key, row.level.lower(), row.text, "") for row in rows(watch.recorded_rows(recorded(w))) if row.level in ("ALERT", "WARN")]


@native("alarm check", writes=False, replaces=("hz-alarm",), help=H(
    "Read GitHub, the queue and the release receipt now; print ALERT and WARN lines, or OK.",
    "Use at every tick; exit 3 means an unacknowledged ALERT holds.",
    "nothing (read-only; it records nothing and notifies nobody: the alarm loop does both).",
    "sm alarm check", "act on each ALERT, then sm alarm ack KEY",
    "ALERT: a queued PR green and unmerged over 15 min (names the cause); main's latest CI run failed (names the jobs); a\n"
    "release candidate run with a failed or cancelled job; a release paused or stuck over 30 min.\n"
    "WARN: admission blocked over 20 min by an uncovered run; focused registry slots under 4,000 B free, or one slot over 85 % (the registry only grows); a check that\n"
    "could not be read (`ALARM CHECK ... unavailable`). Alarms another source recorded are listed too."))
def alarm_check() -> Alarms:
    w = world()
    now = w.now()
    found = watch.merged(watch.collect(w, now), recorded(w), now)
    return Alarms(checked=True, alarms=rows([{**i, "acked": i.get("acked") or i["key"] in w.acks} for i in found]))


@native("alarm list", writes=False, replaces=("hz-alarm",), help=H(
    "The recorded alarms, without any network read.",
    "Use when you want the alarm keys, or to see what is acknowledged.",
    "nothing (read-only, local).", "sm alarm list", "sm alarm ack KEY, or sm alarm check"))
def alarm_list() -> Alarms:
    return Alarms(checked=False, alarms=rows(watch.recorded_rows(recorded(world()))))
