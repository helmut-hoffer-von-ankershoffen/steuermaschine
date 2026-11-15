"""One combined alarm for the beads that ask for a human look: open beads with an ask label (setting `alarm_ask_labels`;
`owner-ask` for the owner's asks, `heinzel-issues` for in-app reports).

It is RECORDED (`queue_lib.stall.process`, source "asks"), because bare `sm` shows only recorded alarms and `sm alarm ack`
accepts only recorded keys. One alarm, never one per bead: "N open owner-ask/heinzel-issues beads, M new since the last
ack". The ack (`sm alarm ack asks:open`) stores its time: that is the baseline. Only beads created after it count as new;
when there are some, the ack is dropped and the alarm holds again. Without an ack every open ask bead is new.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from ..coord_lib.base import child_env, tracker_dir
from .rules import alarm, stamp

KEY = "asks:open"


class AskAlarmSettings(BaseModel):
    """Settings of the ask alarm, a mixin of the plugin's table."""

    alarm_ask_labels: list[str] = Field(["owner-ask", "heinzel-issues"], description="Bead labels whose open beads raise the one combined ask alarm "
                                        "(the owner's asks, in-app reports).")


def read_beads(commands: Any, settings: Any, labels: list[str]) -> list[dict[str, Any]]:
    """The open beads with any of `labels`, from the tracker. Raises RuntimeError when it cannot be read."""
    done = commands.run(["bd", "list", "--status", "open", "--label-any", ",".join(labels), "--json", "--limit", "0"],
                        cwd=str(tracker_dir(settings)), env=child_env(settings), timeout=60)
    if done.rc:
        raise RuntimeError(f"bd exited {done.rc}")
    try:
        found = json.loads(done.out[done.out.index("["):]) if "[" in done.out else []
    except ValueError:
        raise RuntimeError("bd list is not JSON") from None
    return [b for b in found if isinstance(b, dict)] if isinstance(found, list) else []


def counts(beads: list[dict[str, Any]], acked_at: float | None) -> tuple[int, int]:
    """(open ask beads, those created after the ack; all of them without an ack)."""
    if acked_at is None:
        return len(beads), len(beads)
    return len(beads), sum(1 for b in beads if (stamp(b.get("created_at")) or 0) > acked_at)


def build(labels: list[str], total: int, new: int) -> dict[str, Any] | None:
    if not total:
        return None
    return alarm("ALERT", KEY, f"{total} open {'/'.join(labels)} beads, {new} new since the last ack; sm beads tasks ls all")


def record(commands: Any, store: Any, settings: Any, labels: list[str], now: float | None = None) -> dict[str, Any] | None:
    """Read the tracker and record the one alarm (cleared when no ask bead is open). Returns the alarm, or None."""
    from ..queue_lib import stall

    beads = read_beads(commands, settings, labels)
    acks = store.load("alarms", "acks") or {}
    acked_at = acks.get(KEY)
    total, new = counts(beads, float(acked_at) if isinstance(acked_at, int | float) else None)
    if acked_at is not None and new:
        with store.lock("alarms.acks"):  # something new since the ack: the alarm holds again
            data = store.load("alarms", "acks") or {}
            data.pop(KEY, None)
            store.save("alarms", "acks", data)
    found = build(labels, total, new)
    stall.process([found] if found else [], "asks", now, complete=True)
    return found
