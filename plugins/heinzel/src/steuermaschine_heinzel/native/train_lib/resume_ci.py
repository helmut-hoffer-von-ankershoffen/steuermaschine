"""Right after a release tag: re-run the CI that the candidate window cancelled, but only for runs of OPEN pull
requests that are queued for merge or are combine branches.

A port of the old shell tool of the same purpose, decision for decision: runs of closed or merged PRs are dropped
from the deferred list (named, not re-run); runs of other open PRs stay in the list for a later full resume; a run
whose re-run fails stays too. Refuses while a run of a release branch is active unless forced.

One change on purpose: a failed read of GitHub (the active runs or the open PRs) refuses with nothing re-run and the
list unchanged. The old tool counted a failed run list as "no release run" and a failed PR list as "every PR closed",
so one transient error emptied the list.

The list itself is the one `sm ci defer` records and `sm ci deferred` prints (rows `{"id", "branch", "workflow"}`);
the caller reads and replaces it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


def _read(gh: Callable[..., tuple[int, str]], *args: str) -> list[dict[str, Any]] | None:
    """The JSON list `gh ARGS` prints, or None when the call failed or printed something else."""
    code, out = gh(*args)
    if code != 0:
        return None
    try:
        rows = json.loads(out) if out.strip() else []
    except ValueError:
        return None
    return rows if isinstance(rows, list) and all(isinstance(row, dict) for row in rows) else None


def resume(gh: Callable[..., tuple[int, str]], queued: set[str], repo: str, rows: list[dict[str, Any]],
           save: Callable[[list[dict[str, Any]]], None], dry: bool = False, force: bool = False) -> tuple[int, list[str]]:
    """(exit code, lines). `gh(*args)` returns (code, stdout); `queued` are the PR numbers queued for merge; `rows`
    the deferred runs, oldest first; `save(rows)` replaces the list (never called with --dry-run or on a refusal)."""
    if not rows:
        return 0, ["nothing deferred"]
    active = 0
    for status in ("in_progress", "queued"):
        runs = _read(gh, "run", "list", "-R", repo, "--status", status, "--limit", "100", "--json", "headBranch")
        if runs is None:
            return 1, [f"refusing: cannot read the {status} runs of {repo}; nothing re-run, the list is unchanged"]
        active += sum(1 for row in runs if str(row.get("headBranch", "")).startswith("release/"))
    if active and not force:
        return 1, ["refusing: a release/* run is active (use --force)"]
    prs = _read(gh, "pr", "list", "-R", repo, "--state", "open", "--limit", "200", "--json", "number,headRefName")
    if prs is None:
        return 1, [f"refusing: cannot read the open PRs of {repo}; nothing re-run, the list is unchanged"]
    by_branch: dict[str, str] = {}
    for row in prs:
        by_branch.setdefault(str(row.get("headRefName", "")), str(row.get("number", "")))
    lines: list[str] = []
    keep: list[dict[str, Any]] = []
    for row in rows:
        run, branch, name = str(row.get("id", "")), str(row.get("branch", "")), str(row.get("workflow", ""))
        if not run:
            continue
        entry, pr = f"{run} {branch} {name}".rstrip(), by_branch.get(branch, "")
        if not pr:
            lines.append(f"drop {entry} (no open PR: closed/merged, not rerun)")
        elif branch.startswith("combine/") or pr in queued:
            if dry:
                lines.append(f"would rerun {run} #{pr} {branch} {name}".rstrip())
                keep.append(row)
            elif gh("run", "rerun", run, "-R", repo)[0] == 0:
                lines.append(f"rerun {run} #{pr} {branch} {name}".rstrip())
            else:
                lines.append(f"FAILED rerun {run} #{pr} {branch} {name}".rstrip())
                keep.append(row)
        else:
            lines.append(f"later {run} #{pr} {branch} {name} (open, not queued/combine: stays for the full resume of deferred CI)")
            keep.append(row)
    if not dry:
        save(keep)
    return 0, lines
