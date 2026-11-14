"""Focused hosted runs: re-run the failing job of a CI run, or dispatch ONE lane or a few test ids on a hosted runner.

Moved from the former single-lane tool with its decisions unchanged (`rules.py` has the pure ones). What changed is
how it reaches the world: GitHub through the one `Gh` client on the `Commands` provider, time through the `Clock`,
the admission lock through the `Store`, and every path and the repository through settings (`bind`).

Guards, in the order they act:
- never main or release/* without --force; beside an active release/* run only a run that needs no macOS runner,
  within the job ceiling (not even --force lifts that)
- at most 5 focused runs in flight; at the 20-job ceiling or 4 macOS/ARM jobs it refuses while main CI waits
- SHA guard: `dispatch` refuses a branch whose tip is the head of an open PR, a `pull_request:<sha>` key of the focused
  plan registry, or the tip of a remote branch outside validation/*, bisect/* and rehearsal/*. The required checks
  attach by SHA to the OLDEST check suite of a commit; a dispatch there leaves the PR BLOCKED with every check green.
  --force does not lift it, and an unreadable PR list, registry or branch list refuses too.
- --throwaway puts an empty commit on the tip in a temporary worktree of the core clone (ordinary hooks, no bypass),
  pushes it to validation/<name>-<sha8> and dispatches there: the same tree on a SHA nothing else uses.
- observation, the remote start and the local receipt happen under one lock; a start whose outcome is unknown leaves
  a pending marker and is never retried.

The functions raise SystemExit with the refusal as its message (exit 1) or 2 for the SHA guard; `dispatch.py` turns
that into the command's refusal.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from steuermaschine.config import child_environment
from steuermaschine.defaults import SystemClock
from steuermaschine.github import Gh, GitHub
from steuermaschine.protocols import Busy

from . import rules as c

LOCK = "heinzel.dispatch.admission"


class Edge:
    """How the functions below reach the world. `bind` sets it for one command; a test sets what it needs."""

    def __init__(self, commands=None, clock=None, store=None, journal=None, out=None, err=None):
        self.commands, self.clock, self.store = commands, clock or SystemClock(), store
        self.journal = journal or (lambda text: None)
        self.out, self.err = out, err
        self.result: dict | None = None  # what the last `wait` found, for the result model


EDGE = Edge()
R = ""  # OWNER/NAME of the core repository (setting `repo`)
STATE = Path("focused-runs.jsonl")  # one line per focused run of the last 24 hours (setting `dispatch_state_file`)
CORE_CLONE = Path()  # a clone of the core repository, for the throwaway commit (setting `core_repo_dir`)
SCRATCH = Path()  # where the temporary worktree goes (setting `dispatch_scratch_dir`)
CAPS = c.Caps()  # what the account runs at once (settings `ci_job_cap`, `ci_macos_cap`)


def bind(edge: Edge, repo: str, state: Path, core_clone: Path, scratch: Path, caps: c.Caps | None = None) -> None:
    global EDGE, R, STATE, CORE_CLONE, SCRATCH, CAPS
    EDGE, R, STATE, CORE_CLONE, SCRATCH, CAPS = edge, repo, state, core_clone, scratch, caps or c.Caps()


def out(text: str = "") -> None:
    EDGE.out(text) if EDGE.out else print(text, flush=True)


def warn(text: str = "") -> None:
    if EDGE.err:
        EDGE.err(text)
    else:
        import sys

        print(text, file=sys.stderr, flush=True)


def stamp() -> str:
    return datetime.fromtimestamp(EDGE.clock.now()).strftime("%Y-%m-%dT%H:%M:%S")


def fail_log(rid, tail: int = 25, limit: int = 4) -> str:
    """The end of the log of each failed job of a run, a few lines each."""
    hub = GitHub(EDGE.commands, repo=R, timeout=60)
    jobs = hub.jobs(str(rid))
    lines = []
    for job in [j for j in jobs.jobs if j.name in jobs.failed][:limit]:
        log = hub.log_tail(job.id, tail)
        lines += [f"--- {job.name}" + (f" (failed step: {', '.join(job.failed_steps)})" if job.failed_steps else "")]
        lines += log.lines if log.state == "ok" else [f"    log {log.state}"]
    return "\n".join(lines)


POLL, MAX_WAIT = 30, 45 * 60
EXACT_RUNNER_PATH = "scripts/focused_job.py"
EXACT_RUNNER_SHA256 = "1b48073d99191d455ef3991a0242f61c7d33b92c39c1f48bea6673396eec4ca1"
EXACT_WORKFLOW_BLOB = "e0d31fc6ead62eaf0792f876c9a3e00dcc79f0b2"


def gh(*a, ok=False, timeout=60):
    """One `gh` call through the one client. Never retried here: a start that may have happened is not repeated."""
    r = Gh(EDGE.commands, repo=R, timeout=timeout).run(list(a), retry=False)
    if r.rc in (124, 127):
        raise SystemExit(f"refusing: GitHub API unavailable: {Gh.why(r)}")
    if r.rc and not ok:
        raise SystemExit(f"gh {' '.join(a[:3])}: {r.err.strip()[:300]}")
    return r.out if r.rc == 0 else ""


def ghj(*a, ok=False):
    try:
        return json.loads(gh(*a, ok=ok) or "null")
    except json.JSONDecodeError as err:
        raise SystemExit("refusing: malformed GitHub API response") from err


def refused_by_github(text):
    """True only for a definite HTTP 4xx answer from `gh api` (not 408): GitHub did not start anything."""
    return not text.startswith("refusing:") and bool(re.search(r"\(HTTP 4(?!08)\d\d\)", text))


def note(text):
    EDGE.journal("ci one: " + text)


class Unreadable(SystemExit):
    """GitHub's inventory could not be read consistently: a read error, never a fact about capacity or the head.
    It refuses a new start; it is no reason to cancel runs that already started."""


INVENTORY_BACKOFF = (2.0, 5.0)  # seconds before the 2nd and the 3rd read


def _inventory_once(endpoint, key):
    pages = ghj("api", "--paginate", "--slurp", f"repos/{R}/actions/{endpoint}")
    if not isinstance(pages, list) or not pages or any(
        not isinstance(page, dict) or type(page.get("total_count")) is not int or not isinstance(page.get(key), list)
        for page in pages
    ):
        raise SystemExit(f"refusing: malformed GitHub {key} inventory")
    items = [item for page in pages for item in page[key]]
    if any(page["total_count"] != len(items) for page in pages) or any(
        not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] < 1 for item in items
    ) or len({item["id"] for item in items}) != len(items):
        raise SystemExit(f"refusing: incomplete GitHub {key} inventory")
    return items


def inventory(endpoint, key):
    """Every item of one listing, or a refusal. GitHub counts (`total_count`) and lists in separate reads: while runs
    change status or new ones arrive (a lane just dispatched, a push), one answer can disagree with itself or a page
    can shift (an item twice or missing). Such a read is retried a bounded number of times; only a listing that stays
    inconsistent refuses, as `Unreadable`."""
    for pause in (*INVENTORY_BACKOFF, None):
        try:
            return _inventory_once(endpoint, key)
        except SystemExit as err:
            if pause is None:
                raise Unreadable(f"{err.code} after {len(INVENTORY_BACKOFF) + 1} reads") from err
            EDGE.clock.sleep(pause)


def jobs_of(rid):
    jobs = inventory(f"runs/{rid}/jobs?per_page=100&filter=latest", "jobs")
    if any(job.get("status") not in (*c.ACTIVE, "completed") or not isinstance(job.get("labels"), list)
           or (job["status"] in c.ACTIVE and not job["labels"])
           or any(not isinstance(label, str) for label in job["labels"]) for job in jobs):
        raise SystemExit(f"refusing: uncertain runner inventory for run {rid}")
    return jobs


def workflow_path(run):
    return (run.get("path") or "").split("@", 1)[0]


def run_state(rid):
    run = ghj("api", f"repos/{R}/actions/runs/{rid}")
    if (not isinstance(run, dict) or run.get("id") != rid
            or run.get("status") not in (*c.ACTIVE, "completed")
            or type(run.get("run_attempt")) is not int or run["run_attempt"] < 1
            or not isinstance(run.get("head_branch"), str) or not run["head_branch"]
            or not isinstance(run.get("head_sha"), str) or not re.fullmatch(r"[0-9a-f]{40}", run["head_sha"])
            or not isinstance(run.get("path"), str) or not run["path"]
            or (run["status"] == "completed" and not run.get("conclusion"))):
        raise SystemExit(f"refusing: uncertain GitHub state for run {rid}")
    return run


def tally_of(rid):
    """`FOCUSED TALLY k/n green` from a focused-job log, or the run's conclusion for a re-run."""
    try:
        return c.parse_tally(gh("run", "view", str(rid), "-R", R, "--log", ok=True, timeout=300))
    except SystemExit:  # the log could not be read in time: the tally is unknown, the result of the run is not
        return ""


