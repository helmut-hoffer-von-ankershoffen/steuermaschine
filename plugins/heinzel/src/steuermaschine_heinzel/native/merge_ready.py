"""head merge-ready N: go or no-go for a CLEAN pull request while the merge queue is pinned. Read-only.

It only judges. Merging the checked head is not part of this command (that is a write, owned by the queue slice).
"""

from __future__ import annotations

import glob
import re
from pathlib import Path
from typing import Any

import typer

from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native

from .ci_triage import research_dir
from .go import world
from .go_lib import legacy, probes

ACTIONS = "pr-actions.toml"
WORKFLOWS = ".github/workflows/"  # a repository without a release policy: any workflow change is a CI change (GitHub's layout)


class MergeReady(Model):
    """Go or no-go for one pull request, with the head the checks were read for."""

    number: int
    repo: str
    go: bool
    head: str = ""  # the checked head: a merge must be bound to exactly this commit
    reasons: list[str] = []
    read_only: bool = True

    def render(self) -> list[str]:
        return [f"GO #{self.number}"] if self.go else [f"NO-GO #{self.number}: " + "; ".join(self.reasons)]

    @property
    def exit_code(self) -> int:
        return 0 if self.go else 1


def short_names(w: probes.World) -> dict[str, str]:
    """Key prefix of the action table -> OWNER/NAME."""
    owner = w.repo.split("/")[0]
    out = {"": w.repo, "core": w.repo}
    out.update({prefix.strip(): f"{owner}/{name}" for name, prefix in dict(w.settings.get("status_repos") or {}).items() if prefix.strip()})
    return out


def split_key(tokens: list[str] | str, short: dict[str, str]) -> tuple[str, str, str]:
    """`393`, `#393`, `plugins 38`, `plugins #38` -> (repo, number, key as the action table writes it)."""
    words = tokens.split() if isinstance(tokens, str) else list(tokens)
    prefix = words[0] if len(words) > 1 else ""
    number = words[-1].lstrip("#")
    if prefix not in short or not number.isdigit():
        raise ValueError(f"not a PR: {' '.join(words)}")
    return short[prefix], number, f"{prefix} #{number}" if prefix else number


def hold_refusal(pr: int, repo: str, actions_file: Path | None, short: dict[str, str]) -> str | None:
    try:
        if actions_file is None:
            raise OSError("no action table configured")
        actions = legacy.pr_actions(actions_file)
    except (OSError, ValueError):
        return f"cannot read holds from {ACTIONS}"
    # gh accepts an optional host prefix and GitHub repository names are case-insensitive.
    repo = "/".join(repo.lower().rsplit("/", 2)[-2:]).removesuffix(".git")
    for key, action in actions.items():
        hold = action.get("hold", False) if isinstance(action, dict) else None
        if hold is False:
            continue
        try:
            action_repo, number, _ = split_key(key, short)
        except (ValueError, IndexError):
            return f"unrecognized hold key in {ACTIONS}: {key}"
        if action_repo.lower() == repo and int(number) == pr:
            if hold is True:
                return f"held in {ACTIONS}: {key}"
            return f"invalid hold entry in {ACTIONS}: {key}"
    return None


def required_contexts(w: probes.World, repo: str, base: str) -> tuple[frozenset[str] | None, str]:
    """The required checks of `repo`: its release policy at the PR's base, else its branch rules (None: unreadable)."""
    from .queue_lib import core_policy, release_candidate

    def get(path: str) -> Any:
        data, why = w.gh.try_json(["api", f"repos/{repo}/{path}"], timeout=30)
        if why:
            raise release_candidate.CandidateError(why)
        return data

    try:
        return core_policy.at_commit_api(get, base).required_contexts, ""
    except (release_candidate.CandidateError, ValueError) as exc:
        if repo == w.repo:
            return None, f"no reviewed release policy at base {base[:12]}: {exc}"
    rules, why = w.gh.try_json(["api", f"repos/{repo}/rules/branches/main"], timeout=30)
    if why or not isinstance(rules, list):
        return None, f"no release policy and no readable branch rules: {why or 'no answer'}"
    found = {str(c.get("context")) for r in rules if isinstance(r, dict) and r.get("type") == "required_status_checks"
             for c in (r.get("parameters") or {}).get("required_status_checks") or [] if isinstance(c, dict)}
    return frozenset(found), ""


