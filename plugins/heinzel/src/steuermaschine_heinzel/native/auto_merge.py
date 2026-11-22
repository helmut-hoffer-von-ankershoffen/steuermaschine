"""`sm queue auto`: queue (or merge) every pull request that is ready, in every repository of the project, by itself.

Ready means: not a draft, merge state CLEAN (or BEHIND where the queue updates the branch), every CI check ok, the Sonar
check not red or running, no unresolved review thread, no hold in the action table, and, when the PR touches a gate
(`auto_review.needs_review`), an APPROVE verdict recorded for exactly this head (`sm review dispatch`). A rejected or
missing verdict stops the PR with its reason. Nothing weakens a rule: the queue still waits for green checks, honours the
merge freeze and the queue hold, and the repository's ruleset still decides at the merge.

A repository the queue knows (Core, plugins, acme) gets a queue entry; another one (this tool's own) is squash-merged by
`gh pr merge --match-head-commit`, unless a freeze or hold is on.
"""

from __future__ import annotations

import re
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.protocols import Busy
from steuermaschine.native import native, runtime

from . import auto_review, merge_ready, queue, queue_hold
from .go import world
from .queue_lib import hzlib
from .status_lib import prtable

READY_STATES = ("CLEAN", "BEHIND")
PASS_LOCK = "heinzel.queue-auto"


class AutoRow(Model):
    """What `sm queue auto` decided for one pull request."""

    repo: str
    number: int
    action: str  # queued | merged | waiting | skipped | would-queue | would-merge
    why: str = ""

    def line(self) -> str:
        return f"{self.repo} #{self.number}: {self.action}" + (f" ({self.why})" if self.why else "")


class AutoQueued(Model):
    """The result of one pass over the open pull requests."""

    rows: list[AutoRow] = []
    stopped: str = ""  # why nothing was done at all (a hold)

    def render(self) -> list[str]:
        if self.stopped:
            return [f"nothing done: {self.stopped}"]
        return [r.line() for r in self.rows] or ["no open pull request is ready"]


def eligible(row: prtable.PrRow) -> str:
    """"" when the table row passes every cheap rule, else the reason it does not (rows that merely wait are not reasons to act on)."""
    if row.state == "DRAFT":
        return "draft"
    if row.state not in READY_STATES:
        return f"merge state {row.state}"
    if row.ci != "ok":
        return f"CI {row.ci}"
    if row.sonar != "ok":
        return "no Sonar check" if row.sonar == "-" else f"Sonar {row.sonar}"  # a missing check is not a passing one
    if not re.fullmatch(r"[0-9a-f]{40}", row.head):
        return "the head the checks were read for is unknown"
    if row.threads:
        return f"{row.threads} unresolved review thread(s)"
    return ""


def queue_words(repo: str, number: int, owner: str) -> list[str] | None:
    """The words `sm queue add` takes for the PR (`393`, `plugins 38`), or None for a repository the queue does not know."""
    for short, full in hzlib.SHORT.items():
        if full.lower() == f"{owner}/{repo}".lower():
            return [str(number)] if not short or short == "core" else [short, str(number)]
    return None


def review_gate(verdict: str, reasons: list[str]) -> str:
    """"" when the review rule is met, else why not."""
    if not reasons:
        return ""
    if verdict == "APPROVE":
        return ""
    if verdict == "REJECT":
        return "the independent review rejected this head"
    return "needs an independent review of this head (sm review dispatch)"


def hold_reason(w: Any, repo_full: str, number: int) -> str:
    actions = w.settings.get("go_pr_actions_file")
    return merge_ready.hold_refusal(number, repo_full, Path(str(actions)).expanduser() if actions else None, merge_ready.short_names(w)) or ""


@native("queue auto", writes=True, help=H(
    "Queue every ready pull request of the allowed authors: CI ok, Sonar ok, no open thread, a review if gated.",
    "Use when green pull requests wait to be checked and queued by hand; run it each tick.",
    "with --live: queue entries (Core, plugins, acme), a squash merge in a repository without a queue, and with --dispatch a headless reviewer run and its comment; without --live: only the list.",
    "sm queue auto --live --dispatch", "sm queue list; sm why N for a pull request it skipped",
    "Only PRs of auto_queue_authors (default: the owner) are acted on; a public repository needs no approval.\n"
    "A PR that touches a gate (diff guard flag, CI change) is queued only with an APPROVE comment of `sm review dispatch` for exactly its head. --dispatch starts that\n"
    "review when none is recorded. A merge freeze or a hold in the action table keeps the PR where it is; the queue hold stops the whole pass."))
def queue_auto(live: bool = typer.Option(False, "--live", help="Queue and merge; without it only list what would happen."),
               dispatch: bool = typer.Option(False, "--dispatch", help="Start the independent review of a gate-touching PR that has none (needs --live).")) -> AutoQueued:
    if dispatch and not live:
        raise Refuse("--dispatch starts an agent and posts a comment: add --live", "sm queue auto --live --dispatch", code=2)
    rt = runtime()
    if hold := queue_hold.current(rt):
        return AutoQueued(stopped=hold.describe())
    w = world()
    owner = w.repo.split("/")[0]
    settings = auto_review.settings_of(w)
    try:
        with rt.providers.store.lock(PASS_LOCK) if live else nullcontext():
            return one_pass(rt, w, owner, settings, live, dispatch)
    except Busy:
        raise Refuse("another `sm queue auto` pass is running", "sm queue list", code=1) from None