@contextmanager
def admission_lock():
    """One admission at a time on this machine. Blocks: the holder only observes, starts and records."""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(EDGE.store.lock(LOCK))
                break
            except Busy:
                EDGE.clock.sleep(0.05)
        yield


def read_rows():
    try:
        return [json.loads(line) for line in STATE.read_text().splitlines() if line.strip()]
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as err:
        raise SystemExit(f"refusing: unreadable focused-run receipts in {STATE}") from err


def rows(*, strict=False):
    """Recorded runs of the last 24 h, refreshed: active ones get their status, finished ones their result once."""
    now = EDGE.clock.now()
    keep = []
    for row in read_rows():
        if row.get("result") is None:
            try:
                run = run_state(row["run"])
            except (SystemExit, ValueError):
                if strict:
                    raise
            else:
                expected = c.WORKFLOW if row["kind"] == "dispatch" else "ci.yml"
                if "attempt" in row and (
                    workflow_path(run) != f".github/workflows/{expected}" or run["head_branch"] != row["branch"]
                ):
                    raise SystemExit(f"refusing: focused receipt does not match run {row['run']}")
                if "head_sha" in row and not exact_run_matches(run, row["branch"], row["head_sha"]):
                    raise SystemExit(f"refusing: reviewed focused receipt does not match run {row['run']}")
                if run["run_attempt"] >= row.get("attempt", 1):
                    row["status"] = run["status"]
                    if run["status"] == "completed":
                        row["result"] = run["conclusion"]
        if not strict and row.get("result") is not None and row["kind"] == "dispatch" and "tally" not in row:
            row["tally"] = tally_of(row["run"])
        if row.get("result") is None or now - row.get("t", now) <= 24 * 3600:
            keep.append(row)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text("".join(json.dumps(r) + "\n" for r in keep))
    os.replace(temporary, STATE)
    return keep


