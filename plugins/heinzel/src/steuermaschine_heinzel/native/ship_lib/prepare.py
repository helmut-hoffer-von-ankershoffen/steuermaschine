"""Everything `sm ship` needs, found BEFORE the first check: no round trip after a green check.

The old route asked for the bead and the title only after `sm check` had run, refused a branch that was behind main
only after the check too, and did not know the public repository at all. Each of those cost the builder a whole check
and a second `sm ship`. This module runs first and does three things:

  merge     a branch behind origin/main gets `origin/main` merged in by sm itself, with a conventional subject
            (`chore: merge origin/main`); a conflict is aborted, named, and ends the run before anything is checked.
            `sm check` measures from the merge-base, which is then the tip of main: only what the branch itself
            changes is checked again, never what the merge brought in
  derive    the repository from the origin remote (any of the four, `repos.known_repo`); the bead from `--set`, then a
            `Bead:` (or `Closes:`/`Refs:`) trailer of a commit of the branch, then a bead id in the branch name, else
            the plain `none`; the title from `--set`, else the subject of the newest commit of the branch that is a
            conventional subject of at most 72 characters
  refuse    what cannot be derived (no title, an origin that is none of the four, a work branch that is main, a
            dirty tree, a conflict) is listed in ONE refusal, each with the exact `--set` or command that fixes it

A command that fails (no network, a scripted machine) leaves its part out and never refuses by itself: the steps
after this one check the same things and say so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from steuermaschine.engine import Refuse

from . import repos

TITLE_MAX = 72
CONVENTIONAL = re.compile(r"(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([^)\s]+\))?!?: \S.*")
TRAILER = re.compile(r"^(?:bead|closes|refs)\s*:\s*([A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*)\s*$", re.IGNORECASE | re.MULTILINE)
BRANCH_BEAD = re.compile(r"(?:^|[/_-])(?:bead|bd)[-_/]([a-z0-9]{3,})|(heinzel-management-[a-z0-9]{3,})", re.IGNORECASE)
MERGE_SUBJECT = "chore: merge origin/main"
NOT_WORK = ("main", "master", "HEAD", "")
RECORD = "\x1e"
FIELD = "\x1f"


@dataclass
class Plan:
    """What the preparation found: the repository, the inputs it derived (name -> value, with where each came from)."""

    repo: str = ""
    branch: str = ""
    merged: bool = False
    inputs: dict[str, str] = field(default_factory=dict)
    derived: list[str] = field(default_factory=list)


def git(commands: Any, worktree: str, *args: str, timeout: float = 120) -> tuple[int, str]:
    done = commands.run(["git", "-C", worktree, *args], timeout=timeout)
    return int(done.rc), str(done.out).strip()


def merge_main(commands: Any, worktree: str) -> tuple[bool, str]:
    """(merged, problem): bring origin/main into a branch that is behind it. Only a definite answer acts; a clean tree is needed."""
    if git(commands, worktree, "fetch", "--quiet", "origin", "main")[0]:
        return False, ""  # offline or scripted: the push step fetches again and says so
    if git(commands, worktree, "merge-base", "--is-ancestor", "origin/main", "HEAD")[0] != 1:
        return False, ""  # contained already (0), or not answerable
    rc, dirty = git(commands, worktree, "status", "--porcelain")
    if rc == 0 and dirty:
        return False, f"the branch is behind origin/main and the tree has uncommitted changes ({len(dirty.splitlines())} paths): commit or remove them, sm merges main itself"
    rc, out = git(commands, worktree, "merge", "--no-edit", "-m", MERGE_SUBJECT, "origin/main")
    if rc == 0:
        return True, ""
    _, conflicted = git(commands, worktree, "diff", "--name-only", "--diff-filter=U")
    git(commands, worktree, "merge", "--abort")
    names = ", ".join(conflicted.splitlines()[:6]) or (out.splitlines()[-1][:120] if out else "unknown")
    return False, f"merging origin/main conflicts in: {names}; resolve it by hand (git merge origin/main, fix, commit) and run sm ship again. The merge was aborted, the tree is as it was"


def commits(commands: Any, worktree: str) -> list[tuple[str, str]]:
    """(subject, body) of the commits of the branch over origin/main, newest first, without merges; [] when unreadable."""
    rc, out = git(commands, worktree, "log", "--no-merges", f"--format=%s{FIELD}%b{RECORD}", "origin/main..HEAD")
    if rc:
        return []
    rows = [row.strip("\n") for row in out.split(RECORD) if row.strip()]
    return [(subject.strip(), body) for row in rows for subject, _, body in [row.partition(FIELD)]]


def bead_of(rows: list[tuple[str, str]], branch: str) -> tuple[str, str]:
    """(bead, where it came from) from a trailer of the branch's commits, then the branch name; ("", "") when there is none."""
    for _subject, body in rows:
        if (found := TRAILER.search(body)) and not found.group(1).isdigit():
            return found.group(1), "a trailer of the commits"
    if found := BRANCH_BEAD.search(branch):
        return next(g for g in found.groups() if g), "the branch name"
    return "", ""


