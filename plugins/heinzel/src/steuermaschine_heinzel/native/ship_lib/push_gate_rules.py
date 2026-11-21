"""Decision logic for `hz-push-gate`: should a CI-bearing push wait because main CI is starved? Pure functions, no I/O.

Rule: a starved main CI queue (oldest run waited >= `min_age` minutes) is one info line, never a WAIT.
Keep list = PR numbers in research/ci-keep.txt, PRs named `fix PR #N` in research/known-failures.toml (fix for red main),
and the oldest ready PR (all checks completed, none failed) when the file contains the line `auto` or is absent.
"""
import re
from datetime import datetime, timezone

WAIT, OK = 75, 0  # exit codes (75 = EX_TEMPFAIL: try again later)


def queue_age_min(created_at_list, now=None):
    """Minutes the oldest queued main CI run has waited (None when nothing is queued). Timestamps are gh ISO strings."""
    if not created_at_list:
        return None
    now = now or datetime.now(timezone.utc)
    oldest = min(datetime.strptime(c, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) for c in created_at_list)
    return int((now - oldest).total_seconds() // 60)


def decide(age, min_age):
    """Return (exit_code, message): always OK; a starved main queue is one info line."""
    if age is None or age < min_age:
        return OK, f"OK: main CI not starved (oldest queued {'none' if age is None else f'{age} min'} < {min_age}): push"
    return OK, f"OK: main CI has been queued {age} min (>= {min_age}); push anyway"


def auto_defer_plan(age, min_age, keep, last_run_ts, now_ts, cooldown_min=30, special_active=False):
    """hz-tick action: None when main CI is not starved, the last auto-defer ran < cooldown_min ago, or an Acceptance/compose/release-tag run is
    active (special_active: they own macOS runners ~10-20 min), else the argv for
    `hz-ci-defer --if-main-starved MIN [--keep PRS]`. Never touches main or release/* (hz-ci-defer guarantees it)."""
    if age is None or age < min_age:
        return None
    if special_active or (last_run_ts is not None and now_ts - last_run_ts < cooldown_min * 60):
        return None
    argv = ["sm", "ci", "defer", "--if-main-starved", "--minutes", str(min_age)]
    if keep:
        argv += ["--keep", ",".join(str(n) for n in sorted(keep))]
    return argv


SPECIAL_WORKFLOWS = ("acceptance", "release tag", "publish", "standard app composition", "release one plugin", "release bumped plugins")


def special_run_active(runs):
    """True when a run of an Acceptance/Release tag/Publish/compose/plugin-release workflow is queued or in progress, by workflow NAME,
    regardless of event or branch (gh json: workflowName,status)."""
    return any(r.get("status") in ("queued", "in_progress") and (r.get("workflowName") or "").strip().lower() in SPECIAL_WORKFLOWS for r in runs)


def gate_command_pushes(argv):
    """Whether a command run through hz-gate pushes to a remote (git push / make pr): these must pass hz-push-gate first."""
    words = [w for w in argv if not w.startswith("-")]
    return words[:2] == ["git", "push"] or words[:2] == ["make", "pr"]


RELEASE_CAP_MIN = 45  # a release hold never blocks a push longer than this, measured from the oldest active candidate run
NEVER_HELD = ("release/", "bump/")  # prefixes; main is matched exactly (main-x or maintenance/x are held like any branch)


def never_held(branch: str) -> bool:
    """A push to release/*, bump/* or exactly main goes on during a hold."""
    return branch == "main" or branch.startswith(NEVER_HELD)


def release_run_active(runs, target_branch=None, now=None, cap_min=RELEASE_CAP_MIN):
    """(run_id, age_min, capped) of the oldest queued/in-progress CI run on release/v* (gh json: databaseId,headBranch,status,createdAt),
    or None. Pushes to release/*, bump/* and main are never held. capped=True once the run is older than cap_min (hold ends, loud warning)."""
    if target_branch == "HEAD" or (target_branch and never_held(target_branch)):  # detached HEAD: refspec unknown, never held
        return None
    act = [r for r in runs if str(r.get("headBranch", "")).startswith("release/v") and r.get("status") in ("queued", "in_progress", "waiting", "pending")]
    if not act:
        return None
    r = min(act, key=lambda x: x.get("createdAt", ""))
    age = queue_age_min([r["createdAt"]], now) if r.get("createdAt") else 0
    return r.get("databaseId"), age, age >= cap_min


def release_hold(found):
    """(exit_code, message) for an active release candidate: always OK, one info line. The candidate is protected by the merge freeze, not by pushes."""
    if found is None:
        return OK, ""
    rid, age, _ = found
    return OK, f"OK: release candidate CI run {rid} is active ({age} min); PR CI may queue behind it, push"