def admit(start, wants_arm, wait_for_slot, *, new_jobs=1, new_arm_jobs=None, rerun_id=None):
    """Serialize observation, the remote start, and the local receipt. Never retry an ambiguous start."""
    t0 = EDGE.clock.now()
    while True:
        with admission_lock():
            pending = STATE.with_suffix(".pending")
            if pending.exists():
                raise SystemExit(f"refusing: reconcile the uncertain focused start in {pending} before retrying")
            why = _capacity(wants_arm, new_jobs, new_arm_jobs, rerun_id)
            if not why:
                return start(pending)
        if not wait_for_slot or "release/*" in why or EDGE.clock.now() - t0 > 30 * 60:  # beside a candidate a refused start does not wait here
            raise SystemExit(why)
        out(f"{why.removeprefix('refusing: ')}: waiting for a slot")
        EDGE.clock.sleep(60)


def _capacity(wants_arm, new_jobs=1, new_arm_jobs=None, rerun_id=None):
    runs = {}
    for status in c.ACTIVE:
        for run in inventory(f"runs?status={status}&per_page=100", "workflow_runs"):
            if (run.get("status") != status or not isinstance(run.get("head_branch"), str)
                    or not run["head_branch"] or not isinstance(run.get("path"), str) or not run["path"]):
                raise SystemExit("refusing: uncertain GitHub active run state")
            runs[run["id"]] = {**run, "headBranch": run["head_branch"]}
    if why := c.capacity_refusal(runs.values(), [], 0, wants_arm, new_arm_jobs=new_arm_jobs, caps=CAPS):
        return why
    tracked = {row["run"]: row for row in rows(strict=True) if row.get("result") is None}
    if rerun_id is not None and (rerun_id in tracked or rerun_id in runs):
        return f"refusing: run {rerun_id} already in flight"
    focused = set(tracked) | {rid for rid, run in runs.items() if workflow_path(run) == f".github/workflows/{c.WORKFLOW}"}
    if why := c.capacity_refusal(runs.values(), [], len(focused), wants_arm, new_arm_jobs=new_arm_jobs, caps=CAPS):
        return why
    jobs = []
    for rid in runs.keys() | tracked.keys():
        active = [j for j in jobs_of(rid) if j["status"] in c.ACTIVE] if rid in runs else []
        if not active and rid in tracked:
            row = tracked[rid]
            active = [{"labels": ["macos-latest"] if i < row.get("arm_jobs", 1) else []}
                      for i in range(row.get("jobs", 1))]
        jobs.extend({**job, "run_id": rid} for job in active)
    why = c.capacity_refusal(runs.values(), jobs, len(focused), wants_arm, new_jobs=new_jobs, new_arm_jobs=new_arm_jobs, caps=CAPS)
    if not why and (msg := c.capacity_note(jobs, wants_arm, CAPS)):
        out(msg)
    return why


def record(rid, kind, what, branch, attempt, jobs, arm_jobs, *, head_sha=None):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.open("a") as f:
        row = {"run": rid, "kind": kind, "what": what, "branch": branch, "attempt": attempt, "jobs": jobs,
               "arm_jobs": arm_jobs, "at": stamp(), "t": EDGE.clock.now(), "result": None}
        if head_sha is not None:
            row["head_sha"] = head_sha
        f.write(json.dumps(row) + "\n")


