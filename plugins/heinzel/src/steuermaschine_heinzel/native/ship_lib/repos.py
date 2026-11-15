"""Which project repository a worktree belongs to, read from its git origin, and the PR key the queue reads for it.

The repositories are the queue library's (`queue_lib/hzlib.SHORT`), the one reviewed list; nothing here names one.
"""

from __future__ import annotations

import re
from typing import Any

from ..queue_lib import hzlib


def known_repo(url: str) -> str | None:
    """The project repository (a value of SHORT) that a git remote URL names, or None."""
    path = re.sub(r"^[a-z+]+://[^/]+/|^[^@/]+@[^:]+:", "", url.strip()).removesuffix("/").removesuffix(".git")
    found = {v.lower(): v for v in hzlib.SHORT.values()}
    return found.get(path.lower())


def repo_of_checkout(commands: Any, cwd: str | None = None) -> str | None:
    """The project repository the checkout at `cwd` (default: the current directory) has as origin; None when it has none or an unknown one."""
    done = commands.run(["git", "remote", "get-url", "origin"], cwd=cwd, timeout=20)
    return known_repo(done.out) if not done.rc else None


def pr_key(repo: str, number: int | str) -> str:
    """The PR key `hzlib.split_key` reads: the bare number for the core repository, else `WORD NUMBER` (`plugins 38`)."""
    word = next((w for w, r in hzlib.SHORT.items() if w and r.lower() == repo.lower()), "")
    return f"{word} {number}" if word and repo.lower() != hzlib.CORE.lower() else str(number)
