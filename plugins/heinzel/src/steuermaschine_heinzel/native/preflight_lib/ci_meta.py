"""The commit-meta check: what the CI step 'Conventional commits' (`make commits`) checks, run BEFORE a push or PR open.

Checks commit subjects since the base (conventional per the core's pyproject schema_pattern, <= 72 chars), the PR title
(same rules), the branch name and the confidential terms (the core's scripts/check_confidentiality.py). Each problem
names its exact fix. A title edit is NOT seen by a rerun (stale event payload): close and reopen the PR.

Moved from the former commit-meta tool. The rules (`pattern`, `check_subject`, `evaluate`) are unchanged; processes
go through the `Commands` provider, and the core clone, the repository, the base and the terms file are parameters
(the worktree of the call and settings). The confidential terms live in a file OUTSIDE every repository: this module
never reads it, it hands the path to the core's own checker, whose findings name a term by number only.
"""
from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from steuermaschine.config import child_environment

MAX = 72
FALLBACK = r"(feat|fix|perf|refactor|docs|test|build|ci|chore|style|revert)(\([a-z-]+\))?!?: [^\s].*"
TERMS_ENV = "CONFIDENTIAL_TERMS_FILE"  # how the core's checker is told where the terms are


def reopen(pr: str, repo: str = "") -> str:
    where = f" --repo {repo}" if repo else ""
    return f"gh pr close {pr}{where} && gh pr reopen {pr}{where}"


def pattern(core: Path) -> re.Pattern[str]:
    try:
        raw = tomllib.loads((core / "pyproject.toml").read_text())["tool"]["commitizen"]["customize"]["schema_pattern"]
        return re.compile(raw)
    except (OSError, KeyError, re.error):
        return re.compile(FALLBACK)


def check_subject(what: str, subject: str, pat: re.Pattern[str]) -> list[str]:
    out: list[str] = []
    if re.match(r"(Merge |Revert \")", subject):  # commitizen skips merge/revert commits too
        return out
    if len(subject) > MAX:
        out.append(f"{what}: {len(subject)} chars > {MAX}: '{subject}' -> shorten the summary to <= {MAX - 7} chars (a squash adds ' (#N)')")
    if not pat.match(subject):
        out.append(f"{what}: not conventional: '{subject}' -> 'type(scope): summary' with the types/scopes in core pyproject [tool.commitizen.customize]")
    return out


def confidential(texts: list[str], core: Path, commands: Any, terms_file: Path | None = None) -> list[str]:
    """One finding per text the core's checker refuses. `terms_file`: the terms, outside the repository."""
    script = core / "scripts" / "check_confidentiality.py"
    if not script.exists():
        return ["confidential: scripts/check_confidentiality.py missing, not checked"]
    env = child_environment() | {TERMS_ENV: str(terms_file)} if terms_file else None  # the child reads the file; we never do
    out = []
    for t in texts:
        r = commands.run(["uv", "run", "--script", str(script), "--text", t], cwd=str(core), env=env)
        if r.rc != 0:
            out.append(f"confidential: '{t[:60]}' -> {(r.out + r.err).strip()[:160]} -> reword")
    return out


def run(commands: Any, cmd: list[str], cwd: Path | None = None) -> str:
    return str(commands.run(cmd, cwd=str(cwd) if cwd else None).out).strip()


def evaluate(subjects: list[str], title: str | None, branch: str | None, pr: str | None, pat: re.Pattern[str],
             terms_check: Callable[[list[str]], list[str]], repo: str = "") -> list[str]:
    problems: list[str] = []
    for s in subjects:
        problems += check_subject("commit", s, pat)
    if title is not None:
        t = check_subject("PR title", title, pat)
        if pr:
            t = [x + f" -> edit with `gh pr edit {pr} --title ...`, THEN `{reopen(pr, repo)}` (reruns use the old event payload)" for x in t]
        problems += t
    if branch and not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*", branch):
        problems.append(f"branch: '{branch}' -> lowercase letters, digits, - _ . / only")
    problems += terms_check(subjects + [x for x in (title, branch) if x])
    return problems


def gather(core: Path, commands: Any, gh: Any, repo: str, ref: str = "", pr: str | None = None, base: str = "origin/main") -> tuple[list[str], str | None, str]:
    """-> (commit subjects, PR title or None, branch). `pr`: read title, branch and commits of that pull request
    through the GitHub client (`gh`); else the commits of `ref` (default: the worktree's branch) since the base."""
    title = None
    if pr:
        d = gh.json(["pr", "view", pr, "--repo", repo, "--json", "title,headRefName,baseRefName,commits"])
        title, branch = d["title"], d["headRefName"]
        subjects = [c["messageHeadline"] for c in d["commits"]]
    else:
        branch = ref or run(commands, ["git", "rev-parse", "--abbrev-ref", "HEAD"], core)
        if branch == "HEAD":  # detached: the commit stands for the branch, as the head preflight passes it
            branch = run(commands, ["git", "rev-parse", "HEAD"], core)
        since = run(commands, ["git", "merge-base", base, branch], core) or base
        subjects = [s for s in run(commands, ["git", "log", "--format=%s", f"{since}..{branch}"], core).splitlines() if s]
    return subjects, title, branch


def check(core: Path, commands: Any, gh: Any, repo: str, ref: str = "", pr: str | None = None, base: str = "origin/main",
          terms_file: Path | None = None) -> dict[str, Any]:
    """The whole check of one worktree (or one pull request): {"problems", "commits", "title", "branch"}."""
    subjects, title, branch = gather(core, commands, gh, repo, ref, pr, base)
    problems = evaluate(subjects, title, branch, pr, pattern(core), lambda texts: confidential(texts, core, commands, terms_file), repo)
    return {"problems": problems, "commits": len(subjects), "title": title, "branch": branch}
