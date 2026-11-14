"""The validation window: which head may start hosted validation runs now, and on which commit.

Hosted runners are few. When several builders start validation runs for their heads at the same moment, they hit the
cap of runs in flight together, some start half their lanes, and nobody's result is complete. The window puts the
heads in a line:

- one head at a time: a head holds the window from its first run to its last result (or until its lease expires)
- per-branch pending rule: a branch has one place in the line. A newer head of the same branch takes that place and
  the older request is dropped: nobody validates a head that is already replaced
- global cap: a head starts only when the runs in flight plus the runs it wants fit under the cap; it never forces
- first come, first served among the waiting branches

and it never validates on the head's own commit:

- required checks attach to a commit, not to a branch. A validation run on a commit that is, or becomes, the head of
  a pull request takes the place of that pull request's checks and leaves it blocked with every check green.
  `sha_uses` names every other use of a commit; `throwaway_branch` names the branch for a child commit that carries
  the same tree and that nothing else will ever use.

A turn belongs to the command that got it: `enter` names an owner token, and only that owner is answered `go` again or
may `leave`. A second command for the same head waits like any other. A holder whose process is known to be gone is
passed at the next `enter`; `release` ends a turn by hand.

An admission hold keeps the line from starting anything new: a command that must have no validation run active (the
registry write of an admission) registers a `Hold` while it works. While a live hold exists, `enter` answers `wait`
to every head that does not hold the window already: a validation that runs finishes, no new one starts. A hold
whose command is gone, or whose lease ran out, is ignored. A waiting request whose command is gone is skipped at
once, never given the turn, and dropped; `sm ci window drop` (the project's command) removes one by hand.

State is one document in the `Store` (kind `<namespace>.window`), changed only under the store's lock. No process has
to stay alive: `enter` is called again by the same command until it says `go`, `leave` ends the turn.
"""

from __future__ import annotations

import os
import re
import socket
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from .models import Model
from .protocols import Busy, Clock, Store

T = TypeVar("T")

KEY = "state"
HOLDS = "holds"


@dataclass(frozen=True)
class Rules:
    cap: int = 5  # hosted validation runs in flight, all heads together
    lease_minutes: float = 180.0  # a holder that never left is passed after this long
    waiting_minutes: float = 30.0  # a waiting request that was not asked again for this long is dropped


class Turn(Model):
    """One head's place in the window."""

    branch: str
    head: str
    runs: int
    requested: float
    asked: float  # the last time its command asked; a request nobody asks for any more is dropped
    started: float | None = None
    owner: str = ""  # the token of the command that asked; only it holds or leaves this turn
    host: str = ""  # where that command runs, for the liveness check
    pid: int = 0
    data: dict[str, str] = {}  # what the holder left to clean up (a plugin's own keys)


class Hold(Model):
    """A command that wants the window quiet (an admission at prepare or apply): no new validation starts while it exists."""

    owner: str
    label: str = ""
    since: float
    host: str = ""
    pid: int = 0


class Decision(Model):
    """The answer to `enter`: go now, or wait and ask again."""

    state: str  # go | wait
    reason: str = ""
    position: int = 0  # 0: it is this head's turn; N: N heads are ahead
    holder: str = ""  # the branch that holds the window, when it is not this one
    held_by: list[str] = []  # the labels of the admission holds that stop a new validation from starting
    replaced: str = ""  # the older head of the same branch whose request this one replaced
    passed: Turn | None = None  # a holder whose command was gone and whose turn was ended; its `data` may need cleaning up

    def render(self) -> list[str]:
        return [f"{self.state}: {self.reason}" if self.reason else self.state]


