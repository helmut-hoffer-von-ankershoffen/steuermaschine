#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""hz-queue add [--after PR]... [--reviewed "reason"] [--reviewed-head SHA] [--fixes-main | --diagnostics-main "reason" | --safety-fix-main "reason" | --mitigation-main "reason"] <pr>...  |  list  |  why  |  drop <pr>...  |  main-rerun [--dry-run]  |  run [--once] [--interval SECONDS]

One merge queue that runs by itself. `add 393 385 "plugins 38"` queues PRs (core by number, others with a prefix);
`add --after 390 "plugins 38"` holds a PR until 390 has merged. `run` loops (launchd or a background job) and each tick:
reads every queued PR, updates a BEHIND branch outside core, and marks a failing PR `needs work` with the failing checks and tests. Ready PRs merge as one
batch (up to 5 core PRs) every 5 minutes through hz-merge-train, only onto a main whose latest completed run is green and while ~/.cache/hz-merge-freeze is
absent. The advisory `review` check is ignored, and `merge-gate` and `coverage-floor` until main requires them. State: ~/.cache/hz-queue.json (HZ_QUEUE_STATE overrides), log:
~/.cache/hz-queue.log. The idle interval wakes early on a local queue edit, the last merge hold being released,
or tool edits; this does not shorten the five-minute batch cadence. Only one `run` works at a time (flock on ~/.cache/hz-queue.lock); a second waits for the first to
exit, so launchd's instance takes over when a session's ends. `hz-note` gets one line when a PR merges or is stuck. Add a PR again to retry it. `add` runs hz-diff-guard on each diff and refuses a PR that raises timeouts, skips or deletes tests, adds retries
or continue-on-error, lowers a threshold and the like, unless `--reviewed "reason"` is given (the reason is logged with hz-note).
For one normal PR (any repository), --reviewed-head FULL_SHA with --reviewed pins admission to the independently reviewed commit.
The normal queue gates still apply. A changed head needs independent review and requeue.
Owner 11-03: --diagnostics-main permits one independently reviewed, head-pinned core diagnostic PR while main is red.
It requires --reviewed and --diagnostic-head SHA, preserves all PR gates, holds and cadence, and neither claims a fix
nor replaces the failed batch. Example: add --diagnostics-main "measure timeout cause" --diagnostic-head FULL_SHA
--reviewed "independent reviewer NO weakening at FULL_SHA" 481. This does not close the flake or resume a release.
Owner 11-03: --safety-fix-main permits one independently reviewed, head-pinned core safety fix while main is red.
It requires --reviewed and --safety-head FULL_SHA and preserves the same checks, holds and failed-main evidence.
It fixes a separately established safety defect, without claiming to fix the red CI or close the flake.
Owner 11-03: --mitigation-main admits one independently reviewed measured mitigation, with --mitigation-head FULL_SHA
and --mitigation-evidence recording its observed improvement. It preserves the same checks, holds and failed-main
evidence. The cause remains unresolved; mitigation PR green alone cannot resume a release. The later owner
progress policy permits independently verified full first-attempt descendant-main recovery without closing incidents.
Owner 11-03 fast-track: --reviewed-progress-main with --progress-head admits independently reviewed, owner-approved
exact-head PRs in batches up to five despite the already-observed main failure. All real PR checks, holds and cadence
stay required. A changed completed-main anchor stops this admission until fresh review and requeue. This is an
intentional main-admission policy change, not a fix or authority to release; incidents and failed batches remain.
All red-main exceptions require proven failed completed CI; unavailable or inconclusive main evidence holds core.
Owner 11-07 FIX-FORWARD LANE (bead ox6): add --fix-forward --for "JOB=BEAD[@PR]" [--reviewed R] [--dry-run] N lets one
core fix PR merge while main is red, through the normal train (GitHub's required checks, no --admin, no force-push).
It is admitted only when: PR CI on its current head is fully green (every check SUCCESS/SKIPPED/NEUTRAL incl. the aggregate,
none failed, cancelled, pending or deferred by hz-ci-defer); hz-diff-guard PASS or --reviewed; the PR title is
fix|test|ci (no feat, no "!"); every failing job of the red main run is mapped by --for (JOB = case-insensitive
substring; a bare BEAD covers every job) to a flaky/bug/broken-test bead addressed by this PR or by @PR, an earlier
merged PR (the PR or the bead must name the other); at most one fix-forward merge per red main run; no merge freeze
and no release/* run or unfinished hz-release. The head is pinned (a new push needs requeue). Every use goes to
hz-note. It does not clear the pinned failure: the next first-attempt full main pass does, as before.
Owner-delegated policy 11-07 MAIN-RERUN (research/main-red-policy-2026-11-07.md (iii)+(ii)), MERGES ONLY: `main-rerun
[--dry-run]` (each tick runs it too) reruns the failed jobs of main's latest run once (`gh run rerun --failed`) when that
run is attempt 1, completed red, and EVERY failing job except the aggregate matches by job name an entry of
research/known-failures.toml within its `until` (default `added` + 7 days; past it the entry is not KNOWN); never with
a release/* run, merge freeze or unfinished hz-release; never twice per run; logged with hz-note. A separately reviewed
research/known-main-infra.toml can classify one exact main attempt of a pinned-tool HTTP 504 incident. Manual reruns
require the idle queue lock. If a runner owns it, let the automatic tick act; --dry-run remains read-only. Unknown
failures: no rerun, fix forward. When attempt 2 of that same run is fully green and all attempt-1 failures were KNOWN, the queue
treats main as restored for merges (state `main_restored`, hz-note `restored-by-rerun(KNOWN: bead)`); the pinned
failure stays, and hz-release/hz-train still require a first-attempt full green main run."""

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import edge  # moved: siblings are imported relatively, no sys.path entry
from . import combine_notes as hz_combine_notes  # noqa: E402
from . import hzlib  # noqa: E402
from . import known_main_infra  # noqa: E402
from . import main_batch  # noqa: E402
from . import main_checkpoint  # noqa: E402
from . import main_lanes  # noqa: E402
from . import release_candidate as rc  # noqa: E402
from . import core_facts  # noqa: E402
from . import main_plan  # noqa: E402
from . import release_flow  # noqa: E402
from ... import state as statedir  # the plugin's state paths

BATCH_SECONDS = 300
MAX_CORE_BATCH = 5  # explicit public proof may delegate intermediate runs; a red final run names the batch to bisect
SITE_PATH = re.compile(r"docs/.*|[^/]*\.md|mkdocs\.yml|scripts/(site_[a-z_]+|check_docs)\.py")
SITE_EXCEPT = re.compile(r"(CHANGELOG|README)\.md|docs/.*\.sh")
FIRST = edge.records_dir() / "research" / "queue-first.txt"
DONE = ("merged", "needs work")


def state_path() -> Path:
    return Path(edge.setting("queue_state_file") or statedir.path("queue.json"))


class StateUnreadable(RuntimeError):
    """The queue's state file exists but cannot be read or parsed: never an empty queue."""


_STATE_LOCK = threading.local()  # depth per thread: reentrant in one thread, exclusive between threads and processes


def state_lock_path() -> Path:
    return state_path().with_suffix(".state.lock")


@contextlib.contextmanager
def state_lock():
    """Hold an exclusive flock around one load-change-save of the state file. Every writer (`add`, `drop`, the tick's
    updates) takes it, so two writers never lose an update. Reentrant within this process."""
    if getattr(_STATE_LOCK, "depth", 0):
        _STATE_LOCK.depth += 1
        try:
            yield
        finally:
            _STATE_LOCK.depth -= 1
        return
    state_lock_path().parent.mkdir(parents=True, exist_ok=True)
    with state_lock_path().open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        _STATE_LOCK.depth = 1
        try:
            yield
        finally:
            _STATE_LOCK.depth = 0
            fcntl.flock(handle, fcntl.LOCK_UN)


def unreadable(why: str) -> StateUnreadable:
    """Raise the alarm (best effort) and return the error to raise: a broken state file is never read as empty."""
    text = f"queue state {state_path()} is unreadable ({why}); nothing is merged or changed until it is repaired"
    try:
        edge.raise_alarm("queue-state", "ALERT", text)
    except Exception:  # noqa: BLE001 - the refusal below is the loud part; a failed alarm write must not hide it
        pass
    return StateUnreadable(text)


def load() -> dict:
    try:
        text = state_path().read_text()
    except FileNotFoundError:
        data = {}
    except OSError as error:
        raise unreadable(f"{type(error).__name__}: {error.strerror or error}") from None
    else:
        try:
            data = json.loads(text)
        except ValueError as error:
            raise unreadable(f"not JSON: {error}") from None
        if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
            raise unreadable("not a queue state object")
    return {"items": data.get("items", []), "last_batch": data.get("last_batch", 0), "blocked": data.get("blocked", ""),
            "batch": data.get("batch", {}), "red_batch": data.get("red_batch", {}), "main_failure": data.get("main_failure", {}), "progress_history": data.get("progress_history", []), "batch_proof": data.get("batch_proof", {}),
            "fix_forward_runs": data.get("fix_forward_runs", {}), "main_reruns": data.get("main_reruns", {}),
            "main_restored": data.get("main_restored", {}), "main_rerun_last": data.get("main_rerun_last", ""),
            **({"main_proof": data["main_proof"]} if data.get("main_proof") else {})}  # the tick's main-proof attempt


def save(data: dict) -> None:
    state_path().parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path().with_name(f"{state_path().name}.{os.getpid()}.tmp")  # per process: two writers never share one
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(state_path())


def update(key: str | None = None, **fields) -> None:
    """Change one entry (or the top level when key is None) against the freshest file, so `add` during a tick isn't lost."""
    with state_lock():
        data = load()
        if key is None:
            data.update(fields)
        else:
            for item in data["items"]:
                if item["key"] == key:
                    item.update(fields)
        save(data)


def log(text: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}"
    with (statedir.path("queue.log")).open("a") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def lock_path() -> Path:
    return statedir.path("queue.lock")


def acquire_lock(block: bool = True):
    """Hold ~/.cache/hz-queue.lock for the life of the process, so two `run` never overlap. A second runner (launchd while a
    session one lives) waits here until the first exits. Returns the open handle, or None when `block` is false and it is taken."""
    lock_path().parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path().open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
    except OSError:
        handle.close()
        return None
    return handle


def stuck_or_merged(item: dict, status: str, detail: str) -> None:
    update(item["key"], status=status, detail=detail)
    try:  # the item left the queue: its stall alarm no longer holds
        from . import stall as hz_alarm
        hz_alarm.clear(f"stall:{item['key']}", "queue")
    except Exception:  # noqa: BLE001, S110 - never stop the queue for an alarm file
        pass
    log(f"{item['key']}: {status} {detail}".strip())
    if item.get("noted") != status:
        hzlib.note(f"sm queue: {item['key'] if item['repo'] != hzlib.CORE else '#' + item['key']} {status}" + (f": {detail}" if detail and status != "merged" else ""))
        update(item["key"], noted=status)


def deps_met(item: dict, items: list[dict]) -> bool:
    for dep in item.get("after", []):
        try:
            repo, number, key = hzlib.split_key(dep)
        except ValueError:
            return False
        mine = next((i for i in items if i["key"] == key), None)
        if mine and mine["status"] == "merged":
            continue
        view = hzlib.ghj("pr", "view", number, "-R", repo, "--json", "state", default={}) or {}
        if view.get("state") != "MERGED":
            return False
    return True


def assess(item: dict, merge) -> str:
    """One look at a queued PR: `ready`, `wait`, or it has been marked merged / needs work. Acts on BEHIND and failures."""
    repo, number = item["repo"], item["pr"]
    info = hzlib.ghj("pr", "view", number, "-R", repo, "--json", "state,isDraft,headRefOid,mergeStateStatus,statusCheckRollup", default={}) or {}
    if not info:
        return "wait"
    if info["state"] == "MERGED" and item.get("merge_unknown"):
        return "ready"  # the tick records it as this batch's merge (attribution, bead stamp, merge sha) and clears the alarm
    if info["state"] == "MERGED":
        stuck_or_merged(item, "merged", "")
        return "done"
    if info["state"] == "CLOSED":
        stuck_or_merged(item, "needs work", "closed")
        return "done"
    reviewed_head = item.get("reviewed_head")
    kind = reviewed_exception(item) or ("fix-forward" if item.get("fix_forward") else "")
    if (reviewed_head or kind) and (not reviewed_head or info.get("headRefOid") != reviewed_head):
        stuck_or_merged(item, "needs work", f"{kind or 'reviewed'} head changed; independent review and requeue required")
        return "done"
    if info.get("isDraft"):
        stuck_or_merged(item, "needs work", "draft")
        return "done"
    advisory = hzlib.advisory_checks(repo)
    rollup = [c for c in hzlib.latest_checks(info.get("statusCheckRollup", [])) if not hzlib.non_blocking(c.get("name"), advisory)]
    if not merge.settled(rollup):
        record_wait(item, "wait", info, rollup, advisory)
        return "wait"
    bad = [c for c in rollup if c.get("conclusion") in merge.FAILED]
    if bad:
        runs = hzlib.failed_runs(rollup)
        tests = list(dict.fromkeys(t for r in runs for t in hzlib.failing_tests(repo, r)))
        detail = "checks " + ", ".join(c.get("name") or "?" for c in bad) + (f"; tests {', '.join(tests)}" if tests else "; no failing test found in the log")
        stuck_or_merged(item, "needs work", detail)
        return "done"
    if item.get("reviewed_progress_main") and not any(c.get("name") == aggregate() and c.get("conclusion") == "SUCCESS" and c.get("status") == "COMPLETED" for c in rollup):
        return "wait"
    status = info.get("mergeStateStatus")
    if status == "BEHIND" and repo != hzlib.CORE:
        hzlib.gh("pr", "update-branch", number, "-R", repo)
        log(f"{item['key']}: branch updated")
        return "wait"
    if status == "DIRTY":
        stuck_or_merged(item, "needs work", "merge conflicts")
        return "done"
    verdict = "ready" if status in ("CLEAN", "UNSTABLE") or (repo == hzlib.CORE and status == "BEHIND") else "wait"
    record_wait(item, verdict, info, rollup, advisory)
    return verdict


def recheck_needs_work(items: list[dict], merge) -> None:
    """A needs-work row whose pinned (guarded) head is unchanged goes back to queued once its checks are green: a gate
    judged again at the same head (`sm ci merge-gate rejudge` after a reviewed Sonar closure; RCA 2026-11-13, #803) needs
    no new review. Only rows marked for failing checks; a closed, draft, moved or still red PR stays as it is."""
    for item in items:
        if item.get("status") != "needs work" or not item.get("reviewed_head") or not str(item.get("detail") or "").startswith("checks "):
            continue
        info = hzlib.ghj("pr", "view", item["pr"], "-R", item["repo"], "--json", "state,isDraft,headRefOid,statusCheckRollup", default={}) or {}
        if info.get("state") != "OPEN" or info.get("isDraft") or info.get("headRefOid") != item["reviewed_head"]:
            continue
        advisory = hzlib.advisory_checks(item["repo"])
        rollup = [c for c in hzlib.latest_checks(info.get("statusCheckRollup", [])) if not hzlib.non_blocking(c.get("name"), advisory)]
        if not rollup or not merge.settled(rollup) or any(c.get("conclusion") in merge.FAILED for c in rollup):
            continue
        fields = {"status": "queued", "detail": "", "noted": None}
        update(item["key"], **fields)
        item.update(fields)
        log(f"{item['key']}: requeued: checks green again at the pinned head {item['reviewed_head'][:8]}")


def record_wait(item: dict, verdict: str, info: dict, rollup: list, advisory: set, now: float | None = None) -> None:
    """Keep `ready_since` / `waiting_since` and the real reason on the item, name it in the log, and raise the stall
    alarm when GitHub holds a PR whose checks are all green (RCA 2026-11-12: #790 sat BLOCKED for three hours).
    Bookkeeping only: a failure here never changes what the tick does."""
    try:
        from . import stall as hz_alarm
        now = time.time() if now is None else now
        key, state = item["key"], info.get("mergeStateStatus")
        if verdict == "ready":
            reason, fields = "", {"ready_since": item.get("ready_since") or int(now), "waiting_since": None, "wait_reason": ""}
            since = fields["ready_since"]
        else:
            since = item.get("waiting_since") or int(now)
            required = [name for name in hz_alarm.REQUIRED if name not in advisory]
            everything = hzlib.latest_checks(info.get("statusCheckRollup", []))
            green_at = hz_alarm.green_since(rollup) or since
            reason = hz_alarm.wait_reason(state, rollup, required, green_at, now, everything)
            stalled = hz_alarm.stall_alarm(key, state, rollup, required, green_at, now, everything=everything) if reason else None
            reason = reason or hz_alarm.pending_reason(state, rollup)
            fields = {"waiting_since": since, "ready_since": None, "wait_reason": reason}
            if stalled:
                hz_alarm.process([stalled], "queue", now, complete=False)
            else:
                hz_alarm.clear(f"stall:{key}", "queue")
            if reason != item.get("wait_reason") or reason.startswith("BLOCKED"):
                log(f"{key}: not ready: {reason}")
        if verdict == "ready":
            hz_alarm.clear(f"stall:{key}", "queue")
        if any(item.get(name) != value for name, value in fields.items()):
            update(key, **fields)
            item.update(fields)
    except Exception as error:  # noqa: BLE001 - diagnosis must not stop a merge
        log(f"{item.get('key')}: wait bookkeeping failed: {error!r}")


def valid_main_run(row) -> bool:
    statuses = {"completed", "queued", "in_progress", "waiting", "requested", "pending"}
    conclusions = {"success", "failure", "timed_out", "cancelled", "action_required", "stale", "neutral", "skipped", "startup_failure"}
    return (isinstance(row, dict) and isinstance(row.get("status"), str) and row["status"] in statuses
            and type(row.get("databaseId")) is int and row["databaseId"] > 0
            and isinstance(row.get("headSha"), str) and bool(re.fullmatch(r"[0-9a-f]{40}", row["headSha"]))
            and (row["status"] != "completed" or (isinstance(row.get("conclusion"), str) and row["conclusion"] in conclusions)))


def superseded_by_pending(runs, found, done) -> bool:
    """A cancelled main run is superseded when a newer run is still pending and an older completed run exists to judge.

    Pending runs never clear a red pin (see main_state); this only stops a cancelled run from making evidence unknown.
    """
    newer = runs[: runs.index(found)] if found in runs else []
    return any(r.get("status") != "completed" for r in newer) and len(done) > 1


def latest_required_timeout(runs: list[dict]) -> dict:
    """Read exact first-attempt evidence for a started Secret scan's provider timeout.

    Cancellation alone is inconclusive. This narrow required-job shape is red evidence,
    never a green result, a rerun authorization or a replacement for the historical pin.
    """
    if not runs or not valid_main_run(runs[0]):
        return {}
    found = runs[0]
    run_id, sha = found["databaseId"], found["headSha"]
    if (found["status"], found.get("conclusion")) != ("completed", "cancelled") or str(run_id) in deferred_runs():
        return {}
    prefix = f"repos/{hzlib.CORE}"
    tip = hzlib.ghj("api", f"{prefix}/git/ref/heads/main", default=None)
    run = hzlib.ghj("api", f"{prefix}/actions/runs/{run_id}", default=None)
    if (not isinstance(tip, dict) or tip.get("ref") != "refs/heads/main"
            or tip.get("object") != {"sha": sha, "type": "commit", "url": f"https://api.github.com/{prefix}/git/commits/{sha}"}
            or not isinstance(run, dict)
            or type(run.get("id")) is not int or type(run.get("run_attempt")) is not int
            or (run.get("id"), run.get("head_sha"), run.get("head_branch"), run.get("event"), run.get("path"),
                run.get("run_attempt"), run.get("status"), run.get("conclusion"))
            != (run_id, sha, "main", "push", core_facts.at_main().workflow, 1, "completed", "cancelled")
            or not isinstance(run.get("repository"), dict) or run["repository"].get("full_name") != hzlib.CORE
            or not isinstance(run.get("head_repository"), dict) or run["head_repository"].get("full_name") != hzlib.CORE
            or type(run.get("check_suite_id")) is not int or run["check_suite_id"] <= 0):
        return {}
    jobs = attempt_jobs(run_id, 1)
    if (not jobs or any(type(j.get("id")) is not int or j["id"] <= 0
                   or type(j.get("run_id")) is not int or type(j.get("run_attempt")) is not int
                   or not isinstance(j.get("conclusion"), str)
                   or (j.get("run_id"), j.get("run_attempt"), j.get("head_sha"), j.get("status"))
                   != (run_id, 1, sha, "completed") for j in jobs)
            or len({j["id"] for j in jobs}) != len(jobs) or len({j["name"] for j in jobs}) != len(jobs)):
        return {}
    bad = {j["name"]: j for j in jobs if j.get("conclusion") not in {"success", "skipped", "neutral"}}
    if set(bad) != {"Secret scan", aggregate()} or bad[aggregate()].get("conclusion") != "failure":
        return {}
    job = bad["Secret scan"]
    if (job.get("conclusion") != "cancelled" or type(job.get("runner_id")) is not int or job["runner_id"] <= 0
            or not isinstance(job.get("runner_name"), str) or not job["runner_name"].startswith("GitHub Actions ")
            or job.get("runner_group_name") != "GitHub Actions"
            or job.get("check_run_url") != f"https://api.github.com/{prefix}/check-runs/{job['id']}"):
        return {}
    try:
        started = datetime.fromisoformat(job["started_at"])
        completed = datetime.fromisoformat(job["completed_at"])
        steps = job["steps"]
        if (not isinstance(steps, list) or not steps or not started.tzinfo or not completed.tzinfo
                or (completed - started).total_seconds() < 180
                or not all(isinstance(s, dict) and s.get("status") == "completed"
                           and s.get("conclusion") in ("success", "skipped", "cancelled") for s in steps)
                or not any(s.get("conclusion") == "cancelled"
                           and started <= datetime.fromisoformat(s["started_at"])
                           < datetime.fromisoformat(s["completed_at"]) <= completed for s in steps)
                or not isinstance(bad[aggregate()].get("steps"), list)
                or not all(isinstance(s, dict) for s in bad[aggregate()]["steps"])
                or not any(s.get("name") == "Fail if any required job failed" and s.get("conclusion") == "failure"
                           for s in bad[aggregate()]["steps"])):
            return {}
    except (KeyError, TypeError, ValueError):
        return {}
    check = hzlib.ghj("api", f"{prefix}/check-runs/{job['id']}", default=None)
    if (not isinstance(check, dict)
            or (check.get("id"), check.get("name"), check.get("head_sha"), check.get("status"), check.get("conclusion"))
            != (job["id"], "Secret scan", sha, "completed", "cancelled")
            or not isinstance(check.get("app"), dict) or check["app"].get("id") != 15368
            or check["app"].get("slug") != "github-actions"
            or not isinstance(check.get("check_suite"), dict) or check["check_suite"].get("id") != run["check_suite_id"]
            or not isinstance(check.get("output"), dict) or check["output"].get("annotations_count") != 2):
        return {}
    annotations = hzlib.ghj("api", f"{prefix}/check-runs/{job['id']}/annotations?per_page=100", default=None)
    if (not isinstance(annotations, list) or len(annotations) != 2
            or not all(isinstance(a, dict) and a.get("annotation_level") == "failure" and a.get("path") == ".github"
                       and a.get("blob_href") == f"https://github.com/{hzlib.CORE}/blob/{sha}/.github"
                       and a.get("title") == "" and isinstance(a.get("message"), str) for a in annotations)
            or {a.get("message") for a in annotations}
            != {"The job has exceeded the maximum execution time of 3m0s", "The operation was canceled."}):
        return {}
    return {"run": run_id, "sha": sha, "jobs": ["Secret scan"]}


class MergeTier:
    """hzlib.merge_tier(), read at most once and only when a rule needs it."""

    def __init__(self) -> None:
        self.found: bool | None = None

    def __call__(self) -> bool:
        if self.found is None:
            self.found = hzlib.merge_tier()
        return self.found


def main_state() -> str:
    """Green, proven red, or unknown from completed main runs; unavailable evidence never authorizes an exception.

    Keep the latest completed run and the prior batch's completed runs in scope: a newer green diagnostic run
    must not hide that batch's failure. Persist proven failure evidence beyond the ten-run window; only a
    successfully merged --fixes-main PR followed by its successful main CI can supersede it. Pending runs,
    diagnostic, safety-fix or mitigation merges and reruns of the failed commit do not clear the pin.
    """
    runs = hzlib.ghj("run", "list", "-R", hzlib.CORE, "--workflow", ci_workflow(), "--branch", "main", "-L", "10", "--json", "status,conclusion,databaseId,headSha,displayTitle,attempt")
    runs = hzlib.resolve_main_runs(runs)
    if not isinstance(runs, list) or not runs:
        return "unknown"
    runs, lane_runs = main_lanes.split_runs(runs)  # push runs judge main as ever; the macOS lane is read beside them
    if any(row.get("batch_unknown") for row in runs) or not all(valid_main_run(row) for row in [*runs, *lane_runs]):
        return "unknown"
    done = [r for r in runs if r.get("status") == "completed"]
    lane_done = [r for r in lane_runs if r.get("status") == "completed"]
    if not done:
        return "unknown"
    shas = load().get("batch", {}).get("shas", [])
    mine = [r for r in done if shas and any((r.get("headSha") or "").startswith(s)
            or any(member[1].startswith(s) for member in r.get("batch", {}).get("prs", [])) for s in shas)]
    result, failure = "green", None
    tier = MergeTier()  # Core ADR 0149: a replaced main run is not red, and a green rerun repairs a red one
    for run in dict.fromkeys([done[0]["databaseId"]] + [r["databaseId"] for r in mine] + [r["databaseId"] for r in lane_done[:1]]):
        found = next(r for r in [*done, *lane_done] if r["databaseId"] == run)
        if found.get("conclusion") == "success":
            continue
        if found.get("conclusion") == "failure" and hzlib.only_never_started_macos_jobs_failed(hzlib.CORE, run):
            continue
        if found.get("conclusion") in {"failure", "timed_out", "startup_failure"}:
            result, failure = "red", failure or found
        elif found.get("conclusion") == "cancelled" and found in runs and runs.index(found) > 0 and tier():
            continue  # merge tier: one concurrency group for main pushes; a newer push replaced this run, which covers it
        elif found.get("conclusion") == "cancelled" and (any(
            r.get("conclusion") == "success" for r in done[: done.index(found)]
        ) or superseded_by_pending(runs, found, done)):
            continue  # cancelled by a newer push (cancel-in-progress); a newer green or still-pending run covers its commits
        elif found is runs[0] and latest_required_timeout(runs):
            result = "red"  # retain the literal cancelled run and the existing pin unchanged
        else:
            return "unknown"  # cancelled, skipped or undecided is not proof that tests failed
    pinned = load().get("main_failure", {})
    if load().get("red_batch") and not pinned and result != "red":
        return "unknown"  # legacy red attribution without retained evidence cannot be erased by newer green runs
    fixes = pinned.get("fixed_by", []) if isinstance(pinned, dict) else []
    if pinned:
        if (not valid_main_run(pinned) or pinned["status"] != "completed"
                or pinned["conclusion"] not in {"failure", "timed_out", "startup_failure"}
                or not isinstance(fixes, list) or not all(isinstance(s, str) and re.fullmatch(r"[0-9a-f]{8,40}", s) for s in fixes)):
            return "unknown"
        repaired = any(r["databaseId"] == pinned["databaseId"] and r["conclusion"] == "success" for r in done) and tier()
        if main_lanes.is_macos_lane(pinned):  # a macOS-lane failure is cleared only by a newer green lane run
            if not (lane_done and lane_done[0]["conclusion"] == "success" and lane_done[0]["databaseId"] > pinned["databaseId"]):
                result = "red"
        elif not repaired and not any(r["conclusion"] == "success" and any(r["headSha"].startswith(s) or any(member[1].startswith(s) for member in r.get("batch", {}).get("prs", [])) for s in fixes) for r in done):
            result = "red"
    if failure and (not pinned or (fixes and (failure["databaseId"], failure["headSha"]) != (pinned["databaseId"], pinned["headSha"]))):
        update(main_failure=failure)  # source evidence, never a receipt written by hand
    elif result == "green" and pinned:
        update(main_failure={}, red_batch={})  # successful main CI at the actual fix's squash commit
    return result


def main_is_green() -> bool:
    return main_state() == "green"


def attempt_jobs(run_id: int, attempt: int) -> list[dict] | None:
    """Every job of one attempt of a core main run, or None when the listing is incomplete or malformed."""
    payload = hzlib.ghj("api", f"repos/{hzlib.CORE}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100", default=None)
    if not isinstance(payload, dict) or type(payload.get("total_count")) is not int:
        return None
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or not jobs or payload["total_count"] != len(jobs):
        return None
    if any(not isinstance(j, dict) or not isinstance(j.get("name"), str) or not j["name"] for j in jobs):
        return None
    return jobs


def aggregate() -> str:
    """The aggregate job of Core's CI, from Core's release policy at main (CandidateError when unreadable)."""
    return core_facts.at_main().aggregate


def ci_workflow() -> str:
    """The file name of Core's CI workflow, from its release policy at main, for `gh run list --workflow`."""
    return core_facts.at_main().workflow_file


def reviewed_policy(sha: str):
    """Core's reviewed release policy at main commit `sha` (core_policy), or None when it is absent or garbled."""
    from . import core_policy

    def read(path: str):
        value = hzlib.ghj("api", f"repos/{hzlib.CORE}/{path}", default=None)
        rc.require(value is not None, f"{path} unreadable")
        return value

    try:
        return core_policy.at_commit_api(read, sha)
    except rc.CandidateError:
        return None


def full_pass(jobs: list[dict] | None, sha: str) -> bool:
    """Every job completed green and the full matrix succeeded: the aggregate and every Test cell Core's reviewed
    release policy at `sha` names. No policy at that commit: not a full pass (fail closed)."""
    if not jobs or any(j.get("status") != "completed" or j.get("conclusion") not in ("success", "skipped", "neutral") for j in jobs):
        return False
    policy = reviewed_policy(sha)
    if policy is None:
        return False
    names = {j["name"] for j in jobs if j.get("conclusion") == "success"}
    # a split main's push run dispatches the macOS lane: its macOS cells run there (main_lanes), never here
    return main_lanes.required_names(jobs, {policy.aggregate, *policy.test_jobs}).issubset(names)


def later_main_passes() -> bool:
    """Permit progress after a first-attempt full CI pass on a descendant commit; retain incident evidence."""
    pinned = load().get("main_failure", {})
    if not pinned:
        return False
    runs = hzlib.ghj("run", "list", "-R", hzlib.CORE, "--workflow", ci_workflow(), "--branch", "main", "-L", "10", "--json", "status,conclusion,databaseId,headSha,displayTitle,attempt")
    runs = hzlib.resolve_main_runs(runs, restoration_read=True)
    if not isinstance(runs, list) or not runs:
        return False
    runs, lane_runs = main_lanes.split_runs(runs)
    if any(r.get("batch_unknown") for r in runs) or not all(valid_main_run(r) for r in [*runs, *lane_runs]):
        return False
    if main_lanes.is_macos_lane(pinned):  # a macOS-lane incident: only a first-attempt full lane pass at a descendant repairs it
        return lane_repairs(pinned, [r for r in lane_runs if r["status"] == "completed"])
    done = [r for r in runs if r["status"] == "completed"]
    if not done or not runs or done[0]["conclusion"] != "success" or done[0]["headSha"] == pinned.get("headSha"):
        return False
    run = hzlib.ghj("api", f"repos/{hzlib.CORE}/actions/runs/{done[0]['databaseId']}", default={}) or {}
    if not isinstance(run, dict) or type(run.get("run_attempt")) is not int or run.get("run_attempt") != 1 or run.get("status") != "completed" or run.get("head_sha") != done[0]["headSha"] or run.get("conclusion") != "success":
        return False
    jobs = attempt_jobs(done[0]["databaseId"], 1)
    if not full_pass(jobs, done[0]["headSha"]):
        return False
    if main_lanes.is_split_push(jobs) and not main_lanes.macos_proof(done[0]["headSha"], lane_runs, compare_status):
        return False  # Linux green at X needs a green macOS lane at X or a descendant of X
    return compare_status(pinned.get("headSha"), done[0]["headSha"]) == "ahead"


def compare_status(base: str, head: str) -> str:
    """GitHub's compare status of base...head ("ahead", "identical", "diverged", "behind"); "" when unreadable."""
    comparison = hzlib.ghj("api", f"repos/{hzlib.CORE}/compare/{base}...{head}", default={}) or {}
    status = comparison.get("status") if isinstance(comparison, dict) else ""
    return status if isinstance(status, str) else ""


def lane_repairs(pinned: dict, lane_done: list[dict]) -> bool:
    """The newest completed macOS-lane run is a first-attempt, all-green lane pass strictly ahead of the pinned lane failure."""
    if not lane_done or lane_done[0]["conclusion"] != "success" or lane_done[0]["headSha"] == pinned.get("headSha"):
        return False
    rid = lane_done[0]["databaseId"]
    run = hzlib.ghj("api", f"repos/{hzlib.CORE}/actions/runs/{rid}", default={}) or {}
    if (not isinstance(run, dict) or run.get("run_attempt") != 1 or run.get("status") != "completed"
            or run.get("head_sha") != lane_done[0]["headSha"] or run.get("conclusion") != "success"):
        return False
    return main_lanes.lane_pass(attempt_jobs(rid, 1), aggregate()) and compare_status(pinned.get("headSha"), lane_done[0]["headSha"]) == "ahead"


# --- main-rerun and restored-by-rerun (policy 2026-11-07, options (iii)+(ii); MERGES ONLY, never releases) -----------
KNOWN_FILE = edge.records_dir() / "research" / "known-failures.toml"
KNOWN_MAIN_INFRA = KNOWN_FILE.with_name("known-main-infra.toml")
GREEN_JOB = ("success", "skipped", "neutral")


def known_registry() -> list:
    """Entries of research/known-failures.toml still within their `until` deadline (the setting queue_known_failures_file overrides)."""
    from . import known_failures as hz_digest
    return hz_digest.load_known(str(edge.setting("queue_known_failures_file") or KNOWN_FILE))


FAILED_TEST = re.compile(r"^(?:\S+Z )?FAILED (tests/\S+?)(?: - |$)", re.MULTILINE)


def failed_tests(job: dict) -> list[str]:
    """Pytest ids a failed job's log reports as FAILED; [] when the log cannot be read or names none."""
    if type(job.get("id")) is not int:
        return []
    code, log = hzlib.gh("api", "--allow-escape-sequences", f"repos/{hzlib.CORE}/actions/jobs/{job['id']}/logs")
    return sorted(set(FAILED_TEST.findall(log))) if code == 0 else []


def known_failures(jobs: list[dict]) -> tuple[list[str], dict]:
    """(failing jobs without the aggregate, {job: bead or None}) against in-date entries only.

    A job is KNOWN when an entry matches its NAME, or (bead 4mgv) when its log names at least one FAILED pytest id and
    every one of them matches an entry: a test-id entry never covers a job that also failed for any other test, and a
    job that failed without naming a test (setup, lint, a hung step) stays UNKNOWN.
    """
    from . import known_failures as hz_digest
    known = known_registry()
    failed = [j for j in jobs if j["name"] != aggregate() and j.get("conclusion") not in GREEN_JOB]
    beads = {}
    for job in failed:
        bead = hz_digest.known_bead([job["name"]], known)
        if not bead and (tests := failed_tests(job)):
            per_test = [hz_digest.known_bead([test], known) for test in tests]
            bead = per_test[0] if all(per_test) and len(set(per_test)) == 1 else None
        beads[job["name"]] = bead
    return [j["name"] for j in failed], beads


def main_attempt_failures(run: int, sha: str, jobs: list[dict], *, restoring: bool = False) -> tuple[list[str], dict, str]:
    """Classify one main attempt, keeping scoped infrastructure evidence out of all release readers."""
    receipt = known_main_infra.reviewed_incident(KNOWN_MAIN_INFRA, run, sha, jobs)
    recorded = load().get("main_reruns", {}).get(str(run), {}) if restoring else {}
    if restoring and "infra_receipt_sha256" in recorded:
        if (not receipt or recorded.get("sha") != sha
                or recorded["infra_receipt_sha256"] != receipt["receipt_sha256"]
                or recorded.get("infra_rerun_requested") is not True):
            return [], {}, ""  # a scoped rerun cannot be restored by a generic registry entry
    if receipt and (not restoring or "infra_receipt_sha256" in recorded):
        names = [job["name"] for job in jobs if job["name"] != aggregate() and job["conclusion"] == "failure"]
        return names, dict.fromkeys(names, receipt["bead"]), receipt["receipt_sha256"]
    failed, beads = known_failures(jobs)
    return failed, beads, ""


def tagged(beads: dict) -> str:
    return ", ".join(f"{job}=KNOWN({bead})" if bead else f"{job}=UNKNOWN" for job, bead in beads.items())


def main_rerun_plan() -> tuple[str, str, dict]:
    """Read-only: ('rerun', what, facts), ('none', why) when there is nothing to rerun, or ('refuse', why).
    Reruns only main's latest run, only attempt 1, only when every failing job is KNOWN in date and no release runs."""
    rows = hzlib.ghj("run", "list", "-R", hzlib.CORE, "--workflow", ci_workflow(), "--branch", "main", "-L", "10",
                     "--json", "status,conclusion,databaseId,headSha,attempt,displayTitle", default=None)
    if not isinstance(rows, list) or not rows or not all(valid_main_run(r) for r in rows):
        return "refuse", "main CI evidence is unknown", {}
    rows, _ = main_lanes.split_runs(rows)  # reruns repair push runs only; a macOS-lane run is never rerun here
    if not rows:
        return "none", "main has no push run in view", {}
    newer = [r for r in rows if r["status"] != "completed"]
    done = next((r for r in rows if r["status"] == "completed"), None)
    if done is None:
        return "none", f"main's latest run {rows[0]['databaseId']} is {rows[0]['status']}", {}
    if rows[0]["status"] != "completed" and rows.index(done) != len(newer):  # a completed run sits behind an unfinished one only at the head
        return "none", f"main's latest run {rows[0]['databaseId']} is {rows[0]['status']}", {}
    latest = done  # bead qnd: newer runs queued/in progress do not hide the newest COMPLETED run; KNOWN failures only, once per run
    rid = latest["databaseId"]
    if latest["conclusion"] not in ("failure", "timed_out"):
        return "none", f"main's latest run {rid} is {latest['conclusion']}", {}
    if str(rid) in load().get("main_reruns", {}):
        return "refuse", f"run {rid} was already rerun by main-rerun (never more than once per run)", {}
    run = hzlib.ghj("api", f"repos/{hzlib.CORE}/actions/runs/{rid}", default={}) or {}
    if (not isinstance(run, dict) or run.get("id") != rid or run.get("status") != "completed"
            or run.get("conclusion") != latest["conclusion"] or run.get("head_sha") != latest["headSha"]
            or type(run.get("run_attempt")) is not int):
        return "refuse", f"run {rid} cannot be read", {}
    if run["run_attempt"] != 1 or latest.get("attempt") != 1:
        return "refuse", f"run {rid} is attempt {run['run_attempt']}: already rerun (never more than once per run); fix forward (6.2a)", {}
    jobs = attempt_jobs(rid, 1)
    if jobs is None or any(j.get("status") != "completed" for j in jobs):
        return "refuse", f"run {rid} job list is incomplete", {}
    failed, beads, infra_digest = main_attempt_failures(rid, latest["headSha"], jobs)
    if not failed:
        return "refuse", f"run {rid} has no failing job besides {aggregate()}: not a KNOWN job flake", {}
    if refusing := rerun_refusing_jobs(jobs, beads):
        return "refuse", (f"run {rid}: {', '.join(refusing)} would run again on attempt 2, and Core's focused jobs refuse any run "
                          f"attempt but 1, so a rerun can never make main green; a repair ends in a fresh attempt-1 push run: "
                          f"fix forward with `sm queue add --fix-forward --for \"JOB=BEAD\" PR`"), {}
    if unknown := [job for job, bead in beads.items() if not bead]:
        return "refuse", f"run {rid} has UNKNOWN failing job(s) {', '.join(unknown)}: no rerun; name it and fix forward (6.2a)", {}
    if reason := release_active():
        return "refuse", f"release guard: {reason}", {}
    facts = {"run": rid, "sha": latest["headSha"], "jobs": failed, "beads": sorted(set(beads.values()))}
    if infra_digest:
        # The release guard can take time. Refuse a removed, changed or newly expired receipt.
        receipt = known_main_infra.reviewed_incident(KNOWN_MAIN_INFRA, rid, latest["headSha"], jobs)
        if not receipt or receipt["receipt_sha256"] != infra_digest:
            return "refuse", f"run {rid} reviewed infrastructure receipt changed or expired during validation", {}
        facts["infra_receipt_sha256"] = infra_digest
    if hzlib.merge_frozen():
        return "refuse", "release guard: merge freeze is on", {}
    return "rerun", f"run {rid} at {latest['headSha'][:8]}: {tagged(beads)}", facts


FOCUSED_JOB = "Focused tests"  # Core's focused jobs (`Focused tests (python)`, ...) accept run attempt 1 only (CandidateError)


def rerun_refusing_jobs(jobs: list[dict], beads: dict) -> list[str]:
    """The focused jobs a `rerun --failed` of this attempt would start again: every focused job that did not pass. Core refuses
    them on attempt 2, so the rerun cannot pass (bead 4yen)."""
    return [j["name"] for j in jobs if str(j.get("name", "")).startswith(FOCUSED_JOB) and j.get("conclusion") not in GREEN_JOB]


def main_rerun(dry_run: bool = False) -> int:
    """Manual entry point. A live queue owns the lock and performs the same check on its next tick."""
    if dry_run:
        return _main_rerun(dry_run=True)
    lock = acquire_lock(block=False)
    if lock is None:
        print("main-rerun: queue lock unavailable; let the automatic tick act", file=sys.stderr)
        return 1
    with lock:
        return _main_rerun()


def _main_rerun(dry_run: bool = False, auto: bool = False) -> int:
    """Rerun main's failed KNOWN jobs once. Mutating callers must already hold the queue lock."""
    kind, text, facts = main_rerun_plan()
    if kind != "rerun":
        line = f"main-rerun: {'nothing to rerun' if kind == 'none' else 'refused'}: {text}"
        if not auto:
            print(line, file=sys.stderr if kind == "refuse" else sys.stdout)
        elif text != load().get("main_rerun_last"):
            update(main_rerun_last=text)
            log(line)
        return 1 if kind == "refuse" else 0
    if dry_run:
        print(f"main-rerun: would rerun the failed jobs of {text} (dry run, nothing started)")
        return 0
    runs = load().get("main_reruns", {})
    key = str(facts["run"])
    request = {**facts, "at": int(time.time())}
    update(main_reruns={**runs, key: request}, main_rerun_last=text)
    code, _ = hzlib.gh("run", "rerun", key, "--failed", "-R", hzlib.CORE)
    if code != 0:
        if "infra_receipt_sha256" not in facts:
            update(main_reruns={k: v for k, v in load().get("main_reruns", {}).items() if k != key})
        # A scoped request with an uncertain outcome must not be sent twice or authorize restoration.
        log(f"main-rerun: gh run rerun {key} --failed failed (exit {code})")
        return 1
    if "infra_receipt_sha256" in facts:
        update(main_reruns={**load().get("main_reruns", {}), key: {**request, "infra_rerun_requested": True}})
    log(f"main-rerun: reran the failed jobs of {text}")
    hzlib.note(f"sm queue main-rerun: reran the failed jobs of main {text} once; attempt 2 fully green => merges may proceed "
               "(restored-by-rerun), releases still need a first-attempt full green main run")
    print(f"main-rerun: reran the failed jobs of {text}; attempt 2 fully green lets merges proceed, never releases")
    return 0


def rerun_restoration() -> dict:
    """Read-only: {run, sha, beads, jobs} when main's latest completed run is attempt 2 of the same run, fully green, and
    every failing job of attempt 1 was KNOWN (in date). Lifts the red-main hold for MERGES only; {} otherwise."""
    runs = hzlib.ghj("run", "list", "-R", hzlib.CORE, "--workflow", ci_workflow(), "--branch", "main", "-L", "10", "--json", "status,conclusion,databaseId,headSha,displayTitle,attempt")
    runs = hzlib.resolve_main_runs(runs)
    if not isinstance(runs, list) or not runs:
        return {}
    runs, _lane = main_lanes.split_runs(runs)  # the restored run is a push run; lane runs neither restore nor hide it
    if any(r.get("batch_unknown") for r in runs) or not all(valid_main_run(r) for r in runs):
        return {}
    done = [r for r in runs if r["status"] == "completed"]
    if not done or done[0]["conclusion"] != "success" or done[0].get("attempt") != 2:
        return {}
    rid, sha = done[0]["databaseId"], done[0]["headSha"]
    run = hzlib.ghj("api", f"repos/{hzlib.CORE}/actions/runs/{rid}", default={}) or {}
    if (not isinstance(run, dict) or run.get("run_attempt") != 2 or run.get("status") != "completed"
            or run.get("conclusion") != "success" or run.get("head_sha") != sha):
        return {}
    second = attempt_jobs(rid, 2)
    first = attempt_jobs(rid, 1)
    if first is None or any(j.get("status") != "completed" for j in first):
        return {}
    failed, beads, _ = main_attempt_failures(rid, sha, first, restoring=True)
    if not failed or not all(beads.values()):
        return {}  # an UNKNOWN attempt-1 failure keeps the old rule: only a first-attempt full pass restores
    if not full_pass(second, sha):
        return {}
    return {"run": rid, "sha": sha, "jobs": failed, "beads": sorted(set(beads.values()))}




def restored_by_rerun() -> bool:
    """Gate hook: record each restoration once in the queue state and hz-note, so every waiver is auditable."""
    found = rerun_restoration()
    if not found:
        return False
    if load().get("main_restored", {}).get("run") != found["run"]:
        label = f"restored-by-rerun(KNOWN: {', '.join(found['beads'])})"
        update(main_restored={**found, "label": label, "at": int(time.time())})
        pinned = load().get("main_failure", {})
        if found.get("repaired") and pinned.get("databaseId") == found["run"] and pinned.get("headSha") == found["sha"]:
            # The pinned failure was the missing plan itself; its repaired rerun is main's first real test of that tree.
            update(main_failure={**pinned, "fixed_by": sorted({*pinned.get("fixed_by", []), found["sha"]})})
        log(f"main {label}: run {found['run']} at {found['sha'][:8]} green on attempt 2; merges may proceed")
        hzlib.note(f"sm queue: main {label} run {found['run']} at {found['sha'][:8]} (attempt-1 failures {', '.join(found['jobs'])}); "
                   "merges proceed, releases still need a first-attempt full green, the pinned failure stays")
    return True


def freeze_holder() -> str:
    """Read-only: who holds the core merge freeze, from the files hz-release and incident holds write."""
    parts = []
    legacy = statedir.path("merge-freeze")
    if legacy.exists():
        parts.append(f"{legacy} since {time.strftime('%m-%d %H:%M', time.localtime(legacy.stat().st_mtime))}")
    try:
        holds = sorted(p.name for p in hzlib.release_holds().iterdir())
    except OSError:
        holds = []
    if holds:
        parts.append("release holds " + ", ".join(holds))
    try:
        rel = json.loads((statedir.path("release.json")).read_text())
    except (OSError, ValueError):
        rel = {}
    if rel.get("version") and not rel.get("finished"):
        stage = ("acceptance run " + str(rel["acceptance_run_id"]) if rel.get("acceptance_run_id")
                 else "compose done" if rel.get("compose") else "PyPI done" if rel.get("pypi") else "before PyPI")
        parts.append(f"sm release resume {rel['version']} unfinished ({stage}); it lifts the freeze itself when acceptance completes")
    return "; ".join(parts) or "no freeze file found (fails closed on unreadable state)"


def block_detail(blocked: str) -> str:
    if blocked == "merge freeze is on":
        return f" [{freeze_holder()}]" if hzlib.merge_frozen() else " [stale: the freeze is lifted now; the next tick re-gates]"
    return ""


def gate(ready: list[dict], now: float, data: dict) -> str:
    """Why the batch can't go now, or an empty string."""
    if hzlib.merge_frozen() and any(i["repo"] == hzlib.CORE for i in ready):
        return "merge freeze is on"
    if now - data["last_batch"] < BATCH_SECONDS:
        return f"last batch {int((now - data['last_batch']) / 60)} min ago, one per 5 minutes"
    if any(i["repo"] == hzlib.CORE for i in ready):
        state = main_state()
        if state == "red" and not main_checkpoint.permits(state_path(), data.get("main_failure", {})) and not later_main_passes() and not restored_by_rerun():
            return "main's latest run is not green"
        if state not in ("green", "red"):
            return "main CI evidence is unknown"
    return ""


def red_main_fixes(ready: list[dict]) -> list[dict]:
    """Core PRs queued with --fixes-main: they are the way out of a red main, so they go alone while main is red."""
    return [i for i in ready if i["repo"] == hzlib.CORE and i.get("fixes_main")]


# --- fix-forward lane (bead ox6) ---------------------------------------------------------------------------------
FIX_TITLE = re.compile(r"(fix|test|ci)(\([^)]*\))?: \S")
FEATURE_COMMIT = re.compile(r"(feat|[a-z]+(\([^)]*\))?!)(\([^)]*\))?:", re.IGNORECASE)
FIX_OK = {"SUCCESS", "SKIPPED", "NEUTRAL"}
BEAD_KINDS = {"flaky", "bug", "broken-test"}
RUN_ID = re.compile(r"/actions/runs/(\d+)")


def parse_mapping(values: list[str]) -> list[dict]:
    """`--for "web-2=bph,web-1=k2@602"` or a bare `--for bph` (covers every failing job). Raises ValueError."""
    rows = []
    for value in values or []:
        for part in (p.strip() for p in re.split(r"[,;]", value)):
            if not part:
                continue
            job, _, rest = part.partition("=") if "=" in part else ("", "", part)
            bead, _, pr = rest.partition("@")
            bead, pr = bead.strip(), pr.strip().lstrip("#")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", bead) or (pr and not re.fullmatch(r"[1-9][0-9]*", pr)):
                raise ValueError(f"bad --for entry {part!r}: use JOB=BEAD or JOB=BEAD@PR")
            rows.append({"job": job.strip(), "bead": bead if "-" in bead else f"heinzel-management-{bead}", "pr": pr})
    return rows


def pr_facts(number: str) -> dict:
    return hzlib.ghj("pr", "view", number, "-R", hzlib.CORE, "--json",
                     "state,isDraft,headRefOid,title,body,labels,commits,statusCheckRollup", default={}) or {}


def bead_info(bead: str) -> dict:
    """The bead from Beads (`bd show --json`), {} when unknown."""
    import subprocess
    try:
        out = edge.run(["bd", "show", bead, "--json"], capture_output=True, text=True, timeout=30,
                             cwd=edge.records_dir()).stdout
        data = json.loads(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    data = data[0] if isinstance(data, list) and data else data
    return data if isinstance(data, dict) and data.get("id") == bead else {}


def pr_state(number: str) -> dict:
    return hzlib.ghj("pr", "view", number, "-R", hzlib.CORE, "--json", "state,title,body", default={}) or {}


def deferred_runs() -> set[str]:
    try:
        return {line.split()[0] for line in (statedir.path("ci-deferred.txt")).read_text().splitlines() if line.split()}
    except OSError:
        return set()


def release_active() -> str:
    """Why a release forbids fix-forward now, or ''. Unreadable run lists count as active (fail closed)."""
    if hzlib.merge_frozen():
        return f"merge freeze is on [{freeze_holder()}]"
    rows = hzlib.ghj("run", "list", "-R", hzlib.CORE, "-L", "60", "--json", "headBranch,status,databaseId", default=None)
    if not isinstance(rows, list):
        return "cannot read CI runs to rule out a release/* run"
    live = [r for r in rows if isinstance(r, dict) and str(r.get("headBranch", "")).startswith("release/") and r.get("status") != "completed"]
    if live:
        return "release/* run active: " + ", ".join(f"{r.get('headBranch')} {r.get('databaseId')}" for r in live[:3])
    try:
        rel = json.loads((statedir.path("release.json")).read_text())
    except (OSError, ValueError):
        rel = {}
    events = rel.get("hold_events") if isinstance(rel.get("hold_events"), list) else []
    paused = bool(events) and isinstance(events[-1], dict) and events[-1].get("action") == "pause"
    # Tag-on-green-main (release_flow, core ADR 0144) holds no merge window: its red bump commit needs a fix forward.
    if rel.get("version") and not rel.get("finished") and not paused and not release_flow.is_tag_on_main(rel["version"]):
        return f"sm release resume {rel['version']} unfinished"
    return ""  # a paused release (`hz-release X --pause`: PR draft, its own hold removed) holds no merge or rerun


def main_red_jobs() -> dict:
    """The red main run that holds the queue and its failed jobs: the latest completed main run when it failed, else the
    pinned failure run. {"run": id, "sha": ..., "jobs": [names]} or {} when it cannot be proven."""
    runs = hzlib.ghj("run", "list", "-R", hzlib.CORE, "--workflow", ci_workflow(), "--branch", "main", "-L", "10",
                     "--json", "status,conclusion,databaseId,headSha,displayTitle", default=None)
    if not isinstance(runs, list) or not all(valid_main_run(r) for r in runs):
        return {}
    runs, lane_runs = main_lanes.split_runs(runs)
    done = [r for r in runs if r["status"] == "completed"]
    lane_done = [r for r in lane_runs if r["status"] == "completed"]
    if timeout := latest_required_timeout(runs):
        return timeout
    if done and done[0]["conclusion"] in {"failure", "timed_out", "startup_failure"}:
        run = done[0]
    elif lane_done and lane_done[0]["conclusion"] in {"failure", "timed_out", "startup_failure"}:
        run = lane_done[0]
    elif (pinned := load().get("main_failure")) and valid_main_run(pinned):
        run = pinned
    else:
        return {}
    payload = hzlib.ghj("api", f"repos/{hzlib.CORE}/actions/runs/{run['databaseId']}/jobs?per_page=100", default={}) or {}
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list) or not jobs:
        return {}
    failed = [j["name"] for j in jobs if isinstance(j, dict) and isinstance(j.get("name"), str) and j["name"] != aggregate()
              and j.get("conclusion") in ("failure", "timed_out", "cancelled", "startup_failure")]
    return {"run": run["databaseId"], "sha": run["headSha"], "jobs": failed} if failed else {}


def names_bead(text: str, bead: str) -> bool:
    short = bead.removeprefix("heinzel-management-")
    return bool(re.search(rf"\b{re.escape(bead)}\b", text) or re.search(rf"\bbeads?\s+[`'\"]?{re.escape(short)}\b", text, re.IGNORECASE))


def fix_forward_check(number: str, mapping: list[dict], reviewed: bool, guard_pass: bool) -> tuple[list[tuple[str, str]], dict]:
    """Every reason the PR may not fix-forward now, as (kind, text) with kind `hold` (wait, e.g. release, one per run)
    or `refuse`; and the facts recorded on the queue entry. Empty reasons mean admitted."""
    problems: list[tuple[str, str]] = []
    facts: dict = {}
    info = pr_facts(number)
    if not info:
        return [("refuse", f"cannot read PR #{number}")], facts
    facts["head"] = info.get("headRefOid", "")
    if info.get("state") != "OPEN" or info.get("isDraft"):
        problems.append(("refuse", "PR is not open and ready"))
    title = info.get("title") or ""
    if not FIX_TITLE.match(title):
        problems.append(("refuse", f"title {title!r} is not a fix|test|ci conventional commit (no features, no '!')"))
    for commit in info.get("commits") or []:
        head = (commit or {}).get("messageHeadline", "")
        if FEATURE_COMMIT.match(head):
            problems.append(("refuse", f"commit {head!r} is a feature or breaking change"))
    if any((label or {}).get("name", "").lower() in ("feature", "enhancement") for label in info.get("labels") or []):
        problems.append(("refuse", "PR carries a feature label"))
    checks = [c for c in hzlib.latest_checks(info.get("statusCheckRollup") or []) if (c.get("name") or c.get("context")) != "review"]
    deferred = deferred_runs()
    bad = []
    for c in checks:
        name = c.get("name") or c.get("context") or "?"
        if "conclusion" in c or "status" in c:  # a check run
            verdict = c.get("conclusion") if c.get("status") == "COMPLETED" else (c.get("status") or "PENDING")
        else:  # a commit status
            verdict = c.get("state")
        if verdict not in FIX_OK:
            bad.append(f"{name} {verdict}")
        elif (m := RUN_ID.search(c.get("detailsUrl") or "")) and m.group(1) in deferred:
            bad.append(f"{name} deferred by sm ci defer")
    if bad:
        problems.append(("refuse", "PR CI not fully green on the current head: " + "; ".join(bad[:6])))
    if not any(c.get("name") == aggregate() and c.get("conclusion") == "SUCCESS" for c in checks):
        problems.append(("refuse", f"{aggregate()} is not SUCCESS on the current head"))
    if not (guard_pass or reviewed):
        problems.append(("refuse", "sm head guard did not PASS and no --reviewed independent review"))
    red = main_red_jobs()
    facts["main_run"] = red.get("run")
    facts["map"] = []
    if not red:
        problems.append(("hold", "no proven red main run with failed jobs (nothing to fix forward, or evidence unknown)"))
    else:
        used = set()
        for job in red["jobs"]:
            hits = [i for i, row in enumerate(mapping) if row["job"].lower() in job.lower()]
            if not hits:
                problems.append(("refuse", f"failing main job {job!r} (run {red['run']}) is not attributed: add --for \"<job>=<bead>\""))
            for i in hits:
                used.add(i)
                facts["map"].append({"job": job, "bead": mapping[i]["bead"], "pr": mapping[i]["pr"] or number})
        for i, row in enumerate(mapping):
            if i not in used:
                problems.append(("refuse", f"--for {row['job']}={row['bead']} matches no failing job of main run {red['run']}"))
        if str(red["run"]) in load().get("fix_forward_runs", {}):
            problems.append(("hold", f"a fix-forward already merged for main run {red['run']} (at most one per main run)"))
    text = title + "\n" + (info.get("body") or "")
    for row in {(r["bead"], r["pr"] or number) for r in mapping}:
        bead, pr = row
        found = bead_info(bead)
        if not found:
            problems.append(("refuse", f"bead {bead} not found"))
            continue
        labels = {str(x).lower() for x in found.get("labels") or []}
        if not (labels & BEAD_KINDS or found.get("issue_type") == "bug"):
            problems.append(("refuse", f"bead {bead} is not a flake or known bug (label flaky|bug|broken-test or type bug)"))
        bead_text = str(found.get("notes") or "")  # only the `hz-tasks pr ID N` link counts, not a mention in prose
        other = text if pr == number else ""
        if pr != number:
            view = pr_state(pr)
            if view.get("state") != "MERGED":
                problems.append(("refuse", f"#{pr} (addresses {bead}) is not merged"))
                continue
            other = (view.get("title") or "") + "\n" + (view.get("body") or "")
        if not (names_bead(other, bead) or re.search(rf"\bPR #{pr}\b", bead_text)):
            problems.append(("refuse", f"#{pr} does not address {bead}: name the bead in the PR body or `sm beads tasks pr {bead.removeprefix('heinzel-management-')} {pr}`"))
    if reason := release_active():
        problems.append(("hold", reason))
    return problems, facts


def fix_forward_ready(ready: list[dict]) -> list[dict]:
    """Queued fix-forward PRs re-checked now; the first admitted one goes alone. Refusals mark needs work, holds wait."""
    for item in ready:
        if item["repo"] != hzlib.CORE or not item.get("fix_forward"):
            continue
        problems, facts = fix_forward_check(item["pr"], item.get("fix_forward_for", []), bool(item.get("fix_forward_reviewed")), True)
        if facts.get("head") and facts["head"] != item.get("reviewed_head"):
            problems.append(("refuse", "head changed since it was queued"))
        refusals = [text for kind, text in problems if kind == "refuse"]
        if refusals:
            stuck_or_merged(item, "needs work", "fix-forward refused: " + "; ".join(refusals))
            continue
        if problems:
            log(f"{item['key']}: fix-forward held: " + "; ".join(text for _, text in problems))
            continue
        update(item["key"], fix_forward_map=facts["map"], fix_forward_run=facts["main_run"])
        return [{**item, "fix_forward_map": facts["map"], "fix_forward_run": facts["main_run"]}]
    return []


def reviewed_exception(item: dict) -> str:
    """Reviewed, head-pinned exceptions do not claim to fix main or replace its failure evidence."""
    return "safety fix" if item.get("safety_fix_main") else "mitigation" if item.get("mitigation_main") else "diagnostic" if item.get("diagnostics_main") else "reviewed progress" if item.get("reviewed_progress_main") else ""



def main_observations() -> list[dict] | None:
    rows = hzlib.ghj("run", "list", "-R", hzlib.CORE, "--workflow", ci_workflow(), "--branch", "main", "-L", "10",
                     "--json", "status,conclusion,databaseId,headSha", default=None)
    return rows if isinstance(rows, list) and rows and all(valid_main_run(row) for row in rows) else None


def completed_main_anchor() -> dict:
    """Bind all bounded completed results AND pending runs, not only the newest-created completion."""
    rows = main_observations()
    if rows is None or not any(row["status"] == "completed" for row in rows):
        return {}
    return {"completed": [row for row in rows if row["status"] == "completed"],
            "pending": [row for row in rows if row["status"] != "completed"]}


def progress_anchor_unchanged(anchor: dict) -> bool:
    """Any newly completed run stops admission, including an older pending run behind a newer result."""
    if not isinstance(anchor, dict) or not isinstance(anchor.get("completed"), list) or not anchor["completed"] or not isinstance(anchor.get("pending"), list):
        return False
    watched = anchor["completed"] + anchor["pending"]
    if not all(valid_main_run(row) for row in watched) or len(watched) > 10:
        return False
    if any(row["status"] != "completed" for row in anchor["completed"]) or any(row["status"] == "completed" for row in anchor["pending"]):
        return False
    rows = main_observations()
    if rows is None:
        return False
    if not {row["databaseId"] for row in rows}.intersection(row["databaseId"] for row in watched):
        return False  # The bounded observations no longer cover the interval that was approved.
    done = {row["databaseId"]: row for row in anchor["completed"]}
    if any(row["status"] == "completed" and row != done.get(row["databaseId"]) for row in rows):
        return False
    current = {row["databaseId"]: row for row in rows}
    for old in anchor["pending"]:
        row = current.get(old["databaseId"])
        if row is None:  # Newer pending runs may push the watched old run outside the bounded listing.
            row = hzlib.ghj("run", "view", str(old["databaseId"]), "-R", hzlib.CORE,
                            "--json", "status,conclusion,databaseId,headSha", default=None)
        if not valid_main_run(row) or row["databaseId"] != old["databaseId"] or row["headSha"] != old["headSha"] or row["status"] == "completed":
            return False
    return True


def red_main_exceptions(ready: list[dict]) -> list[dict]:
    """New completed evidence needs fresh assessment before owner-approved progress continues."""
    candidates = [item for item in ready if item["repo"] == hzlib.CORE and reviewed_exception(item) and item.get("reviewed_head")]
    return [item for item in candidates if not item.get("reviewed_progress_main")
            or progress_anchor_unchanged(item.get("progress_anchor"))]


def main_commit(item: dict) -> str:
    """The commit a squash merge made on main (the PR's head sha is not on main), as 8 characters; empty when unknown."""
    info = hzlib.ghj("pr", "view", item["pr"], "-R", item["repo"], "--json", "mergeCommit", default={}) or {}
    return str((info.get("mergeCommit") or {}).get("oid") or "")[:8]


def record_merge(item: dict) -> None:
    """Bead 4p8.4: keep pr + merge_sha on the queue row and stamp its bead (best effort; a stamp failure never blocks the queue)."""
    sha = main_commit(item)
    update(item["key"], merge_sha=sha)
    if item.get("bead") and item["repo"] == hzlib.CORE and sha:
        edge.stamp_merged(item["bead"], item["pr"])  # moved: the stamp is written here, no script is started


def repin_after_update(item: dict, pinned: str) -> str:
    """The head the diff guard read, or the head the queue's own update-branch made of it: a merge commit whose two parents are
    the pinned head and a commit already on main adds main's content and nothing of the PR's own diff, so the guard's verdict
    still holds. Anything else (a push by hand, a different parent) keeps the old pin and the train refuses the moved head."""
    info = hzlib.ghj("pr", "view", item["pr"], "-R", hzlib.CORE, "--json", "headRefOid", default={}) or {}
    current = info.get("headRefOid", "") if isinstance(info, dict) else ""
    if len(current) != 40 or current == pinned:
        return pinned
    commit = hzlib.ghj("api", f"repos/{hzlib.CORE}/commits/{current}", default={}) or {}
    parents = [p.get("sha", "") for p in commit.get("parents", [])] if isinstance(commit, dict) else []
    if len(parents) != 2 or pinned not in parents:
        return pinned
    other = parents[1] if parents[0] == pinned else parents[0]
    onto_main = hzlib.ghj("api", f"repos/{hzlib.CORE}/compare/{other}...main", default={}) or {}
    if not isinstance(onto_main, dict) or onto_main.get("status") not in ("ahead", "identical"):
        return pinned  # the second parent is not on main: this is not main merged into the branch
    update(item["key"], guarded_head=current)
    item["guarded_head"] = current
    log(f"{item['key']}: re-pinned {pinned[:8]} -> {current[:8]} (main merged into the branch by the queue's update)")
    return current


def plan_guard(item: dict, earlier, refusal: list[str]):
    """The last check before a core merge: an earlier guard, then the main plan check of exactly this head on current
    the merge rule (main_plan.gap). A refusal leaves its reason in `refusal`; train sees only False."""
    def guard() -> bool:
        if earlier is not None and not earlier():
            return False
        reason = main_plan.gap(item["pr"], item.get("reviewed_head") or item.get("guarded_head") or "")  # the pin that merges
        if reason:
            refusal[:] = [reason]
        return not reason
    return guard


ASKS_EVERY = 300  # seconds between two reads of the tracker for the ask alarm
_asks_read = 0.0


def record_asks(now: float) -> None:
    """The combined ask alarm (`sm alarm asks`), at most every ASKS_EVERY seconds; a failure is logged, never stops merges."""
    global _asks_read
    if now - _asks_read < ASKS_EVERY:
        return
    _asks_read = now
    try:
        import importlib

        from steuermaschine.native import runtime

        # by name: an import statement would put the alarm code into the review digest of every queue writer
        asks = importlib.import_module("steuermaschine_heinzel.native.alarm_lib.asks")
        rt = runtime()
        labels = list(rt.settings.get("alarm_ask_labels") or asks.AskAlarmSettings.model_fields["alarm_ask_labels"].default)
        asks.record(rt.providers.commands, rt.providers.store, rt.settings, labels, now)
    except Exception as error:  # noqa: BLE001 - an alarm read must not stop merges
        log(f"ask alarm failed: {error!r}")


def tick(now: float | None = None) -> None:
    """Run one tick under the runner's queue lock."""
    now = time.time() if now is None else now
    merge = hzlib.load("merge_train")
    try:  # one rerun of main's failed KNOWN jobs (policy 2026-11-07); a failure here never stops merges
        _main_rerun(auto=True)
    except Exception as error:
        log(f"main-rerun failed: {error!r}")
    record_asks(now)
    data = load()
    try:  # a needs-work row at its unchanged pinned head whose checks turned green is queued again
        recheck_needs_work(data["items"], merge)
    except Exception as error:  # noqa: BLE001 - a re-check must not stop merges
        log(f"needs-work re-check failed: {error!r}")
    ready = []
    for item in data["items"]:
        if item["status"] == "queued" and deps_met(item, data["items"]) and assess(item, merge) == "ready":
            ready.append(item)
    if not ready:
        queued = [i["key"] for i in data["items"] if i["status"] == "queued"]
        log("waiting: " + (f"none of {len(queued)} queued PRs is ready ({', '.join(queued)})" if queued else "queue has nothing queued"))
        return
    blocked = gate(ready, now, load())
    if blocked == "main's latest run is not green":
        batch = load().get("batch", {})
        if batch.get("prs") and load().get("red_batch") != batch and not (load().get("main_failure") and load().get("red_batch")):
            update(red_batch=batch)  # main went red after this batch: the fixer starts from these PRs
            log("main is red after batch: " + " ".join(batch["prs"]))
    elif not blocked and any(i["repo"] == hzlib.CORE for i in ready) and load().get("red_batch") and not load().get("main_failure"):
        update(red_batch={})
    others = [i for i in ready if i["repo"] != hzlib.CORE]
    if blocked == "main's latest run is not green" and red_main_fixes(ready):
        log(f"main is red; the fix goes alone: {', '.join(i['key'] for i in red_main_fixes(ready))}")
        ready, blocked = red_main_fixes(ready) + others, ""
    elif blocked == "main's latest run is not green" and (forward := fix_forward_ready(ready)):
        log(f"main is red; fix-forward goes alone: {forward[0]['key']} for main run {forward[0]['fix_forward_run']}")
        ready, blocked = forward + others, ""
    elif blocked == "main's latest run is not green" and (exceptions := red_main_exceptions(ready)):
        # A safety fix, measured mitigation or diagnostic never claims to repair red CI.
        rank = {"safety fix": 0, "mitigation": 1, "diagnostic": 2, "reviewed progress": 3}
        selected = min(exceptions, key=lambda i: rank[reviewed_exception(i)])
        log(f"main is red; {reviewed_exception(selected)} admitted (not a main fix): {selected['key']}")
        chosen = [item for item in exceptions if item.get("reviewed_progress_main")] if selected.get("reviewed_progress_main") else [selected]
        ready, blocked = chosen + others, ""
    elif blocked in ("merge freeze is on", "main's latest run is not green", "main CI evidence is unknown") and others:
        # core's main says nothing about another repo's merge: those go, core's wait
        log(f"core held, {blocked}; batch goes without it")
        ready, blocked = others, gate(others, now, load())
    if blocked:
        log(f"waiting: {blocked}{block_detail(blocked)}; {len(ready)} ready ({', '.join(i['key'] for i in ready)})")
        if blocked != load().get("blocked"):
            update(blocked=blocked)
        return
    ready = core_batch(ready)
    update(last_batch=now, blocked="")
    log("batch: " + ", ".join(i["key"] for i in ready))
    core = [item for item in ready if item["repo"] == hzlib.CORE]
    want_intent = (bool(edge.setting("queue_main_batch_publish")) and len(core) > 1
                   and all(not reviewed_exception(item) or item.get("reviewed_progress_main") for item in core))
    base = (hzlib.ghj("api", f"repos/{hzlib.CORE}/commits/main", default={}) or {}) if want_intent else {}
    base = base.get("sha", "") if isinstance(base, dict) else ""
    intent_commits = []
    published = False
    went, shas, fix_shas, merged_core = [], [], [], []
    for item in ready:
        if item.get("reviewed_progress_main") and not progress_anchor_unchanged(item.get("progress_anchor")):
            stuck_or_merged(item, "needs work", "completed main observation changed or unavailable; reassessment and requeue required")
            continue
        pinned = item.get("reviewed_head") or item.get("guarded_head") or ""
        if not pinned:  # queued before the guard's verdict was bound to a head: nothing proves which diff it read
            stuck_or_merged(item, "needs work", "no guarded head recorded; ship it again so the diff guard reads the head that merges")
            continue
        if item["repo"] == hzlib.CORE and not item.get("reviewed_head"):
            pinned = repin_after_update(item, pinned)
        head_guard = {"expected_head": pinned}
        if item.get("reviewed_progress_main"):
            head_guard["pre_merge_guard"] = lambda item=item: progress_anchor_unchanged(item.get("progress_anchor"))
        plan_refusal: list[str] = []
        if item["repo"] == hzlib.CORE:  # the merge rule: checks green on the pinned head, main an ancestor of it, a verdict for protected paths
            head_guard["pre_merge_guard"] = plan_guard(item, head_guard.get("pre_merge_guard"), plan_refusal)
        merged, detail = merge.train(item["repo"], item["pr"], queue_core=item["repo"] == hzlib.CORE, **head_guard)
        if not merged and plan_refusal:
            detail = plan_refusal[0]
            log(f"{item['key']}: {detail}")
        if not merged and item.get("merge_unknown") and str(detail).startswith("skip: MERGED"):
            merged, detail = True, pinned[:8]  # the earlier merge whose reply was lost did land: record it now
        if not merged and str(detail).startswith(merge.MERGE_UNKNOWN):
            update(item["key"], detail=detail, merge_unknown=True)  # stays queued: neither merged nor needs work
            text = f"#{item['key']} {detail}; the queue reads it again next tick and stops this batch"
            log(text)
            hzlib.note(f"sm queue: {text}")
            try:
                edge.raise_alarm(f"merge-unknown:{item['key']}", "ALERT", text)
            except Exception as error:  # noqa: BLE001 - the log and the note above already say it
                log(f"alarm failed: {error!r}")
            break  # main's state is uncertain: merge nothing more on top of it in this tick
        if merged and item.get("merge_unknown"):
            update(item["key"], merge_unknown=False)
            try:
                edge.clear_alarm(f"merge-unknown:{item['key']}")
            except Exception as error:  # noqa: BLE001 - a stale alarm is visible, not harmful
                log(f"alarm clear failed: {error!r}")
        if not merged and not item.get("reviewed_head") and str(detail).startswith("reviewed head changed"):
            detail = "head moved after the diff guard read it; ship it again" + str(detail)[len("reviewed head changed; independent review and requeue required"):]
        if merged:
            if item["repo"] == hzlib.CORE:
                merged_core.append(item["pr"])
            if want_intent and item["repo"] == hzlib.CORE:
                if commit := publish_intent(base, item):
                    intent_commits.append(commit)
            stuck_or_merged(item, "merged", detail)
            record_merge(item)
            if item.get("fix_forward_run"):  # only set when the lane admitted it past a red main
                runs = load().get("fix_forward_runs", {})
                update(fix_forward_runs={**runs, str(item["fix_forward_run"]): item["key"]})
                hzlib.note(f"sm queue: fix-forward #{item['key']} merged at {item['reviewed_head'][:8]} while main run {item['fix_forward_run']} is red; "
                           + "mapping " + ", ".join(f"{m['job']}={m['bead']}@#{m['pr']}" for m in item.get("fix_forward_map", []))
                           + "; pin clears only on the next first-attempt full main pass")
            if item["repo"] == hzlib.CORE:
                if kind := reviewed_exception(item):
                    hzlib.note(f"sm queue: {kind} #{item['key']} merged (not a main fix); failed batch and release holds retained")
                    if item.get("reviewed_progress_main"):
                        history = load().get("progress_history", [])
                        update(progress_history=[*history, {"pr": item["key"], "head": item["reviewed_head"],
                            "main": main_commit(item), "at": int(now), "anchor": item["progress_anchor"]}])
                else:
                    went.append(item["key"])
                    commit = main_commit(item)
                    shas.append(commit or str(detail)[:8])
                    if commit and item.get("fixes_main"):
                        fix_shas.append(commit)
        else:
            stuck_or_merged(item, "needs work", detail)
    if went:  # keep every member for attribution; intermediate admissions never claim full CI
        fields = {"batch": {"prs": went, "shas": shas, "at": int(now)}}
        if fix_shas and (pinned := load().get("main_failure")):
            fields["main_failure"] = {**pinned, "fixed_by": fix_shas}
        update(**fields)
        hzlib.note("sm queue: batch: " + " ".join(went))
    if want_intent and len(merged_core) > 1:
        published = publish_batch(base, merged_core)
    if intent_commits and not published:
        close_intents(base, intent_commits)



def publish_intent(base: str, item: dict) -> str:
    """Give only a planned multi-PR batch a bounded assembly grace after its actual squash merge."""
    try:
        return _publish_intent(base, item)
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        return ""  # Unknown publication never delays recording the actual merge or claims CI success.


def _publish_intent(base: str, item: dict) -> str:
    if not isinstance(base, str) or not main_batch.SHA.fullmatch(base):
        return ""
    identity = hzlib.ghj("api", "user", default={}) or {}
    if not isinstance(identity, dict) or type(identity.get("id")) is not int or identity["id"] != main_batch.PUBLISHER_ID:
        return ""
    info = hzlib.ghj("pr", "view", item["pr"], "-R", hzlib.CORE, "--json", "mergeCommit", default={}) or {}
    merged = info.get("mergeCommit") if isinstance(info, dict) else None
    commit = merged.get("oid") if isinstance(merged, dict) else None
    if not isinstance(commit, str) or not main_batch.SHA.fullmatch(commit) or commit == base:
        return ""
    code, _ = hzlib.gh("api", "--method", "POST", f"repos/{hzlib.CORE}/statuses/{commit}",
                     "-f", "state=pending", "-f", f"context={main_batch.CONTEXT}",
                     "-f", "description=Batch assembly only; full CI remains required until final proof",
                     "-f", f"target_url={main_batch.intent_url(base, commit)}")
    return commit if code == 0 else ""


def close_intents(base: str, commits: list[str]) -> None:
    """Best-effort close surviving pending intent; a crash instead expires within its fixed lifetime."""
    identity = hzlib.ghj("api", "user", default={}) or {}
    if not isinstance(identity, dict) or type(identity.get("id")) is not int or identity["id"] != main_batch.PUBLISHER_ID:
        return
    for commit in commits:
        rows = hzlib.ghj("api", f"repos/{hzlib.CORE}/commits/{commit}/statuses?per_page=100", default=[]) or []
        if not isinstance(rows, list):
            continue
        status = next((r for r in rows if isinstance(r, dict) and str(r.get("context", "")).casefold() == main_batch.CONTEXT), {})
        creator = status.get("creator")
        if (status.get("state") != "pending" or not isinstance(creator, dict)
                or type(creator.get("id")) is not int or creator["id"] != main_batch.PUBLISHER_ID
                or status.get("target_url") != main_batch.intent_url(base, commit)):
            continue  # never overwrite a published proof or another publisher's newer status
        hzlib.gh("api", "--method", "POST", f"repos/{hzlib.CORE}/statuses/{commit}",
                 "-f", "state=success", "-f", f"context={main_batch.CONTEXT}",
                 "-f", "description=Batch assembly closed without delegation; full CI is required",
                 "-f", f"target_url={main_batch.intent_url(base, commit)}")


def publish_batch(base: str, prs: list[str]) -> bool:
    """Publish only a verified contiguous merged prefix; unknown evidence leaves ordinary full CI."""
    try:
        return _publish_batch(base, prs)
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        log("batch proof malformed or unavailable; every unproven push keeps full CI")
        return False


def _publish_batch(base: str, prs: list[str]) -> bool:
    if (not isinstance(base, str) or not main_batch.SHA.fullmatch(base)
            or not isinstance(prs, list) or not 2 <= len(prs) <= MAX_CORE_BATCH
            or not all(isinstance(number, str) and re.fullmatch(r"[1-9][0-9]*", number) for number in prs)):
        return False
    identity = hzlib.ghj("api", "user", default={}) or {}
    if not isinstance(identity, dict) or type(identity.get("id")) is not int or identity["id"] != main_batch.PUBLISHER_ID:
        return False
    members = []
    for number in prs:
        info = hzlib.ghj("pr", "view", number, "-R", hzlib.CORE, "--json", "mergeCommit", default={}) or {}
        commit = (info.get("mergeCommit") or {}).get("oid", "")
        if not isinstance(commit, str) or not main_batch.SHA.fullmatch(commit):
            return False
        members.append([int(number), commit])
    if not main_batch._valid_members(members) or base in {member[1] for member in members}:
        return False

    def get(endpoint):
        return hzlib.ghj("api", f"repos/{hzlib.CORE}/{endpoint}", default={}) or {}

    if not main_batch._valid_history({"base": base, "prs": members}, get):
        log("batch ancestry is not the actual contiguous merged prefix; full CI retained")
        return False
    final = members[-1][1]
    found = None
    for attempt in range(7):
        payload = hzlib.ghj("api", f"repos/{hzlib.CORE}/{core_facts.at_main().workflow_runs}?event=push&branch=main&head_sha={final}&per_page=100", default={}) or {}
        runs = payload.get("workflow_runs", [])
        matches = [r for r in runs if isinstance(r, dict) and str(r.get("display_title", "")).startswith(main_batch.PREFIX) and main_batch.valid_run(r, {"run": r.get("id"), "prs": members})]
        if len(matches) == 1:
            found = matches[0]
            break
        if attempt < 6:
            hzlib.sleep(5)
    if not found:
        log("batch proof unavailable; every push keeps full CI")
        return False
    url = main_batch.proof_url(base, members, found["id"])
    # The final attestation must exist first; consumers also require its aggregate-base marker.
    for _, commit in [members[-1], *members[:-1]]:
        code, _ = hzlib.gh("api", "--method", "POST", f"repos/{hzlib.CORE}/statuses/{commit}",
                         "-f", "state=success", "-f", f"context={main_batch.CONTEXT}",
                         "-f", "description=Batch identity only; full CI is recorded by the linked run",
                         "-f", f"target_url={url}")
        if code:
            log("batch proof incomplete; unproven pushes keep full CI")
            return False
    # Proof identity is independent of ordinary/failed batch attribution and reviewed progress history.
    update(batch_proof={"base": base, "final_run": found["id"], "members": members})
    return True


def site_only(item: dict) -> bool:
    """Same test as the CI workflow's site_only: such a PR runs the site's tests alone and cannot break the app's, so it isn't capped."""
    info = hzlib.ghj("pr", "view", item["pr"], "-R", item["repo"], "--json", "files", default={}) or {}
    paths = [f["path"] for f in info.get("files", []) if isinstance(f, dict)]
    return bool(paths) and all(SITE_PATH.fullmatch(p) and not SITE_EXCEPT.fullmatch(p) for p in paths)


def core_batch(ready: list[dict]) -> list[dict]:
    """Keep at most MAX_CORE_BATCH core PRs; site-only PRs go first, then queue-first.txt order."""
    first = FIRST.read_text().split() if FIRST.exists() else []
    rank = {key: place for place, key in enumerate(first)}
    core = [i for i in ready if i["repo"] == hzlib.CORE]
    site = [i for i in core if site_only(i)]
    app = sorted((i for i in core if i not in site), key=lambda i: rank.get(i["key"], len(first)))
    return (site + app)[:MAX_CORE_BATCH] + [i for i in ready if i["repo"] != hzlib.CORE]


def tools_changed_at() -> float:
    return edge.code_changed_at(__file__)  # moved: the loop restarts itself when any file next to this module changed


def queue_stamp() -> tuple | None:
    """Cheap identity for the queue's atomic file replacement, without reading GitHub or queue contents."""
    try:
        info = state_path().stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    except FileNotFoundError:
        return None
    except OSError:
        return ("unreadable",)


def wait_for_tick(interval: float, started: float) -> str:
    """Wake an idle runner on local events; unchanged state keeps the normal remote polling interval.

    Events during an active tick are still handled by the normal next tick. This wait never reads or changes
    remote checks, holds, the queue file or lock ownership, and every awakened tick runs all existing gates.
    """
    deadline = time.monotonic() + interval
    stamp, frozen = queue_stamp(), hzlib.merge_frozen()
    while (remaining := deadline - time.monotonic()) > 0:
        hzlib.sleep(min(1.0, remaining))
        if tools_changed_at() != started:
            return "tools changed"
        if queue_stamp() != stamp:
            return "queue changed"
        now_frozen = hzlib.merge_frozen()
        if frozen and not now_frozen:
            return "merge hold released"
        frozen = now_frozen
    return ""


def pr_words(tokens: list[str]) -> list[list[str]]:
    """`393 "plugins 38"` and `393 plugins 38` both mean two PRs."""
    out, held = [], []
    for token in tokens:
        parts = token.split()
        if len(parts) == 1 and parts[0] in hzlib.SHORT and parts[0]:
            held = parts
            continue
        out.append(held + parts)
        held = []
    return out


def guard_findings(repo: str, number: str):
    """hz-diff-guard's findings for a PR (None when its diff can't be read) and the text to show for them."""
    guard = hzlib.load("diff_guard")
    found = guard.check(repo, number)
    return found, guard.render(f"{repo}#{number}", found)


def pr_head(repo: str, number: str) -> str:
    """The PR's current head SHA, or empty when it cannot be read."""
    info = hzlib.ghj("pr", "view", number, "-R", repo, "--json", "headRefOid", default={}) or {}
    head = info.get("headRefOid", "") if isinstance(info, dict) else ""
    return head if isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}", head) else ""


def add(args) -> int:
    """Queue the PRs. A PR whose diff weakens a check (hz-diff-guard) is refused unless `--reviewed "reason"` is given, which
    queues it and notes the reason. Returns 1 when any PR was refused."""
    after = [hzlib.split_key(a)[2] for a in args.after]
    reviewed = (getattr(args, "reviewed", None) or "").strip()
    diagnostics = (getattr(args, "diagnostics_main", None) or "").strip()
    safety_fix = (getattr(args, "safety_fix_main", None) or "").strip()
    mitigation = (getattr(args, "mitigation_main", None) or "").strip()
    progress = (getattr(args, "reviewed_progress_main", None) or "").strip()
    evidence = (getattr(args, "mitigation_evidence", None) or "").strip()
    if mitigation and not evidence:
        print("refused: --mitigation-main requires --mitigation-evidence of the measured improvement", file=sys.stderr)
        return 1
    if getattr(args, "mitigation_evidence", None) is not None and not mitigation:
        print("refused: --mitigation-evidence requires --mitigation-main", file=sys.stderr)
        return 1
    purposes = (("diagnostics-main", "diagnostic-head", diagnostics), ("safety-fix-main", "safety-head", safety_fix), ("mitigation-main", "mitigation-head", mitigation), ("reviewed-progress-main", "progress-head", progress))
    for option, head_option, purpose in purposes:
        if getattr(args, option.replace("-", "_"), None) is not None and not purpose:
            print(f"refused: --{option} needs a purpose", file=sys.stderr)
            return 1
        head = (getattr(args, head_option.replace("-", "_"), None) or "").strip()
        if head and not purpose:
            print(f"refused: --{head_option} requires --{option}", file=sys.stderr)
            return 1
        if purpose and (not reviewed or not re.fullmatch(r"[0-9a-f]{40}", head) or len(args.pr) != 1 or hzlib.split_key(args.pr[0])[0] != hzlib.CORE or getattr(args, "fixes_main", False)):
            print(f"refused: --{option} requires one core PR, --reviewed independent review, --{head_option} SHA, and no --fixes-main", file=sys.stderr)
            return 1
    normal_head = getattr(args, "reviewed_head", None)
    fix_forward = getattr(args, "fix_forward", False) or getattr(args, "fix_for", None) or getattr(args, "dry_run", False)
    if normal_head is not None and (
        not reviewed or not re.fullmatch(r"[0-9a-f]{40}", normal_head)
        or len(args.pr) != 1
        or fix_forward or getattr(args, "fixes_main", False) or any(purpose for _, _, purpose in purposes)
    ):
        print("refused: --reviewed-head requires one normal PR, --reviewed reason and a full SHA", file=sys.stderr)
        return 1
    if fix_forward:
        return add_fix_forward(args, reviewed, after)
    purpose = safety_fix or mitigation or diagnostics or progress
    kind = "safety fix" if safety_fix else "mitigation" if mitigation else "diagnostic" if diagnostics else "reviewed progress"
    head_option = "safety-head" if safety_fix else "mitigation-head" if mitigation else "diagnostic-head" if diagnostics else "progress-head"
    if normal_head:
        head_option, kind = "reviewed-head", "reviewed"
    expected_head = normal_head or (getattr(args, head_option.replace("-", "_"), None) or "").strip()
    progress_anchor = completed_main_anchor() if progress else {}
    if progress and not progress_anchor:
        print("refused: reviewed progress requires a known completed main observation", file=sys.stderr)
        return 1
    refused, queued = 0, []
    for words in args.pr:
        repo, number, key = hzlib.split_key(words)
        guarded_head = pr_head(repo, number)  # the guard's verdict is bound to this head: the merge refuses any other
        found, shown = guard_findings(repo, number)
        if not guarded_head or pr_head(repo, number) != guarded_head:
            print(f"refused {key}: the PR head is unreadable or moved while the diff guard read it; ship it again", file=sys.stderr)
            refused += 1
            continue
        if found is None or found:
            print(shown, file=sys.stderr)
            if not reviewed:
                print(f"refused {key}: fix the change, or review it and add --reviewed \"reason\"", file=sys.stderr)
                refused += 1
                continue
        if repo in (hzlib.CORE, hzlib.PLUGINS):
            meta = hzlib.ghj("pr", "view", number, "-R", repo, "--json", "title,body", default={}) or {}
            notes = hz_combine_notes.problems(meta.get("title", ""), meta.get("body", ""))
            if notes:
                print(f"refused {key}: " + "; ".join(notes) + " (not bypassable by --reviewed; fix the PR body)", file=sys.stderr)
                refused += 1
                continue
        reviewed_head = ""
        if expected_head:
            reviewed_head = guarded_head
            if reviewed_head != expected_head:
                print(f"refused {key}: current {kind} head differs from --{head_option}; independent review required", file=sys.stderr)
                refused += 1
                continue
            if normal_head:
                hzlib.note(f"sm queue: #{key} pinned to {reviewed_head}; independent review: {reviewed}")
            else:
                hzlib.note(f"sm queue: {kind} #{key} at {reviewed_head}, not a main fix; purpose: {purpose}; independent review: {reviewed}")
            if mitigation:
                hzlib.note(f"sm queue: mitigation #{key} measured evidence: {evidence}; underlying main failure remains unresolved")
        if found is None or found:
            what = "unreadable diff" if found is None else ", ".join(sorted({f[2] for f in found}))
            hzlib.note(f"sm queue: {key if repo != hzlib.CORE else '#' + key} queued past the diff guard ({what}), reviewed: {reviewed}")
        queued = [i for i in queued if i["key"] != key]
        queued.append({"key": key, "repo": repo, "pr": number, "after": after, "status": "queued", "detail": "", "reruns": 0, "noted": "", "added": int(time.time()), "fixes_main": bool(getattr(args, "fixes_main", False)), "diagnostics_main": diagnostics, "safety_fix_main": safety_fix, "mitigation_main": mitigation, "mitigation_evidence": evidence, "reviewed_progress_main": progress, "progress_anchor": progress_anchor, "reviewed_head": reviewed_head, "guarded_head": guarded_head, "bead": (getattr(args, "bead", "") or "").strip()})
        print(f"queued {key}" + (f" after {', '.join(after)}" if after else ""))
    if queued:
        with state_lock():
            data = load()  # a remote diff/head read may overlap a tick that pins a main failure
            keys = {i["key"] for i in queued}
            data["items"] = [i for i in data["items"] if i["key"] not in keys] + queued
            save(data)
    return 1 if refused else 0


def add_fix_forward(args, reviewed: str, after: list[str]) -> int:
    """`add --fix-forward --for MAP [--reviewed R] [--dry-run] N`: check the lane's rules now, print each, queue on pass."""
    if not getattr(args, "fix_forward", False):
        print("refused: --for and --dry-run belong to --fix-forward", file=sys.stderr)
        return 1
    if len(args.pr) != 1 or hzlib.split_key(args.pr[0])[0] != hzlib.CORE or after:
        print("refused: --fix-forward takes exactly one core PR and no --after", file=sys.stderr)
        return 1
    try:
        mapping = parse_mapping(getattr(args, "fix_for", None) or [])
    except ValueError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    if not mapping:
        print("refused: --fix-forward needs --for \"JOB=BEAD[@PR]\" for every failing main job", file=sys.stderr)
        return 1
    repo, number, key = hzlib.split_key(args.pr[0])
    found, shown = guard_findings(repo, number)
    guard_pass = found == []
    print(f"sm head guard: {'PASS' if guard_pass else 'FLAG or unreadable'}" + ("" if guard_pass else f"; reviewed: {reviewed or 'NO'}"))
    if not guard_pass:
        print(shown, file=sys.stderr)
    problems, facts = fix_forward_check(number, mapping, bool(reviewed), guard_pass)
    for row in facts.get("map", []):
        print(f"attributed: main run {facts['main_run']} job {row['job']!r} -> bead {row['bead']} addressed by #{row['pr']}")
    # One-per-run waits in the queue for the next red run; everything else refuses now (incl. freeze and release).
    blocking = [text for kind, text in problems if not text.startswith("a fix-forward already merged")]
    for kind, text in problems:
        print(f"{'REFUSE' if text in blocking else 'HOLD'}: {text}", file=sys.stderr)
    verdict = "refused" if blocking else "admissible"
    print(f"fix-forward #{number} at {facts.get('head', '?')[:8]}: {verdict}" + (" (dry run, nothing queued)" if args.dry_run else ""))
    if blocking or args.dry_run:
        return 1 if blocking else 0
    if found and reviewed:
        hzlib.note(f"sm queue: #{key} queued past the diff guard ({', '.join(sorted({f[2] for f in found})) if found else 'unreadable diff'}), reviewed: {reviewed}")
    hzlib.note(f"sm queue: fix-forward #{key} queued at {facts['head'][:8]} for red main run {facts['main_run']}; mapping "
               + ", ".join(f"{m['job']}={m['bead']}@#{m['pr']}" for m in facts["map"]) + (f"; review: {reviewed}" if reviewed else "; guard PASS"))
    entry = {"key": key, "repo": repo, "pr": number, "after": [], "status": "queued", "detail": "", "reruns": 0, "noted": "",
             "added": int(time.time()), "fixes_main": False, "diagnostics_main": "", "safety_fix_main": "", "mitigation_main": "",
             "mitigation_evidence": "", "reviewed_progress_main": "", "progress_anchor": {}, "reviewed_head": facts["head"],
             "fix_forward": True, "fix_forward_for": mapping, "fix_forward_reviewed": reviewed, "fix_forward_map": facts["map"]}
    with state_lock():
        data = load()
        data["items"] = [i for i in data["items"] if i["key"] != key] + [entry]
        save(data)
    print(f"queued {key} (fix-forward)")
    return 0


def listing(active: bool = False) -> None:
    data = load()
    if not data["items"]:
        print("queue empty")
    hidden = [i["key"] for i in data["items"] if active and i["status"] in DONE]
    for i in data["items"]:
        if active and i["status"] in DONE:  # --active: merged and needs-work rows are history, not work
            continue
        label = reviewed_exception(i)
        purpose = i.get("safety_fix_main") or i.get("mitigation_main") or i.get("diagnostics_main")
        kind = f"{label} (not a main fix) at {i.get('reviewed_head', '?')[:8]}: {purpose}  " if label else ""
        if i.get("mitigation_main"):
            kind += f"cause unresolved; measured evidence: {i.get('mitigation_evidence', '?')}  "
        if i.get("reviewed_head") and not label and not i.get("fix_forward"):
            kind = f"reviewed at {i['reviewed_head'][:8]}  "
        if i.get("fix_forward"):
            kind += f"fix-forward at {i.get('reviewed_head', '?')[:8]} for " + ", ".join(f"{m['job']}={m['bead']}@#{m['pr']}" for m in i.get("fix_forward_map", [])) + "  "
        print(f"{i['key']:<12} {i['status']:<11} " + (f"after {','.join(i['after'])}  " if i["after"] else "") + kind + i.get("detail", ""))
    if hidden:
        print(f"({len(hidden)} merged/needs-work rows hidden; `sm queue list` shows all)")
    if data.get("red_batch", {}).get("prs"):
        print("main is RED after batch: " + " ".join(data["red_batch"]["prs"]) + f" (bisect: gh api repos/{hzlib.CORE}/compare/<last-green-sha>...<red-sha>)")
    if pinned := data.get("main_failure"):
        print(f"main failure pinned: run {pinned['databaseId']} at {pinned['headSha'][:8]}" + (f"; awaiting green main at fix {', '.join(pinned['fixed_by'])}" if pinned.get("fixed_by") else "; a diagnostic, safety fix, mitigation or rerun does not clear it"))
    if restored := data.get("main_restored"):
        print(f"main {restored.get('label')}: run {restored.get('run')} at {str(restored.get('sha'))[:8]} (merges only, never releases)")
    if data["last_batch"]:
        print(f"last batch {time.strftime('%m-%d %H:%M', time.localtime(data['last_batch']))}" + (f"; waiting: {data['blocked']}{block_detail(data['blocked'])}" if data.get("blocked") else ""))


def item_waits(items: list[dict], now: float) -> list[str]:
    """`item 790: waiting 277 min: BLOCKED with green checks ...` for every queued PR, from what the last tick recorded."""
    out = []
    for item in items:
        if item.get("status") != "queued":
            continue
        if item.get("waiting_since"):
            out.append(f"item {item['key']}: waiting {int((now - item['waiting_since']) / 60)} min: {item.get('wait_reason') or 'reason not recorded'}")
        elif item.get("ready_since"):
            age = int((now - item["ready_since"]) / 60)
            if age > 15:
                out.append(f"item {item['key']}: ready and unmerged for {age} min; a gate above holds it, or the runner is not ticking (sm mini loops)")
    return out


def why() -> int:
    """One-call, read-only diagnosis of why nothing merges. Changes no state (unlike tick, never calls main_state)."""
    data = load()
    queued = [i["key"] for i in data["items"] if i["status"] == "queued"]
    print(f"queued: {', '.join(queued) or 'none'}")
    frozen = hzlib.merge_frozen()
    print(f"merge freeze: {'ON [' + freeze_holder() + ']' if frozen else 'off'}")
    if data["last_batch"]:
        age = int((time.time() - data["last_batch"]) / 60)
        print(f"last batch {age} min ago" + ("" if age >= BATCH_SECONDS / 60 else " (cadence: one per 5 minutes)"))
    runs = hzlib.ghj("run", "list", "-R", hzlib.CORE, "--workflow", ci_workflow(), "--branch", "main", "-L", "5",
                     "--json", "status,conclusion,databaseId,headSha,attempt,createdAt") or []
    for r in runs[:5]:
        print(f"main run {r.get('databaseId')} {str(r.get('headSha'))[:8]} attempt {r.get('attempt')} {r.get('status')} {r.get('conclusion') or ''}")
    if pinned := data.get("main_failure"):
        print(f"pinned failure: run {pinned.get('databaseId')} at {str(pinned.get('headSha'))[:8]}; "
              f"later first-attempt full main pass restores progress: {'yes' if later_main_passes() else 'no'}")
    restored = rerun_restoration()
    print("restored-by-rerun (merges only): " + (f"yes, run {restored['run']} attempt 2 green, KNOWN: {', '.join(restored['beads'])}" if restored else "no"))
    kind, text, _ = main_rerun_plan()
    print(f"main-rerun: {'would rerun ' + text if kind == 'rerun' else ('nothing to rerun: ' if kind == 'none' else 'refused: ') + text}")
    if data.get("blocked"):
        print(f"last tick waiting: {data['blocked']}")
    stalls = item_waits(data["items"], time.time())
    for line in stalls:
        print(line)
    stale = [(i["key"], i["detail"]) for i in data["items"]
             if i.get("status") == "needs work" and str(i.get("detail", "")).startswith(main_plan.PREFIX)]
    for item in data["items"]:  # bead q9kh: a queued core PR whose merge would land a tree without a plan
        if item.get("status") == "queued" and item.get("repo") == hzlib.CORE:
            if reason := main_plan.gap(item["pr"], item.get("reviewed_head", "")):
                stale.append((item["key"], reason))
    for key, reason in stale:
        print(f"item {key}: {reason}")
    if stale:
        print(f"verdict: #{stale[0][0]} {stale[0][1]}")
        return 0
    if not queued:
        reason = "nothing queued"
    elif frozen:
        reason = "merge freeze holds all core PRs"
    elif data.get("blocked") == "merge freeze is on":
        reason = "the freeze was lifted after the last tick; the next tick (within 5 minutes) gates again"
    elif data.get("blocked"):
        reason = f"last tick: {data['blocked']}"
    elif stalls:
        reason = "no queue-level block recorded, but " + "; ".join(line.removeprefix("item ") for line in stalls)
    else:
        reason = "no block recorded; the next tick should merge"
    print(f"verdict: {reason}")
    return 0


def main(argv: list[str]) -> int:
    """Every command; an unreadable state file refuses with exit 1 (and its alarm), never as an empty queue. Core's
    release policy at main unreadable (no manifest yet): one line and exit 1, nothing merges."""
    try:
        return _main(argv)
    except StateUnreadable as error:
        print(f"sm queue: REFUSED: {error}", file=sys.stderr)
        return 1
    except rc.CandidateError as error:
        if argv[:1] == ["checkpoint"]:
            raise  # the checkpoint command reports its own refusal (sm queue checkpoint)
        print(f"sm queue: REFUSED: {error}", file=sys.stderr)
        return 1


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sm queue", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("--after", action="append", default=[])
    p.add_argument("--bead", default="", help="the bead this PR closes; on merge the queue records pr + merge_sha and runs sm beads stamp")
    p.add_argument("--reviewed", metavar="REASON", help="queue a PR the diff guard flagged; the reason goes to sm note")
    p.add_argument("--reviewed-head", metavar="SHA", help="pin one normal PR (any repository) to its reviewed full SHA; requires --reviewed")
    kind = p.add_mutually_exclusive_group()
    kind.add_argument("--fixes-main", action="store_true", help="the PR that makes a red main green: it may merge while main is red, alone")
    kind.add_argument("--diagnostics-main", metavar="REASON", help="one reviewed core diagnostic, not a fix; requires --reviewed and pins its head")
    kind.add_argument("--safety-fix-main", metavar="REASON", help="one reviewed core safety fix, not a main CI fix; requires --reviewed and pins its head")
    kind.add_argument("--mitigation-main", metavar="REASON", help="one reviewed measured core mitigation; main cause remains unresolved; requires --reviewed, --mitigation-head and --mitigation-evidence")
    kind.add_argument("--reviewed-progress-main", metavar="REASON", help="owner-approved reviewed progress past a known main failure; real PR checks stay required")
    kind.add_argument("--fix-forward", action="store_true", help="one fix|test|ci core PR with fully green PR CI may merge while main is red; needs --for (see top of --help)")
    p.add_argument("--for", dest="fix_for", action="append", metavar="JOB=BEAD[@PR]", help="fix-forward: attribute each failing main job to a flaky/bug bead this PR (or merged @PR) addresses; comma- or semicolon-separated, or repeated")
    p.add_argument("--dry-run", action="store_true", help="fix-forward: print every check and the verdict, queue nothing")
    p.add_argument("--progress-head", metavar="SHA", help="exact independently reviewed progress head; must match current PR head")
    p.add_argument("--mitigation-head", metavar="SHA", help="the full mitigation commit independently reviewed; must match the current PR head")
    p.add_argument("--mitigation-evidence", metavar="EVIDENCE", help="observed improvement supporting this mitigation, independently reviewed at --mitigation-head")
    p.add_argument("--safety-head", metavar="SHA", help="the full safety-fix commit independently reviewed; must match the current PR head")
    p.add_argument("--diagnostic-head", metavar="SHA", help="the full diagnostic commit independently reviewed; must match the current PR head")
    p.add_argument("pr", nargs="+")
    p = sub.add_parser("main-rerun", help="rerun main's failed jobs once when every failing job is KNOWN (merges only)")
    p.add_argument("--dry-run", action="store_true", help="print what would be rerun, start nothing")
    p = sub.add_parser("checkpoint", help="verify merged-release and focused-main continuity; merges only, pins unchanged")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--run", type=int, required=True)
    p.add_argument("--core-repo", type=Path, required=True)
    p.add_argument("--apply", action="store_true", help="record evidence identities after verification; each gate revalidates them")
    p = sub.add_parser("list")
    p.add_argument("--active", action="store_true", help="hide merged and needs-work rows (stale history); prints how many were hidden")
    sub.add_parser("why", help="read-only one-call diagnosis of why the queue is not merging")
    p = sub.add_parser("drop")
    p.add_argument("pr", nargs="+")
    p = sub.add_parser("run")
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=int, default=300)
    args = parser.parse_args(argv)
    if args.cmd == "run" and args.interval < 0:
        parser.error("--interval must be non-negative")
    if args.cmd == "add":
        args.pr = pr_words(args.pr)
        return add(args)
    elif args.cmd == "main-rerun":
        return main_rerun(dry_run=args.dry_run)
    elif args.cmd == "checkpoint":
        held = acquire_lock(block=False)
        if held is None:
            print("sm queue: checkpoint requires the idle queue lock", file=sys.stderr)
            return 1
        try:
            proof = main_checkpoint.establish(state_path(), load().get("main_failure", {}),
                                              args.pr, args.run, args.core_repo, apply=args.apply)
            print(json.dumps(proof, sort_keys=True))
            return 0
        finally:
            held.close()
    elif args.cmd == "list":
        listing(args.active)
    elif args.cmd == "why":
        return why()
    elif args.cmd == "drop":
        keys = [hzlib.split_key(p)[2] for p in pr_words(args.pr)]
        with state_lock():
            data = load()
            data["items"] = [i for i in data["items"] if i["key"] not in keys]
            save(data)
    else:
        if (held := acquire_lock(block=False)) is None:
            log(f"another sm daemon queue holds {lock_path()}; waiting for it to exit")
            held = acquire_lock()
        if held is None:
            print("sm queue: cannot acquire queue lock", file=sys.stderr)
            return 1
        log(f"sm daemon queue started, pid {os.getpid()}")
        started = tools_changed_at()
        while True:
            if tools_changed_at() != started:  # the train is reloaded each tick; a stale hzlib here broke a batch on 11-01
                os.execv(sys.executable, [sys.executable, *sys.argv])  # noqa: S606 - this same program, its own argv
            tick_started = time.monotonic()
            log("tick started")
            try:
                tick()
            except Exception as error:  # a bad tick must not end the loop
                log(f"tick failed: {error!r}")
            finally:
                log(f"tick finished in {time.monotonic() - tick_started:.1f}s")
            if args.once:
                return 0
            if reason := wait_for_tick(args.interval, started):
                log(f"idle wake: {reason}")
    return 0

