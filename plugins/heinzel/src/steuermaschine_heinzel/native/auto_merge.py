"""`sm queue auto`: queue (or merge) every pull request that is ready, in every repository of the project, by itself.

Ready PRs are open, not drafts, and CLEAN (or BEHIND where the queue updates the branch). They meet the target branch
requirements, have a reported green Sonar check, no red checks, no open review threads, no hold in the action table, and, when a gate changes,
an APPROVE verdict recorded for exactly this head (`sm review dispatch`). A rejected or
missing verdict stops the PR with its reason. Nothing weakens a rule: the queue still waits for green checks, honours the
merge freeze and the queue hold, and the repository's ruleset still decides at the merge.

A repository the queue knows (Core, plugins, acme) gets a queue entry; another one (this tool's own) is squash-merged by
`gh pr merge --match-head-commit`, unless a freeze or hold is on. The direct request pins the head only. Branch rules,
base, optional checks and holds are observed locally and can change before the request. GitHub enforces its rules
when merging. For queued PRs, GitHub can merge after a later auto-merge arm under its own required checks.
"""

from __future__ import annotations

import re
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import typer

from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime
from steuermaschine.protocols import Busy

from . import auto_review, merge_ready, queue, queue_hold
from .go import world
from .queue_lib import auto_arm, hzlib
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
        return "no Sonar check" if row.sonar == "-" else f"Sonar {row.sonar}"
    if not re.fullmatch(r"[0-9a-f]{40}", row.head):
        return "the head the checks were read for is unknown"
    if row.threads:
        return f"{row.threads} unresolved review thread(s)"
    return ""


CHECKS_QUERY = """query AutoMergeChecks($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) { pullRequest(number: $number) {
    state isDraft mergeStateStatus headRefOid baseRefOid baseRef { name branchProtectionRule { requiresStatusChecks requiredStatusChecks { context app { databaseId } } } }
    reviewThreads(first: 100) { totalCount nodes { isResolved } }
    commits(last: 1) { nodes { commit { oid statusCheckRollup { contexts(first: 100) { totalCount nodes {
      __typename ... on CheckRun { name status conclusion checkSuite { app { databaseId } } }
      ... on StatusContext { context state }
    } } } } } }
  } }
}"""