class WindowStatus(Model):
    """Who holds the validation window and who waits, in order."""

    cap: int
    active: Turn | None = None
    held_minutes: float = 0.0
    holder_alive: bool | None = None  # None: cannot be known from here (another machine)
    waiting: list[Turn] = []
    gone: list[Turn] = []  # waiting requests whose command is gone: skipped, never given the turn
    holds: list[Hold] = []  # admissions that keep new validations from starting

    def render(self) -> list[str]:
        lines = [f"validation window: cap {self.cap} runs in flight, one head at a time"]
        if self.active:
            gone = "; ITS COMMAND IS GONE: `release` ends the turn" if self.holder_alive is False else ""
            lines.append(f"  active   {self.active.branch} {self.active.head[:9]} ({self.active.runs} runs, {self.held_minutes:.0f} min, "
                         f"pid {self.active.pid} on {self.active.host or '?'}{gone})")
        else:
            lines.append("  active   nobody")
        lines += [f"  waiting  {n}. {turn.branch} {turn.head[:9]} ({turn.runs} runs)" for n, turn in enumerate(self.waiting, 1)]
        lines += [f"  gone     {turn.branch} {turn.head[:9]} (pid {turn.pid} is gone; skipped, never gets the turn: "
                  f"`sm ci window drop {turn.head[:9]}` removes it)" for turn in self.gone]
        lines += [f"  hold     {hold.label or hold.owner} [{hold.owner}] (pid {hold.pid} on {hold.host or '?'}): no new validation starts until it ends "
                  f"(`sm ci window release-hold {hold.owner}` if stale)" for hold in self.holds]
        return lines


def sha_uses(sha: str, branch: str, open_prs: Iterable[tuple[int, str]], reserved_keys: Iterable[str], tip_branches: Iterable[str],
             throwaway_prefixes: Sequence[str] = ("validation/",), reserved_key: str = "pull_request:{sha}",
             reserved_text: str = "a plan registry holds {key} (an admitted or pre-open pull request head)") -> list[str]:
    """Why a validation run must not start on `sha`: every other use of that commit, empty when there is none.

    open_prs: (number, head sha) of the open pull requests; reserved_keys: keys of a registry that reserves commits
    for pull requests that are not open yet; tip_branches: remote branches whose tip is `sha` (the branch the run
    would start on always counts: a work branch can become a pull request head)."""
    reasons = [f"it is the head of open PR #{number}" for number, head in sorted(open_prs) if head == sha]
    key = reserved_key.format(sha=sha)
    if key in set(reserved_keys):
        reasons.append(reserved_text.format(key=reserved_key.format(sha=sha[:8])))
    reasons += [f"it is the tip of the non-validation branch {name}"
                for name in sorted({branch, *tip_branches}) if not name.startswith(tuple(throwaway_prefixes))]
    return reasons


