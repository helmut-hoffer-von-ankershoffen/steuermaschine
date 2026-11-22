"""`sm ship WORKTREE`: the one verb that takes a head from its worktree to a merged PR, as a driver over `sm head admit`.

It adds no step of its own: it hands the head to the `head admit` pipeline (local check, push, PR, queue) and, after the
queue step, can wait for the PR to merge. The queue arms GitHub auto-merge and GitHub merges it on green; a PR the
diff guard flags waits for its review.
"""

from __future__ import annotations

import re
from typing import Any

import typer

from steuermaschine import config
from steuermaschine.engine import Engine, Options, Refuse, Report, Wait
from steuermaschine.github import Gh
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime, switch_pending

from ..pipelines.common import run
from ..pipelines.head_admit import identify, pipeline
from .release_lib.binding import core_repo
from .ship_lib import prepare

POLL_SECONDS = 60  # between two reads of the PR and the queue while waiting for the merge


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


def queue_pr(number: str, live: bool, reason: str | None) -> ShipResult:
    """`sm ship PR`: guard the diff and queue one open PR (the old `sm head ship PR`)."""
    from . import ship as head

    if not live:
        raise Refuse(f"queueing PR #{number} writes the merge queue: add --live", f"sm ship {number} --live", code=2)
    done = head.head_ship(pr=number, reason=reason or None, reviewed_head=None, fix_forward=False, fix_for=[], reviewed=None, dry_run=False)
    return ShipResult(worktree=number, status=done.verdict, phase=f"PR #{number}: {done.verdict}", pr=int(number), exit=done.exit_code,
                      next=f"sm why {number}", lines=[*done.lines, *done.errors])


def other_repo(rt: Any, worktree: str) -> str | None:
    """The `owner/name` of the worktree's origin when it is not the core repository, else None (also when unreadable)."""

    found = rt.providers.commands.run(["git", "-C", worktree, "remote", "get-url", "origin"], timeout=30)
    url = str(found.out).strip() if found.rc == 0 else ""
    name = re.sub(r"\.git$", "", re.sub(r"^.*github\.com[:/]", "", url))
    return name if name and "/" in name and name.lower() != core_repo().lower() else None


def open_elsewhere(rt: Any, worktree: str, repo: str, live: bool, given: dict[str, str]) -> ShipResult:
    """Another repository: the old `sm head pr` (checks, one push, the PR, the queue entry)."""
    from pathlib import Path

    from . import ship as head

    plan = prepare.prepare(rt.providers.commands, worktree, given, configured=core_repo(), live=live)  # merges main when behind, derives bead and title, refuses once, before anything else
    title, bead = plan.inputs.get("title", ""), plan.inputs.get("bead", "none")
    if not title:
        raise Refuse("no title: the commit subjects of the branch could not be read", f"sm ship {worktree} --set title=\"type(scope): title\"", code=2)
    done = head.head_pr(bead=bead, title=title, body_file=Path(given["body"]) if given.get("body") else None,
                        reason=given.get("reviewed") or None, worktree=Path(worktree), live=live)
    number = next((int(m.group(1)) for line in done.lines if (m := re.search(r"PR #(\d+)", line))), None)
    return ShipResult(worktree=worktree, status=done.verdict, phase=f"{repo}: {done.verdict}", pr=number, exit=done.exit_code,
                      next=f"sm why {number}" if number else f"sm ship {worktree} --live", derived=plan.derived,
                      lines=[*done.lines, *done.errors, *([f"DERIVED  {', '.join(plan.derived)}"] if plan.derived else [])])