def complete_nodes(connection: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = connection["nodes"]
    if not isinstance(nodes, list) or type(connection["totalCount"]) is not int or len(nodes) != connection["totalCount"]:
        raise ValueError("incomplete GitHub inventory")
    return nodes


def requirement(context: str, app: int | None) -> tuple[str, int | None]:
    if not isinstance(context, str) or not context.strip() or (app is not None and (type(app) is not int or app <= 0)):
        raise ValueError("invalid required check or integration")
    return context, app


def check_requirements(rules: list[Any], classic: dict[str, Any] | None) -> set[tuple[str, int | None]]:
    if not isinstance(rules, list):
        raise ValueError("unreadable target branch rules")
    required = set()
    for rule in rules:
        if rule["type"] != "required_status_checks":
            continue
        checks = rule["parameters"]["required_status_checks"]
        if not isinstance(checks, list) or not checks:
            raise ValueError("unreadable required status checks")
        required.update(requirement(c["context"], c.get("integration_id")) for c in checks)
    if classic is not None:
        if type(classic["requiresStatusChecks"]) is not bool:
            raise ValueError("unreadable classic branch protection")
        if classic["requiresStatusChecks"]:
            checks = classic["requiredStatusChecks"]
            if not isinstance(checks, list) or not checks:
                raise ValueError("unreadable classic required checks")
            for c in checks:
                app = c["app"]
                if app is not None and type(app["databaseId"]) is not int:
                    raise ValueError("unreadable classic required check integration")
                required.add(requirement(c["context"], app["databaseId"] if app is not None else None))
    if not required:
        raise ValueError("no required checks found for the target branch")
    return required


def required_gap(gh: Any, owner: str, repo: str, number: int, head: str, base: str, branch: str) -> str:
    """Check all head contexts and match each actual branch requirement to its issuing app when specified."""
    data, why = gh.try_json(["api", "graphql", "-f", f"query={CHECKS_QUERY}", "-F", f"owner={owner}", "-F", f"repo={repo}", "-F", f"number={number}"], timeout=30)
    if why:
        return f"cannot read head checks: {why}"
    try:
        if data.get("errors"):
            raise ValueError("GitHub returned partial check data")
        pr = data["data"]["repository"]["pullRequest"]
        if pr["state"] != "OPEN" or pr["isDraft"] is not False or pr["mergeStateStatus"] not in READY_STATES:
            return "the PR is no longer ready for merge"
        target = pr["baseRef"]
        commit, = pr["commits"]["nodes"]
        if not branch or (pr["headRefOid"], commit["commit"]["oid"], pr["baseRefOid"], target["name"]) != (head, head, base, branch):
            return "the PR head or target branch changed while reading checks"
        if any(t["isResolved"] is not True for t in complete_nodes(pr["reviewThreads"])):
            return "unresolved review threads"
        passed: set[tuple[str, int | None]] = set()
        skipped: set[tuple[str, int | None]] = set()
        for row in complete_nodes(commit["commit"]["statusCheckRollup"]["contexts"]):
            if row["__typename"] == "CheckRun":
                name, result = row["name"], row["conclusion"] if row["status"] == "COMPLETED" else "RUNNING"
                app = row["checkSuite"]["app"]
                issuer = app["databaseId"] if app is not None else None
            elif row["__typename"] == "StatusContext":
                name, result, issuer = row["context"], row["state"], None
            else:
                raise ValueError("unknown check context type")
            if result not in prtable.GOOD:
                return f"{name}: {result}"
            outcomes = passed if result == "SUCCESS" else skipped
            outcomes.add(requirement(name, issuer))
            outcomes.add((name, None))
        pages, why = gh.try_json(["api", "--paginate", "--slurp", f"repos/{owner}/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100"], timeout=30)
        if why:
            return f"cannot read target branch rules: {why}"
        if not isinstance(pages, list) or not pages or not all(isinstance(page, list) for page in pages):
            raise ValueError("unreadable target branch rules")
        rules = [rule for page in pages for rule in page]
        missing = check_requirements(rules, target["branchProtectionRule"]) - (passed - skipped)
        if missing:
            return "required checks missing or not successful: " + ", ".join(sorted(f"{n} (app {a})" if a else n for n, a in missing))
        # The local queue policy also requires a reported Sonar result, even if the branch rules do not.
        return "" if any("sonar" in name.lower() for name, _ in passed | skipped) else "no Sonar check on the current head"
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        return f"cannot verify target branch checks: {error}"


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
    "Queue every ready pull request of the allowed authors: required branch checks and Sonar green, no open thread, a review if gated.",
    "Use when green pull requests wait to be checked and queued by hand; run it each tick.",
    "with --live: queue entries (Core, plugins, acme), a squash merge in a repository without a queue, and with --dispatch a headless reviewer run and its comment; without --live: only the list.",
    "sm queue auto --live --dispatch", "sm queue list; sm why N for a pull request it skipped",
    "Only PRs of auto_queue_authors (default: the owner) are acted on; a public repository needs no approval.\n"
    "Pass --repo OWNER/REPO and --pr N together to consider just that PR.\n"
    "Direct merges pin the head. For queued and direct merges, GitHub enforces its branch rules at merge. The base and local "
    "hold and optional-check observations can change before then.\n"
    "A PR that touches a gate (diff guard flag, CI change) is queued only with an APPROVE comment of `sm review dispatch` for exactly its head. --dispatch starts that\n"
    "review when none is recorded. Release holds, incident holds, and uncertain hold state block ordinary merges in every "
    "repository. Operator holds also apply. An independently reviewed exact Core release candidate uses the separate "
    "release queue path."))
