#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""hz-merge-train <repo> <pr>...: for each PR in order, update it against main, wait for checks, squash-merge only when
every check is green at that exact head. A red or conflicted PR is reported and skipped; nothing is forced.

The core queue may use queue_core to merge a green, behind PR without updating it; direct and non-core calls still update.
The advisory `review` check is ignored, and so are `merge-gate` and `coverage-floor` only until main requires them; GitHub
refuses the merge itself once they are required. BEHIND is updated again, UNKNOWN is waited out, and while
~/.cache/hz-merge-freeze exists nothing merges to core main. A repository with no aggregate check is judged on all its checks.

One narrow waiver (research/release-pr-acceptance-advisory.md): on a core release/vX.Y.Z PR, the pull_request-triggered
`Acceptance (<os>)` dry runs test the PUBLISHED build, not the candidate. They are advisory only when the previous
release's tail receipt (~/.cache/hz-release/vP.json) is finished, NOT accepted and absorbed_by exactly X.Y.Z. The decision
is printed and appended to the interventions log. Otherwise they block as before; required checks are never waived."""
import sys as _sys
if _sys.argv[1:2] in (['--help'], ['-h']):
    print('merge train <repo> <pr>...: for each PR in order update against main, wait for checks, squash-merge only when green at that head. Red or conflicted PRs are reported and skipped. Called by sm queue.')
    raise SystemExit(0)

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import edge  # moved: siblings are imported relatively, no sys.path entry
from ... import environment  # moved: the process context is a typed model
from . import hzlib  # noqa: E402
from steuermaschine.config import child_environment
from ... import state as statedir  # the plugin's state paths
from . import core_facts  # noqa: E402

CORE = "helmut-hoffer-von-ankershoffen/heinzel"
FREEZE = statedir.path("merge-freeze")
FAILED = ("FAILURE", "CANCELLED", "TIMED_OUT")
RELEASE_DIR = statedir.path("release")  # hz-release tail receipts vX.Y.Z.json
INTERVENTIONS = edge.records_dir() / "research/mini-onboarding-2026-11-07/interventions.log"
RELEASE_BRANCH = re.compile(r"release/v([0-9]+)\.([0-9]+)\.([0-9]+)")
LOGGED: set[tuple[str, str, frozenset]] = set()  # (pr, head, checks) already logged: once per decision, not per poll
NEVER_WAIVED = core_facts.CONTEXTS  # the ruleset's required checks (Core's release policy); candidate CI jobs are never named Acceptance (
MAX_MERGE_DIAGNOSTIC_BYTES = 512
TERMINAL_ESCAPE = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~])")
MERGE_CREDENTIAL = re.compile(
    r"\bauthorization\s*[:=]\s*(?:(?:bearer|basic|token)\s+)?[^\s,;]+"
    r"|\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|\b(?:gh[pousr]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+)"
    r"|(?<=://)[^\s/@]+@|(?<=[?&])(?:access_token|token)=[^&\s]+",
    re.IGNORECASE,
)


def gh(*args: str) -> tuple[int, str]:
    is_merge = args[:2] == ("pr", "merge")
    # API debugging can print request headers and full response bodies.
    env = child_environment(GH_DEBUG="") if is_merge else None
    limit = hzlib.gh_timeout(args)
    try:
        done = edge.run(
            ["gh", *args], capture_output=True, text=True, check=False,
            env=env, errors="replace" if is_merge else None, timeout=limit,
        )
    except subprocess.TimeoutExpired:
        return hzlib.GH_TIMED_OUT, f"gh {' '.join(args[:2])} timed out after {limit}s"
    if is_merge and done.returncode and done.stderr.strip():
        return done.returncode, done.stderr
    return done.returncode, done.stdout


def merge_diagnostic(text: str) -> str:
    """Keep a bounded single-line failure reason for the queue and intervention log."""
    text = TERMINAL_ESCAPE.sub("", text)
    for token in environment.tokens().values():
        text = text.replace(token, "[redacted]")
    text = " ".join("".join(char if char.isprintable() else " " for char in text).split())
    safe = MERGE_CREDENTIAL.sub("[redacted]", text).encode("utf-8")
    if len(safe) > MAX_MERGE_DIAGNOSTIC_BYTES:
        return safe[:MAX_MERGE_DIAGNOSTIC_BYTES - 3].decode("utf-8", errors="ignore") + "..."
    return safe.decode("utf-8")


def view(repo: str, pr: str, fields: str) -> dict:
    code, out = gh("pr", "view", pr, "-R", repo, "--json", fields)
    try:
        return json.loads(out) if code == 0 else {}
    except ValueError:
        return {}


SETTLE_POLLS = 80  # 45 s apart: one hour for the checks of one PR to settle
FREEZE_POLLS = 60  # 60 s apart: one hour for a merge freeze to lift before the merge gives up
MERGE_UNKNOWN = "merge state unknown"  # the queue keeps such a PR, raises an alarm and reads it again next tick


def read_back(repo: str, pr: str) -> str:
    """After a failed `gh pr merge`: "merged", "not merged" (open or closed) or "unknown" (the state cannot be read)."""
    state = view(repo, pr, "state,mergeCommit").get("state")
    if state == "MERGED":
        return "merged"
    return "not merged" if state in ("OPEN", "CLOSED") else "unknown"


def settled(rollup: list) -> bool:
    """Every check completed (a status without a `status` field counts as not completed). The aggregate is read only when
    the repository has one; others (heinzel-sf) name their gate differently. At least one check must exist."""
    return bool(rollup) and all(c.get("status") == "COMPLETED" for c in rollup)


def previous_release(version: tuple[int, int, int]) -> tuple[str, dict] | None:
    """The newest tail receipt below `version`, or None. An unreadable receipt is no evidence: None."""
    found = []
    for path in RELEASE_DIR.glob("v*.json"):
        match = re.fullmatch(r"v([0-9]+)\.([0-9]+)\.([0-9]+)\.json", path.name)
        if match and (key := tuple(int(g) for g in match.groups())) < version:
            found.append((key, path))
    if not found:
        return None
    key, path = max(found)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return ".".join(map(str, key)), data if isinstance(data, dict) else {}


def published_dry_run(repo: str, check: dict, head: str) -> bool:
    """An `Acceptance (<os>)` check of a pull_request run at this head: acceptance.yml then runs EXPECTED_VERSION=published."""
    name = check.get("name") or ""
    if name in NEVER_WAIVED or not name.startswith("Acceptance (") or check.get("workflowName") != "Acceptance":
        return False
    match = re.search(r"/actions/runs/([0-9]+)", check.get("detailsUrl") or "")
    if not match:
        return False
    code, out = gh("api", f"repos/{repo}/actions/runs/{match.group(1)}", "--jq", "[.event, .head_sha] | @tsv")
    return code == 0 and out.strip().split("\t") == ["pull_request", head]


def release_acceptance_waiver(repo: str, pr: str, branch: str, head: str, rollup: list) -> set[str]:
    """Acceptance dry-run checks treated advisory on this PR (usually none). Loud: printed and logged when non-empty."""
    match = RELEASE_BRANCH.fullmatch(branch or "")
    if repo != CORE or not match:
        return set()
    version = tuple(int(g) for g in match.groups())
    found = previous_release(version)
    if found is None:
        return set()
    previous, data = found
    this = ".".join(map(str, version))
    if not data.get("finished") or data.get("accepted") is True or data.get("absorbed_by") != this:
        return set()
    waived = {c.get("name") for c in hzlib.latest_checks(rollup) if c.get("conclusion") in FAILED and published_dry_run(repo, c, head)}
    if waived and (pr, head, frozenset(waived)) not in LOGGED:
        why = (f"previous release {previous} NOT ACCEPTED and absorbed_by {this} ({data.get('absorb_reason', '')}); "
               "pull_request Acceptance dry runs test the published build, not this candidate")
        line = f"#{pr} ADVISORY on release PR {branch} at {head[:9]}: {', '.join(sorted(waived))}: {why}"
        print(line, flush=True)
        log = Path(edge.setting("queue_interventions_log") or INTERVENTIONS)
        try:
            with log.open("a") as handle:
                handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} merge train: {line}\n")
        except OSError as error:
            print(f"#{pr} interventions log not written ({error}); waiver withdrawn", flush=True)
            return set()
        LOGGED.add((pr, head, frozenset(waived)))
    return waived


def update(repo: str, pr: str) -> None:
    gh("pr", "update-branch", pr, "-R", repo)
    time.sleep(30)


def train(repo: str, pr: str, ignore_freeze: bool = False, queue_core: bool = False, expected_head: str = "", pre_merge_guard: Callable[[], bool] | None = None) -> tuple[bool, str]:
    """Returns (merged, detail): what happened, in the words printed."""
    info = view(repo, pr, "state,isDraft,headRefOid,headRefName")
    branch = info.get("headRefName", "")
    if expected_head and info.get("headRefOid") != expected_head:
        return False, "reviewed head changed; independent review and requeue required"
    state = f"{info.get('state', '')}\t{str(info.get('isDraft', '')).lower()}"
    if state != "OPEN\tfalse":
        print(f"#{pr} skip: {state}", flush=True)
        return False, f"skip: {state}"
    queue_core = queue_core and repo == CORE
    if not queue_core:
        update(repo, pr)
    merged, head, status, merge_failure = False, "", "", ""
    advisory = hzlib.advisory_checks(repo)
    polls = 0
    for _ in range(3):
        # a check that doesn't decide isn't waited for: the gate can wait 30 min for Sonar
        while not settled([c for c in view(repo, pr, "statusCheckRollup").get("statusCheckRollup", []) if c.get("name") not in advisory - {"review"}]):
            polls += 1
            if polls > SETTLE_POLLS:  # bounded: a check that never completes must not hold the queue lock forever
                detail = f"checks did not settle within {SETTLE_POLLS * 45 // 60} min; requeue once they complete"
                print(f"#{pr} NOT merged, {detail}", flush=True)
                return False, detail
            time.sleep(45)
        info = view(repo, pr, "headRefOid,mergeStateStatus,statusCheckRollup")
        head, status = info.get("headRefOid", ""), info.get("mergeStateStatus", "")
        if expected_head and head != expected_head:
            return False, "reviewed head changed; independent review and requeue required"
        if queue_core and not settled([c for c in info.get("statusCheckRollup", []) if c.get("name") not in advisory - {"review"}]):
            continue  # the head or its checks changed after the preceding poll
        # the advisory Claude review failing to run is not a verdict
        waived = release_acceptance_waiver(repo, pr, branch, head, info.get("statusCheckRollup", []))
        bad = [c for c in hzlib.latest_checks(info.get("statusCheckRollup", [])) if not hzlib.non_blocking(c.get("name"), advisory | waived) and c.get("conclusion") in FAILED]
        if status == "BEHIND" and not queue_core:
            update(repo, pr)
            continue
        if status == "UNKNOWN":  # GitHub is still computing it after another merge
            time.sleep(60)
            continue
        passed = {c.get("name") for c in info.get("statusCheckRollup", []) if c.get("conclusion") == "SUCCESS"}
        absent = hzlib.tool_required_checks(repo) - passed
        if absent:
            print(f"#{pr} required by the tools, not passed: {', '.join(sorted(absent))}", flush=True)
        elif (status in ("CLEAN", "UNSTABLE") or (queue_core and status == "BEHIND")) and not bad:
            frozen_polls = 0
            while repo == CORE and not ignore_freeze and hzlib.merge_frozen(FREEZE):  # main's CI must finish before a release
                frozen_polls += 1
                if frozen_polls > FREEZE_POLLS:  # bounded: a freeze that outlives the wait must not hold the queue lock
                    detail = f"merge freeze still on after {FREEZE_POLLS} min; requeue once it lifts"
                    print(f"#{pr} NOT merged, {detail}", flush=True)
                    return False, detail
                time.sleep(60)
            if pre_merge_guard is not None:
                try:
                    allowed = pre_merge_guard()
                except Exception:
                    allowed = False
                if not allowed:
                    return False, "main evidence changed or unavailable after waits; reassessment required"
                if repo == CORE and not ignore_freeze and hzlib.merge_frozen(FREEZE):
                    return False, "merge freeze appeared during final evidence validation"
            code, output = gh("pr", "merge", pr, "-R", repo, "--squash", "--match-head-commit", head)
            merged = code == 0
            if not merged:
                merge_failure = f"gh pr merge exited {code}"
                if reason := merge_diagnostic(output):
                    merge_failure += f": {reason}"
                outcome = read_back(repo, pr)  # a failed call may still have merged: never assume either way
                if outcome == "merged":
                    print(f"#{pr} merged at {head[:8]} (read back after {merge_failure})", flush=True)
                    return True, head[:8]
                if outcome == "unknown":
                    detail = f"{MERGE_UNKNOWN}: {merge_failure}; the read-back of its state failed"
                    print(f"#{pr} {detail}", flush=True)
                    return False, detail
        break
    if merged:
        print(f"#{pr} merged at {head[:8]}", flush=True)
        return True, head[:8]
    fields = "headRefOid,statusCheckRollup" if expected_head else "statusCheckRollup"
    current = view(repo, pr, fields)
    if expected_head and current.get("headRefOid") not in (None, expected_head):
        detail = "reviewed head changed; independent review and requeue required"
        if not merge_failure:
            return False, detail
    else:
        rollup = current.get("statusCheckRollup", [])
        failing = ",".join(c.get("name") or "" for c in rollup if c.get("conclusion") == "FAILURE")
        detail = f"state {status or '?'}: {failing}"
    if merge_failure:
        detail = f"{detail.rstrip(': ')}; {merge_failure}"
    print(f"#{pr} NOT merged, {detail}", flush=True)
    return False, detail


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: merge train <repo> <pr>...")
    for pr in sys.argv[2:]:
        train(sys.argv[1], pr)

