"""`sm ship WORKTREE`: the one verb that takes a head from its worktree to a merged PR, as a driver over `sm head admit`.

It adds no step of its own. It derives the inputs `head admit` asks for (the selection, the review root, the Core
repository), hands them to the `head admit` pipeline unchanged and, after the queue step, can wait for the PR to merge.
An input given with `--set` (now or earlier: the pipeline remembers them) is never replaced by a derived one. The stops
are those of `head admit`; the review gate of the pipeline and of every step it runs still holds.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import typer

from steuermaschine import INTERNAL, config
from steuermaschine.engine import Engine, Options, Refuse, Report, Wait
from steuermaschine.github import Gh
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import gate_problem, native, runtime, switch_pending

from .. import state as statedir
from ..pipelines.common import core_repo, run
from ..pipelines.head_admit import identify, pipeline

POLL_SECONDS = 60  # between two reads of the PR and the queue while waiting for the merge
BASE = "origin/main"  # what the selection is taken against
LANES = {"python": "pytest", "web": "vitest"}  # selection lane -> the field of `head select` that holds its ids


class ShipResult(Model):
    """What `sm ship` did: the run's phase and next command, the inputs it derived, and how the wait ended."""

    worktree: str
    status: str = ""
    phase: str = ""
    next: str = ""
    waiting_on: str = ""
    trap: str = ""
    derived: list[str] = []
    pr: int | None = None
    merged: bool = False
    exit: int = 0
    lines: list[str] = []

    def render(self) -> list[str]:
        return self.lines

    @property
    def exit_code(self) -> int:
        return self.exit


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "head"


def _given(store: Any, pl: Any, key: str, set_: list[str] | None) -> dict[str, Any]:
    """The inputs that stay as they are: the ones remembered by an earlier run of this head, then the ones of --set."""
    names = pl.inputs
    kept: dict[str, Any] = dict((store.load(f"runs.{pl.id}", key) or {}).get("inputs") or {})
    for pair in set_ or []:
        name, eq, value = pair.partition("=")
        if not eq or name not in names:
            raise Refuse(f"--set {pair}: expected NAME=VALUE with NAME one of: {', '.join(names)}", code=2)
        kept[name] = value
    return kept