RUN_READ_BACKOFF = (2.0, 5.0, 10.0)  # seconds before the 2nd, 3rd and 4th read of one run


def run_view(rid):
    """The status of one run, read again a bounded number of times: GitHub answers a fresh or busy run with a transient
    error (a 404 right after the start, a 502). Reading never changes anything, so it is safe to repeat; a run that
    stays unreadable refuses as `Unreadable` (a read error, never a fact about the run)."""
    for pause in (*RUN_READ_BACKOFF, None):
        try:
            d = ghj("run", "view", str(rid), "-R", R, "--json", "status,conclusion,url,attempt")
            if isinstance(d, dict) and isinstance(d.get("status"), str) and type(d.get("attempt")) is int:
                return d
            why = f"malformed answer for run {rid}"
        except SystemExit as err:
            why = str(err.code)
        if pause is None:
            raise Unreadable(f"refusing: cannot read run {rid}: {why[:200]} after {len(RUN_READ_BACKOFF) + 1} reads")
        EDGE.clock.sleep(pause)


def wait(rid, names, as_json, label, attempt=1):
    t0 = EDGE.clock.now()
    while True:
        d = run_view(rid)
        if d["status"] == "completed" and d["attempt"] >= attempt:
            break
        if EDGE.clock.now() - t0 > MAX_WAIT:
            raise SystemExit(f"run {rid}: attempt {attempt} did not complete within the wait limit")
        EDGE.clock.sleep(POLL)
    jobs = jobs_of(rid)
    lines = c.summarize(jobs, names)
    arts = [a["name"] for a in (ghj("api", f"repos/{R}/actions/runs/{rid}/artifacts", "-q", ".artifacts", ok=True) or [])]
    mins = (EDGE.clock.now() - t0) / 60
    tally = tally_of(rid) if d["status"] == "completed" else ""
    result = {"run": rid, "url": d.get("url"), "attempt": d.get("attempt"), "status": d["status"], "conclusion": d.get("conclusion"),
              "jobs": [{"name": j["name"], "conclusion": j.get("conclusion")} for j in jobs if names is None or j["name"] in names],
              "artifacts": arts, "waited_min": round(mins, 1), "tally": tally}
    fail = d.get("conclusion") not in ("success",) and d["status"] == "completed"
    log = ""
    if fail:
        log = fail_log(rid)
        result["faillog"] = log
    note(f"{label}: run {rid} {d['status']}/{d.get('conclusion')} after {mins:.0f} min ({'; '.join(lines)[:160]})")
    EDGE.result = result
    if as_json:
        out(json.dumps(result, indent=1))
    else:
        out(f"run {rid} attempt {d.get('attempt')}: {d['status']}/{d.get('conclusion')} after {mins:.0f} min  {d.get('url')}")
        out("\n".join(lines) or "(no matching jobs)")
        if tally:
            out(f"tally: {tally}")
        if arts:
            out(f"artifacts: {', '.join(arts)}  (gh run download {rid} -R {R} -D {SCRATCH}/{rid})")
        if log:
            out(log.rstrip())
    return 0 if d.get("conclusion") == "success" else 1


def start_rerun(run, targets, one_job, wait_for_slot):
    rid = run["id"]
    attempt = run["run_attempt"] + 1
    arm_jobs = sum(c.is_arm(job["labels"]) for job in targets)
    names = ", ".join(job["name"] for job in targets)

    def start(pending):
        current = run_state(rid)
        if current["status"] != "completed" or any(
            current.get(key) != run.get(key) for key in ("head_sha", "head_branch", "path", "run_attempt")
        ):
            raise SystemExit(f"refusing: run {rid} changed before re-run admission")
        pending.write_text(json.dumps({"kind": "rerun", "run": rid, "attempt": attempt}) + "\n")
        args = ["--job", str(targets[0]["id"])] if one_job else ["--failed"]
        gh("run", "rerun", str(rid), "-R", R, *args)
        record(rid, "rerun", names, run["head_branch"], attempt, len(targets), arm_jobs)
        pending.unlink()
        return attempt

    return admit(start, bool(arm_jobs), wait_for_slot, new_jobs=len(targets), new_arm_jobs=arm_jobs, rerun_id=rid)


