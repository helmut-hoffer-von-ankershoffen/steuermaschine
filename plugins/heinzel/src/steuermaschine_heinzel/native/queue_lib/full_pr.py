"""Require protected full PR evidence when a local check deferred the Vitest suite.

The queue rechecks main before an immediate, head-pinned REST merge. GitHub's
merge endpoint does not atomically pin the base; this retains Core's existing
non-strict base race rather than claiming an atomic base guarantee.
"""

import importlib
import re
from pathlib import Path

from . import hzlib, main_checkpoint

SHA = re.compile(r"[0-9a-f]{40}")
LEGACY = "the whole Vitest suite"


def base_unchanged(proof):
    """Check the protected main ref after the reader and immediately before merge."""
    if not isinstance(proof, dict) or not isinstance(proof.get("base"), str) or not SHA.fullmatch(proof["base"]):
        return False
    ref = hzlib.ghj("api", f"repos/{hzlib.CORE}/git/ref/heads/main", default=None)
    return isinstance(ref, dict) and (ref.get("object") or {}).get("sha") == proof["base"]


def required(head):
    """Read the exact-head receipt, including receipts written before the explicit flag."""
    native = importlib.import_module("steuermaschine.native")
    receipt = native.runtime().providers.store.get_receipt("checks", head)
    if receipt is None:
        return None
    if not isinstance(receipt, dict) or receipt.get("head") != head or receipt.get("green") is not True:
        raise ValueError("the exact-head local check receipt is invalid")
    deferred = receipt.get("ci_must_run", [])
    if not isinstance(deferred, list) or not all(isinstance(item, str) for item in deferred):
        raise ValueError("the local CI obligations are unreadable")
    flag = receipt.get("requires_full_pr", False)
    if type(flag) is not bool:
        raise ValueError("the full PR obligation is unreadable")
    return receipt if flag or LEGACY in deferred else None


def verify(pr, head, receipt):
    """Authenticate one original full PR run against current protected main and the tested merge tree."""
    if not SHA.fullmatch(head) or not str(pr).isdigit():
        raise ValueError("invalid PR or head")
    repo = hzlib.CORE
    base_doc = hzlib.ghj("api", f"repos/{repo}/git/ref/heads/main", default=None)
    base = ((base_doc or {}).get("object") or {}).get("sha", "")
    pull = hzlib.ghj("api", f"repos/{repo}/pulls/{pr}", default=None)
    if not isinstance(base, str) or not SHA.fullmatch(base) or not isinstance(pull, dict):
        raise ValueError("protected main or pull request unavailable")
    merge = pull.get("merge_commit_sha")
    if ((pull.get("head") or {}).get("sha") != head or (pull.get("base") or {}).get("sha") != base
            or not isinstance(merge, str) or not SHA.fullmatch(merge)):
        raise ValueError("the pull request head, base or merge commit changed")
    commit = hzlib.ghj("api", f"repos/{repo}/commits/{merge}", default=None)
    tree = (((commit or {}).get("commit") or {}).get("tree") or {}).get("sha", "")
    if not isinstance(tree, str) or not SHA.fullmatch(tree):
        raise ValueError("the tested merge tree is unavailable")
    runs = hzlib.ghj("api", f"repos/{repo}/actions/workflows/ci.yml/runs?event=pull_request&head_sha={head}&per_page=100", default=None)
    rows = runs.get("workflow_runs") if isinstance(runs, dict) else None
    if not isinstance(rows, list) or runs.get("total_count") != len(rows) or not rows:
        raise ValueError("the complete PR CI run inventory is unavailable")
    candidates = [row for row in rows if isinstance(row, dict) and row.get("head_sha") == head
                  and row.get("event") == "pull_request" and row.get("status") == "completed"
                  and row.get("conclusion") == "success" and row.get("run_attempt") == 1
                  and type(row.get("id")) is int and row["id"] > 0]
    if not candidates:
        raise ValueError("no successful original full PR CI run is available")
    worktree = receipt.get("worktree")
    if not isinstance(worktree, str) or not Path(worktree).is_absolute():
        raise ValueError("the local receipt has no absolute Core object source")
    run_id = max(row["id"] for row in candidates)
    expected = {"type": "full-pr/v1", "head": head, "base": base, "tree": tree, "run": run_id, "attempt": 1}
    proof = main_checkpoint.isolated_reader({"mode": "full-pr", "core_repo": worktree, "reader_head": base,
                                             "head": head, "tree": tree, "pr": int(pr), "run": run_id})
    if proof != expected:
        raise ValueError("protected full PR proof differs from the requested identity")
    return proof
