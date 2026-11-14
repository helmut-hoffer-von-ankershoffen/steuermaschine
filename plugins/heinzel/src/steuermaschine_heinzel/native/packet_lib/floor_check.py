#!/usr/bin/env python3
"""hz-floor-check [--core DIR] [--repo OWNER/NAME] [--base REV] WORKTREE_OR_BRANCH: is this admitted core PR ready for the merge gate?

Read-only, local plus three `gh` reads (PR list, repository variables, head check runs). Mirrors the preconditions of
Core scripts/focused_gate.py (`pull_identity`, `verify`) so a head is not admitted and then fails like #778 did:

  1. an open PR exists for the branch, targets main and its head is the local head (pushed);
  2. the PR is NOT a draft (`pr[draft] is False` in pull_identity). #778 failed because merge-gate ran on a draft:
     never leave an admitted PR in draft; `gh pr ready N` before the push-triggered merge-gate, or rerun merge-gate;
  3. a focused plan `pull_request:<head>` is in FOCUSED_CI_PLANS / FOCUSED_CI_PLANS_V2_* (decoded by the trusted
     origin/main focused_plan.py in an isolated interpreter), its base is origin/main or an ancestor of it, and the
     head's blobs of the protected producer files equal the plan's execution pins;
  4. coverage floor evidence: every lane of the plan selection needs a successful `Focused tests (<lane>)` check run on the head (the whole-project `Coverage` job is skipped for focused PRs); a
     failed `coverage-floor` or `merge-gate` check run is reported;
  5. a whole-suite web selection (`web: ["web/ALL"]`, one id) needs origin/main's focused_job VITEST_ALL and stands
     alone in the web lane; its evidence is the same `Focused tests (web)` check run.

Prints READY (exit 0) or `NOT READY: reason; reason` (exit 1); exit 2 on usage or unreadable state."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_REPO = "helmut-hoffer-von-ankershoffen/heinzel"
LEGACY = "FOCUSED_CI_PLANS"
SLOTS = tuple(f"FOCUSED_CI_PLANS_V2_{i}" for i in range(4))
DECODE = r'''
import json, os, sys, types
root = sys.argv[1]
sys.path.insert(0, root)
pkg = types.ModuleType("scripts"); pkg.__path__ = [os.path.join(root, "scripts")]; pkg.__package__ = "scripts"
sys.modules["scripts"] = pkg
from scripts import focused_plan as f
req = json.load(sys.stdin)
reg = f.plan_registry(req["legacy"], tuple(req["slots"]))
plan = reg.get(req["key"])
print(json.dumps({"plan": plan, "protected": list(f.PROTECTED_FILES), "vitest_all": getattr(f, "VITEST_ALL", None)}, sort_keys=True))
'''


class Unreadable(Exception):
    pass


def run(cmd, cwd=None):
    done = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=120)
    if done.returncode:
        raise Unreadable(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} failed: {done.stderr.strip()[:200]}")
    return done.stdout


def local_state(target, base, git=run):
    """(worktree-or-core path, branch, head sha, base sha) for a worktree path or a branch name."""
    path = Path(target).expanduser()
    if path.is_dir():
        where = str(path)
        branch = git(["git", "-C", where, "branch", "--show-current"]).strip()
        head = git(["git", "-C", where, "rev-parse", "HEAD"]).strip()
    else:
        where, branch = None, target
        head = None
    return where, branch, head


def decode_plan(core, legacy, slots, key):
    """Run the trusted origin/main focused_plan in a scratch dir; return (plan or None, PROTECTED_FILES, VITEST_ALL or None)."""
    with tempfile.TemporaryDirectory(prefix="sm-floor-check-") as scratch:
        archive = subprocess.run(["git", "-C", core, "archive", "origin/main", "scripts"], capture_output=True)
        if archive.returncode:
            raise Unreadable("cannot archive origin/main scripts")
        subprocess.run(["tar", "-x", "-C", scratch], input=archive.stdout, check=True)
        done = subprocess.run([sys.executable, "-I", "-c", DECODE, scratch], input=json.dumps(
            {"legacy": legacy, "slots": list(slots), "key": key}).encode(), capture_output=True, cwd=scratch, env={"PATH": os.defpath})
        if done.returncode:
            raise Unreadable("focused plan registry unreadable: " + done.stderr.decode(errors="replace")[-200:])
        out = json.loads(done.stdout)
        return out["plan"], out["protected"], out.get("vitest_all")


def evaluate(facts):
    """Reasons a head is not ready; empty means READY. `facts` is plain data so tests need no network."""
    reasons = []
    pr = facts.get("pr")
    if not pr:
        return ["no open PR for the branch; open one and mark it ready"]
    if pr["state"].upper() != "OPEN":
        reasons.append(f"PR #{pr['number']} is {pr['state']}")
    if pr["baseRefName"] != "main":
        reasons.append(f"PR #{pr['number']} targets {pr['baseRefName']}, not main")
    if pr["isDraft"]:
        reasons.append(f"PR #{pr['number']} is a draft (pull_identity needs pr[draft] is False): `gh pr ready {pr['number']}`, then rerun merge-gate")
    if pr["headRefOid"] != facts["head"]:
        reasons.append(f"PR head {pr['headRefOid'][:10]} is not the local head {facts['head'][:10]} (push or fetch first)")
    plan = facts.get("plan")
    if plan is None:
        reasons.append(f"no focused plan pull_request:{facts['head'][:10]} in {LEGACY}*; admit the head")
        return reasons
    if plan.get("head") not in (None, facts["head"]):
        reasons.append("the plan is for another head")
    if plan.get("base") != facts["main"] and not facts["base_is_ancestor"]:
        reasons.append(f"plan base {str(plan.get('base'))[:10]} is neither origin/main nor its ancestor; re-admit on current main")
    for path, blob in sorted((plan.get("execution") or {}).items()):
        if facts["blobs"].get(path) != blob:
            reasons.append(f"protected file differs from the plan pin: {path}")
    if set(plan.get("execution") or {}) != set(facts["protected"]):
        reasons.append("plan execution pins do not cover the protected producer files")
    sentinel = facts.get("vitest_all")
    for lane, tests in sorted((plan.get("selection") or {}).items()):
        if "web/ALL" in (tests or []) or (sentinel and sentinel in (tests or [])):
            if not sentinel:
                reasons.append(f"{lane} selects the whole Vitest suite (web/ALL) but origin/main's focused_job.py has no VITEST_ALL")
            elif lane != "web" or tests != [sentinel]:
                reasons.append(f"the whole Vitest suite {sentinel} is only a web lane on its own, not {lane} {tests}")
    checks = {c["name"]: c.get("conclusion") for c in facts["checks"]}
    for lane in sorted(plan.get("selection") or {}):
        if checks.get(f"Focused tests ({lane})") != "success":
            reasons.append(f"coverage floor evidence missing: `Focused tests ({lane})` on the head is {checks.get(f'Focused tests ({lane})') or 'absent'}")
    if plan.get("production") and "python" not in (plan.get("selection") or {}):
        reasons.append("the plan has production files but no python coverage lane")
    for name in ("coverage-floor", "merge-gate"):
        if checks.get(name) == "failure":
            reasons.append(f"{name} check on the head failed")
    return [r for r in reasons if r]


def gather(target, core, repo, base, git=run, gh=run, decode=decode_plan):
    where, branch, head = local_state(target, base, git)
    cwd = where or core
    if head is None:
        head = git(["git", "-C", core, "rev-parse", "--verify", f"{branch}^{{commit}}"]).strip()
    main = git(["git", "-C", core, "rev-parse", "--verify", f"{base}^{{commit}}"]).strip()
    prs = json.loads(gh(["gh", "pr", "list", "-R", repo, "--head", branch, "--state", "open", "--json", "number,state,isDraft,baseRefName,headRefOid"]))
    values = {v["name"]: v["value"] for v in json.loads(gh(["gh", "api", f"repos/{repo}/actions/variables?per_page=100"]))["variables"]}
    plan, protected, *sentinel = decode(core, values.get(LEGACY, ""), tuple(values.get(s, "") for s in SLOTS if s in values), f"pull_request:{head}")
    blobs = {}
    for path in protected:
        try:
            blobs[path] = git(["git", "-C", cwd, "rev-parse", f"{head}:{path}"]).strip()
        except Unreadable:  # file absent at this head: reported as a pin difference
            blobs[path] = ""
    ancestor = bool(plan) and subprocess_ok(git, core, plan.get("base"), main)
    checks = json.loads(gh(["gh", "api", f"repos/{repo}/commits/{head}/check-runs?per_page=100"])).get("check_runs", [])
    return {"pr": prs[0] if prs else None, "head": head, "main": main, "plan": plan, "protected": protected, "blobs": blobs,
            "base_is_ancestor": ancestor, "checks": checks, "vitest_all": sentinel[0] if sentinel else None}


def subprocess_ok(git, core, ancestor, main):
    try:
        git(["git", "-C", core, "merge-base", "--is-ancestor", str(ancestor), main])
        return True
    except Unreadable:
        return False

