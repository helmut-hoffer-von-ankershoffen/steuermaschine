"""The texts of a pull request, checked before they are published: the title against the commit-subject rule, title
and body against the confidential terms.

    problems = check(commands, worktree, title, body, terms_file)        # [] = publish; raises Unchecked = do not

A problem line names the rule and the fix. It never repeats the title or the body and never a term: the caller
prints the lines, and they end up in logs. The terms are matched by the head's own checker
(`scripts/check_confidentiality.py`), which names a finding by its line and the term's number; this module reads
neither the terms nor the checker's words, only the line numbers. The texts reach the checker as files, not as
arguments, so they are in no process list.

It refuses (raises `Unchecked`) rather than pass when the answer is not known: the worktree has no checker, a
configured terms file cannot be read, or the checker ended without a verdict.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from steuermaschine.config import child_environment

from . import ci_meta

CHECKER = Path("scripts") / "check_confidentiality.py"
FINDING = re.compile(r":(\d+): confidential term #\d+\s*$", re.M)
NO_TERMS = re.compile(r"no (confidential )?terms configured", re.I)


class Unchecked(Exception):
    """The texts could not be checked. Nothing may be published on the strength of this call."""


def title_problems(title: str, worktree: Path) -> list[str]:
    """The commit-subject rule for a title (conventional by the head's own pattern, at most 72 characters), without
    repeating the title."""
    return [problem.replace(f": '{title}' -> ", " -> ", 1) for problem in ci_meta.check_subject("PR title", title, ci_meta.pattern(worktree))]


def term_lines(commands: Any, worktree: Path, text: str, terms_file: Path | None) -> list[int]:
    """The lines of `text` on which the head's checker finds a confidential term. Raises `Unchecked` without a verdict."""
    env = child_environment() | {ci_meta.TERMS_ENV: str(terms_file)} if terms_file else None
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "text"
        path.write_text(text, encoding="utf-8")
        done = commands.run(["uv", "run", "--quiet", "--script", str(worktree / CHECKER), *(["--require"] if terms_file else []), "--commit-msg", str(path)],
                            cwd=str(worktree), env=env, timeout=120)
    said = f"{done.out}\n{done.err}"
    lines = sorted({int(number) for number in FINDING.findall(said)})
    if done.rc == 0 and not lines and not NO_TERMS.search(said):
        return []
    if lines:
        return lines
    if NO_TERMS.search(said):
        raise Unchecked("no confidential terms are loaded: " + (f"the terms file {terms_file} has no term" if terms_file else "set preflight_terms_file in [heinzel]"))
    raise Unchecked(f"the head's confidential-terms checker ended with exit {done.rc} and no verdict (run it by hand in {worktree}: uv run --script {CHECKER} --text x)")


def meta_check(commands: Any, worktree: Path, subjects: list[str], title: str | None, branch: str | None, pr: str | None, repo: str = "",
               terms_file: Path | None = None) -> list[str]:
    """The rules of `ci_meta.evaluate` (subjects, PR title, branch name, confidential terms), with lines that name
    where a text is ("commit 2 subject", "PR title", "branch name") and never the text. The terms are checked in
    one call of the head's checker on a file, not on argv. Raises `Unchecked` when the terms could not be checked."""
    if not (worktree / CHECKER).is_file():
        raise Unchecked(f"{worktree} has no {CHECKER}: the confidential terms cannot be checked there")
    pat = ci_meta.pattern(worktree)
    named = [(f"commit {number} subject (1 = newest)", subject) for number, subject in enumerate(subjects, 1)]
    problems: list[str] = []
    for what, text in named:
        problems += [p.replace(f": '{text}' -> ", " -> ", 1) for p in ci_meta.check_subject(what, text, pat)]
    if title is not None:
        named.append(("PR title", title))
        hint = f" -> edit with `gh pr edit {pr} --title ...`, THEN `{ci_meta.reopen(pr, repo)}` (reruns use the old event payload)" if pr else ""
        problems += [p.replace(f": '{title}' -> ", " -> ", 1) + hint for p in ci_meta.check_subject("PR title", title, pat)]
    if branch:
        named.append(("branch name", branch))
        if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*", branch):
            problems.append("branch name -> lowercase letters, digits, - _ . / only")
    where: list[str] = []  # the label of each line of the text the checker sees
    for what, text in named:
        where += [what] * max(1, len(text.splitlines()))
    text = "\n".join(line for _, t in named for line in (t.splitlines() or [""]))
    found = term_lines(commands, worktree, text, terms_file) if text.strip() else []
    for what in dict.fromkeys(where[line - 1] for line in found if 0 < line <= len(where)):
        problems.append(f"confidential: the {what} contains a confidential term -> reword it (the term is not shown)")
    if found and not any(0 < line <= len(where) for line in found):
        problems.append("confidential: a commit subject, the PR title or the branch name contains a confidential term -> reword it (the term is not shown)")
    return problems


def check(commands: Any, worktree: Path, title: str, body: str, terms_file: Path | None = None) -> list[str]:
    """Every problem of a title and a body, each with its fix; [] when both may be published."""
    if not (worktree / CHECKER).is_file():
        raise Unchecked(f"{worktree} has no {CHECKER}: the confidential terms cannot be checked there")
    if terms_file is not None and not (terms_file.is_file() and os.access(terms_file, os.R_OK)):
        raise Unchecked(f"the configured terms file {terms_file} cannot be read (setting preflight_terms_file)")
    problems = title_problems(title, worktree)
    for what, text in (("PR title", title), ("PR body", body)):
        problems += [f"{what}: contains a confidential term (line {line}) -> reword that line" for line in (term_lines(commands, worktree, text, terms_file) if text.strip() else [])]
    return problems