def ci_reasons(w: probes.World, repo: str, base: str, head: str, files: list[dict[str, Any]]) -> tuple[str, ...]:
    """Why the PR changes CI: Core's ci_changes on the local Core clone (the base's not-CI paths); another repository by
    its workflow files. No clone of Core: a reason (fail closed)."""
    from .queue_lib import core_policy

    if repo != w.repo:
        return tuple(f"{f['path']}: a workflow file" for f in files if str(f.get("path", "")).startswith(WORKFLOWS))
    if not w.core:
        return ("no local Core clone (setting core_repo_dir) to judge the CI paths",)
    return core_policy.ci_changes(w.core, base, head)


def check(d: dict[str, Any], pr: int, repo: str, research: Path | None, actions_file: Path | None, short: dict[str, str],
          required: frozenset[str] | None = None, ci: tuple[str, ...] = (), why: str = "") -> tuple[list[str], str]:
    """Return refusal reasons and the head observed with the checks."""
    bad = []
    head = d.get("headRefOid")
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
        bad.append("missing or invalid checked head")
    if d["state"] != "OPEN":
        bad.append("state " + d["state"])
    if d["mergeStateStatus"] != "CLEAN":
        bad.append("mergeState " + d["mergeStateStatus"])
    rolled = d.get("statusCheckRollup") or []
    if required is None:
        bad.append("required checks unknown: " + (why or "no policy"))
    for req in sorted(required or ()):
        hits = [c for c in rolled if (c.get("name") or c.get("context") or "") == req]
        ok = hits and all((c.get("conclusion") or c.get("state")) in ("SUCCESS", "SKIPPED", "NEUTRAL") for c in hits)
        if not ok:
            bad.append("required check not green: " + req)
    if ci:
        bad.append("touches CI: " + "; ".join(ci[:5]) + (f"; and {len(ci) - 5} more" if len(ci) > 5 else ""))
    has_file = glob.glob(str(research / f"review-{pr}-*.md")) if research else []
    has_comment = any(any(w in (c.get("body") or "").lower() for w in ("verdict", "ship"))
                      for c in d.get("comments") or [])
    if not (has_file or has_comment):
        bad.append("no review verdict on file")
    if hold := hold_refusal(pr, repo, actions_file, short):
        bad.append(hold)
    return bad, head if isinstance(head, str) else ""


@native("head merge-ready", writes=False, replaces=("hz-merge-ready",), help=H(
    "Go or no-go for one CLEAN pull request while the merge queue is pinned.",
    "Use when the queue is pinned and a reviewed, green pull request must be judged before a normal squash merge.",
    "nothing (read-only: one GitHub read, the action table, the review files). It never merges.",
    "sm head merge-ready 795", "on GO: the queue's own merge of exactly the checked head; on NO-GO: sm ci why 795",
    "GO needs: state OPEN and CLEAN, every required check of Core's release policy at the base green (another repository:\n"
    "its own policy or branch rules), no CI change (Core: ci_changes by the base's not-CI paths; another repository: no\n"
    "workflow file), a review verdict on file (a review file for the number, or a comment with a verdict), and no hold\n"
    "in the action table. Anything unreadable is a NO-GO. The JSON document carries the checked head."))
def merge_ready(number: int = typer.Argument(..., metavar="NUMBER", help="The pull request number."),
                repo: str = typer.Option("", "--repo", metavar="OWNER/NAME", help="Another repository than Core.")) -> MergeReady:
    w = world()
    full = repo or w.repo
    data, why = w.gh.try_json(["pr", "view", str(number), "-R", full, "--json",
                               "mergeStateStatus,statusCheckRollup,files,comments,state,headRefOid,baseRefOid"], timeout=30)
    if why or not isinstance(data, dict):
        return MergeReady(number=number, repo=full, go=False, reasons=["gh failed: " + (why or "no answer")])
    actions = w.settings.get("go_pr_actions_file")
    base, head = str(data.get("baseRefOid") or ""), str(data.get("headRefOid") or "")
    if re.fullmatch(r"[0-9a-f]{40}", base) and re.fullmatch(r"[0-9a-f]{40}", head):
        required, why = required_contexts(w, full, base)
        ci = ci_reasons(w, full, base, head, list(data.get("files") or []))
    else:
        required, why, ci = None, "no exact base commit", ("no exact base and head commits",)
    bad, head = check(data, number, full, research_dir(w), Path(str(actions)).expanduser() if actions else None, short_names(w),
                      required, ci, why)
    return MergeReady(number=number, repo=full, go=not bad, head=head, reasons=bad)
