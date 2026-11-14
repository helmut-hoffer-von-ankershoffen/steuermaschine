"""The release-window lock: from a release's `sm release admit prepare` to its `sm release admit apply`, hosted
validations of heads wait, so their runs do not stand in the way of the cut (prepare and apply refuse while an
uncovered Actions run is active).

One record in the store (kind `heinzel.release-window`): which step took it, for which packet, by which process, and
until when. Prepare takes it and leaves it in place for the review that follows (`release_window_minutes`, default 60);
apply takes it again for its own run and gives it back when the registry write succeeded. A record past its time is
no lock. `sm head validate` reads it (exit 75 while it holds); nothing else here writes outside this machine.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from steuermaschine.models import Model

KIND, KEY = "heinzel.release-window", "lock"
DEFAULT_MINUTES = 60.0


class ReleaseWindowHold(Model):
    """Who holds the release window, and until when (epoch seconds)."""

    step: str  # prepare | apply
    packet: str
    pid: int
    host: str
    since: float
    until: float

    def describe(self) -> str:
        return (f"a release {self.step} holds the release window ({self.packet}, pid {self.pid} on {self.host}) until "
                f"{datetime.fromtimestamp(self.until).astimezone().strftime('%H:%M')}")


def minutes(rt: Any) -> float:
    value = rt.settings.get("release_window_minutes") if rt.settings else None
    return float(value) if value else DEFAULT_MINUTES


def holder(rt: Any) -> ReleaseWindowHold | None:
    """The hold in force now, or None (none recorded, or past its time)."""
    data = rt.providers.store.load(KIND, KEY)
    if not data:
        return None
    hold = ReleaseWindowHold.model_validate(data)
    return hold if hold.until > rt.providers.clock.now() else None


def take(rt: Any, step: str, packet: str) -> ReleaseWindowHold:
    """Take (or renew) the release window for `release_window_minutes` from now."""
    now = rt.providers.clock.now()
    hold = ReleaseWindowHold(step=step, packet=packet, pid=os.getpid(), host=socket.gethostname(), since=now, until=now + minutes(rt) * 60)
    rt.providers.store.save(KIND, KEY, hold.model_dump())  # one document, replaced whole: the last release step wins
    return hold


def give_back(rt: Any, hold: ReleaseWindowHold, before: ReleaseWindowHold | None = None) -> None:
    """Give back only this step's own hold: a record someone else wrote since stays. The hold this step replaced, when
    it was another packet's and is still in force (a later prepare's), is put back instead of clearing the window."""
    if rt.providers.store.load(KIND, KEY) != hold.model_dump():
        return
    other = before is not None and before.packet != hold.packet and before.until > rt.providers.clock.now()
    rt.providers.store.save(KIND, KEY, before.model_dump() if other and before is not None else {})


@contextmanager
def held(rt: Any, step: str, packet: str, *, give_back_on_success: bool) -> Iterator[ReleaseWindowHold]:
    """The release window for the time of one step; apply gives it back when it succeeded, prepare leaves it for the
    review and the apply (until its time runs out)."""
    before = holder(rt)
    hold = take(rt, step, packet)
    yield hold
    if give_back_on_success:
        give_back(rt, hold, before)
