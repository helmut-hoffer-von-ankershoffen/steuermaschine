"""One writer per branch: which agent owns a branch (and its worktree), and the refusal for any other agent.

Two builders committed to the same branch on 2026-11-14 (bead nn5m). The tool now records the owner of a branch and
refuses a second agent's write step on it, naming the owner.

The agent is named by the environment variable `SM_AGENT` (the coordinator sets it when it starts a builder; a
command may also be given `--agent`). Without a name the tool cannot tell agents apart and does not refuse (it says
so in `sm branch check`); the record is never guessed. The record is one small JSON file per branch of a repository
under `branch-owners/` in the plugin's state directory, keyed by the repository's git common directory (shared by all
its worktrees) and the branch name, so a second worktree of the same branch is the same branch.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from steuermaschine.engine import Refuse

from .. import state as statedir

FOLDER = "branch-owners"
ENV = "SM_AGENT"
NOT_A_WORK_BRANCH = ("main", "master", "HEAD", "")


def agent_name(given: str | None = None) -> str:
    """The acting agent: `given`, else the environment's SM_AGENT; "" when neither names one."""
    return (given or os.environ.get(ENV, "")).strip()


def where(commands: Any, worktree: Path) -> tuple[str, str]:
    """(git common directory, branch) of the worktree. Refuses when it is not a git worktree."""
    def read(*args: str) -> str:
        done = commands.run(["git", "-C", str(worktree), *args], timeout=30)
        if done.rc:
            raise Refuse(f"{worktree} is not a git worktree: {(done.err or done.out).strip()[:200]}", "sm branch check --help", code=2)
        return done.out.strip()

    common = Path(read("rev-parse", "--git-common-dir"))
    common = common if common.is_absolute() else Path(worktree) / common
    return str(common.resolve()), read("rev-parse", "--abbrev-ref", "HEAD")


def record_path(common: str, branch: str, root: Path | None = None) -> Path:
    name = hashlib.sha256(f"{common}\0{branch}".encode()).hexdigest()[:20]
    return (statedir.path(FOLDER) if root is None else root / FOLDER) / f"{name}.json"


def owner_of(common: str, branch: str, root: Path | None = None) -> dict[str, Any] | None:
    """The record of the branch, or None: none written, unreadable, or naming another branch."""
    try:
        data = json.loads(record_path(common, branch, root).read_text())
    except (OSError, ValueError):
        return None
    ok = isinstance(data, dict) and data.get("branch") == branch and data.get("common") == common and isinstance(data.get("agent"), str)
    return data if ok else None


def refusal(branch: str, owner: dict[str, Any], me: str) -> Refuse:
    return Refuse(f"branch {branch} belongs to agent {owner['agent']} (worktree {owner.get('worktree', '?')}, since {owner.get('since', '?')}); "
                  f"you are {me}: one writer per branch, so a second agent does not commit or push here",
                  f"ask {owner['agent']} to hand it over (`sm branch claim --take` by the coordinator), or work on a branch of your own", code=1)


def check(commands: Any, worktree: Path, agent: str = "") -> tuple[str, dict[str, Any] | None]:
    """Refuse when another agent owns the worktree's branch. -> (branch, the record or None). A branch that is main (or
    detached), and an unnamed agent, are never refused."""
    common, branch = where(commands, Path(worktree).expanduser().resolve())
    me = agent_name(agent)
    owner = None if branch in NOT_A_WORK_BRANCH else owner_of(common, branch)
    if owner and me and owner["agent"] != me:
        raise refusal(branch, owner, me)
    return branch, owner


def claim(commands: Any, worktree: Path, agent: str = "", take: bool = False, root: Path | None = None) -> dict[str, Any]:
    """Record the agent as owner of the worktree's branch (a no-op for its owner). Another agent's branch is refused
    unless `take` (the coordinator's hand-over). Needs an agent name."""
    worktree = Path(worktree).expanduser().resolve()
    common, branch = where(commands, worktree)
    me = agent_name(agent)
    if not me:
        raise Refuse(f"no agent name: set {ENV} or give --agent", "sm branch claim --help", code=2)
    if branch in NOT_A_WORK_BRANCH:
        raise Refuse(f"'{branch}' is not a work branch; only work branches have one owner", "sm branch claim --help", code=2)
    owner = owner_of(common, branch, root)
    if owner and owner["agent"] != me and not take:
        raise refusal(branch, owner, me)
    if owner and owner["agent"] == me:
        return owner
    data = {"common": common, "branch": branch, "agent": me, "worktree": str(worktree), "since": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    path = record_path(common, branch, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True) + "\n")
    tmp.replace(path)
    return data


def guard_write(commands: Any, worktree: Path, agent: str = "", *, record: bool = True) -> None:
    """For a write step (`sm head pr`): refuse another agent's branch; with `record`, the first named writer of an unowned
    work branch becomes its owner. Without an agent name nothing is read, recorded or refused."""
    if not agent_name(agent):
        return
    branch, owner = check(commands, worktree, agent)
    if record and owner is None and branch not in NOT_A_WORK_BRANCH:
        claim(commands, worktree, agent)