def one_pass(rt: Any, w: Any, owner: str, settings: auto_review.AutoReviewSettings, live: bool, dispatch: bool) -> AutoQueued:
    module: Any = queue._queue()  # type: ignore[no-untyped-call]
    frozen = bool(module.hzlib.merge_frozen())
    queued = {str(i["key"]) for i in queue._load(module)["items"] if i["status"] == "queued"}
    names = list(w.settings.get("status_pr_repos") or prtable.DEFAULT_REPOS)
    now = datetime.now(UTC)
    budget = [settings.review_max_per_pass]  # headless reviews this pass may still start
    rows: list[AutoRow] = []
    for name in names:
        listed, why = prtable.fetch(w.gh, owner, name, now)
        if why:
            rows.append(AutoRow(repo=name, number=0, action="skipped", why=why))
            continue
        for pr in listed:
            rows.append(handle(rt, w, module, owner, pr, (live, dispatch, frozen), queued, settings, budget))
    return AutoQueued(rows=[r for r in rows if r.action != "idle"])


def handle(rt: Any, w: Any, module: Any, owner: str, pr: prtable.PrRow, mode: tuple[bool, bool, bool], queued: set[str],
           settings: auto_review.AutoReviewSettings, budget: list[int]) -> AutoRow:
    """One pull request through the rules; an `idle` row is one that merely is not ready yet (it is not listed).
    `mode` is (live, dispatch, frozen); `budget[0]` the headless reviews this pass may still start."""
    live, dispatch, frozen = mode

    def row(action: str, why: str = "") -> AutoRow:
        return AutoRow(repo=pr.repo, number=pr.number, action=action, why=why)

    full = f"{owner}/{pr.repo}"
    if pr.author.lower() not in {a.lower() for a in settings.auto_queue_authors}:
        return row("idle", f"author {pr.author} is not on auto_queue_authors")  # a public repository: a green PR of anyone else is not ours to merge
    if bad := eligible(pr):
        return row("idle", bad)
    words = queue_words(pr.repo, pr.number, owner)
    key = module.hzlib.split_key(words)[2] if words else ""
    if key and key in queued:
        return row("idle", "already queued")
    if held := hold_reason(w, full, pr.number):
        return row("skipped", held)
    data, why = w.gh.try_json(["pr", "view", str(pr.number), "-R", full, "--json", "headRefOid,baseRefOid,files,comments,commits"], timeout=30)
    head, base = (str(data.get("headRefOid") or ""), str(data.get("baseRefOid") or "")) if isinstance(data, dict) else ("", "")
    if why or not (re.fullmatch(r"[0-9a-f]{40}", head) and re.fullmatch(r"[0-9a-f]{40}", base)):
        return row("skipped", f"head unreadable: {why or 'no exact commits'}")
    if head != pr.head:  # CI, Sonar and threads were read for another commit: the next pass reads them again
        return row("idle", f"the head moved from {pr.head[:12]} to {head[:12]} since the checks were read")
    reasons = auto_review.needs_review(full, pr.number, base, head, list(data.get("files") or []))
    verdict = ""
    if reasons:
        messages = [f"{c.get('messageHeadline', '')}\n{c.get('messageBody', '')}" for c in data.get("commits") or []]
        verdict = auto_review.recorded_verdict(list(data.get("comments") or []), head, settings.review_comment_authors, auto_review.author_families(messages))
        if not verdict and dispatch:
            if budget[0] <= 0:
                return row("waiting", f"{settings.review_max_per_pass} reviews were started in this pass; the next pass goes on")
            budget[0] -= 1
            try:
                res = auto_review.review_dispatch(number=pr.number, repo=full, model="", live=True)
            except Refuse as refused:
                return row("waiting", str(refused)[:120])
            verdict = res.verdict
            if not verdict:
                return row("waiting", res.note or "review started, no verdict yet")
    if stop := review_gate(verdict, reasons):
        return row("waiting" if verdict != "REJECT" else "skipped", stop)
    if words:
        reason = f"review: sm-review comment on head {head[:12]}" if reasons else f"sm queue auto: no gate touched, ready at head {head[:12]}"
        if not live:
            return row("would-queue")
        done = queue._changed("add", ["add", "--reviewed", reason, "--reviewed-head", head, *words], words)  # every entry is bound to the head the checks were read for
        return row("queued" if not done.exit else "skipped", "" if not done.exit else "; ".join(done.errors or done.lines)[:120])
    if frozen:
        return row("waiting", "merge freeze is on")
    if not live:
        return row("would-merge")
    merged = rt.providers.commands.run(["gh", "pr", "merge", str(pr.number), "-R", full, "--squash", "--match-head-commit", head], timeout=120)
    return row("merged" if merged.rc == 0 else "skipped", "" if merged.rc == 0 else merged.err.strip()[:120])