def selection(worktree: Path, branch: str, head: str) -> Path:
    """The packet selection of this head, from `sm head select` against main, stored per branch and head in the state directory."""
    found = run("head select", repo=worktree, base=BASE)
    if getattr(found, "exit_code", 0):
        raise Refuse(f"sm head select failed: {'; '.join(found.render())[:300]}", "sm head select --help")
    if found.full:
        raise Refuse("sm head select says this change needs the full suite: no narrow selection can be derived. "
                     "Write one by hand and give it with --set sel=FILE", "sm head select --help")
    if found.e2e_lanes:
        raise Refuse(f"sm head select names browser journeys ({', '.join(found.e2e_lanes)}) and their test ids cannot be derived: "
                     "write the selection by hand and give it with --set sel=FILE", "sm head select --help")
    chosen = {lane: list(getattr(found, field)) for lane, field in LANES.items() if getattr(found, field)}
    if not chosen:
        raise Refuse("sm head select names no test for this change: give a selection with --set sel=FILE", "sm head select --help")
    target = statedir.path(f"ship/sel/{_slug(branch)}-{head[:12]}.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(chosen, indent=2, sort_keys=True) + "\n")
    return target


def derive(worktree: str, branch: str, head: str, kept: dict[str, Any]) -> dict[str, Any]:
    """The inputs `head admit` needs and nobody gave: core_repo (the setting, or a refusal naming it), review_root
    (the state directory), sel (`head select`). Each is only derived when it is not already in KEPT."""
    out: dict[str, Any] = {}
    if not kept.get("core_repo"):
        out["core_repo"] = str(core_repo(runtime().settings.get("core_repo_dir"), "set core_repo_dir (sm self config) or give --set core_repo=DIR"))
    if not kept.get("review_root"):
        out["review_root"] = str(statedir.path("ship/reviews"))
    sel = kept.get("sel")
    if not sel or not Path(str(sel)).expanduser().is_file():
        out["sel"] = str(selection(Path(worktree), branch, head))
    return out


def engine() -> Engine:
    rt = runtime()
    return Engine(rt.project.name, rt.providers, rt.settings, rt.project.lanes, out=rt.say)


def _repo() -> str:
    repo = str(runtime().settings.get("repo") or "")
    if "/" not in repo:
        raise Refuse("no repository: set repo (OWNER/NAME)", "sm self config", code=2)
    return repo


def outcome(number: int) -> tuple[str, str]:
    """(merged | closed | queued | dropped | needs work, detail) for one PR: GitHub first, then the queue. Read-only."""
    rt = runtime()
    data, _why = Gh(rt.providers.commands, repo=_repo()).try_json(["pr", "view", str(number), "-R", _repo(), "--json", "state"])
    state = str((data or {}).get("state") or "").upper() if isinstance(data, dict) else ""
    if state == "MERGED":
        return "merged", ""
    if state == "CLOSED":
        return "closed", "the PR was closed without a merge"
    entry = next((e for e in run("queue list", active=False).entries if str(e.pr) == str(number)), None)
    if entry is None:
        if not state:  # GitHub could not be read: the missing entry proves nothing
            return "unknown", "GitHub could not be read and the PR has no queue entry"
        return "dropped", "the PR is not in the queue any more"
    if entry.status == "merged":
        return "merged", ""
    if entry.status == "needs work":
        return "needs work", entry.detail or "the queue marked it needs work"
    return "queued", entry.detail


def wait_for_merge(number: int, minutes: int) -> None:
    """Poll until the PR merged (return), closed or was dropped (Refuse), or the ceiling passed (Wait, exit 75)."""
    clock = runtime().providers.clock
    ceiling = clock.now() + minutes * 60
    while True:
        state, detail = outcome(number)
        if state == "merged":
            return
        if state not in ("queued", "unknown"):
            raise Refuse(f"PR #{number} will not merge: {state}" + (f" ({detail})" if detail else ""), f"sm why {number}")
        if (pid := switch_pending(runtime().providers.commands, clock.now())) is not None:
            raise Wait(f"a promote (pid {pid}) is switching the live sm: the wait ends here; run the same command again", "the promote")
        if clock.now() + POLL_SECONDS > ceiling:
            raise Wait(f"PR #{number} is {state}, not merged after {minutes} minutes" + (f" ({detail})" if state == "unknown" else ""), "the merge queue")
        clock.sleep(POLL_SECONDS)


def _pr(store: Any, pl: Any, key: str) -> int | None:
    value = ((store.load(f"runs.{pl.id}", key) or {}).get("data") or {}).get("pr")
    return int(str(value)) if str(value or "").isdigit() else None


def _summary(worktree: str, report: Report, derived: list[str], pr: int | None, merged: bool) -> ShipResult:
    lines = [*report.lines(), *([f"DERIVED  {', '.join(derived)}"] if derived else []), *([f"MERGED   PR #{pr}"] if merged else [])]
    return ShipResult(worktree=worktree, status=report.status, phase=report.phase, next=report.next, waiting_on=report.waiting_on, trap=report.trap,
                      derived=derived, pr=pr, merged=merged, exit=report.exit_code, lines=lines)


@native("ship", writes=True, review="ship.md", help=H(
    "Take one head from its worktree to a merged PR: `head admit` with its inputs derived, then wait for the merge.",
    "Use when a head is committed in its own worktree and should go out; type it again after every stop.",
    "what `head admit` writes: state on this machine, the selection and the packets under the plugin state directory; with --live also the plan slot, "
    "one push, the PR and one queue entry. Waiting only reads.",
    "sm ship ~/Code/heinzel-fix --live --wait 45 --set bead=abc --set title=\"fix(web): short title\"",
    "the NEXT line it prints (a review stop names the file, its digest and the reviewer brief); exit 75: the same command again",
    "Derived unless given with --set (or remembered from an earlier run): sel (`head select` against origin/main, python and web lanes; browser journeys are not derived and refuse), review_root\n"
    "(the plugin state directory), core_repo (the setting core_repo_dir; it refuses naming it when unset). The PR is found by itself.\n"
    "The stops are those of `head admit`: both review stops stay, a verdict comes from a different agent (--approved SHA256).\n"
    "--wait MINUTES, after the queue step: exit 0 when the PR merged, exit 1 with the reason when it was closed or the queue dropped it,\n"
    "exit 75 when the ceiling is reached (run it again to keep waiting)."))
def ship(worktree: str = typer.Argument(..., metavar="WORKTREE", help="The clean worktree of the head, on its own work branch."),
         live: bool = typer.Option(False, "--live", help="Also run the steps that write outside this machine (apply, push and PR, queue)."),
         approved: str = typer.Option("", "--approved", metavar="SHA256", help="The digest a different agent approved at the review stop."),
         author: str = typer.Option("", "--as", metavar="NAME", help="Who drives this run; the reviewer must be someone else."),
         set_: list[str] | None = typer.Option(None, "--set", metavar="NAME=VALUE", help="An input of `head admit` (remembered); it replaces a derived one."),
         wait: int | None = typer.Option(None, "--wait", min=1, metavar="MINUTES", help="After the queue step, wait up to MINUTES for the PR to merge.")) -> ShipResult:
    rt = runtime()
    pl = pipeline()
    if problem := gate_problem(pl.unit, rt.project):
        raise Refuse(problem + "; nothing was run", "sm self reviews", code=INTERNAL)
    subject = identify(rt.providers, rt.settings, worktree)
    store = rt.providers.store
    kept = _given(store, pl, subject.key, set_)
    derived = derive(subject.facts["worktree"], subject.facts["branch"], subject.key, kept)
    # only what the caller gave now goes to the pipeline as given; a remembered input is already in its state
    now = {pair.partition("=")[0]: pair.partition("=")[2] for pair in set_ or []}
    report = engine().advance(pl, worktree, Options(live=live, approved=approved, author=author or config.environment().steuermaschine_agent or "coordinator",
                                                    inputs={**derived, **now}))
    number = _pr(store, pl, subject.key)
    if wait is None or report.status != "finished" or report.exit_code:
        return _summary(worktree, report, sorted(derived), number, False)
    if number is None:
        raise Refuse("the run finished but no PR is recorded for this head: nothing to wait for", "sm queue list")
    for line in report.lines():
        rt.say(line)
    wait_for_merge(number, wait)
    return _summary(worktree, report, sorted(derived), number, True)

