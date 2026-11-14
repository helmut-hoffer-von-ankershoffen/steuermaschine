"""Fail a release cut whose notes drop a combine's members (0.9.39 listed #770 and lost #732, #737, #742, #745, #746, #757)."""

import re
import subprocess
from . import edge  # moved: records, processes and what used to be separate scripts

MEMBERS = re.compile(r"^(?:Combines #(\d+)\b.*|Merged via this PR:\s*(.*))$", re.M)


def member_numbers(body: str) -> set[str]:
    """Every member PR number a squash body names, by either form."""
    found: set[str] = set()
    for single, listed in MEMBERS.findall(body or ""):
        found.update([single] if single else re.findall(r"#(\d+)", listed))
    return found


def top_section(changelog: str) -> str:
    """The newest release section of CHANGELOG.md."""
    parts = re.split(r"^## ", changelog, flags=re.M)
    return parts[1] if len(parts) > 1 else changelog


def missing(commit_bodies: list[str], changelog: str) -> list[str]:
    """Member PR numbers named in the commit bodies but absent from the newest changelog section."""
    section = top_section(changelog)
    wanted = set().union(*(member_numbers(b) for b in commit_bodies)) if commit_bodies else set()
    return sorted((n for n in wanted if not re.search(rf"#{n}\b", section)), key=int)


def audit(work: str, base: str) -> list[str]:
    """Check the release checkout `work` against the commits since the previous tag up to `base`."""
    def git(*args: str) -> str:
        done = edge.run(["git", "-C", work, *args], capture_output=True, text=True, check=False)
        return done.stdout if done.returncode == 0 else ""
    last = git("describe", "--tags", "--abbrev=0", "--match", "v*", base).strip()
    bodies = [b for b in git("log", "--format=%B%x00", f"{last}..{base}" if last else base).split("\x00") if b.strip()]
    try:
        with open(f"{work}/CHANGELOG.md", encoding="utf-8") as fh:
            changelog = fh.read()
    except OSError:
        return ["CHANGELOG.md unreadable"]
    return missing(bodies, changelog)
