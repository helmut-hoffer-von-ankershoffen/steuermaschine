"""Close the still-open members of a merged combine pull request with "merged via #X at <sha>", and add a
"merged via, not yet released" note to the beads that mention a member. It never closes a bead. Dry run unless applied."""

from __future__ import annotations

import re
from typing import Any

from steuermaschine.github import Gh
from steuermaschine.models import Model
from steuermaschine.native import runtime

from ..coord_lib.base import child_env, tracker_dir


class MemberRow(Model):
    pull_request: int
    state: str  # OPEN | MERGED | CLOSED | unknown
    closed: bool = False  # this run closed it
    close_failed: str = ""
    beads: list[str] = []  # beads that mention the member
    noted: list[str] = []  # beads this run wrote the note to


class CombineClose(Model):
    """What closing the members of one merged combine pull request did, or would do."""

    combine: int
    repository: str
    merge_commit: str
    message: str
    applied: bool
    members: list[MemberRow] = []

    @property
    def exit_code(self) -> int:
        return 1 if any(m.close_failed for m in self.members) else 0

    def render(self) -> list[str]:
        lines = []
        for m in self.members:
            lines.append(f"#{m.pull_request}: {m.state}" + (" -> close" if m.state == "OPEN" else "") + (f"  CLOSE FAILED: {m.close_failed}" if m.close_failed else ""))
            lines += [f'  bead {bead}: note "merged via, not yet released"' for bead in m.beads]
        if not self.members:
            lines.append(f"#{self.combine} names no member (no `Combines #N` line in its body)")
        if not self.applied:
            lines.append("dry run; pass --apply")
        return lines


class NotMerged(Exception):
    """The combine pull request is not merged, or cannot be read; the message says which."""


def members(body: str | None) -> list[int]:
    return [int(n) for n in re.findall(r'^\s*Combines\s+#(\d+)', body or '', re.M)]


def close(combine: int, apply: bool = False, repo: str = "", rt: Any = None) -> CombineClose:
    """Raises NotMerged; then nothing was written."""
    rt = rt or runtime()
    s = rt.settings or {}
    repo = repo or str(s.get("repo") or "")
    commands, env = rt.providers.commands, child_env(s)
    tracker = str(tracker_dir(s))  # resolved before the first write: without a configured tracker nothing is closed
    gh = Gh(commands, repo, clock=rt.providers.clock)
    d, why = gh.try_json(["pr", "view", str(combine), "-R", repo, "--json", "body,state,mergeCommit"])
    if not isinstance(d, dict):
        raise NotMerged(f"cannot read #{combine} in {repo}: {why or 'no answer'}")
    if d.get("state") != "MERGED":
        raise NotMerged(f"#{combine} is {d.get('state')}, not merged: refusing")
    sha = ((d.get("mergeCommit") or {}).get("oid") or "")[:9]
    msg = f"merged via #{combine} at {sha}"
    rows = []
    for m in members(d.get("body")):
        state = gh.run(["pr", "view", str(m), "-R", repo, "--json", "state", "--jq", ".state"]).out.strip() or "unknown"
        row = MemberRow(pull_request=m, state=state)
        if state == "OPEN" and apply:
            done = gh.run(["pr", "close", str(m), "-R", repo, "--comment", msg], retry=False)
            row.closed, row.close_failed = done.rc == 0, "" if done.rc == 0 else Gh.why(done)
        found = commands.run(["bd", "search", f"#{m}"], cwd=tracker, env=env, timeout=60).out
        row.beads = sorted(set(re.findall(r'\b(management-[a-z0-9]{3,})\b', found)))[:5]
        if apply:
            row.noted = [bead for bead in row.beads if commands.run(["bd", "note", bead, f"{msg}; merged via, not yet released"], cwd=tracker, env=env, timeout=60).rc == 0]
        rows.append(row)
    return CombineClose(combine=combine, repository=repo, merge_commit=sha, message=msg, applied=apply, members=rows)
