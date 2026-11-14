"""The builder brief: create the builder's worktree on a new branch from origin/main (an existing one is reused) and
return the exact prompt to hand to the builder.

Refuses when the machine is full: the 1-minute load average is over the limit, or as many agents are working as the
limit allows. "Working agents" are the agent command-line processes whose current directory is inside a builder's
worktree (a directory `<prefix>*-<topic>` under the code directory that is not a main clone or the tracker's
repository); each process counts once. A reused worktree whose builder is still running counts too.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from steuermaschine.models import Model
from steuermaschine.native import runtime
from steuermaschine.runlock import sandbox_load

from ..coord_lib.base import child_env, document, optional_path, setting, tracker_dir
from ..coord_lib.journal import note

DEFAULT_TASK = "<the task in one paragraph, with what done looks like>"


class BriefSettings(BaseModel):
    """Settings of the briefs (mixin of the plugin's settings table)."""

    brief_code_dir: Path | None = Field(None, description="The directory that holds the clones and the builders' worktrees; none: the directory that contains the tracker's repository.")
    brief_clones: dict[str, str] = Field({"core": "heinzel", "plugins": "heinzel-plugins", "acme": "heinzel-acme", "sf": "heinzel-sf"},
                                         description="Short repository name -> directory of its main clone under brief_code_dir. The first one is the default.")
    brief_max_load: float = Field(40.0, description="No new builder above this 1-minute load average.")
    brief_max_agents: int = Field(5, description="No new builder when this many agents are working.")
    brief_agent_commands: list[str] = Field(["claude", "codex"], description="Process names that count as a working agent.")
    brief_test_load: float | None = Field(None, description="A fixed load for tests; ignored unless HOME is a sandbox.")


def get(settings: Mapping[str, Any] | None, name: str) -> Any:
    return setting(settings, name, BriefSettings)


class BuilderBrief(Model):
    """A builder's worktree and the prompt to hand over."""

    worktree: str
    branch: str
    repository: str
    created: bool  # False: the worktree existed and is reused
    forced: str = ""  # why the machine was full, when --force went ahead
    prompt: str

    def render(self) -> list[str]:
        return self.prompt.splitlines()


class MachineFull(Exception):
    """No new builder now; the message says why."""


class BadTopic(ValueError):
    """The topic is no plain name, or the directory it names is not a place for a builder's worktree."""


TOPIC = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def worktree_branch(listing: str, tree: Path) -> str | None:
    """The branch of `tree` in `git worktree list --porcelain`, '' when it is listed detached, None when not listed."""
    for block in listing.split("\n\n"):
        rows = block.strip().splitlines()
        if rows and rows[0] == f"worktree {tree}":
            return next((row.removeprefix("branch refs/heads/") for row in rows if row.startswith("branch refs/heads/")), "")
    return None


def agent_pids(ps_output: str, agent_commands: Iterable[str] = ("claude", "codex")) -> list[int]:
    """Process ids of command-line agents out of `ps -axo pid=,comm=` (the desktop apps' helpers have other names)."""
    names = set(agent_commands)
    pids = []
    for line in ps_output.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and Path(command.strip()).name in names:
            pids.append(int(pid))
    return pids


def worktree_agents(cwds: dict[int, str], code: Path, prefix: str = "heinzel", not_worktrees: Iterable[str] = ()) -> list[tuple[int, str]]:
    """Agents whose cwd is inside a worktree directly under `code` (named `prefix*`, not a main clone or the tracker's repository)."""
    skip = set(not_worktrees)
    found = []
    for pid, cwd in sorted(cwds.items()):
        try:
            parts = Path(cwd).relative_to(code).parts
        except ValueError:
            continue
        if parts and parts[0].startswith(prefix) and parts[0] not in skip:
            found.append((pid, parts[0]))
    return found


def full(load: float, agents: list[tuple[int, str]], max_load: float = 40, max_agents: int = 5) -> str:
    """Why no new builder now, or an empty string."""
    reasons = []
    if load > max_load:
        reasons.append(f"load {load:.1f} is over {max_load:g} (1-minute load average)")
    if len(agents) >= max_agents:
        reasons.append(f"{len(agents)} agents are working, the limit is {max_agents} (agent processes inside a worktree: {', '.join(sorted({a[1] for a in agents}))})")
    return "; ".join(reasons)


def working_agents(rt: Any, code: Path, prefix: str, not_worktrees: Iterable[str]) -> list[tuple[int, str]]:
    commands = rt.providers.commands
    ps = commands.run(["ps", "-axo", "pid=,comm="], timeout=20).out
    pids = agent_pids(ps, get(rt.settings, "brief_agent_commands"))
    if not pids:
        return []
    lsof = commands.run(["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(map(str, pids))], timeout=20).out
    cwds, pid = {}, 0
    for line in lsof.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid:
            cwds[pid] = line[1:]
    return worktree_agents(cwds, code, prefix, not_worktrees)


def shown(path: Path) -> str:
    home = str(Path.home())
    return "~" + str(path)[len(home):] if str(path).startswith(home + os.sep) else str(path)


def brief(topic: str, branch: str, repository: str = "", task: str = DEFAULT_TASK, bead: str = "", force: str = "", rt: Any = None,
          load: Callable[[], float] | None = None) -> BuilderBrief:
    """Raises MachineFull (nothing done), KeyError for an unknown repository, RuntimeError when git refuses the worktree."""
    rt = rt or runtime()
    s = rt.settings
    clones: dict[str, str] = dict(get(s, "brief_clones"))
    default = next(iter(clones))
    repository = repository or default
    if repository not in clones:
        raise KeyError(repository)
    tracker = tracker_dir(s)
    code = optional_path(s, "brief_code_dir", BriefSettings) or tracker.parent
    if not TOPIC.fullmatch(topic):
        raise BadTopic(f"the topic '{topic}' is not a plain name (letters, digits, dot, dash, underscore; no path)")
    target = (code / f"{clones[repository]}-{topic}").resolve()
    if target.parent != code.resolve():
        raise BadTopic(f"the topic '{topic}' leads outside {code}")
    if target.name in clones.values():
        raise BadTopic(f"the topic '{topic}' names the main clone {target.name}, not a builder's worktree")
    if target == tracker.resolve():
        raise BadTopic(f"the topic '{topic}' names the tracker's repository")
    measured = (load or sandbox_load(get(s, "brief_test_load")) or (lambda: os.getloadavg()[0]))()
    why = full(measured, working_agents(rt, code, clones[default], {*clones.values(), tracker.name}), float(get(s, "brief_max_load")), int(get(s, "brief_max_agents")))
    if why and not force.strip():
        raise MachineFull(why)
    if why:
        note(f"builder brief {topic} forced past the limit ({why}): {force.strip()}", rt)
    clone = code / clones[repository]
    tree = code / f"{clones[repository]}-{topic}"
    commands, env = rt.providers.commands, child_env(s)
    created = not tree.exists()
    if created:
        fetched = commands.run(["git", "-C", str(clone), "fetch", "-q", "origin", "main"], env=env, timeout=120)
        if fetched.rc != 0:
            raise RuntimeError(f"git fetch origin main failed in {clone}: {fetched.err.strip()}")
        made = commands.run(["git", "-C", str(clone), "worktree", "add", "-q", str(tree), "-b", branch, "origin/main"], env=env, timeout=120)
        if made.rc != 0:
            raise RuntimeError(made.err.strip())
    else:
        listed = worktree_branch(commands.run(["git", "-C", str(clone), "worktree", "list", "--porcelain"], env=env, timeout=60).out, tree)
        if listed is None:
            raise RuntimeError(f"{tree} exists and is not a worktree of {clone}")
        if listed != branch:
            raise RuntimeError(f"{tree} is on branch {listed or '(detached)'}, not {branch}")
    bead_line = f"Bead: {bead} (note the PR on it; after merge: sm beads stamp {bead} --merged <PR>)\n" if bead else ""
    prompt = f"""Worktree: {tree} (branch {branch}). Work only there.
Task: {task}
{bead_line}Read section 6 of {shown(document(s))} first; there is no other brief.
Report once, in under 150 words: PR URL, head SHA, checks, what you did not verify, decisions needed. Don't merge, release or send progress messages."""
    return BuilderBrief(worktree=str(tree), branch=branch, repository=repository, created=created, forced=why if force.strip() else "", prompt=prompt)
