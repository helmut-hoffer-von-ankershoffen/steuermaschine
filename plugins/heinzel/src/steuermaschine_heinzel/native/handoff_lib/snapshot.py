"""The open-work snapshot (open pull requests, main CI, releases, local work that is on no remote) and its
publication to the remote of the tracker's repository.

The publication touches nothing it did not write: it stages and commits only the snapshot file (and paths the
configuration names in handoff_files), by explicit path; it never stashes, rebases, merges or adds anything else, so
uncommitted work in the checkout stays exactly as it is. It pushes fast-forward only, and only when the commit it
made is the only one the remote lacks, unless the caller says the other local commits may go too. Everything that
can refuse is checked BEFORE the snapshot file is written. Every step's result is checked.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from steuermaschine.github import Gh
from steuermaschine.models import Model

from ..coord_lib.base import child_env, setting, tracker_dir


class HandoffSettings(BaseModel):
    """Settings of the handoff and the hourly update (mixin of the plugin's settings table)."""

    handoff_snapshot: str = Field("research/open-work.md", description="The snapshot file, relative to the tracker's repository.")
    handoff_files: list[str] = Field([], description="Further paths of the tracker's repository that a handoff stages and commits besides the snapshot file (for example the tracking document). "
                                     "Default: none, so a handoff commits only the file it wrote.")
    handoff_remote: str = Field("origin", description="The remote of the tracker's repository a handoff pushes to.")
    handoff_branch: str = Field("main", description="The branch a handoff commits on and pushes; a checkout on another branch is refused.")
    handoff_extra_repos: list[str] = Field([], description="Further repositories (OWNER/NAME) whose open pull requests and releases the snapshot lists.")
    handoff_ci_workflow: str = Field("ci.yml", description="The workflow whose runs on main the snapshot lists.")
    handoff_worktree_prefix: str = Field("heinzel", description="Directories next to the tracker's repository whose name starts with this are looked at for unpushed work.")
    handoff_recent_days: int = Field(3, description="Only worktrees changed within this many days are listed as local work.")
    handoff_commit_message: str = Field("docs: refresh the tracking document", description="The commit message of a handoff.")
    hour_facts: list[str] = Field(["doc asks", "go", "queue list", "ci flakes", "ci main", "release status", "alarm list", "gate status"],
                                  description="Command paths whose text the hourly update shows as facts; only read-only commands without arguments are run.")


def get(settings: Mapping[str, Any] | None, name: str) -> Any:
    return setting(settings, name, HandoffSettings)


class Handoff(Model):
    """What a handoff did. `commit` is the tracker repository's HEAD after the push."""

    snapshot_file: str
    committed: bool = False
    pushed: bool = False
    commit: str = ""
    published_commits: int = 0  # commits the push sent: this handoff's own, plus earlier local ones when they were allowed
    staged: list[str] = []
    dry_run: bool = False
    snapshot: str = ""

    def render(self) -> list[str]:
        if self.dry_run:
            return [*self.snapshot.splitlines(), f"dry run: nothing written; a handoff would write {self.snapshot_file} and commit only: {', '.join(self.staged)}"]
        if not self.pushed:
            return [f"handoff: nothing new to commit and nothing to push; the remote is at {self.commit}"]
        return [f"handoff: {'committed and ' if self.committed else 'nothing new to commit; '}pushed {self.published_commits} commit(s), on the remote at {self.commit}"]


class StepFailed(Exception):
    """One step of the publication failed; the message names it. Earlier steps stay done."""


class World:
    def __init__(self, rt: Any):
        s = rt.settings or {}
        self.s, self.commands, self.clock, self.env = s, rt.providers.commands, rt.providers.clock, child_env(s)
        self.root, self.repo = tracker_dir(s), str(s.get("repo") or "")

    def git(self, directory: Path, *args: str, timeout: float = 120) -> Any:
        return self.commands.run(["git", "-C", str(directory), *args], env=self.env, timeout=timeout)

    def gh_list(self, *args: str) -> list[dict[str, Any]]:
        data, _why = Gh(self.commands, self.repo, clock=self.clock).try_json(list(args))
        return data if isinstance(data, list) else []

    # -- the snapshot
    def pr_table(self, repo: str) -> list[str]:
        rows = self.gh_list("pr", "list", "-R", repo, "--state", "open", "--limit", "100", "--json", "number,title,headRefName,isDraft,updatedAt")
        out = [f"**Open pull requests of {repo}** ({len(rows)})", "", "| PR | Title | Branch | Draft | Updated |", "|---|---|---|---|---|"]
        out += [f"| #{r.get('number')} | {str(r.get('title', '')).replace('|', '/')[:90]} | `{r.get('headRefName', '')}` | {'yes' if r.get('isDraft') else ''} | {str(r.get('updatedAt', ''))[:16]} |"
                for r in sorted(rows, key=lambda r: r.get("number") or 0)]
        return [*out, ""]

    def clones(self) -> list[Path]:
        """Every clone or worktree next to the tracker's repository whose name has the prefix."""
        return [d for d in sorted(self.root.parent.glob(f"{get(self.s, 'handoff_worktree_prefix')}*/")) if (d / ".git").exists()]

    def recently_changed(self, directory: Path) -> bool:
        """Like `find DIR -maxdepth 1 -mtime -N`: the directory or any direct entry changed within the last days."""
        limit = self.clock.now() - int(get(self.s, "handoff_recent_days")) * 86400
        return directory.stat().st_mtime > limit or any(e.stat(follow_symlinks=False).st_mtime > limit for e in directory.iterdir())

    def local_row(self, directory: Path) -> str | None:
        if not self.recently_changed(directory):
            return None
        branch = self.git(directory, "rev-parse", "--abbrev-ref", "HEAD")
        if branch.rc != 0 or branch.out.strip() in ("main", "HEAD"):
            return None
        counted = self.git(directory, "rev-list", "--count", "HEAD", "--not", "--remotes")
        unpushed = counted.out.strip() if counted.rc == 0 else "?"
        dirty = len(self.git(directory, "status", "--porcelain").out.splitlines())
        return f"| {directory} | `{branch.out.strip()}` | {unpushed} | {dirty} |" if unpushed != "0" or dirty != 0 else None

    def build(self) -> str:
        stamp = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(self.clock.now()))
        repos = [self.repo, *get(self.s, "handoff_extra_repos")]
        out = ["# Open work", "", f"Generated {stamp} by `sm doc handoff`. Don't edit by hand.", ""]
        for repo in repos:
            out += self.pr_table(repo)
        out += ["**Main and releases**", ""]
        for run in self.gh_list("run", "list", "-R", self.repo, "--workflow", str(get(self.s, "handoff_ci_workflow")), "--branch", "main", "-L", "5", "--json", "conclusion,status,headSha"):
            out.append(f"- CI {run.get('conclusion') or run.get('status')} `{str(run.get('headSha', ''))[:8]}`")
        for repo in repos:
            for rel in self.gh_list("release", "list", "-R", repo, "-L", "4", "--json", "tagName,isLatest,isPrerelease"):
                out.append(f"- {repo.split('/')[-1]} release `{rel.get('tagName')}` latest={str(rel.get('isLatest')).lower()} pre={str(rel.get('isPrerelease')).lower()}")
        days = int(get(self.s, "handoff_recent_days"))
        out += ["", f"**Local work not on any remote, in worktrees changed in the last {days} days**", "", "| Worktree | Branch | Unpushed commits | Uncommitted files |", "|---|---|---|---|",
                *(row for row in map(self.local_row, self.clones()) if row)]
        return "\n".join(out) + "\n"

    # -- the publication
    def paths(self) -> tuple[Path, list[str]]:
        snapshot = str(get(self.s, "handoff_snapshot"))
        return self.root / snapshot, list(dict.fromkeys([snapshot, *(str(f) for f in get(self.s, "handoff_files"))]))

    def precheck(self, include_local_commits: bool) -> int:
        """Everything that can refuse, before a byte is written. Returns the number of local commits the remote lacks."""
        remote, branch = str(get(self.s, "handoff_remote")), str(get(self.s, "handoff_branch"))
        current = self.git(self.root, "rev-parse", "--abbrev-ref", "HEAD")
        self.step("rev-parse", current)
        if current.out.strip() != branch:
            raise StepFailed(f"the tracker's repository is on branch {current.out.strip()}, not {branch}: a handoff commits and pushes {branch} only")
        self.step(f"fetch {remote} {branch}", self.git(self.root, "fetch", "-q", remote, branch, timeout=300))
        if self.git(self.root, "merge-base", "--is-ancestor", f"{remote}/{branch}", "HEAD").rc != 0:
            raise StepFailed(f"{remote}/{branch} has commits this checkout lacks: a handoff pushes fast-forward only and never rebases or merges; integrate them yourself, then run it again")
        counted = self.git(self.root, "rev-list", "--count", f"{remote}/{branch}..HEAD")
        self.step("rev-list", counted)
        ahead = int(counted.out.strip() or 0)
        if ahead and not include_local_commits:
            raise StepFailed(f"the checkout has {ahead} local commit(s) the remote lacks, which this handoff did not make: a push would publish all of them. "
                             "Pass --include-local-commits to publish them with the handoff")
        return ahead

    def publish(self, section: str, include_local_commits: bool = False) -> Handoff:
        remote, branch = str(get(self.s, "handoff_remote")), str(get(self.s, "handoff_branch"))
        snapshot, wanted = self.paths()
        ahead = self.precheck(include_local_commits)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(section.rstrip() + "\n")
        present = [f for f in wanted if (self.root / f).exists()]
        self.step("add", self.git(self.root, "add", "--", *present))
        committed = self.git(self.root, "diff", "--cached", "--quiet", "--", *present).rc != 0
        if committed:
            self.step("commit", self.git(self.root, "-c", "commit.gpgsign=false", "commit", "-q", "--only", "-m", str(get(self.s, "handoff_commit_message")), "--", *present))
        sending = ahead + (1 if committed else 0)
        if sending:
            self.step("push", self.git(self.root, "push", "-q", remote, f"{branch}:{branch}", timeout=300))
        return Handoff(snapshot_file=str(snapshot), committed=committed, pushed=bool(sending), published_commits=sending, staged=present,
                       commit=self.git(self.root, "rev-parse", "--short", "HEAD").out.strip())

    @staticmethod
    def step(name: str, done: Any) -> None:
        if done.rc != 0:
            raise StepFailed(f"git {name} failed in the tracker's repository: {(done.err or done.out).strip()[:300] or 'exit ' + str(done.rc)}")


def handoff(rt: Any, dry_run: bool = False, include_local_commits: bool = False) -> Handoff:
    """Raises StepFailed. A dry run builds the snapshot and writes nothing (it does not fetch either)."""
    world = World(rt)
    section = world.build()
    if dry_run:
        return Handoff(snapshot_file=str(world.paths()[0]), dry_run=True, snapshot=section, staged=world.paths()[1])
    return world.publish(section, include_local_commits)