def rerun_repeat(run, job, n, as_json):
    """N sequential re-runs of one job (each after the previous finished); prints the tally."""
    rid = run["id"]
    results = []
    for i in range(n):
        attempt = start_rerun(run, [job], True, True)
        t0 = EDGE.clock.now()
        while True:
            run = run_state(rid)
            if run["status"] == "completed" and run["run_attempt"] >= attempt:
                break
            if EDGE.clock.now() - t0 > MAX_WAIT:
                raise SystemExit(f"run {rid}: attempt {attempt} did not complete within the wait limit")
            EDGE.clock.sleep(POLL)
        mine = [j for j in jobs_of(rid) if j["name"] == job["name"]]
        concl = mine[0].get("conclusion") if mine else "unknown"
        results.append(concl)
        job = mine[0] if mine else job
        out(f"round {i + 1}/{n}: {concl}")
    tally = c.tally_line(results)
    note(f"rerun-repeat {job['name']} @ {run['head_branch']}: {tally}")
    out(json.dumps({"run": rid, "job": job["name"], "results": results, "tally": tally}) if as_json else tally)
    return 0 if results and all(r == "success" for r in results) and len(results) == n else 1


def cmd_repeats(a):
    out(c.rounds_for(c.parse_rate(a.fail_rate)))
    return 0


def cmd_rerun(a):
    n = int(a.target)
    if n > 100000:
        rid = n
        run = run_state(rid)
        branch = run["head_branch"]
    else:
        pr = ghj("pr", "view", str(n), "-R", R, "--json", "headRefName,headRefOid")
        branch = pr["headRefName"]
        runs = ghj("run", "list", "-R", R, "--branch", branch, "--limit", "30", "--json", "databaseId,headSha,workflowName,createdAt,status")
        picked = c.pick_run(runs, pr["headRefOid"])
        if not picked:
            raise SystemExit(f"#{n}: no CI run for head {pr['headRefOid'][:8]}")
        rid = picked["databaseId"]
        run = run_state(rid)
        if run["head_branch"] != branch or run.get("head_sha") != pr["headRefOid"]:
            raise SystemExit(f"refusing: run {rid} does not match the PR branch and head")
    if workflow_path(run) != ".github/workflows/ci.yml":
        raise SystemExit(f"refusing: run {rid} is outside the CI workflow")
    # Merge tier (Core ADR 0149): a rerun of a red main run is a valid repair; release/* keeps the first-attempt rule.
    if why := c.branch_refusal(branch, a.force or (branch == "main" and getattr(a, "merge_tier", lambda: False)())):
        raise SystemExit(why)
    if run.get("status") != "completed":
        raise SystemExit(f"run {rid} is still {run.get('status')}: GitHub re-runs jobs only of a finished run (sm ci one watch {rid})")
    jobs = jobs_of(rid)
    if a.job:
        job, err = c.match_job(jobs, a.job)
        if err:
            raise SystemExit(err)
        targets = [job]
    else:
        targets = c.failed_jobs(jobs)
        if not targets:
            raise SystemExit(f"run {rid}: no failed job to re-run")
    if a.repeat > 1:
        if not a.job:
            raise SystemExit("--repeat needs --job (one job, re-run N times)")
        if a.repeat > c.MAX_RERUN_REPEAT:
            raise SystemExit(f"--repeat at most {c.MAX_RERUN_REPEAT} for re-runs; use dispatch --test --repeat for more")
        return rerun_repeat(run, targets[0], a.repeat, a.json)
    names = [j["name"] for j in targets]
    attempt = start_rerun(run, targets, bool(a.job), a.wait)
    label = f"rerun {'#' + a.target if n <= 100000 else rid} {branch} [{', '.join(names)[:80]}]"
    note(f"{label} started (verification, not a fix)")
    out(f"re-running {len(names)} job(s) of run {rid} ({branch}): {', '.join(names)}")
    if not a.wait:
        out(f"wait with: sm ci one watch {rid}")
        return 0
    return wait(rid, [*names, "CI OK"], a.json, label, attempt)


def exact_file_blob(path, head):
    """Read the directory entry to bind a Git blob without following a symlink."""
    directory = path.rsplit("/", 1)[0]
    entries = ghj("api", f"repos/{R}/contents/{directory}?ref={head}")
    if not isinstance(entries, list):
        raise SystemExit(f"refusing: {directory} contents are unavailable at --reviewed-head")
    matches = [entry for entry in entries if isinstance(entry, dict) and entry.get("path") == path]
    if len(matches) != 1 or matches[0].get("type") != "file":
        raise SystemExit(f"refusing: {path} is not an unambiguous file at --reviewed-head")
    sha = matches[0].get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SystemExit(f"refusing: {path} has no Git blob identity at --reviewed-head")
    return sha


