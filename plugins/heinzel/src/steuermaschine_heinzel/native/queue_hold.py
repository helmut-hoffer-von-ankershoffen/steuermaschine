"""The queue hold: `sm queue hold --reason TEXT` and `sm queue unhold`.

A freeze of the whole merge queue for a critical head (a release-blocking fix): while it holds, `sm daemon queue`
runs no tick (nothing merges, in any repository), and `sm head pr --live` and `sm head ship` refuse with the reason.
It is one record in the store of the project's state directory (kind `heinzel.queue-hold`), separate from the
release's and incidents' merge freeze, which the moved queue code keeps reading as before.
"""

from __future__ import annotations

import os
import socket
from datetime import datetime
from typing import Any

import typer

from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime

KIND, KEY = "heinzel.queue-hold", "hold"


class QueueHold(Model):
    """The hold in force: why, since when (epoch seconds), and who set it."""

    reason: str
    since: float
    host: str = ""
    pid: int = 0
    excepted: list[str] = []  # OWNER/REPO#N of the PRs that may still ship and merge: the critical heads the hold is for

    def describe(self) -> str:
        but = f" (except {', '.join(self.excepted)})" if self.excepted else ""
        return f"the merge queue is held since {datetime.fromtimestamp(self.since).astimezone().strftime('%m-%d %H:%M')}{but}: {self.reason}"

    def lets(self, queued: list[str]) -> bool:
        """Whether a tick may run: only when every queued PR (OWNER/REPO#N) is an excepted one (the tick merges every ready PR)."""
        return bool(self.excepted) and bool(queued) and all(str(key).lower() in self.excepted for key in queued)


def pr_key(pr: str, repo: str = "") -> str:
    """OWNER/REPO#N: a bare number (or #N) is a PR of `repo`, by default the project's repository. A number alone never
    matches, so PR 812 of another repository is not let through by a hold for this one's 812."""
    text = str(pr).strip()
    where, _, number = text.rpartition("#") if "/" in text else ("", "", text.lstrip("#"))
    if not number.isdigit() or (where and where.count("/") != 1):
        raise ValueError(text)
    return f"{(where or repo or default_repo()).lower()}#{int(number)}"


def queued_key(item: dict[str, Any]) -> str:
    """OWNER/REPO#N of a queue item; "" for an item without a readable number (it is never excepted)."""
    try:
        return pr_key(str(item.get("pr") or item.get("key") or ""), str(item.get("repo") or ""))
    except ValueError:
        return ""


def default_repo(rt: Any | None = None) -> str:
    """The project's repository (the setting `repo`, OWNER/NAME)."""
    return str((rt or runtime()).settings.get("repo") or "")


class QueueHoldChanged(Model):
    """What `queue hold` or `queue unhold` did."""

    held: bool
    hold: QueueHold | None = None
    before: QueueHold | None = None

    def render(self) -> list[str]:
        if self.held and self.hold is not None:
            replaced = f" (it replaced the hold: {self.before.reason})" if self.before else ""
            but = " (an excepted PR ships, and merges once it is the only kind queued)" if self.hold.excepted else ""
            return [f"{self.hold.describe()}{replaced}", f"nothing merges and `sm head pr --live` and `sm head ship` refuse{but} until: sm queue unhold"]
        if self.before is not None:
            return [f"the merge queue hold is lifted (it was: {self.before.reason}); the next tick merges again"]
        return ["the merge queue was not held"]


def current(rt: Any | None = None) -> QueueHold | None:
    """The hold in force, or None."""
    data = (rt or runtime()).providers.store.load(KIND, KEY)
    return QueueHold.model_validate(data) if data else None


def refuse_while_held(what: str, rt: Any | None = None, pr: str = "", repo: str = "") -> None:
    """Refuse `what` (a push, a PR, a ship) while the queue is held, with the hold's reason; an excepted PR (this
    repository and number) passes."""
    try:
        key = pr_key(pr, repo or default_repo(rt)) if pr else ""
    except ValueError:
        key = ""
    if (hold := current(rt)) and not (key and key in hold.excepted):
        raise Refuse(f"{what} refused: {hold.describe()}. Nothing was pushed, opened or queued", "sm queue why   (the owner of the hold lifts it: sm queue unhold)")


@native("queue hold", writes=True, help=H(
    "Hold the whole merge queue: nothing merges, and `sm head pr --live` and `sm head ship` refuse with the reason.",
    "Use when one head must merge first or nothing may merge (a release-blocking fix), instead of messaging every builder.",
    "the queue hold record in the state directory; the next `sm daemon queue` tick merges nothing.",
    "sm queue hold --reason \"release 0.9.42 blocker: fix/clock first\" --except 812",
    "sm queue unhold   (when the critical head merged)",
    "--except PR (repeatable; N is this project's repository, OWNER/REPO#N another) lets that PR through: `sm head ship PR` works, and a tick runs while the excepted PRs are the\n"
    "only queued ones (a tick merges every ready PR, so others queued keep it from running).\n"
    "A second hold replaces the first (it says which). The hold is separate from the release's merge freeze."))
def queue_hold(reason: str = typer.Option(..., "--reason", metavar="TEXT", help="Why the queue is held; printed to everyone it stops."),
               excepted: list[str] = typer.Option([], "--except", metavar="PR[,PR...]", help="A PR that may still ship and merge: N (this project's repository) or OWNER/REPO#N; repeatable.")) -> QueueHoldChanged:
    if not reason.strip():
        raise Refuse("--reason must say why the queue is held", "sm queue hold --help", code=2)
    rt = runtime()
    numbers, bad = [], []
    for pr in excepted:
        try:
            numbers.append(pr_key(pr, default_repo(rt)))
        except ValueError:
            bad.append(pr)
    if bad:
        raise Refuse(f"--except takes N or OWNER/REPO#N: {', '.join(bad)}", "sm queue hold --help", code=2)
    before = current(rt)
    hold = QueueHold(reason=reason.strip(), since=rt.providers.clock.now(), host=socket.gethostname(), pid=os.getpid(), excepted=numbers)
    rt.providers.store.save(KIND, KEY, hold.model_dump())
    return QueueHoldChanged(held=True, hold=hold, before=before)


@native("queue unhold", writes=True, help=H(
    "Lift the merge queue hold: the next tick merges again, and `sm head pr` and `sm head ship` work again.",
    "Use when the critical head the hold was for has merged.",
    "the queue hold record in the state directory.",
    "sm queue unhold", "sm queue why"))
def queue_unhold() -> QueueHoldChanged:
    rt = runtime()
    before = current(rt)
    rt.providers.store.save(KIND, KEY, {})
    return QueueHoldChanged(held=False, before=before)