def title_of(rows: list[tuple[str, str]]) -> str:
    return next((subject for subject, _ in rows if CONVENTIONAL.fullmatch(subject) and len(subject) <= TITLE_MAX), "")


def prepare(commands: Any, worktree: str, given: dict[str, str], *, known: dict[str, str] | None = None, configured: str = "") -> Plan:
    """Merge main when behind, derive repository, bead and title; refuse once, at the start, for what is missing.
    `given`: the inputs of this call (--set); `known`: the ones an earlier run of this head remembers; `configured`: the repository of the setting `repo`, also one of ours."""
    known = known or {}
    plan, problems = Plan(), []
    rc, url = git(commands, worktree, "remote", "get-url", "origin")
    if rc == 0 and url:
        plan.repo = repos.known_repo(url) or (configured if configured and repos.slug_of(url).lower() == configured.lower() else "")
        if not plan.repo:
            problems.append(f"the origin of this worktree ({url.rsplit('/', 1)[-1].removesuffix('.git')}) is none of the repositories sm ships "
                            f"({', '.join(sorted({*repos.shippable(), *([configured] if configured else [])}))}); run it in a worktree of one of them")
    rc, branch = git(commands, worktree, "rev-parse", "--abbrev-ref", "HEAD")
    plan.branch = branch if rc == 0 else ""
    if rc == 0 and branch in NOT_WORK:
        problems.append(f"the branch is '{branch}', not a work branch: git switch -c TYPE/NAME, commit there, and run sm ship again")
    merged, why = merge_main(commands, worktree)
    plan.merged = merged
    if merged:
        plan.derived.append("merged origin/main into the branch (it was behind)")
    if why:
        problems.append(why)
    rows = commits(commands, worktree)
    for name in ("bead", "title"):
        value = given.get(name) or known.get(name) or ""
        if value:
            plan.inputs[name] = value
    if "bead" not in plan.inputs:
        bead, where = bead_of(rows, plan.branch)
        plan.inputs["bead"] = bead or "none"
        plan.derived.append(f"bead={bead} (from {where})" if bead else "bead=none (no Bead: trailer, no bead id in the branch name; --set bead=ID names one)")
    if "title" not in plan.inputs and rows:
        if title := title_of(rows):
            plan.inputs["title"] = title
            plan.derived.append(f"title={title!r} (the newest conventional commit subject)")
        else:
            problems.append("no title: no commit of the branch has a conventional subject of at most 72 characters; "
                            f"sm ship {worktree} --set title=\"type(scope): subject\"")
    if (title := plan.inputs.get("title", "")) and len(title) > TITLE_MAX:
        problems.append(f"the title is {len(title)} characters, the limit is {TITLE_MAX}: sm ship {worktree} --set title=\"type(scope): shorter subject\"")
    if problems:
        text = "sm ship stopped before any check; nothing was pushed:\n" + "\n".join(f"  {n}. {p}" for n, p in enumerate(problems, 1))
        raise Refuse(text, f"sm ship {worktree}", code=2)
    return plan
