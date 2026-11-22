"""Which project repository a worktree belongs to, read from its git origin, and the PR key the queue reads for it.

The repositories are the queue library's (`queue_lib/hzlib.SHORT`), the one reviewed list; nothing here names one.
"""

from __future__ import annotations

import re
from typing import Any

from ..queue_lib import hzlib

UNQUEUED = (f"{hzlib.OWNER}/steuermaschine", f"{hzlib.OWNER}/heinzel-management")
"""The repositories `sm ship` opens PRs in that the merge queue does not know: the public tool and the private management
repository. Their PRs merge when CI and the Gate review are green (`sm queue auto` squashes them); there is no queue entry."""


def shippable() -> list[str]:
    """The repositories `sm ship` takes a worktree of: the queue's and the two without a queue."""
    return sorted({*(v for v in hzlib.SHORT.values()), *UNQUEUED})


def queued(repo: str) -> bool:
    """Whether the merge queue knows the repository (it has a PR key); False for the two that merge by `sm queue auto`."""
    return repo.lower() not in (r.lower() for r in UNQUEUED)


def slug_of(url: str) -> str:
    """OWNER/NAME of a git remote URL (https, ssh or scp form)."""
    return re.sub(r"^[a-z+]+://[^/]+/|^[^@/]+@[^:]+:", "", url.strip()).removesuffix("/").removesuffix(".git")


def known_repo(url: str, *, ship: bool = True) -> str | None:
    """The repository that a git remote URL names, or None: with `ship` any `sm ship` takes, else only the queue's
    (what a bare PR number means in a checkout)."""
    path = re.sub(r"^[a-z+]+://[^/]+/|^[^@/]+@[^:]+:", "", url.strip()).removesuffix("/").removesuffix(".git")
    found = {v.lower(): v for v in (shippable() if ship else hzlib.SHORT.values())}
    return found.get(path.lower())


def repo_of_checkout(commands: Any, cwd: str | None = None, *, ship: bool = False) -> str | None:
    """The queue's repository the checkout at `cwd` (default: the current directory) has as origin; with `ship` any repository
    `sm ship` takes. None when it has none or an unknown one."""
    done = commands.run(["git", "remote", "get-url", "origin"], cwd=cwd, timeout=20)
    return known_repo(done.out, ship=ship) if not done.rc else None


def pr_key(repo: str, number: int | str) -> str:
    """The PR key `hzlib.split_key` reads: the bare number for the core repository, else `WORD NUMBER` (`plugins 38`)."""
    if repo.lower() == hzlib.CORE.lower():
        return str(number)
    word = next((w for w, r in hzlib.SHORT.items() if w and r.lower() == repo.lower()), "")
    if not word:
        raise ValueError(f"repository {repo} is not supported by the merge queue")
    return f"{word} {number}"
