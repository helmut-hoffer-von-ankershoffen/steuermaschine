"""go: where is the ship pipeline, and what is the ONE next command? Read-only.

The decision is in go_lib/decide.py (pure), the probes in go_lib/probes.py, the adopted state files in
go_lib/legacy.py. Every command it names is an sm command.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, Field

from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime
from steuermaschine.surface import translate

from .. import state as statedir  # the plugin's state paths
from .go_lib import decide as rules
from .go_lib import legacy, probes, views
from .go_lib import live as live_commands
from .go_lib.words import Words
from .surface import RENAMES


class GoSettings(BaseModel):
    """Settings of the read-only commands (mixin of the plugin's settings table)."""

    legacy_state_dir: Path | None = Field(None, description="Where the read-only views read the queue, release and train state files; none: the project's state directory.")
    core_repo_dir: Path | None = Field(None, description="The Core repository on this machine, for git reads of main and tags. Unset: those reads report it, the commands that need a Core refuse.")
    go_packet_globs: list[str] = Field([], description="Globs of admission wave.json files (direct and predict/bound route). Empty: no packet is listed.")
    go_packet_hours: float = Field(30.0, description="How old an admission packet may be to be listed.")
    go_stall_minutes: int = Field(15, description="A green queued PR unmerged for this long is STALLED.")
    go_history_days: float = Field(2.0, description="A pipeline run not updated for this many days is history: it stays in its journal and is never a NEXT.")
    go_budget_seconds: float = Field(18.0, description="Deadline for all probes of `go` together.")
    go_pr_actions_file: Path | None = Field(None, description="The reviewed PR action table (TOML with hold = true rows); none: no PR holds are known.")
    mini_cli: str = Field("hz", description="The command of the product on this machine, asked for its version.")
    go_tracking_doc: Path | None = Field(None, description="The tracking document (Markdown) the views start and next read: checkpoints, asks, paused work. None: those parts say so.")
    go_timers_file: Path | None = Field(None, description="The timer manifest (TOML: name -> every_minutes, codex_id) the view start compares. None: not compared.")
    go_codex_home: Path | None = Field(None, description="The Codex home whose automation records the view start reads. None: records are not read.")


class GoAlso(Model):
    phase: str
    next: str = ""
    waiting_on: str = ""


class GoAlarm(Model):
    key: str
    level: str  # alert
    text: str
    acked: bool = False


class Go(Model):
    """The `go` contract of the Heinzelmännchen ship pipeline."""

    schema_version: int = 1
    phase: str
    kind: str  # the rank name of the phase, for programs: candidate-failed, apply-running, prepare-running, idle ...
    next: str = ""  # ONE command that can be run now; empty: nothing runnable
    next_for: str = ""  # the item NEXT belongs to, when it is not the phase
    waiting_on: str = ""
    then: str = ""  # the command after the wait or the review
    trap: str = ""
    also: list[GoAlso] = []
    aside: list[str] = []  # not on the ship path: conflicts, holds, history
    alarms: list[GoAlarm] = []  # unacknowledged ALERTs, oldest first
    facts: list[str] = []
    unknown: list[str] = []  # probes that failed, with the reason
    probes: dict[str, Any] = {}  # what the probes read (without raw run and PR lists)
    seconds: float = 0.0
    read_only: bool = True
    view: views.GoView | None = None  # with --view: the view that was asked for (the decision above is computed as always)
    show: int = Field(8, exclude=True)

    def render(self) -> list[str]:
        if self.view is not None:
            return [*self.view.render(), f"         ({self.seconds}s, read-only)"]
        d = {"alerts": [f"{a.text}  (silence: sm alarm ack {a.key})" for a in self.alarms], "phase": self.phase, "next": self.next,
             "next_for": self.next_for, "waiting": self.waiting_on, "then": self.then, "trap": self.trap,
             "also": [a.phase for a in self.also], "side": self.aside, "facts": self.facts, "unknown": self.unknown}
        return [*rules.render(d, self.show), f"         ({self.seconds}s, read-only)"]


def world() -> probes.World:
    rt = runtime()
    return world_of(rt.providers, rt.settings)


def world_of(providers: Any, settings: Any) -> probes.World:
    """The providers and settings the read-only commands work with (also for the hooks, which get them as arguments)."""
    s = settings or {}
    focus_list = providers.store.load("focus", "list")  # written by `sm focus add|done`
    acks = providers.store.load("alarms", "acks") or {}  # written by `sm alarm ack`
    rehearsal = providers.store.load(probes.REHEARSAL_KIND, probes.REHEARSAL_KEY)  # written by `sm release rehearse` and its daemon
    runs = [state for kind in probes.PIPELINE_KINDS for key in providers.store.keys(kind) if (state := providers.store.load(kind, key))]  # `sm head admit`, `sm release cut`
    return probes.World(commands=providers.commands, clock=providers.clock, focus_list=focus_list if isinstance(focus_list, dict) else None, acks=dict(acks),
                        rehearsal=rehearsal, runs=runs,
                        state_dir=statedir.dashboard_dir(s),
                        core=str(Path(str(s["core_repo_dir"])).expanduser()) if s.get("core_repo_dir") else "", repo=str(s.get("repo") or ""), settings=dict(s))


def go_items(providers: Any, settings: Any) -> list[Any]:
    """For the hook `sm_go_items`, should the core `go` stay the frame: the same decision as ranked items.
    The phase comes first; every other thing in flight keeps its own next command."""
    from steuermaschine.plugins import GoItem

    w = world_of(providers, settings)
    d = rules.decide(probes.collect(w), w.now(), SmWords(), int(w.settings.get("go_stall_minutes") or 15))
    own_next = d["next"] if not d["next_for"] else ""
    out = [GoItem(10, d["phase"], own_next or "", d["waiting"] or "", d["trap"] or "")]
    if d["next_for"]:
        out.append(GoItem(11, d["next_for"], d["next"] or "", "", d["trap"] or ""))
    out += [GoItem(50 + n, i["phase"], i["next"], i["waiting_on"]) for n, i in enumerate(d["also_items"]) if i["phase"] != d["next_for"]]
    return out


def tool_rows() -> list[Any]:
    from ..toolmap import ROWS

    return ROWS


class SmWords(Words):
    def rename(self, text: str) -> str:
        return legacy.sweep(text, tool_rows())


def report(state: dict[str, Any], now: float, seconds: float = 0.0, show: int = 8, stall_minutes: int = 15, acks: dict[str, Any] | None = None,
           commands: list[dict[str, Any]] | None = None) -> Go:
    """The decision as the `go` contract. Every old command name is written as its visible verb (surface.RENAMES). With
    `commands` (the rows of the running command tree) every `sm` command on NEXT, THEN and ALSO that is not a visible
    command is shown as missing instead of offered (ka69, h8jg5)."""
    result = _report(state, now, seconds, show, stall_minutes, acks)
    visible = functools.partial(translate, renames=RENAMES)  # NEXT names only visible commands: old names become the new verbs
    result.next, result.then, result.trap = visible(result.next), visible(result.then), visible(result.trap)
    result.also = [GoAlso(phase=visible(a.phase), next=visible(a.next), waiting_on=visible(a.waiting_on)) for a in result.also]
    result.aside = [visible(a) for a in result.aside]
    if commands is not None:
        check = live_commands.checker(commands)
        result.next, result.then = check(result.next), check(result.then)
        result.also = [GoAlso(phase=a.phase, next=check(a.next), waiting_on=a.waiting_on) for a in result.also]
    return result


def _report(state: dict[str, Any], now: float, seconds: float = 0.0, show: int = 8, stall_minutes: int = 15, acks: dict[str, Any] | None = None) -> Go:
    alarms = state.get("alarms") if rules.known(state.get("alarms")) else {}
    alarms = {k: ({**e, "acked": True} if k in (acks or {}) and isinstance(e, dict) else e) for k, e in (alarms or {}).items()}
    state = {**state, "alarms": alarms} if rules.known(state.get("alarms")) else state
    try:
        d = rules.decide(state, now, SmWords(), stall_minutes)
    except Exception as exc:
        d = {"phase": f"unknown: go could not decide ({type(exc).__name__}: {exc})", "kind": "unknown", "next": SmWords().undecided(),
             "next_for": None, "then": None, "trap": "", "waiting": None, "also_items": [], "side": [],
             "unknown": [f"{k}: {v['unknown']}" for k, v in state.items() if not rules.known(v)], "facts": []}
    slim = {k: v for k, v in state.items() if k not in ("runs", "prs", "remote_heads", "receipts", "alarms")}
    slim["prs"] = [rules.classify_pr(p) for p in state["prs"]] if rules.known(state.get("prs")) and state.get("prs") is not None else state.get("prs")
    return Go(phase=d["phase"], kind=d["kind"], next=d["next"] or "", next_for=d["next_for"] or "", waiting_on=d["waiting"] or "",
              then=d["then"] or "", trap=d["trap"] or "", also=[GoAlso(**i) for i in d["also_items"]], aside=d["side"],
              alarms=[GoAlarm(key=key, level="alert", text=SmWords().rename(e.get("text") or key)) for key, e in rules.open_alert_rows(alarms)],
              facts=d["facts"], unknown=d["unknown"], probes=_plain(slim), seconds=seconds, show=show)


def _plain(value: Any) -> Any:
    import json

    return json.loads(json.dumps(value, default=str))


@native("go", writes=False, replaces=("hz-go",), help=H(
    "Where is the ship pipeline, and what is the ONE next command? The front door.",
    "Use at every tick, cold start and context reset, and whenever you are unsure what to type next.",
    "nothing (read-only: git and GitHub reads, the process list, state files; no lock, no write).",
    "sm go            (machines: sm --format json go; prove the live Mini too: sm go --live)",
    "the command on the NEXT line, after reading the TRAP line.",
    "Prints PHASE, NEXT, WAITING, THEN, TRAP, ALSO (the other things in flight), ASIDE (not on the ship path), STATE and\n"
    "UNKNOWN (probes that failed: the decision is made on what is left). Unacknowledged ALERTs come first.\n"
    "A declared focus head (sm focus add WORKTREE --for X.Y.Z) is followed through its steps, including a packet built by\n"
    "the predict -> bind route and a prepare or admission that is still running; no rehearsal and no cut is offered while\n"
    "it is unmerged. Until the first `sm focus add` the focus file of the scripted era is read.\n"
    "--view start|next|prs|wait|tick prints one read-only view from the same probes: the cold-start route with the latest\n"
    "checkpoint and timers; the ranked next actions (owner's order; claims of the scripted era are shown, never written);\n"
    "the pull request table of sm status; the biggest wait and what unblocks it; the anomaly digest of a tick (it never\n"
    "defers or cancels CI)."))
def go(all_: bool = typer.Option(False, "--all", help="List every item, not the first 8."),
       live: bool = typer.Option(False, "--live", help="Also prove that the Mini runs the latest accepted release (the reads of sm mini verify)."),
       view: str | None = typer.Option(None, "--view", metavar="|".join(views.VIEWS), help=(
           "Print one view instead: start (cold-start route, latest checkpoint, timers), next (ranked next actions), "
           "prs (the pull request table), wait (the biggest wait and what unblocks it), tick (the anomaly digest).")),
       thread: str | None = typer.Option(None, "--thread", metavar="ID", help="With --view start: label saved timer bindings as this chat or another.")) -> Go:
    import time as _time

    from steuermaschine.engine import Refuse

    view = view if isinstance(view, str) else None  # a direct call (not through the command tree) leaves the option objects
    thread = thread if isinstance(thread, str) else None
    if view is not None and view not in views.VIEWS:
        raise Refuse(f"--view must be one of {', '.join(views.VIEWS)}", "sm go --help", code=2)
    w = world()
    started = _time.monotonic()  # the duration shown is real time, whatever the clock provider is
    state = probes.collect(w)
    if live and rules.known(state.get("mini")):
        from . import mini  # here, not at the top: mini builds on this module

        proof = mini.verify(w)
        state["mini"].update(live=proof.render()[-1], live_ok=proof.live)
    result = report(state, w.now(), 0.0, 10**6 if all_ else 8, int(w.settings.get("go_stall_minutes") or 15), w.acks, live_commands.rows_of_running_tree())
    if view is not None:
        result.view = views.build(view, w, state, result, all_, thread)
    result.seconds = round(_time.monotonic() - started, 1)
    return result
