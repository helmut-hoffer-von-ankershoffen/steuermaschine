"""A ready-to-paste independent-reviewer brief for a pull request the diff guard flagged."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from steuermaschine.github import Gh
from steuermaschine.models import Model
from steuermaschine.native import runtime

from ..coord_lib.base import tracker_dir


class ReviewBrief(Model):
    """The brief for the independent reviewer of one flagged pull request; empty when the guard passed."""

    pull_request: int
    repository: str
    flagged: bool
    members: list[int] = []  # member pull requests when this one combines others
    verdict_files: list[str] = []
    text: str

    def render(self) -> list[str]:
        return self.text.splitlines()


def members(body: str | None) -> list[int]:
    return sorted({int(n) for n in re.findall(r"(?:combines|supersedes|member|includes|#)\s*#?(\d{2,5})", body or "", re.I)})


def verdict_files(n: int | str, root: Path) -> list[str]:
    return sorted(str(p) for p in (root / "research").glob(f"review-*{n}*.md"))


def brief(n: int, flagged: str, view: dict[str, Any], files: list[str], repo: str) -> str:
    mem = [m for m in members(view.get("body")) if m != n]
    flags = "\n".join("  " + line for line in flagged.strip().splitlines()) or "  (guard printed nothing)"
    return f"""Independent review of PR #{n} ({view.get('title', '')}) in {repo}. You did not write it.
The diff guard FLAGGED these hunks (a flag may hide a red check made to go away instead of fixed):
{flags}
Read: gh pr diff {n} -R {repo}; for each flagged hunk decide JUSTIFIED or MASKS-A-FAILURE with the reason in one line.
{('Member PRs of this combine (check each flagged hunk against its origin): ' + ', '.join('#' + str(m) for m in mem)) if mem else 'Not a combine.'}
{('Existing verdict files (do not repeat): ' + ', '.join(Path(f).name for f in files)) if files else 'No verdict file yet.'}
Write the verdict to research/review-pr{n}.md (APPROVE or REQUEST-CHANGES, first line), do not push or merge, report in under 80 words."""


def build(pull_request: int, flagged: str, repo: str = "", rt: Any = None) -> ReviewBrief:
    """`flagged` is what the diff guard printed for the pull request; empty means it passed."""
    rt = rt or runtime()
    repo = repo or str((rt.settings or {}).get("repo") or "")
    if not flagged.strip():
        return ReviewBrief(pull_request=pull_request, repository=repo, flagged=False, text=f"PR #{pull_request}: guard PASS, no independent review needed")
    view, _why = Gh(rt.providers.commands, repo, clock=rt.providers.clock).try_json(["pr", "view", str(pull_request), "-R", repo, "--json", "title,body"])
    view = view if isinstance(view, dict) else {}
    files = verdict_files(pull_request, tracker_dir(rt.settings))
    return ReviewBrief(pull_request=pull_request, repository=repo, flagged=True, members=[m for m in members(view.get("body")) if m != pull_request],
                       verdict_files=[Path(f).name for f in files], text=brief(pull_request, flagged, view, files, repo))