def throwaway_branch(branch: str, sha: str, prefix: str = "validation/") -> str:
    """<prefix><name>-<sha8>: the source branch flattened, bound to the source commit it validates."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", branch.removeprefix(prefix)).strip("-.")[:80] or "branch"
    return f"{prefix}{name}-{sha[:8]}"


class Released(Model):
    """What `release` did."""

    released: bool
    reason: str
    turn: Turn | None = None

    def render(self) -> list[str]:
        return [self.reason]


def process_alive(turn: Turn | Hold) -> bool | None:
    """Whether the command that holds a turn still runs: True, False, or None when it runs on another machine."""
    if not turn.pid or turn.host != socket.gethostname():
        return None
    try:
        os.kill(turn.pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # it exists and belongs to someone else
    return True


class Withdrawn(Model):
    """What `release_hold` did."""

    withdrawn: bool
    reason: str
    turn: Turn | None = None

    def render(self) -> list[str]:
        return [self.reason]


class Window:
    def __init__(self, store: Store, clock: Clock, rules: Rules | None = None, namespace: str = "core", alive: Callable[[Any], bool | None] = process_alive):
        self.store, self.clock, self.rules, self.alive = store, clock, rules or Rules(), alive
        self.kind = f"{namespace}.window"

    # ------------------------------------------------------------------ state
    def _load(self) -> tuple[Turn | None, list[Turn]]:
        data = self.store.load(self.kind, KEY) or {}
        active = Turn(**data["active"]) if data.get("active") else None
        return active, [Turn(**row) for row in data.get("waiting") or []]

    def _save(self, active: Turn | None, waiting: list[Turn]) -> None:
        self.store.save(self.kind, KEY, {"active": active.model_dump() if active else None, "waiting": [turn.model_dump() for turn in waiting]})

    def _locked(self, change: Callable[[], T]) -> T:
        """Run `change` under the store's lock; another process inside it is waited for briefly."""
        for _ in range(200):
            try:
                with self.store.lock(self.kind):
                    return change()
            except Busy:
                self.clock.sleep(0.05)
        raise Busy(self.kind)

    def _expire(self, active: Turn | None, waiting: list[Turn]) -> tuple[Turn | None, list[Turn]]:
        now = self.clock.now()
        if active is not None and now - (active.started or active.requested) > self.rules.lease_minutes * 60:
            active = None
        return active, [turn for turn in waiting if now - turn.asked <= self.rules.waiting_minutes * 60 and self.alive(turn) is not False]

    def _hold_rows(self) -> list[Hold]:
        return [Hold(**row) for row in (self.store.load(self.kind, HOLDS) or {}).get("rows") or []]

    def _holds(self) -> list[Hold]:
        """The holds that count: their command may still run and their lease has not run out."""
        now = self.clock.now()
        rows = self._hold_rows()
        return [hold for hold in rows if now - hold.since <= self.rules.lease_minutes * 60 and self.alive(hold) is not False]

    # ------------------------------------------------------------------ the calls
    def enter(self, branch: str, head: str, runs: int, in_flight: int, owner: str = "", pid: int = 0, host: str = "") -> Decision:
        """Ask for the window. `in_flight`: hosted validation runs that are active now, whoever started them.
        `owner`: a token only this command knows. Call it again (the same command, the same token) until the state
        is `go`; then start the runs and call `leave` at the end."""
        if runs > self.rules.cap:
            return Decision(state="wait", reason=f"{runs} runs wanted, the cap is {self.rules.cap}: ask for fewer")

        def change() -> Decision:
            now = self.clock.now()
            active, waiting = self._expire(*self._load())
            passed = None
            if active is not None and active.owner != owner and self.alive(active) is False:  # the holder's command is gone
                passed, active = active, None
            if active is not None and active.branch == branch and active.head == head and active.owner == owner:
                self._save(active.model_copy(update={"asked": now}), waiting)
                return Decision(state="go", reason="this command holds the window")
            replaced = ""
            mine = next((turn for turn in waiting if turn.branch == branch), None)
            fresh: dict[str, Any] = {"runs": runs, "asked": now, "owner": owner, "pid": pid, "host": host}
            if mine is not None and mine.head != head:  # the per-branch pending rule: the newer head takes the place
                replaced = mine.head
                waiting = [turn.model_copy(update={"head": head, **fresh}) if turn is mine else turn for turn in waiting]
            elif mine is not None:
                waiting = [turn.model_copy(update=fresh) if turn is mine else turn for turn in waiting]
            else:
                waiting.append(Turn(branch=branch, head=head, requested=now, **fresh))
            position = next(n for n, turn in enumerate(waiting) if turn.branch == branch)
            if active is not None:
                self._save(active, waiting)
                if active.branch == branch and active.head == head:
                    why = f"this head is already being validated by another command (pid {active.pid} on {active.host or '?'})"
                else:
                    same = " (an older head of this branch: it finishes first)" if active.branch == branch else ""
                    why = f"{active.branch} {active.head[:9]} holds the window{same}"
                return Decision(state="wait", reason=why, position=position + 1, holder=active.branch, replaced=replaced)
            if position:
                self._save(active, waiting)
                return Decision(state="wait", reason=f"{position} head(s) ahead: {', '.join(turn.branch for turn in waiting[:position])}",
                                position=position, replaced=replaced, passed=passed)
            if holds := self._holds():
                self._save(active, waiting)
                return Decision(state="wait", reason=f"an admission is pending ({', '.join(hold.label or hold.owner for hold in holds)}): no new validation starts",
                                replaced=replaced, passed=passed, held_by=[hold.label or hold.owner for hold in holds])
            if in_flight + runs > self.rules.cap:
                self._save(active, waiting)
                return Decision(state="wait", reason=f"{in_flight} runs in flight + {runs} wanted > cap {self.rules.cap}", replaced=replaced, passed=passed)
            turn = waiting.pop(0)
            self._save(turn.model_copy(update={"started": now, "asked": now}), waiting)
            return Decision(state="go", reason=f"{in_flight} runs in flight + {runs} wanted <= cap {self.rules.cap}", replaced=replaced, passed=passed)

        return self._locked(change)

    def note(self, owner: str, data: dict[str, str]) -> None:
        """The holder records what it would leave behind (a branch, a commit), for whoever has to clean up after it."""

        def change() -> None:
            active, waiting = self._load()
            if active is not None and active.owner == owner:
                self._save(active.model_copy(update={"data": {**active.data, **data}}), waiting)

        self._locked(change)

    def leave(self, branch: str, head: str, owner: str = "") -> bool:
        """End this command's turn, or give up its place in the line. True when it held the window. Another
        command's turn, also one for the same head, is left alone."""

        def change() -> bool:
            active, waiting = self._load()
            held = active is not None and active.branch == branch and active.head == head and active.owner == owner
            self._save(None if held else active, [turn for turn in waiting if not (turn.branch == branch and turn.head == head and turn.owner == owner)])
            return held

        return self._locked(change)

    def hold(self, owner: str, label: str = "", pid: int = 0, host: str = "") -> Hold:
        """Register an admission hold: from now on no head starts a new validation. Validations that run finish."""

        def change() -> Hold:
            mine = Hold(owner=owner, label=label, since=self.clock.now(), host=host, pid=pid)
            kept = [hold for hold in self._holds() if hold.owner != owner]
            self.store.save(self.kind, HOLDS, {"rows": [row.model_dump() for row in [*kept, mine]]})
            return mine

        return self._locked(change)

    def unhold(self, owner: str) -> bool:
        """End this command's hold. True when it had one."""

        def change() -> bool:
            rows = self._hold_rows()
            had = any(hold.owner == owner for hold in rows)
            self.store.save(self.kind, HOLDS, {"rows": [hold.model_dump() for hold in rows if hold.owner != owner]})
            return had

        return self._locked(change)

    def release_hold(self, owner: str, force: bool = False) -> Withdrawn:
        """End a hold by hand (a stale one: its pid was reused, so it still looks alive). A hold whose command is gone or unknown to be
        gone is refused unless `force` when it may still run."""

        def change() -> Withdrawn:
            rows = self._hold_rows()
            found = [hold for hold in rows if hold.owner == owner]
            if not found:
                return Withdrawn(withdrawn=False, reason=f"no admission hold {owner}")
            if self.alive(found[0]) is not False and not force:
                return Withdrawn(withdrawn=False, reason=f"{found[0].label or owner} may still run (pid {found[0].pid} on {found[0].host or '?'}): wait for it, or --force")
            self.store.save(self.kind, HOLDS, {"rows": [hold.model_dump() for hold in rows if hold.owner != owner]})
            return Withdrawn(withdrawn=True, reason=f"released the admission hold of {found[0].label or owner}")

        return self._locked(change)

    def release(self, force: bool = False) -> Released:
        """End the holder's turn by hand: when its command is gone or its lease ran out, or with `force`."""

        def change() -> Released:
            stored, waiting = self._load()
            if stored is None:
                return Released(released=False, reason="nobody holds the window")
            expired, _ = self._expire(stored, [])
            alive = self.alive(stored)
            if not force and expired is not None and alive is not False:
                state = "its command still runs" if alive else "its command runs on another machine and cannot be checked from here"
                return Released(released=False, reason=f"{stored.branch} {stored.head[:9]} holds the window and {state} (pid {stored.pid} on {stored.host or '?'})", turn=stored)
            self._save(None, waiting)
            why = "forced" if force and alive is not False and expired is not None else "its command is gone" if alive is False else "its lease ran out"
            return Released(released=True, reason=f"released the window held by {stored.branch} {stored.head[:9]} ({why})", turn=stored)

        return self._locked(change)

    def status(self) -> WindowStatus:
        active, waiting = self._expire(*self._load())
        held = (self.clock.now() - (active.started or active.requested)) / 60 if active else 0.0
        return WindowStatus(cap=self.rules.cap, active=active, held_minutes=round(held, 1), holder_alive=self.alive(active) if active else None,
                            waiting=waiting, gone=self._gone(), holds=self._holds())

    def _gone(self) -> list[Turn]:
        """Waiting requests that are still within their time but whose command is gone."""
        now = self.clock.now()
        return [turn for turn in self._load()[1] if now - turn.asked <= self.rules.waiting_minutes * 60 and self.alive(turn) is False]
