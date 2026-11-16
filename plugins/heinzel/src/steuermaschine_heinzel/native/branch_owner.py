"""One writer per branch: which agent owns a branch (and its worktree), and the refusal for any other agent.

Two builders committed to the same branch on 2026-11-14 (bead nn5m). The tool now records the owner of a branch and
refuses a second agent's write step on it, naming the owner.

The agent is named by the environment variable `STEUERMASCHINE_AGENT` (the coordinator sets it when it starts a builder; a
command may also be given `--agent`). Without a name the tool cannot tell agents apart and does not refuse (it says
so in `sm branch check`); the record is never guessed. The record is one small JSON file per branch of a repository
under `branch-owners/` in the plugin's state directory, keyed by the repository's git common directory (shared by all
its worktrees) and the branch name, so a second worktree of the same branch is the same branch.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from steuermaschine.config import environment
from steuermaschine.engine import Refuse

from .. import state as statedir

FOLDER = "branch-owners"
ENV = "STEUERMASCHINE_AGENT"  # the typed environment setting that names the acting agent (`--as` of the pipelines)
DETACHED = ("HEAD", "")  # no branch at all; the default branch is read from the repository, never listed here


def agent_name(given: str | None = None) -> str:
    """The acting agent: `given`, else the environment's STEUERMASCHINE_AGENT; "" when neither names one."""
    return (given or environment().steuermaschine_agent or "").strip()


def where(commands: Any, worktree: Path) -> tuple[str, str]:
    """(git common directory, branch) of the worktree. Refuses when it is not a git worktree."""
    def read(*args: str) -> str:
        done = commands.run(["git", "-C", str(worktree), *args], timeout=30)
        if done.rc:
            raise Refuse(f"{worktree} is not a git worktree: {(done.err or done.out).strip()[:200]}", "sm branch check --help", code=2)
        return str(done.out).strip()

    common = Path(read("rev-parse", "--git-common-dir"))
    common = common if common.is_absolute() else Path(worktree) / common
    return str(common.resolve()), read("rev-parse", "--abbrev-ref", "HEAD")


def default_branch(commands: Any, worktree: Path) -> str:
    """The repository's default branch: origin's HEAD as the clone recorded it, else GitHub's `defaultBranchRef`. Refuses
    when neither can be read (an owner is never guessed for a branch that might be the default one)."""
    done = commands.run(["git", "-C", str(worktree), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], timeout=30)
    name = done.out.strip().removeprefix("origin/") if not done.rc else ""
    if not name:
        done = commands.run(["gh", "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"], cwd=str(worktree), timeout=60)
        name = done.out.strip() if not done.rc else ""
    if not name:
        raise Refuse(f"cannot tell the default branch of {worktree} (origin/HEAD is not set and `gh repo view` failed)",
                     "git remote set-head origin --auto", code=2)
    return name


def is_work_branch(commands: Any, worktree: Path, branch: str) -> bool:
    return branch not in DETACHED and branch != default_branch(commands, worktree)


def record_path(common: str, branch: str, root: Path | None = None) -> Path:
    name = hashlib.sha256(f"{common}\0{branch}".encode()).hexdigest()[:20]
    return (statedir.path(FOLDER) if root is None else root / FOLDER) / f"{name}.json"


def owner_of(common: str, branch: str, root: Path | None = None) -> dict[str, Any] | None:
    """The record of the branch, or None when none was written. A record that exists but cannot be read, or names another
    branch, refuses: it is never taken for "unowned" (the next writer would overwrite an owner)."""
    path = record_path(common, branch, root)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise Refuse(f"the owner record of branch {branch} ({path}) cannot be read: {error}", "ask the coordinator to check it (`sm branch claim --take` replaces it)", code=1) from None
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not (isinstance(data, dict) and data.get("branch") == branch and data.get("common") == common and isinstance(data.get("agent"), str)):
        raise Refuse(f"the owner record of branch {branch} ({path}) is corrupt: nobody is assumed not to own the branch",
                     "the coordinator replaces it with `sm branch claim --take`", code=1)
    return data


def refusal(branch: str, owner: dict[str, Any], me: str) -> Refuse:
    return Refuse(f"branch {branch} belongs to agent {owner['agent']} (worktree {owner.get('worktree', '?')}, since {owner.get('since', '?')}); "
                  f"you are {me}: one writer per branch, so a second agent does not commit or push here",
                  f"ask {owner['agent']} to hand it over (`sm branch claim --take` by the coordinator), or work on a branch of your own", code=1)


def check(commands: Any, worktree: Path, agent: str = "") -> tuple[str, dict[str, Any] | None]:
    """Refuse when another agent owns the worktree's branch. -> (branch, the record or None). A branch that is main (or
    detached), and an unnamed agent, are never refused."""
    common, branch = where(commands, Path(worktree).expanduser().resolve())
    me = agent_name(agent)
    owner = owner_of(common, branch) if is_work_branch(commands, Path(worktree).expanduser().resolve(), branch) else None
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
    if not is_work_branch(commands, worktree, branch):
        raise Refuse(f"'{branch}' is not a work branch; only work branches have one owner", "sm branch claim --help", code=2)
    path = record_path(common, branch, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as lock:  # one claim at a time; the lock goes with the process, so a crash leaves none
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refuse(f"another claim of branch {branch} is in progress", "run the command again in a moment", code=75) from None
        return _write_claim(common, branch, worktree, me, take, path, root)


def _write_claim(common: str, branch: str, worktree: Path, me: str, take: bool, path: Path, root: Path | None) -> dict[str, Any]:
    owner = None if take else owner_of(common, branch, root)  # the hand-over replaces even a record that cannot be read
    if owner and owner["agent"] != me:
        raise refusal(branch, owner, me)
    if owner and owner["agent"] == me:
        return owner
    data = {"common": common, "branch": branch, "agent": me, "worktree": str(worktree), "since": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, sort_keys=True) + "\n")
    tmp.replace(path)
    return data


def guard_write(commands: Any, worktree: Path, agent: str = "", *, record: bool = True) -> None:
    """For a write step (`sm head pr`): refuse another agent's branch; with `record`, the first named writer of an unowned
    work branch becomes its owner. Without an agent name nothing is read, recorded or refused."""
    if not agent_name(agent):
        return
    branch, owner = check(commands, worktree, agent)
    if record and owner is None and is_work_branch(commands, Path(worktree).expanduser().resolve(), branch):
        claim(commands, worktree, agent)
