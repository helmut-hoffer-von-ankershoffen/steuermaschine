"""`sm branch claim|check`: one writer per branch (bead nn5m). The rule and the record are in `branch_owner.py`."""

from __future__ import annotations

from pathlib import Path

import typer

from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import Group, native, runtime

from . import branch_owner

UNIT = ("branches.py", "branch_owner.py")

GROUP = Group("branch", H(
    "branch: which agent owns a branch; one writer per branch.",
    "Use when a builder is started on a branch (claim it) and in a commit or push hook (check).",
    "claim writes one small record under branch-owners/ in the state directory; check only reads.",
    "sm branch check",
    "sm branch claim --agent NAME"))


class BranchOwner(Model):
    branch: str
    owner: str = ""  # "" when nobody owns it yet
    worktree: str = ""
    since: str = ""
    agent: str = ""  # who is asking ("" when SM_AGENT is not set and --agent is not given)
    ok: bool = True

    def render(self) -> list[str]:
        who = f"owned by {self.owner} (worktree {self.worktree}, since {self.since})" if self.owner else "owned by nobody yet"
        note = "" if self.agent else "   (no agent name: set SM_AGENT or give --agent; nothing can be refused without one)"
        return [f"branch {self.branch}: {who}{note}"]


def _view(branch: str, record: dict | None, agent: str) -> BranchOwner:
    record = record or {}
    return BranchOwner(branch=branch, owner=record.get("agent", ""), worktree=record.get("worktree", ""), since=record.get("since", ""), agent=agent)


@native("branch check", writes=False, help=H(
    "Refuse (exit 1) when another agent owns the worktree's branch, naming the owner; otherwise say who owns it.",
    "Use before you commit or push to a branch you did not create, and in a pre-commit or pre-push hook.",
    "nothing (read-only).",
    "sm branch check --worktree ~/Code/steuermaschine-topic",
    "refused: ask the owner to hand it over, or use a branch of your own",
    "The acting agent is the environment variable SM_AGENT, or --agent. Without a name nothing can be refused. main is never owned."))
def branch_check(worktree: Path = typer.Option(Path("."), "--worktree", metavar="DIR", help="The worktree (default: here)."),
                 agent: str = typer.Option("", "--agent", metavar="NAME", help="The acting agent (default: SM_AGENT).")) -> BranchOwner:
    branch, owner = branch_owner.check(runtime().providers.commands, worktree, agent)
    return _view(branch, owner, branch_owner.agent_name(agent))


@native("branch claim", writes=True, files=UNIT, help=H(
    "Record the agent as the one writer of the worktree's branch; a second agent's claim is refused.",
    "Use when a builder is started on a branch, so no other agent commits or pushes there. `sm head pr` also claims an unowned branch.",
    "writes one small record file under branch-owners/ in the state directory; nothing else.",
    "sm branch claim --agent a9b40bbe597ed25d8 --worktree ~/Code/steuermaschine-topic",
    "sm branch check",
    "--take (the coordinator's hand-over) replaces another agent's claim. The acting agent is SM_AGENT, or --agent."))
def branch_claim(worktree: Path = typer.Option(Path("."), "--worktree", metavar="DIR", help="The worktree (default: here)."),
                 agent: str = typer.Option("", "--agent", metavar="NAME", help="The agent that owns it (default: SM_AGENT)."),
                 take: bool = typer.Option(False, "--take", help="Take the branch over from its owner.")) -> BranchOwner:
    record = branch_owner.claim(runtime().providers.commands, worktree, agent, take)
    return _view(record["branch"], record, branch_owner.agent_name(agent))