@native("ship", writes=True, help=H(
    "Take one change from its worktree to a merged PR: check, push, open the PR, arm auto-merge (via the queue); optionally wait.",
    "Use when a head is committed in its own worktree and should go out; type it again after every stop.",
    "state on this machine; with --live also one push, the PR and one queue entry. Waiting only reads.",
    "sm ship ~/Code/heinzel-fix --live --wait 45 --set bead=abc --set title=\"fix(web): short title\"",
    "the NEXT line it prints (a review stop names the digest and the brief); exit 75: the same command again",
    "WORKTREE of the core repository: the core pipeline below. A worktree of another repository (its origin decides, for\n"
    "example the plugins repository): checks, one push, the PR and the queue entry, with the title from --set title or the last\n"
    "commit subject and the bead from --set bead. A PR number instead of a worktree: queue that open PR (with --live; a\n"
    "flagged PR needs --set reviewed=\"review: FILE\").\n"
    "Before the first check, in one pass: the repository is read from the origin (core, plugins, acme, sf, the public steuermaschine,\n"
    "heinzel-management); a branch behind origin/main gets main merged in by sm (subject \"chore: merge origin/main\"; a conflict is\n"
    "aborted and named); the bead comes from --set bead, a `Bead:` trailer of a commit, or a bead id in the branch name, else `none`;\n"
    "the title from --set title or the newest conventional commit subject. What cannot be found is ONE refusal naming the exact\n"
    "--set, before any check. DERIVED in the output says what was found. The queue loop arms GitHub auto-merge (squash, pinned to\n"
    "the guarded head; off during a Core release freeze); no branch has to be up to date with main, a red main is reverted.\n"
    "steuermaschine and heinzel-management have no queue entry: their PR merges when its required checks are green.\n"
    "No packet, no plan slot: Core's CI runs the full matrix on every PR. One blocking stop is kept: a head that changes .github/, the\n"
    "release, publish, PyPI or acceptance scripts, quarantine.toml, the coverage config or a conftest.py stops with the digest of exactly\n"
    "those paths; a different agent records `sm review verdict DIGEST --approve`, then run this again with --approved DIGEST.\n"
    "The diff guard stays too: a flagged PR is queued only with --set reviewed=\"review: FILE\".\n"
    "--wait MINUTES, after the queue step: exit 0 when the PR merged, exit 1 with the reason when it was closed or the queue dropped it,\n"
    "exit 75 when the ceiling is reached (run it again to keep waiting)."))
def ship(worktree: str = typer.Argument(..., metavar="WORKTREE", help="The clean worktree of the head, on its own work branch; or an open PR number."),
         live: bool = typer.Option(False, "--live", help="Also run the steps that write outside this machine (push and PR, queue)."),
         approved: str = typer.Option("", "--approved", metavar="SHA256", help="The digest a different agent approved at the protected-path stop."),
         author: str = typer.Option("", "--as", metavar="NAME", help="Who drives this run; the reviewer must be someone else."),
         set_: list[str] | None = typer.Option(None, "--set", metavar="NAME=VALUE", help="An input of `head admit` (remembered)."),
         wait: int | None = typer.Option(None, "--wait", min=1, metavar="MINUTES", help="After the queue step, wait up to MINUTES for the PR to merge.")) -> ShipResult:
    rt = runtime()
    given = {pair.partition("=")[0]: pair.partition("=")[2] for pair in set_ or []}
    if worktree.isdigit():
        return queue_pr(worktree, live, given.get("reviewed"))
    if (origin := other_repo(rt, worktree)) is not None:
        return open_elsewhere(rt, worktree, origin, live, given)
    pl = pipeline()
    _given(rt.providers.store, pl, "", set_)  # refuses a --set of a name the pipeline does not take, before anything is touched
    plan = prepare.prepare(rt.providers.commands, worktree, given, configured=core_repo(), live=live)  # main merged when behind; repository, bead, title found; ONE refusal, before the check
    subject = identify(rt.providers, rt.settings, worktree)  # after the merge: the head of this run
    store = rt.providers.store
    kept = _given(store, pl, subject.key, set_)
    inputs = {**plan.inputs, **kept}  # --set wins, then what this head remembers, then what was derived
    report = engine().advance(pl, worktree, Options(live=live, approved=approved, author=author or config.environment().steuermaschine_agent or "coordinator", inputs=inputs))
    number = _pr(store, pl, subject.key)
    if wait is None or report.status != "finished" or report.exit_code:
        return _summary(worktree, report, plan.derived, number, False)
    if number is None:
        raise Refuse("the run finished but no PR is recorded for this head: nothing to wait for", "sm queue list")
    for line in report.lines():
        rt.say(line)
    wait_for_merge(number, wait)
    return _summary(worktree, report, plan.derived, number, True)
