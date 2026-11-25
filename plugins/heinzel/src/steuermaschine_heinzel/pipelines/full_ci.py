"""Bind a requested full-candidate route to source for independent review.

No workflow interpreter or automatic full-main classification lives here.
The native candidate verifier remains the authority for executed CI evidence.
"""

from __future__ import annotations

import hashlib

from steuermaschine.engine import Refuse

from ..native.queue_lib import core_policy
from ..native.release_admit_lib import release_scope

# The actual producer used by Core's batch protocol; never an operator-selected
# file that could stand in for an unrelated harmless source.
BATCH_SOURCE = "scripts/main_batch.py"


def source_request(clone: str, base: str, head: str, version: str) -> dict[str, object]:
    """Return exact source bindings, not a verdict on whether its jobs run or pass."""
    try:
        policy = core_policy.at_commit_git(clone, base)
        paths = {core_policy.PATH, policy.workflow, policy.recorder, policy.judge_workflow, BATCH_SOURCE}
        protected = core_policy.blobs_at(clone, base, paths)
        prospective = core_policy.blobs_at(clone, head, paths)
        if prospective != protected:
            raise Refuse("full-candidate request changes protected workflow, batch or verification sources")
        validate_generated_release(clone, base, head, version)
    except (OSError, ValueError, TypeError, KeyError, core_policy.CandidateError) as exc:
        raise Refuse(f"cannot bind full-candidate source review: {exc}") from None
    return {"repository": policy.repository, "base": base, "head": head,
            "workflow": policy.workflow, "batch_producer": BATCH_SOURCE,
            "source_sha256": {path: hashlib.sha256(raw).hexdigest() for path, raw in sorted(protected.items())},
            "validation_requested": "full-first-attempt", "execution_evidence": None,
            "review_required": "Verify every required job condition and matrix, the actual batch producer, and that no focused reservation is needed."}


def validate_generated_release(clone: str, base: str, head: str, version: str) -> None:
    """Only generated version edits and changelog prose may differ from the trusted base."""
    metadata = {side: core_policy.blobs_at(clone, commit, set(release_scope.SCOPE))
                for side, commit in (("base", base), ("head", head))}
    raw = core_policy.git_read(clone, "diff", "--raw", "--no-abbrev", "--no-renames", "--ignore-submodules=none", "-z", base, head, "--")
    release_scope.validate(lambda side, path: metadata[side][path], raw, version)
