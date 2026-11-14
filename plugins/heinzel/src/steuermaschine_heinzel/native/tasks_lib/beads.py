"""A thin layer over the issue tracker (bd) with this project's conventions.

IDs may be the short suffix or full. Deadlines are stored as a label `due-YYYYMMDD-HHMM` (local time), because the
tracker's own due parser proved unreliable (absolute times came back wrong). Notes are appended with the time and
never replace history.

Every call of the tracker is checked: a non-zero exit is a refusal that carries the tracker's own message, and nothing
that would follow it runs (no ask row for a bead that was not created, no new due label when the old ones could not be
removed). A bead id, given or printed by the tracker, must be the prefix plus a suffix of the project's form
(`[a-z0-9]{3,12}`, optionally `.N` child numbers); anything else is refused before the tracker is called.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from steuermaschine import USAGE
from steuermaschine.engine import Refuse
from steuermaschine.models import Model

from ..coord_lib.base import child_env, setting, tracker_dir


class TaskDone(Model):
    """What one write to the tracker did; `output` is the tracker's last line."""

    action: str
    bead: str
    output: str = ""
    ask_row: str = ""  # with --ask: the row added to the tracking document

    def render(self) -> list[str]:
        return [self.bead if self.action == "add" else self.output or "ok"]


class TaskRow(Model):
    bead: str
    priority: int | str | None = None
    assignee: str = ""
    due: str = ""  # MM-DD HH:MM local, or empty
    title: str
    overdue: bool | None = None

    def line(self) -> str:
        return f"{self.bead:<5} P{'?' if self.priority is None else self.priority} {(self.assignee or '-')[:16]:<16} {self.due or '-':<11} {self.title[:70]}"


class TaskList(Model):
    rows: list[TaskRow] = []
    view: str = ""

    def render(self) -> list[str]:
        if self.view == "due":
            return [("OVERDUE " if r.overdue else "due     ") + r.line() for r in self.rows]
        return [r.line() for r in self.rows]


class TaskShown(Model):
    bead: str
    text: str

    def render(self) -> list[str]:
        return self.text.splitlines()


SUFFIX = re.compile(r"[a-z0-9]{3,12}(?:\.[0-9]+)*")


def dlabel(x: str | None, now: float) -> str | None:
    if not x:
        return None
    v = time.strftime("%Y-%m-%d ", time.localtime(now)) + x if (len(x) <= 5 and ":" in x) else x
    return "due-" + v.replace("-", "").replace(" ", "-").replace(":", "")[:13]


def dts(i: dict[str, Any]) -> float | None:
    for lab in i.get("labels") or []:
        if lab.startswith("due-") and len(lab) == 17:
            try:
                return time.mktime(time.strptime(lab[4:], "%Y%m%d-%H%M"))
            except Exception:
                pass
    return None


