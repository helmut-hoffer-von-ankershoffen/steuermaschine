"""`sm head admit WORKTREE`: one head from a clean worktree to a queued PR, as one resumable engine pipeline.

    check -> protected review (STOP, only for protected paths) -> pr* -> queue*

Core's CI runs the full matrix on every PR, so nothing is admitted first: no packet, no plan slot. One blocking STOP
is kept: a head that changes `.github/`, the release, publish, PyPI or acceptance scripts, `quarantine.toml`, the
coverage config or a `conftest.py` stops until a different agent approves the digest of exactly those paths
(`sm review verdict`). The diff guard of `head pr` stays too (a flagged PR is queued only with --set reviewed=...).
Every other head is queued and merges when CI and Sonar are green on its exact head.

Each step runs the existing native command (`check`, `head pr`, `queue add`) through its review gate; nothing here
reimplements one. The state is kept per head (the exact SHA); a new head is a new run that supersedes the old one.
(* writes outside this machine: only with --live.)
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Any

from steuermaschine import USAGE, verdicts
from steuermaschine.engine import Ctx, Pipeline, Refuse, ReviewStop, Step, Subject, identify_head, need_check
from steuermaschine.github import Gh
from steuermaschine.native import unit

from .. import state as statedir
from .common import need, ok, run

VERB = "admit"
BINDS = ("head",)
INPUTS = {
    "bead": "the bead this head closes (for the PR)",
    "title": "the PR title, at most 72 characters",
    "body_file": "a file with the PR body (default: `Bead BEAD.`)",
    "reviewed": "the written reason of an independent review, when the diff guard flagged the PR",
}
TRAPS = {
    "check": "Fix ALL findings in ONE commit: every correction is a new head and a new run of CI.",
    "protected review": "The reviewer reads the changed protected paths themselves, not the author's summary; any new commit changes the digest.",
    "pr": "One push, the checked commit, under the run lock: never push the head by hand while this run is open.",
    "queue": "A green PR unmerged after 15 minutes is an incident, not a wait: sm queue why PR.",
}


def anchor() -> None:
    """Anchor of the review unit of `sm head admit`: this module and every first-party file it imports."""


def identify(providers: Any, settings: dict[str, Any], worktree: str) -> Subject:
    subject = identify_head(providers, settings, worktree)
    subject.fingerprint = {"head": subject.key}
    return subject


# ----------------------------------------------------------------------------- local check
def check_probe(ctx: Ctx) -> str | None:
    receipt, _why = verdicts.check_receipt(ctx.providers.store, ctx.subject.key)
    return f"`sm check` is green for {ctx.subject.key[:12]}" if receipt is not None else None


def check(ctx: Ctx) -> str:
    wt = ctx.subject.facts["worktree"]
    report = run("check", worktree=Path(wt))
    if getattr(report, "exit_code", 0):
        failed = "; ".join(line.strip() for line in report.render() if line.strip())[:500]
        raise Refuse(f"NOT READY: {failed}", f"fix all of it in one commit, then: sm head admit {wt}")
    return f"READY at {ctx.subject.key[:12]}"


# ----------------------------------------------------------------------------- the blocking review of protected paths
PROTECTED = (".github/*", "release.sh", "*/release.sh", "scripts/*release*", "scripts/*publish*", "scripts/*pypi*", "scripts/*acceptance*",
             "quarantine.toml", "*/quarantine.toml", "conftest.py", "*/conftest.py", ".coveragerc", "*/.coveragerc", "scripts/*coverage*")


def protected_changes(ctx: Ctx) -> list[str]:
    """The paths this head changes against main that need a blocking review. A diff that cannot be read refuses (fail closed)."""
    wt = ctx.subject.facts["worktree"]
    done = ctx.providers.commands.run(["git", "-C", wt, "diff", "--name-only", "-z", "origin/main...HEAD"], timeout=120)
    if done.rc != 0:
        raise Refuse(f"cannot read the changed paths of {wt} against origin/main: {(done.err or done.out).strip()[:200]}", f"git -C {wt} fetch origin main")
    names = [n for n in str(done.out).split("\0") if n]
    found = {n for n in names if any(fnmatch.fnmatchcase(n, pat) for pat in PROTECTED)}
    if "pyproject.toml" in names:  # the coverage config lives in it: only a diff that names coverage counts
        diff = ctx.providers.commands.run(["git", "-C", wt, "diff", "-U0", "origin/main...HEAD", "--", "pyproject.toml"], timeout=120)
        if diff.rc != 0 or "coverage" in str(diff.out).lower():
            found.add("pyproject.toml")
    return sorted(found)


def protected_review(ctx: Ctx) -> tuple[str, str, str]:
    changed = protected_changes(ctx)
    if not changed:
        return "", "", ""  # nothing protected: no stop
    head, wt = ctx.subject.key, ctx.subject.facts["worktree"]
    record = json.dumps({"head": head, "protected": changed}, indent=1, sort_keys=True) + "\n"
    digest = hashlib.sha256(record.encode()).hexdigest()
    command = f"sm check {wt}"
    need_check(ctx.providers.store, digest, head, command)
    path = statedir.path(f"ship/protected/{head[:12]}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record)
    brief = (f"Review the protected paths changed by {ctx.subject.label} at {head[:12]}: {', '.join(changed)}.\n"
             f"Read the diff of exactly these paths (git -C {wt} diff origin/main...HEAD -- PATH). Approve the digest {digest[:12]}... only if they weaken no "
             "gate, test, threshold or publish step. The digest is sha256 of the record: the head and the sorted protected paths.")
    return digest, brief, str(path)


def _repo(ctx: Ctx) -> str:
    repo = str(ctx.settings.get("repo") or "")
    if "/" not in repo:
        raise Refuse("no repository: set repo (OWNER/NAME); a run or commit is never looked up in the current directory's repository",
                     "sm self config", code=USAGE)
    return repo


def _gh(ctx: Ctx) -> Gh:
    return Gh(ctx.providers.commands, repo=_repo(ctx))


# ----------------------------------------------------------------------------- PR and queue
def _pr_of(ctx: Ctx) -> int | None:
    data, _why = _gh(ctx).try_json(["pr", "list", "-R", _repo(ctx), "--head", ctx.subject.facts["branch"], "--state", "open", "--json", "number,headRefOid"])
    for row in data or []:
        if row.get("headRefOid") == ctx.subject.key:
            return int(row["number"])
    return None


def pr_probe(ctx: Ctx) -> str | None:
    number = _pr_of(ctx)
    if number is None:
        return None
    ctx.data["pr"] = number
    return f"PR #{number} is open at this head"


def pr(ctx: Ctx) -> str:
    bead = need(ctx, "bead", "the bead this head closes")
    title = need(ctx, "title", "the PR title")
    body = ctx.inputs.get("body_file")
    done = run("head pr", bead=bead, title=title, body_file=Path(str(body)).expanduser() if body else None, reason=ctx.inputs.get("reviewed") or None,
               worktree=Path(ctx.subject.facts["worktree"]), live=True)
    if done.verdict not in ("done", "needs_review"):
        text = "; ".join(done.lines + done.errors)[:500]
        if done.exit_code == 75:
            from steuermaschine.engine import Wait

            raise Wait(f"sm head pr: {text}", "the push check (main CI or the run lock)")
        raise Refuse(f"sm head pr refused: {text}")
    ctx.data["guard"] = done.verdict
    ctx.data["pr"] = _pr_of(ctx)
    return f"PR #{ctx.data['pr']} open" + (" and queued" if done.verdict == "done" else "; the diff guard flagged it")


def queue_probe(ctx: Ctx) -> str | None:
    number = ctx.data.get("pr")
    if not number:
        return None
    listed = run("queue list", active=False)
    hit = next((e for e in listed.entries if str(e.pr) == str(number)), None)
    return f"PR #{number} is in the queue ({hit.status})" if hit else None


def queue(ctx: Ctx) -> str:
    number = ctx.data.get("pr")
    if not number:
        raise Refuse("no PR is known for this head: the pr step did not record one", f"sm head admit {ctx.subject.facts['worktree']}")
    reviewed = ctx.inputs.get("reviewed")
    if ctx.data.get("guard") == "needs_review" and not reviewed:
        raise Refuse(f"the diff guard flagged PR #{number}: an independent review first, then --set reviewed=\"review: FILE\"", f"sm head guard {number}")
    ok("queue add", pr=[str(number)], bead=ctx.inputs.get("bead") or None, reviewed=reviewed or None, reviewed_head=ctx.subject.key if reviewed else None)
    return f"PR #{number} queued at {ctx.subject.key[:12]}"


def pipeline() -> Pipeline:
    steps = [
        Step("check", check, probe=check_probe, binds=BINDS, trap=TRAPS["check"], changes="the check receipt of this head on this machine (sm check)"),
        ReviewStop("protected review", protected_review, trap=TRAPS["protected review"]),
        Step("pr", pr, probe=pr_probe, external=True, trap=TRAPS["pr"], changes="one push of the checked commit, the PR, the guard (sm head pr --live)",
             plan=lambda ctx: f"sm head pr {ctx.inputs.get('bead', 'BEAD')} TITLE --worktree {ctx.subject.facts['worktree']} --live"),
        Step("queue", queue, probe=queue_probe, external=True, trap=TRAPS["queue"], changes="one entry in the merge queue (sm queue add)",
             plan=lambda ctx: f"sm queue add {ctx.data.get('pr', 'PR')}"),
    ]
    return Pipeline("head", VERB, "Ship one head: local check, push, PR and queue (CI merges on green); resumable.",
                    steps, identify, inputs=INPUTS, when="Use when a head is committed in its own worktree and should ship.", example_arg="~/Code/heinzel-fix",
                    unit=unit("head admit", anchor))
