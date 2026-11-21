"""`sm work brief BEAD`: a builder brief generated from a bead, so no coordinator retypes the same rules. It reads the
bead (title, description), the owner's commit identity from git config and the clone of the repository, and prints the
identity, the rules, the worktree commands, the proofs and the report format. It creates nothing."""

from __future__ import annotations

import json
import re
from typing import Any

from steuermaschine.models import Model
from steuermaschine.native import runtime

from ..coord_lib.base import child_env, tracker_dir
from ..tasks_lib import beads
from . import builder

RULES = (
    "Commit with the owner identity above (git config as it is); never set another name or email, never a bot identity.",
    "Never skip a hook (--no-verify), never force-push, never weaken a test, threshold or limit, never rerun a failed check as the fix.",
    "Never touch the live Mini hz or the owner's MacBook Air; reproduce in an isolated profile with its own HOME and HZ_* directories.",
    "Stop only processes you started (no pkill, no killall); no git stash (the stack is shared); use the scratchpad folder, not /tmp.",
    "Nothing private or confidential in a public repository or pull request; user copy says Heinzel / Heinzels.",
    "Touch only your own worktree and branch; do not delete branches; commit after every green step, subject at most 72 characters.",
    "Do not merge, release or promote; do not send progress messages.",
)
PROOFS = (
    "Cheap checks first (ruff, mypy, the touched tests, by file), then `sm check` once when you are done: it must say READY.",
    "Add tests for every behaviour you change; a fix covers the whole class of the finding, not one instance.",
    "Say what you could not verify; never claim a live or UI result you did not see.",
)
REPORT = "Report once, in at most 150 words: PR number or READY, the full head SHA, the commands you ran with their results, what is NOT verified, the decisions you need."


class WorkBrief(Model):
    """A generated builder brief."""

    bead: str
    title: str
    topic: str
    branch: str
    repository: str
    prompt: str

    def render(self) -> list[str]:
        return self.prompt.splitlines()


def slug(title: str, limit: int = 40) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", title.lower())).strip("-")[:limit].strip("-") or "work"


def identity(commands: Any, clone: str, env: dict[str, str]) -> str:
    def get(key: str) -> str:
        done = commands.run(["git", "-C", clone, "config", key], env=env, timeout=20)
        return done.out.strip() if done.rc == 0 else ""

    name, email = get("user.name"), get("user.email")
    return f"{name} <{email}>" if name and email else "not set in this clone: check `git config user.name` and `git config user.email` before the first commit"


def bead_data(tracker: beads.Tracker, bead: str) -> tuple[str, str, str]:
    """(full id, title, description) of exactly one bead."""
    full = tracker.exact(bead)
    rows = json.loads(tracker.run("bd", "show", full, "--json"))
    row = (rows if isinstance(rows, list) else [rows])[0]
    return full, str(row.get("title") or ""), str(row.get("description") or "").strip()


def generate(bead: str, repository: str = "", rt: Any = None) -> WorkBrief:
    """Raises Refuse from the tracker (unknown bead) and KeyError for an unknown repository."""
    rt = rt or runtime()
    s = rt.settings
    clones: dict[str, str] = dict(builder.get(s, "brief_clones"))
    repository = repository or next(iter(clones))
    if repository not in clones:
        raise KeyError(repository)
    tracker = beads.Tracker(rt)
    full, title, description = bead_data(tracker, bead)
    short = full[len(tracker.prefix):]
    code = builder.optional_path(s, "brief_code_dir", builder.BriefSettings) or tracker_dir(s).parent
    clone = str(code / clones[repository])
    topic, branch = short, f"feat/{slug(title)}"
    tree = f"{builder.shown(code)}/{clones[repository]}-{topic}"
    prompt = "\n".join([
        f"Builder brief for bead {full}: {title}",
        "",
        "Task",
        description or "(the bead has no description: read `bd show` and ask the coordinator what done looks like)",
        "",
        f"Identity: {identity(rt.providers.commands, clone, child_env(s))}",
        "",
        "Rules",
        *[f"- {r}" for r in RULES],
        "",
        "Worktree (one per head, from origin/main; the coordinator may have made it already)",
        f"  git -C {builder.shown(code)}/{clones[repository]} fetch -q origin main",
        f"  git -C {builder.shown(code)}/{clones[repository]} worktree add {tree} -b {branch} origin/main",
        "  Work only there. Start with bare `sm`, read TRAP; type only sm, git, gh and bd. Follow ~/Code/heinzel-management/skills/hz-build/SKILL.md.",  # old name kept: the skill's folder is named so
        "",
        "Proofs",
        *[f"- {p}" for p in PROOFS],
        "",
        "Report",
        REPORT,
        f"Note the PR on the bead (`sm beads tasks note {short} ...`); do not close it.",
    ])
    return WorkBrief(bead=full, title=title, topic=topic, branch=branch, repository=repository, prompt=prompt)
