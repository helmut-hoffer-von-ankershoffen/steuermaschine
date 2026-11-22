"""`sm review dispatch PR`: an independent reviewer for a gate-touching pull request, started by sm itself.

A PR touches a gate when the diff guard flags it (a raised timeout, a lowered threshold, a new skip, quarantine, ...) or
when it changes CI (Core's CI paths, another repository's workflow files). Such a PR needs a review by an agent that did
not write it. This command starts one headless (`claude -p`, read-only tools), on a model different from the author's
(read from the `Co-Authored-By` trailers of the PR's commits), and posts its verdict as a comment bound to the exact head:

    sm-review head=<40 hex> reviewer-model=<model> author-model=<model>
    VERDICT: APPROVE | REJECT
    <the reviewer's reasons>

`sm queue auto` reads that comment (`recorded_verdict`): a verdict for another head never counts.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, Field

from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime

from .go import world
from .merge_ready import ci_reasons
from .queue_lib import hzlib

SHA = re.compile(r"[0-9a-f]{40}")
MARK = re.compile(r"^sm-review head=([0-9a-f]{40})\b.*$", re.M)
VERDICT = re.compile(r"^VERDICT:\s*(APPROVE|REJECT)\b", re.M)
TRAILER = re.compile(r"^Co-Authored-By:\s*(.+?)\s*(?:<[^>]*>)?\s*$", re.M | re.I)
FAMILIES = ("opus", "sonnet", "haiku", "fable", "gpt", "codex")
READ_ONLY_TOOLS = "Bash(gh pr diff:*),Bash(gh pr view:*),Bash(gh pr checks:*),Read,Grep,Glob"


class AutoReviewSettings(BaseModel):
    """Settings of `sm review dispatch` (mixin of the plugin's settings table)."""

    review_agent_command: list[str] = Field(["claude", "-p"], description="The headless agent that reviews a gate-touching PR; the prompt is the last argument.")
    review_agent_timeout_seconds: int = Field(1500, ge=60, description="How long one headless review may run.")
    review_models: dict[str, str] = Field({"opus": "sonnet", "sonnet": "opus", "haiku": "opus", "": "opus"},
                                          description="Author model family -> reviewer model: always a different one. The empty key is for an author that is not recognized.")


class Dispatched(Model):
    """What `sm review dispatch` found and did for one PR."""

    number: int
    repo: str
    head: str = ""
    needed: bool = False
    reasons: list[str] = []
    author_model: str = ""
    reviewer_model: str = ""
    verdict: str = ""  # APPROVE | REJECT | "" (none recorded or started)
    posted: bool = False
    note: str = ""

    def render(self) -> list[str]:
        if not self.needed:
            return [f"PR #{self.number}: no independent review needed"]
        why = "; ".join(self.reasons[:4])
        if self.posted:
            return [f"PR #{self.number} head {self.head[:12]}: {self.verdict} by a {self.reviewer_model} reviewer (author: {self.author_model or 'unknown'}), posted as a comment"]
        if self.verdict:
            return [f"PR #{self.number} head {self.head[:12]}: review on file: {self.verdict}"]
        return [f"PR #{self.number} head {self.head[:12]}: needs an independent review ({why}); {self.note}"]

    @property
    def exit_code(self) -> int:
        return 1 if self.verdict == "REJECT" else 0


def family(text: str) -> str:
    """The model family named in a trailer or model id (`Claude Sonnet 5.5`, `claude-opus-4`), else ""."""
    low = text.lower()
    return next((name for name in FAMILIES if name in low), "")


def author_family(messages: list[str]) -> str:
    """The family of the agent that wrote the commits: the first recognized `Co-Authored-By` trailer; "" when none names one."""
    for message in messages:
        for who in TRAILER.findall(message):
            if found := family(who):
                return found
    return ""


def reviewer_model(author: str, table: dict[str, str], override: str = "") -> str:
    """A model that differs from the author's. An explicit choice of the same family is refused."""
    if override:
        if author and family(override) == author:
            raise Refuse(f"the reviewer model {override} is the author's family ({author}): an independent review needs another", "sm review dispatch PR --model MODEL", code=2)
        return override
    return table.get(author) or table[""]


def recorded_verdict(comments: list[dict[str, Any]], head: str) -> str:
    """The verdict (APPROVE or REJECT) of the newest `sm-review` comment bound to exactly `head`, else "". A verdict of an
    older head never counts: a changed head needs a new review."""
    found = ""
    for comment in comments:
        body = str(comment.get("body") or "")
        mark = MARK.search(body)
        verdict = VERDICT.search(body)
        if mark and verdict and mark.group(1) == head:
            found = verdict.group(1)
    return found


def needs_review(repo: str, number: int, base: str, head: str, files: list[dict[str, Any]], guard: Any = None) -> list[str]:
    """Why the PR touches a gate (empty: it does not): diff-guard findings and CI changes. An unreadable diff or an unreadable CI
    judgment counts as a gate touch (fail closed)."""
    w = world()
    reasons: list[str] = []
    found = (guard or hzlib.load("diff_guard").check)(repo, str(number))
    if found is None:
        reasons.append("the diff guard could not read the diff")
    else:
        reasons += [f"diff guard: {str(item[1]) if len(item) > 1 else str(item)}"[:100] for item in found[:5]]
    reasons += list(ci_reasons(w, repo, base, head, files))
    return reasons


def brief(repo: str, number: int, head: str, reasons: list[str]) -> str:
    listed = "\n".join(f"- {r}" for r in reasons[:12]) or "- (none printed)"
    return (f"Independent review of PR #{number} in {repo}, head {head}. You did not write it and you cannot change anything.\n"
            f"The PR touches a gate (CI, a threshold, a skip, quarantine or similar). The findings:\n{listed}\n"
            f"Read the diff: gh pr diff {number} -R {repo}. Decide for each finding whether it is JUSTIFIED or hides a red check made to go away "
            "instead of fixed; also look for anything else that weakens a gate (a raised timeout, a lowered floor, a skipped or deleted test, "
            "continue-on-error, secrets in workflows, a changed permission).\n"
            "Answer in under 150 words. The LAST line must be exactly `VERDICT: APPROVE` or `VERDICT: REJECT` followed by one reason.")


def comment_body(head: str, reviewer: str, author: str, answer: str, verdict: str) -> str:
    text = "\n".join(line for line in answer.strip().splitlines() if not VERDICT.match(line)).strip()[:3000]
    return f"sm-review head={head} reviewer-model={reviewer} author-model={author or 'unknown'}\nVERDICT: {verdict}\n{text}\n"


def verdict_of(answer: str) -> str:
    """The reviewer's last VERDICT line; "" when it gave none (nothing is posted then)."""
    found = VERDICT.findall(answer)
    return found[-1] if found else ""


@native("review dispatch", writes=True, help=H(
    "Start an independent headless reviewer for a gate-touching PR and post its verdict on the exact head.",
    "Use when `sm queue auto` or `sm why PR` says a PR needs a review; it runs by itself in the auto-queue.",
    "with --live: one headless agent run (read-only tools) and one comment on the PR; without it: only what would run.",
    "sm review dispatch 931 --live", "sm queue auto --live  (reads the verdict from the comment and queues an approved PR)",
    "A PR needs a review when the diff guard flags it or it changes CI. The reviewer model is another family than the author's\n"
    "(trailers of the commits): Opus for a Sonnet author, Sonnet for an Opus author (setting review_models). --model forces one;\n"
    "the author's own family is refused. A verdict already recorded for this head is reported, nothing is started again."))
def review_dispatch(number: int = typer.Argument(..., metavar="PR", help="The pull request number."),
                    repo: str = typer.Option("", "-R", "--repo", metavar="OWNER/NAME", help="Another repository than Core."),
                    model: str = typer.Option("", "--model", metavar="MODEL", help="Force the reviewer model (never the author's family)."),
                    live: bool = typer.Option(False, "--live", help="Start the reviewer and post its verdict.")) -> Dispatched:
    rt = runtime()
    w = world()
    full = repo or w.repo
    data, why = w.gh.try_json(["pr", "view", str(number), "-R", full, "--json", "headRefOid,baseRefOid,files,comments,commits,state"], timeout=30)
    if why or not isinstance(data, dict):
        raise Refuse(f"gh failed: {why or 'no answer'}", f"sm why {number}", code=1)
    head, base = str(data.get("headRefOid") or ""), str(data.get("baseRefOid") or "")
    if data.get("state") != "OPEN" or not SHA.fullmatch(head) or not SHA.fullmatch(base):
        raise Refuse(f"PR #{number} is not open with an exact head and base", f"sm why {number}", code=1)
    reasons = needs_review(full, number, base, head, list(data.get("files") or []))
    out = Dispatched(number=number, repo=full, head=head, needed=bool(reasons), reasons=reasons)
    if not reasons:
        return out
    out.verdict = recorded_verdict(list(data.get("comments") or []), head)
    if out.verdict:
        return out
    messages = [f"{c.get('messageHeadline', '')}\n{c.get('messageBody', '')}" for c in data.get("commits") or []]
    out.author_model = author_family(messages)
    settings = AutoReviewSettings.model_validate({k: v for k, v in dict(w.settings).items() if k in AutoReviewSettings.model_fields})
    out.reviewer_model = reviewer_model(out.author_model, settings.review_models, model)
    if not live:
        out.note = f"add --live to start a {out.reviewer_model} reviewer"
        return out
    argv = [*settings.review_agent_command, "--model", out.reviewer_model, "--allowedTools", READ_ONLY_TOOLS, brief(full, number, head, reasons)]
    with tempfile.TemporaryDirectory(prefix="sm-review-") as scratch:
        res = rt.providers.commands.run(argv, cwd=scratch, timeout=float(settings.review_agent_timeout_seconds))
    verdict = verdict_of(res.out) if res.rc == 0 else ""
    if not verdict:
        out.note = f"the reviewer gave no verdict (exit {res.rc}); nothing was posted"
        return out
    body = comment_body(head, out.reviewer_model, out.author_model, res.out, verdict)
    with tempfile.TemporaryDirectory(prefix="sm-review-body-") as scratch:
        path = Path(scratch) / "body.md"
        path.write_text(body)
        posted = rt.providers.commands.run(["gh", "pr", "comment", str(number), "-R", full, "--body-file", str(path)], timeout=60)
    if posted.rc:
        out.note = f"the verdict {verdict} could not be posted: {posted.err.strip()[:120]}"
        return out
    out.verdict, out.posted = verdict, True
    return out