def queue_auto(live: bool = typer.Option(False, "--live", help="Queue and merge; without it only list what would happen."),
               dispatch: bool = typer.Option(False, "--dispatch", help="Start the independent review of a gate-touching PR that has none (needs --live)."),
               repo: str | None = typer.Option(None, "--repo", metavar="OWNER/REPO", help="Limit this pass to one configured repository; requires --pr."),
               pr: int | None = typer.Option(None, "--pr", min=1, metavar="N", help="Limit this pass to one PR; requires --repo.")) -> AutoQueued:
    if (repo is None) != (pr is None):
        raise Refuse("select one PR with --repo OWNER/REPO and --pr N together", "sm queue auto --help", code=2)
    if dispatch and not live:
        raise Refuse("--dispatch starts an agent and posts a comment: add --live", "sm queue auto --live --dispatch", code=2)
    rt = runtime()
    if hold := queue_hold.current(rt):
        return AutoQueued(stopped=hold.describe())
    w = world()
    owner = w.repo.split("/")[0]
    settings = auto_review.settings_of(w)
    names = list(w.settings.get("status_pr_repos") or prtable.DEFAULT_REPOS)
    if repo is not None:
        names = [name for name in names if f"{owner}/{name}".lower() == repo.lower()]
        if len(names) != 1:
            raise Refuse("the selected repository must be configured in status_pr_repos", "sm queue auto --help", code=2)
    try:
        with rt.providers.store.lock(PASS_LOCK) if live else nullcontext():
            return one_pass(rt, w, owner, settings, live, dispatch, names, pr)
    except Busy:
        raise Refuse("another `sm queue auto` pass is running", "sm queue list", code=1) from None


def one_pass(rt: Any, w: Any, owner: str, settings: auto_review.AutoReviewSettings, live: bool, dispatch: bool,
             names: list[str], number: int | None) -> AutoQueued:
    module: Any = queue._queue()  # type: ignore[no-untyped-call]
    queued = {str(i["key"]) for i in queue._load(module)["items"] if i["status"] == "queued"}
    now = datetime.now(UTC)
    budget = [settings.review_max_per_pass]  # headless reviews this pass may still start
    rows: list[AutoRow] = []
    for name in names:
        listed, why = prtable.fetch(w.gh, owner, name, now)
        if why:
            rows.append(AutoRow(repo=name, number=0, action="skipped", why=why))
            continue
        if number is not None:
            listed = [pr for pr in listed if (pr.repo, pr.number) == (name, number)]
            if len(listed) != 1:
                rows.append(AutoRow(repo=name, number=number, action="skipped", why="the selected open PR could not be identified"))
                continue
        for pr in listed:
            rows.append(handle(rt, w, module, owner, pr, (live, dispatch), queued, settings, budget))
    return AutoQueued(rows=[r for r in rows if r.action != "idle"])


def handle(rt: Any, w: Any, module: Any, owner: str, pr: prtable.PrRow, mode: tuple[bool, bool], queued: set[str],
           settings: auto_review.AutoReviewSettings, budget: list[int]) -> AutoRow:
    """One pull request through the rules; an `idle` row is one that merely is not ready yet (it is not listed).
    `mode` is (live, dispatch); `budget[0]` the headless reviews this pass may still start."""
    live, dispatch = mode

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
    data, why = w.gh.try_json(["pr", "view", str(pr.number), "-R", full, "--json", "headRefOid,baseRefOid,baseRefName,files,comments,commits"], timeout=30)
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
            if auto_review.AUTH_NOTE in res.note:
                budget[0] = 0  # no login: the rest of the pass starts no review
            if not verdict:
                return row("waiting", res.note or "review started, no verdict yet")
    if stop := review_gate(verdict, reasons):
        return row("waiting" if verdict != "REJECT" else "skipped", stop)
    if gap := required_gap(w.gh, owner, pr.repo, pr.number, head, base, data.get("baseRefName", "")):
        return row("waiting", gap)
    if words:
        reason = f"review: sm-review comment on head {head[:12]}" if reasons else f"sm queue auto: no gate touched, ready at head {head[:12]}"
        if not live:
            return row("would-queue")
        done = queue._changed("add", ["add", "--reviewed", reason, "--reviewed-head", head, *words], words)  # every entry is bound to the head the checks were read for
        return row("queued" if not done.exit else "skipped", "" if not done.exit else "; ".join(done.errors or done.lines)[:120])
    if held := queue_hold.current(rt):
        return row("waiting", held.describe())
    if held := hold_reason(w, full, pr.number):
        return row("skipped", held)
    if held := auto_arm.freeze_without_candidate(full):
        return row("waiting", held)
    if not live:
        return row("would-merge")
    merged = rt.providers.commands.run(["gh", "pr", "merge", str(pr.number), "-R", full, "--squash", "--match-head-commit", head], timeout=120)
    return row("merged" if merged.rc == 0 else "skipped", "" if merged.rc == 0 else merged.err.strip()[:120])
