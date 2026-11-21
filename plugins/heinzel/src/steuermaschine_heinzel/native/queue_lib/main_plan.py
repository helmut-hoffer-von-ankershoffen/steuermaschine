"""The main plan check: does the focused plan registry hold the plan main will need after this PR merges?

Main's push CI resolves the reviewed plan of its exact tree (`push-tree:<tree>`, base = the previous main). An
admission prepares that plan for the tree of `merge-tree(main at admission, head)`. When another PR merged since,
the PR's squash onto the NEW main is another tree without a plan, and its merge turns main red (the batch plan trap,
2026-11-12 and again 2026-11-13 via a re-queue: #804 merged, then #803 on its old plan). This module computes the
tree the merge would produce on CURRENT main and looks it up in the registry with Core's own codec, read from current
main. A missing plan may instead be satisfied by protected Core verification of an original full hosted PR run
against the exact current merge tree, including its complete job inventory and whole-project coverage. The queue
refuses when neither independent route proves the current source.

Fail closed: no Core checkout configured, an unreadable registry, a conflict or any error is a refusal with its reason.
"""
# mypy: disable-error-code="no-untyped-call"
# (main_checkpoint is moved, untyped code; this module calls its reviewed registry reader)
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from . import edge, hzlib, main_checkpoint

SETTING = "queue_core_repo"
ORIGIN = f"https://github.com/{hzlib.CORE}.git"
PREFIX = "stale main plan"
_core: list[Path] = []


def configure(core_repo: Path | str | None) -> None:
    """Set the Core checkout the merge tree is computed from (setting `queue_core_repo`); objects only, never changed."""
    _core.clear()
    if core_repo:
        _core.append(Path(core_repo).expanduser())


def core_repo() -> Path | None:
    return _core[0] if _core else None


def _git(repo: Path, *args: str) -> str:
    result = edge.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=120, check=False)
    if result.returncode:
        raise ValueError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()[-300:]}")
    return str(result.stdout).strip()


def merge_tree(core: Path, pr: str, main: str, head: str, origin: str = ORIGIN) -> str:
    """The tree a squash of `head` onto `main` produces, from a disposable clone (the configured checkout is not changed)."""
    with tempfile.TemporaryDirectory(prefix="sm-main-plan-") as directory:
        repo = Path(directory) / "repo"
        _git(Path(directory), "clone", "--quiet", "--shared", "--no-checkout", "--", str(core), str(repo))
        _git(repo, "fetch", "--quiet", "--no-tags", origin, "+refs/heads/main:refs/sm/main", f"+refs/pull/{pr}/head:refs/sm/head")
        if _git(repo, "rev-parse", "refs/sm/main") != main:
            raise ValueError(f"main moved while it was read ({main[:8]})")
        if _git(repo, "rev-parse", "refs/sm/head") != head:
            raise ValueError(f"the head of #{pr} moved while it was read ({head[:8]})")
        result = edge.run(["git", "merge-tree", "--write-tree", main, head], cwd=str(repo), capture_output=True, text=True,
                          timeout=120, check=False)
        if result.returncode:
            raise ValueError(f"#{pr} does not merge cleanly onto main {main[:8]}")
        return str(result.stdout).split()[0]


def registry(core: Path, main: str) -> dict[str, Any]:
    """The decoded focused plan registry, through Core's codec of `main` (the checkpoint's isolated protected reader)."""
    values = main_checkpoint.transports()
    decoded: dict[str, Any] = main_checkpoint.isolated_reader({
        "mode": "registry", "core_repo": str(core), "reader_head": main, "legacy": values["FOCUSED_CI_PLANS"],
        "slots": [values[f"FOCUSED_CI_PLANS_V2_{i}"] for i in range(4)]})
    return decoded


def next_command(pr: str, main: str) -> str:
    return f"#{pr} needs a new head on main {main[:8]}: sm ship WORKTREE --live"


def full_pr(core: Path, pr: str, main: str, head: str, tree: str) -> bool:
    """Use original or narrowly recovered PR evidence through the authenticated protected Core reader."""
    from . import core_policy

    def api(path: str) -> dict[str, Any]:
        value = hzlib.ghj("api", f"repos/{hzlib.CORE}/{path}", default=None)
        if not isinstance(value, dict):
            raise TypeError("full PR evidence unavailable")
        return value

    policy = core_policy.at_commit_api(api, main)
    runs = api(f"{policy.workflow_runs}?head_sha={head}&event=pull_request&per_page=1").get("workflow_runs")
    if not isinstance(runs, list) or len(runs) != 1:
        raise ValueError("no unique latest full PR run")
    run = runs[0]
    attempt = run.get("run_attempt")
    if (run.get("head_sha") != head or type(attempt) is not int or attempt not in (1, 2)
            or type(run.get("id")) is not int or run["id"] <= 0
            or run.get("status") != "completed" or run.get("conclusion") != "success"):
        raise ValueError("latest full PR run is not successful on an eligible attempt")
    capacity = attempt == 2
    proof = main_checkpoint.isolated_reader({
        "mode": "full-pr-capacity" if capacity else "full-pr", "core_repo": str(core), "reader_head": main,
        "pr": pr, "head": head, "tree": tree, "run": run["id"],
    })
    expected = {"type": "full-pr-capacity/v1" if capacity else "full-pr/v1", "head": head, "base": main, "tree": tree,
                "run": run["id"], "attempt": attempt, **({"original_attempt": 1} if capacity else {})}
    return (isinstance(proof, dict) and proof == expected
            and all(type(proof[key]) is int for key in ("run", "attempt", *(("original_attempt",) if capacity else ()))))


def gap(pr: str, head: str = "") -> str:
    """Accept an exact current-main plan or independently verified complete hosted evidence; otherwise refuse."""
    core = core_repo()
    if core is None:
        return f"{PREFIX}: cannot check #{pr}: the setting {SETTING} (a Core checkout) is not set; sm self config"
    try:
        if not head:
            info = hzlib.ghj("pr", "view", pr, "-R", hzlib.CORE, "--json", "headRefOid", default={}) or {}
            head = info.get("headRefOid", "") if isinstance(info, dict) else ""
        ref = hzlib.ghj("api", f"repos/{hzlib.CORE}/git/ref/heads/main", default={}) or {}
        main = (ref.get("object") or {}).get("sha", "") if isinstance(ref, dict) else ""
        if len(head) != 40 or len(main) != 40:
            return f"{PREFIX}: cannot check #{pr}: its head or main is unreadable"
        tree = merge_tree(core, pr, main, head)
    except Exception as error:  # noqa: BLE001 - fail closed: every error is a named refusal
        return f"{PREFIX}: cannot check #{pr}: {error}"
    try:
        plan = registry(core, main).get(f"push-tree:{tree}")
    except Exception:  # noqa: BLE001 - independent full proof may still authenticate admission
        plan = None
    if isinstance(plan, dict) and plan.get("base") == main:
        return ""
    full_refusal = "no exact full hosted proof"
    try:
        if full_pr(core, pr, main, head, tree):
            return ""
    except Exception as error:  # noqa: BLE001 - fail closed with the independent proof refusal
        full_refusal = str(error)
    return (f"{PREFIX}: no focused plan for tree {tree[:8]} of #{pr} ({head[:8]}) on main {main[:8]}; "
            f"Full hosted proof refused: {full_refusal}. Next: {next_command(pr, main)}")
