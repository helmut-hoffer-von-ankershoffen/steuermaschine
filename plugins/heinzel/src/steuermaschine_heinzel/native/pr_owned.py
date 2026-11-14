"""Which pull requests `sm head pr` opened or adopted on this machine: the ownership record focused admission accepts
for an existing PR (`sm head prepare --pr N`) when the PR has no entry in the hold file.

One small JSON file per PR under `pr-opened/` in the plugin's state directory, written after `sm head pr` read the
PR back as this repository's, on the branch, at the pushed head, into main. Holds stay where they were: an entry in
the hold file still decides first, and a held PR is refused whatever this record says.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .. import state as statedir

FOLDER = "pr-opened"


def record_path(repo: str, pr: int, root: Path | None = None) -> Path:
    """The record's file; `root`: a state directory other than the plugin's (the admission runtime's)."""
    base = statedir.path(FOLDER) if root is None else root / FOLDER
    return base / f"{re.sub(r'[^a-z0-9._-]+', '--', repo.lower())}-{int(pr)}.json"


def record(repo: str, pr: int, branch: str, head: str, how: str) -> Path:
    """Note that `sm head pr` opened (`created`) or adopted (`adopted`) PR `pr` of `repo` for `branch` at `head`."""
    path = record_path(repo, pr)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"repo": repo, "pr": int(pr), "branch": branch, "head": head, "how": how}, sort_keys=True) + "\n")
    tmp.replace(path)
    return path


def owned(repo: str, pr: int, root: Path | None = None) -> dict[str, Any] | None:
    """The record of PR `pr` of `repo`, or None: none written, unreadable, or naming another PR."""
    try:
        data = json.loads(record_path(repo, pr, root).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or str(data.get("repo", "")).lower() != repo.lower() or data.get("pr") != int(pr):
        return None
    return data