class Tracker:
    def __init__(self, rt: Any):
        s = rt.settings
        self.rt, self.commands, self.clock, self.env = rt, rt.providers.commands, rt.providers.clock, child_env(s)
        self.root, self.prefix = str(tracker_dir(s)), str(setting(s, "coord_bead_prefix"))

    def run(self, *args: str) -> str:
        """The tracker's output (stdout and stderr). A non-zero exit refuses with the tracker's message."""
        done = self.commands.run(list(args), cwd=self.root, env=self.env, timeout=60)
        text = str(done.out + done.err).strip()
        if done.rc != 0:
            said = " / ".join(text.splitlines()[-3:]) or "no output"
            raise Refuse(f"the tracker refused ({' '.join(args[:2])}, exit {done.rc}): {said}", "sm beads tasks show ID")
        return text

    def last(self, *args: str) -> str:
        return (self.run(*args).splitlines() or [""])[-1]

    def bid(self, x: str) -> str:
        """The full id of a short or full bead id; refuses anything that is not of the project's form."""
        suffix = x[len(self.prefix):] if x.startswith(self.prefix) else x
        if not SUFFIX.fullmatch(suffix):
            raise Refuse(f"not a bead id: {x!r}", "sm beads tasks ls all", code=USAGE)
        return self.prefix + suffix

    def stamp(self) -> str:
        return time.strftime("%H:%M", time.localtime(self.clock.now()))

    def jl(self, status: str | None = None, ready: bool = False, strict: bool = False) -> list[dict[str, Any]]:
        out = self.run("bd", "ready", "--json") if ready else self.run("bd", "list", *(["--status", status] if status else []), "--json", "--limit", "0")
        try:
            d = json.loads(out)
            return d if isinstance(d, list) else d.get("issues", [])
        except Exception:
            if strict:
                raise Refuse("the tracker's bead list is not JSON: the old due labels cannot be found, nothing changed", "sm beads tasks show ID") from None
            return []

    def row(self, i: dict[str, Any], now: float | None = None) -> TaskRow:
        t = dts(i)
        return TaskRow(bead=str(i["id"]).replace(self.prefix, ""), priority=i.get("priority"), assignee=i.get("assignee") or "",
                       due=time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "", title=str(i.get("title", "")), overdue=None if now is None or not t else t < now)

    def setdue(self, id_: str, x: str) -> None:
        for i in self.jl(None, strict=True):
            if i["id"] == id_:
                for lab in i.get("labels") or []:
                    if lab.startswith("due-"):
                        self.run("bd", "label", "remove", id_, lab)
        lab = dlabel(x, self.clock.now())
        if lab:
            self.run("bd", "label", "add", id_, lab)

    # -- writes
    def add(self, title: str, priority: str = "2", agent: str = "", labels: str = "", due: str | None = None, description: str = "", kind: str = "task", ask: bool = False) -> TaskDone:
        all_labels = ",".join(filter(None, [labels, "owner-ask" if ask else "", dlabel(due, self.clock.now()) or ""]))
        args = ["bd", "create", "--title", title, "-p", priority, "-t", kind, "--silent", *(["-a", agent] if agent else []), *(["-l", all_labels] if all_labels else []),
                *(["-d", description] if description else [])]
        new = self.last(*args)
        if not (new.startswith(self.prefix) and SUFFIX.fullmatch(new[len(self.prefix):])):
            raise Refuse(f"the tracker printed no bead id (last line: {new!r}); no ask row was written: look for the bead before adding it again", "sm beads tasks ls all")
        row = ""
        if ask:
            from ..asks_lib import asks

            row = asks.add(f"{title} ({self.stamp()})", self.rt).row
        return TaskDone(action="add", bead=new.replace(self.prefix, ""), output=new, ask_row=row)

    def note(self, bead: str, text: str) -> TaskDone:
        return TaskDone(action="note", bead=bead, output=self.last("bd", "update", self.bid(bead), "--append-notes", f"{self.stamp()} {text}"))

    def claim(self, bead: str, agent: str, due: str | None = None) -> TaskDone:
        out = self.last("bd", "update", self.bid(bead), "--status", "in_progress", "--assignee", agent)
        if due:
            self.setdue(self.bid(bead), due)
        return TaskDone(action="claim", bead=bead, output=out)

    def due_set(self, bead: str, when: str) -> TaskDone:
        self.setdue(self.bid(bead), when)
        return TaskDone(action="due-set", bead=bead, output="ok")

    def pr(self, bead: str, number: str, state: str = "open") -> TaskDone:
        return TaskDone(action="pr", bead=bead, output=self.last("bd", "update", self.bid(bead), "--append-notes", f"{self.stamp()} PR #{number} {state}"))

    def exact(self, bead: str) -> str:
        """The full id of `bead` only when the tracker knows exactly that bead: bd matches a partial id (`abc` finds
        `abcd`), so the id bd show resolves must be the one given. Refuses anything else before a write."""
        full = self.bid(bead)
        out = self.run("bd", "show", full, "--json")
        try:
            found = json.loads(out)
        except ValueError:
            raise Refuse(f"the tracker's answer for {full} is not JSON: nothing changed", "sm beads tasks show ID") from None
        rows = found if isinstance(found, list) else [found]
        ids = [str(r.get("id", "")) for r in rows if isinstance(r, dict)]
        if ids != [full]:
            raise Refuse(f"{bead!r} is not the exact id of one bead (the tracker resolves {full} to {', '.join(ids) or 'nothing'}): "
                         "nothing changed; give the whole id", "sm beads tasks ls all", code=USAGE)
        return full

    def done(self, bead: str, evidence: str) -> TaskDone:
        return TaskDone(action="done", bead=bead, output=self.last("bd", "close", self.exact(bead), "--force", "--reason", evidence))

    # -- reads
    def due(self, minutes: int = 60) -> TaskList:
        now = self.clock.now()
        lim = now + 60 * minutes
        rows = [self.row(i, now) for i in sorted(self.jl("in_progress"), key=lambda i: dts(i) or 9e18) if (dts(i) or 9e18) <= lim]
        return TaskList(rows=rows, view="due")

    def ls(self, status: str = "in_progress") -> TaskList:
        found = self.jl(ready=True) if status == "ready" else self.jl(None if status == "all" else status)
        return TaskList(rows=[self.row(i) for i in found[:60]], view=status)

    def show(self, bead: str) -> TaskShown:
        return TaskShown(bead=bead, text=self.run("bd", "show", self.bid(bead))[:1800])
