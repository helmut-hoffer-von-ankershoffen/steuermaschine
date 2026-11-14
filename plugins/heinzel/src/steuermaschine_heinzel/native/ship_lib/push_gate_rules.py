"""Decision logic for `hz-push-gate`: should a CI-bearing push wait because main CI is starved? Pure functions, no I/O.

Rule: when the oldest queued main CI run has waited >= `min_age` minutes, a push to a PR branch (which spawns ~60 jobs
that compete with main CI for the 20-job / 4 macOS-ARM ceiling) must WAIT, unless the PR is on the keep list.
Keep list = PR numbers in research/ci-keep.txt, PRs named `fix PR #N` in research/known-failures.toml (fix for red main),
and the oldest ready PR (all checks completed, none failed) when the file contains the line `auto` or is absent.
"""
import re
from datetime import datetime, timezone

WAIT, OK = 75, 0  # exit codes (75 = EX_TEMPFAIL: try again later)


def parse_keep(text):
    """ci-keep.txt -> (set of PR numbers, auto flag). `#` starts a comment; `auto` enables the oldest-ready rule."""
    nums, auto = set(), text is None or not text.strip() or False
    for line in (text or "").splitlines():
        for tok in re.split(r"[\s,]+", line.split("#", 1)[0].strip()):
            if tok.isdigit():
                nums.add(int(tok))
            elif tok == "auto":
                auto = True
    return nums, auto


def fix_prs(known_text):
    """PR numbers named as `fix PR #N` in known-failures.toml notes."""
    return {int(n) for n in re.findall(r"fix PR\s*#(\d+)", known_text or "", flags=re.I)}


def queue_age_min(created_at_list, now=None):
    """Minutes the oldest queued main CI run has waited (None when nothing is queued). Timestamps are gh ISO strings."""
    if not created_at_list:
        return None
    now = now or datetime.now(timezone.utc)
    oldest = min(datetime.strptime(c, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) for c in created_at_list)
    return int((now - oldest).total_seconds() // 60)


def oldest_ready(prs):
    """Number of the oldest open non-draft PR whose checks are all completed and none failed (gh pr list json)."""
    ready = []
    for p in prs:
        roll = p.get("statusCheckRollup") or []
        if p.get("isDraft") or not roll:
            continue
        if all(c.get("status") == "COMPLETED" and c.get("conclusion") in ("SUCCESS", "SKIPPED", "NEUTRAL") for c in roll):
            ready.append((p.get("createdAt", ""), p["number"]))
    return min(ready)[1] if ready else None


def decide(pr, age, min_age, keep, auto_ready=None):
    """Return (exit_code, message). `pr` may be None for a branch without a PR yet (never kept)."""
    if age is None or age < min_age:
        return OK, f"OK: main CI not starved (oldest queued {'none' if age is None else f'{age} min'} < {min_age}): push"
    if pr is not None and pr in keep:
        return OK, f"OK: PR #{pr} is on the keep list (fix for red main or ci-keep.txt); main CI queued {age} min, push anyway"
    if pr is not None and pr == auto_ready:
        return OK, f"OK: PR #{pr} is the oldest ready PR; main CI queued {age} min, push anyway"
    who = f"PR #{pr}" if pr is not None else "a new branch"
    return WAIT, (f"WAIT: main CI has been queued {age} min (>= {min_age}); {who} is not on the keep list. Defer the push "
                  f"(commit locally), retry after `sm go` shows main CI running or when `sm head push {pr or '<PR>'}` prints OK. Keep list: research/ci-keep.txt")


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


def release_hold(found, waived=False):
    """(exit_code, message) for the release-candidate hold. `waived` = coordinator waiver (--min or the setting head_push_minimum, same as the
    main-starvation waiver): allow but still name the run. Past the cap: allow with a loud warning."""
    if found is None:
        return OK, ""
    rid, age, capped = found
    if waived:
        return OK, f"OK: release candidate CI run {rid} is active ({age} min), waiver (--min): push"
    if capped:
        return OK, f"WARNING: release candidate CI run {rid} has been active {age} min (>= {RELEASE_CAP_MIN} cap); hold lifted, push. Check the candidate is not stuck"
    return WAIT, (f"WAIT: release candidate CI run {rid} (release/v*, {age} min of max {RELEASE_CAP_MIN}) is active; non-P0 pushes wait so the candidate "
                  f"gets the runners. Commit locally and retry; P0 waiver: sm head push PR --min MINUTES")