def exact_capability(branch, head):
    """Verify the unused ref and reviewed bytes under the admission lock."""
    if any(row.get("branch") == branch and row.get("kind") == "dispatch" for row in read_rows()):
        raise SystemExit("refusing: use a fresh validation/* branch for a parametrized repeat")
    history = ghj("api", f"repos/{R}/actions/workflows/{c.WORKFLOW}/runs?branch={quote(branch, safe='')}&per_page=1")
    if (not isinstance(history, dict) or type(history.get("total_count")) is not int
            or history["total_count"] != 0 or history.get("workflow_runs") != []):
        raise SystemExit("refusing: validation branch is used or its focused run history is uncertain")

    workflow = f".github/workflows/{c.WORKFLOW}"
    if exact_file_blob(workflow, head) != EXACT_WORKFLOW_BLOB:
        raise SystemExit("refusing: focused workflow blob differs from the reviewed version")
    runner_blob = exact_file_blob(EXACT_RUNNER_PATH, head)
    runner = ghj("api", f"repos/{R}/git/blobs/{runner_blob}")
    if (not isinstance(runner, dict) or runner.get("sha") != runner_blob
            or runner.get("encoding") != "base64" or not isinstance(runner.get("content"), str)):
        raise SystemExit("refusing: focused runner contents are unavailable")
    try:
        raw = base64.b64decode("".join(runner["content"].split()), validate=True)
    except ValueError as err:
        raise SystemExit("refusing: malformed focused runner contents") from err
    if hashlib.sha256(raw).hexdigest() != EXACT_RUNNER_SHA256:
        raise SystemExit("refusing: focused runner digest differs from the reviewed version")

    # Dispatch only accepts a named ref. Read it last and verify the actual run after GitHub resolves it.
    ref = ghj("api", f"repos/{R}/git/ref/heads/{branch}")
    expected_object = ref.get("object") if isinstance(ref, dict) else None
    if (not isinstance(expected_object, dict) or ref.get("ref") != f"refs/heads/{branch}"
            or expected_object.get("type") != "commit" or expected_object.get("sha") != head):
        raise SystemExit("refusing: validation branch does not point at --reviewed-head")


def exact_run_matches(run, branch, head):
    workflow, _, workflow_ref = run["path"].partition("@")
    return (run["head_sha"] == head and run["head_branch"] == branch and run["run_attempt"] == 1
            and run.get("event") == "workflow_dispatch" and workflow == f".github/workflows/{c.WORKFLOW}"
            and workflow_ref in ("", branch, f"refs/heads/{branch}")
            and all(isinstance(run.get(key), dict) and run[key].get("full_name") == R
                    for key in ("repository", "head_repository")))


THROWAWAY_SUBJECT = "chore: validation only"


def remote_tip(branch):
    ref = ghj("api", f"repos/{R}/git/ref/heads/{branch}", ok=True)
    tip = ref.get("object") if isinstance(ref, dict) else None
    if (not isinstance(tip, dict) or ref.get("ref") != f"refs/heads/{branch}" or tip.get("type") != "commit"
            or not isinstance(tip.get("sha"), str) or not c.SHA.fullmatch(tip["sha"])):
        raise SystemExit(f"refusing: cannot read the tip of {branch} on the remote (push it first)")
    return tip["sha"]


def open_pr_heads():
    pages = ghj("api", "--paginate", "--slurp", f"repos/{R}/pulls?state=open&per_page=100")
    heads = []
    if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
        raise SystemExit("refusing: cannot read the open PRs; the SHA guard cannot be decided")
    for pr in (pr for page in pages for pr in page):
        head = pr.get("head") if isinstance(pr, dict) else None
        if (type(pr.get("number") if isinstance(pr, dict) else None) is not int or not isinstance(head, dict)
                or not isinstance(head.get("sha"), str) or not c.SHA.fullmatch(head["sha"])):
            raise SystemExit("refusing: malformed open PR list; the SHA guard cannot be decided")
        heads.append((pr["number"], head["sha"]))
    return heads


def plan_registry_keys():
    """Keys of the focused plan registry, decoded by the admission tool's own decoder; undecodable refuses."""
    try:
        from ..admission_lib import focused_slots as slots  # the admission area's decoder: one reading of the registry
    except ImportError as err:  # fail closed: without the decoder the guard cannot know the reserved heads
        raise SystemExit("refusing: the focused plan registry decoder is not installed; the SHA guard cannot be decided") from err

    pages = ghj("api", "--paginate", "--slurp", f"repos/{R}/actions/variables?per_page=30")
    if not isinstance(pages, list) or not pages or any(
            not isinstance(page, dict) or type(page.get("total_count")) is not int
            or not isinstance(page.get("variables"), list) for page in pages):
        raise SystemExit("refusing: cannot read the focused plan registry; the SHA guard cannot be decided")
    found = [item for page in pages for item in page["variables"]]
    if any(page["total_count"] != len(found) for page in pages) or any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in found):
        raise SystemExit("refusing: incomplete focused plan registry listing; the SHA guard cannot be decided")
    values = {name: "" for name in slots.NAMES} | {
        item["name"]: item.get("value") for item in found if item["name"] in slots.NAMES}
    try:
        text = slots.registry_text(values)
    except slots.a.AdmissionError as err:
        raise SystemExit(f"refusing: focused plan registry is undecodable ({err}); the SHA guard cannot be decided") from err
    return {key for line in text.split("\n") for key in json.loads(line)}


def tip_branches(sha):
    found = ghj("api", f"repos/{R}/commits/{sha}/branches-where-head")
    if not isinstance(found, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"] for item in found):
        raise SystemExit("refusing: cannot read the branches at this SHA; the SHA guard cannot be decided")
    return [item["name"] for item in found]


def attachment_facts(sha):
    """The three remote reads of the SHA guard; tests replace this boundary."""
    return open_pr_heads(), plan_registry_keys(), tip_branches(sha)


def attachment_guard(branch, sha):
    reasons = c.sha_refusal(sha, branch, *attachment_facts(sha))
    if reasons:
        warn(c.sha_refusal_text(sha, branch, reasons))
        raise SystemExit(2)


def git(*args, cwd):
    """Ordinary git in the core clone or the temporary worktree; hooks run, nothing is bypassed."""
    env = child_environment(drop=("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"))
    done = EDGE.commands.run(["git", *args], cwd=str(cwd), timeout=1800, env=env)
    if done.rc in (124, 127):
        raise SystemExit(f"refusing: git {args[0]} did not complete: {done.err.strip()}")
    if done.rc:
        raise SystemExit(f"refusing: git {args[0]} failed: {(done.err or done.out).strip()[-400:]}")
    return done.out.strip()


def make_throwaway(branch, sha):
    """Empty commit on `sha` in a temporary worktree, pushed to validation/<name>-<sha8>. -> (branch, new sha)."""
    target = c.throwaway_branch(branch, sha)
    if ghj("api", f"repos/{R}/git/ref/heads/{target}", ok=True) is not None:
        raise SystemExit(f"refusing: {target} already exists; dispatch it by name or delete it first")
    origin = git("remote", "get-url", "origin", cwd=CORE_CLONE).removesuffix(".git")
    if not origin.endswith((f"/{R}", f":{R}")):
        raise SystemExit(f"refusing: {CORE_CLONE} is not a clone of {R} (setting core_repo_dir)")
    SCRATCH.mkdir(parents=True, exist_ok=True)
    work = SCRATCH / f"throwaway-{os.getpid()}-{int(EDGE.clock.now())}"
    git("fetch", "--no-tags", "origin", f"refs/heads/{branch}", cwd=CORE_CLONE)
    git("worktree", "add", "--detach", str(work), sha, cwd=CORE_CLONE)
    try:
        git("commit", "--allow-empty", "-m", THROWAWAY_SUBJECT, cwd=work)
        new = git("rev-parse", "HEAD", cwd=work)
        if (not c.SHA.fullmatch(new) or new == sha or git("rev-parse", "HEAD^", cwd=work) != sha
                or git("rev-parse", "HEAD^{tree}", cwd=work) != git("rev-parse", f"{sha}^{{tree}}", cwd=work)):
            raise SystemExit("refusing: the validation commit is not one empty commit on the branch tip")
        git("push", "origin", f"HEAD:refs/heads/{target}", cwd=work)
    finally:
        EDGE.commands.run(["git", "worktree", "remove", "--force", str(work)], cwd=str(CORE_CLONE), timeout=600)
    if remote_tip(target) != new:
        raise SystemExit(f"refusing: {target} does not point at the validation commit {new[:8]} after the push")
    out(f"validation branch {target} @ {new[:8]} (empty commit on {branch} @ {sha[:8]})")
    return target, new


def delete_throwaway(target, sha):
    """Delete only the validation branch this command made, and only while it still points at its commit."""
    if not target.startswith("validation/") or remote_tip(target) != sha:
        warn(f"not deleting {target}: it no longer points at {sha[:8]}")
        return False
    gh("api", "--method", "DELETE", f"repos/{R}/git/refs/heads/{target}")
    out(f"deleted {target}")
    return True


def cmd_dispatch(a):
    if not c.REF.fullmatch(a.branch) or ".." in a.branch:
        raise SystemExit(f"{a.branch!r} is not a branch name")
    if why := c.throwaway_refusal(a.throwaway, a.cleanup, a.wait, a.reviewed_head):
        raise SystemExit(why)
    if why := c.branch_refusal(a.branch, a.force):
        raise SystemExit(why)
    kind, selector, err = c.selection(a.suite, a.test or [], a.kind)
    reviewed_head = a.reviewed_head
    err = err or c.repeat_refusal(a.repeat, a.suite, selector, reviewed_head=reviewed_head, branch=a.branch, kind=kind)
    if err:
        raise SystemExit(err)
    for ref, why in (("main", "GitHub dispatches only workflows the default branch has: wait for core #600 to merge"),
                     (a.branch, f"merge origin/main into {a.branch} (the run uses the branch's copy of the workflow)")):
        branch_ref = quote(f"refs/heads/{ref}", safe="")
        sha = gh("api", f"repos/{R}/contents/.github/workflows/{c.WORKFLOW}?ref={branch_ref}", "-q", ".sha", ok=True)
        if not re.fullmatch(r"[0-9a-f]{40}", sha.strip()):
            raise SystemExit(f"{c.WORKFLOW} is not on {ref}: {why}; `sm ci one rerun` works now")
    source, tip = a.branch, remote_tip(a.branch)
    if a.throwaway:
        a.branch, tip = make_throwaway(source, tip)
    try:
        attachment_guard(a.branch, tip)
    except SystemExit:
        if a.throwaway:
            warn(f"{a.branch} stays on the remote")
        raise
    label = f"dispatch {kind} {selector[:60]} x{a.repeat}{' stress' if a.stress else ''} on {a.os} @ {a.branch}"
    arm = a.os == "macos-latest"

    def start(pending):
        receipt = {"kind": "dispatch", "branch": a.branch, "workflow": c.WORKFLOW, "at": EDGE.clock.now()}
        if reviewed_head is not None:
            exact_capability(a.branch, reviewed_head)
            receipt.update(head_sha=reviewed_head, attempt=1, selection_kind=kind, selector=selector,
                           repeat=a.repeat, os=a.os, stress=a.stress)
        pending.write_text(json.dumps(receipt) + "\n")
        inputs = {"ref": a.branch, "os": a.os, "kind": kind, "selector": selector,
                  "repeat": str(a.repeat), "stress": str(a.stress).lower()}
        fields = [arg for key, value in inputs.items() for arg in ("-f", f"inputs[{key}]={value}")]
        try:
            reply = ghj("api", "--method", "POST", "-H", "X-GitHub-Api-Version: 2026-03-10",
                        f"repos/{R}/actions/workflows/{c.WORKFLOW}/dispatches", "-f", f"ref=refs/heads/{a.branch}", *fields)
        except SystemExit as err:
            if not refused_by_github(str(err)):
                raise  # timeout, 5xx, no reply: the start may have happened, so the marker stays
            pending.unlink()
            raise SystemExit(f"refusing: GitHub refused the dispatch, nothing started: {err}") from err
        rid = reply.get("workflow_run_id") if isinstance(reply, dict) else None
        if type(rid) is not int or rid < 1 or reply.get("html_url") != f"https://github.com/{R}/actions/runs/{rid}":
            raise SystemExit(f"refusing: no exact dispatch receipt; reconcile {pending} before retrying")
        if reviewed_head is not None:
            receipt["run"] = rid
            temporary = pending.with_suffix(".pending.tmp")
            temporary.write_text(json.dumps(receipt) + "\n")
            os.replace(temporary, pending)
            try:
                run = run_state(rid)
            except SystemExit as err:
                raise SystemExit(f"refusing: cannot verify run {rid}; reconcile {pending} before retrying ({err})") from err
            if not exact_run_matches(run, a.branch, reviewed_head):
                raise SystemExit(f"refusing: run {rid} does not match reviewed dispatch; reconcile {pending} before retrying")
        record(rid, "dispatch", label, a.branch, 1, 1, int(arm), head_sha=reviewed_head)
        pending.unlink()
        return rid

    try:
        rid = admit(start, arm, getattr(a, "wait_for_slot", a.wait))
    except SystemExit:
        if a.throwaway:
            warn(f"{a.branch} stays on the remote; dispatch it by name once the refusal is resolved")
        raise
    note(f"{label} started: run {rid}")
    out(f"{label}: run {rid}")
    if not a.wait:
        out(f"wait with: sm ci one watch {rid}")
        return 0
    code = wait(rid, None, a.json, label)
    if a.cleanup:
        try:
            delete_throwaway(a.branch, tip)
        except SystemExit as err:  # a failed cleanup never replaces the result of the run
            warn(f"not deleted {a.branch}: {err}")
    return code


def cmd_status(a):
    with admission_lock():
        rs = [r for r in rows() if not a.branch or r.get("branch") == a.branch]
        pending = STATE.with_suffix(".pending")
        if pending.exists():
            warn(f"admission paused: reconcile the uncertain focused start in {pending}")
    if a.json:
        out(json.dumps(rs, indent=1))
    else:
        out("\n".join(c.status_line(r) for r in rs) or f"no focused run in the last 24 h (max {c.MAX_FOCUSED} in flight)")
    return 0


def cmd_watch(a):
    with admission_lock():
        attempt = max((row.get("attempt", 1) for row in read_rows() if row["run"] == a.run), default=1)
    return wait(a.run, None, a.json, f"watch {a.run}", attempt)
