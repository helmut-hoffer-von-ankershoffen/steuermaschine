"""Timings of the delivery path, measured from records and never estimated.

  gate_stats       the last hour of the local run lock's history (waits and command time), without a GitHub call
  hosted           1-8 exact Actions runs: their observed attempt and job ids. Dispatch wait uses run creation for first
                   manual attempts. Wall time uses job start and completion.
  stages           bead creation (a proxy for the ask), core PR creation and merge, and GitHub publication.
                   Publication requires an exact release tag containing the merge. Live status needs separate proof.

Moved from the former wait tool with its checks unchanged. What changed is how it reaches the world: GitHub through the
one `Gh` client and the tracker through the `Commands` provider, the time through the `Clock`, the history file, the
repository and the tracker directory through settings (`bind`), output through `out`/`warn` (the edge collects it) and
the result as data in `EDGE.result`. Not moved: the diagnosis of the biggest wait (it is `go` now) and the block
appended to the velocity log (a write into a tracked document).

Read-only on GitHub, never starts a run.

Duplicates until their slices land: `VERSION` (the release snapshot's), the bead id shape (the digest's).
`ACTIVE` is the core forge's list of unfinished run statuses (not copied); `WAITING` is that list without in_progress.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from steuermaschine.defaults import SystemClock
from steuermaschine.github import Gh
from steuermaschine.github.forge import ACTIVE

WAITING = frozenset(ACTIVE) - {"in_progress"}

VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
BEAD_SUFFIX = re.compile(r"[a-z0-9]{2,6}")


class Edge:
    """How the functions below reach the world. `bind` sets it for one command; a test sets what it needs."""

    def __init__(self, commands: Any = None, clock: Any = None, out: Any = None, err: Any = None):
        self.commands, self.clock = commands, clock or SystemClock()
        self.out, self.err = out, err
        self.result: dict[str, Any] | None = None  # what the last report found, for the result model


EDGE = Edge()
REPO = ""  # OWNER/NAME of the core repository (setting `repo`)
BEAD_PREFIX = ""  # setting `tick_bead_prefix`
BEAD_ROOT = ""  # the tracker's directory (setting `tick_beads_dir`)
HIST = Path("gate-history.jsonl")  # the run lock's history (setting `lock_history`)


def bind(edge: Edge, repo: str, bead_prefix: str, bead_root: str, history: Path) -> None:
    global EDGE, REPO, BEAD_PREFIX, BEAD_ROOT, HIST
    EDGE, REPO, BEAD_PREFIX, BEAD_ROOT, HIST = edge, repo, bead_prefix, bead_root, history


def out(text: str = "") -> None:
    EDGE.out(text) if EDGE.out else print(text, flush=True)


def warn(text: str = "") -> None:
    if EDGE.err:
        EDGE.err(text)
    else:
        import sys

        print(text, file=sys.stderr, flush=True)


def bead_id(text: str) -> bool:
    """A tracker id with or without the project prefix, never error text."""
    return bool(BEAD_SUFFIX.fullmatch(text.removeprefix(BEAD_PREFIX)))


HEAD_SHA = re.compile(r"[0-9a-f]{40}")
CONCLUSIONS = ("action_required", "cancelled", "failure", "neutral", "skipped", "stale", "startup_failure",
               "success", "timed_out")


class HostedUnavailable(RuntimeError):
    """Hosted timing could not establish the run or job evidence."""


class StagesUnavailable(RuntimeError):
    """Stage timing could not establish the requested evidence."""


def sh(*a: str) -> str:
    r = EDGE.commands.run(list(a), timeout=90)
    return r.out if r.rc == 0 else ""


def gh(*a: str) -> Any:
    r = Gh(EDGE.commands, timeout=90, clock=EDGE.clock).run(list(a))
    text = r.out if r.rc == 0 else ""
    return json.loads(text) if text.strip() else []


def pctl(v: list[float], p: float) -> float | None:
    v = sorted(v)
    return v[min(len(v) - 1, max(0, int(round(p / 100 * len(v) + 0.4999)) - 1))] if v else None  # noqa: RUF046


def gate_stats(path: Path | str | None = None, since: float = 3600) -> dict[str, Any]:
    path = Path(path if path is not None else HIST)
    rows = []
    if path.exists():
        for l in path.read_text().splitlines():  # noqa: E741
            try:  # noqa: SIM105
                rows.append(json.loads(l))
            except ValueError:
                pass
    cut = EDGE.clock.now() - since
    rec = [r for r in rows if datetime.strptime(r["at"], "%Y-%m-%dT%H:%M:%S%z").timestamp() >= cut]
    w = [r.get("waited_s", 0) for r in rec]
    ran = [r.get("ran_s", 0) for r in rec]
    return {
        "runs": len(rec),
        "wait_med_s": statistics.median(w) if w else None,
        "wait_p90_s": pctl(w, 90),
        "wait_total_s": sum(w),
        "ran_med_s": statistics.median(ran) if ran else None,
        "ran_p90_s": pctl(ran, 90),
        "ran_total_s": sum(ran),
        "nonzero_exit": sum(1 for r in rec if str(r.get("exit")) != "0"),
    }


def elapsed_seconds(begin: Any, end: Any) -> float | None:
    if not isinstance(begin, str) or not isinstance(end, str):
        return None
    try:
        first, last = (datetime.fromisoformat(value.replace("Z", "+00:00")) for value in (begin, end))
    except ValueError:
        return None
    if first.utcoffset() is None or last.utcoffset() is None or last < first:
        return None
    return (last - first).total_seconds()


def valid_outcome(row: dict[str, Any]) -> bool:
    status, conclusion = row.get("status"), row.get("conclusion")
    return conclusion in CONCLUSIONS if status == "completed" else status in ACTIVE and conclusion is None


def optional_strings(row: dict[str, Any], *fields: str) -> bool:
    return all(row.get(field) is None or isinstance(row[field], str) for field in fields)


def valid_job_fields(job: dict[str, Any], attempt: int) -> bool:
    job_attempt, runner, steps = job.get("run_attempt", attempt), job.get("runner_id"), job.get("steps")
    return (type(job_attempt) is int and job_attempt == attempt
            and (runner is None or (type(runner) is int and runner >= 0))
            and optional_strings(job, "started_at", "completed_at")
            and (steps is None or (isinstance(steps, list) and all(
                isinstance(step, dict) and optional_strings(step, "started_at") for step in steps))))


def hosted_job(run: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    status, conclusion = job["status"], job.get("conclusion")
    started, completed = job.get("started_at"), job.get("completed_at")
    runner, steps = job.get("runner_id"), job.get("steps")
    dispatch, wall = None, None
    if status in WAITING:
        timing = "queued"
    elif conclusion == "skipped" or (status == "completed" and runner in (0, None) and steps == []):
        timing = "not_started"
    elif elapsed_seconds(started, started) is None:
        timing = "missing_or_invalid_start"
    elif not (type(runner) is int and runner > 0) and not any(
        isinstance(step, dict) and elapsed_seconds(step.get("started_at"), step.get("started_at")) is not None
        for step in (steps if isinstance(steps, list) else [])
    ):
        timing = "unverified_start"
    else:
        if run["event"] == "workflow_dispatch" and run["run_attempt"] == 1:
            dispatch = elapsed_seconds(run.get("created_at"), started)
        if status == "in_progress":
            timing = "running"
        else:
            wall = elapsed_seconds(started, completed)
            timing = "measured" if wall is not None else "missing_or_invalid_completion"
    return {"id": job["id"], "head_sha": job["head_sha"], "name": job["name"], "status": status,
            "conclusion": conclusion, "runner_id": runner, "started_at": started, "completed_at": completed,
            "timing": timing, "dispatch_to_start_s": dispatch, "job_wall_s": wall}


def hosted_read(*args: str) -> Any:
    try:
        response = gh("api", *args)
    except (OSError, ValueError):
        raise HostedUnavailable("GitHub API request failed") from None
    if not response:
        raise HostedUnavailable("GitHub API request failed")
    return response


def hosted_run(run_id: int) -> dict[str, Any]:
    endpoint = f"repos/{REPO}/actions/runs/{run_id}"
    run = hosted_read(endpoint)
    if (not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] != run_id
            or not isinstance(run.get("head_sha"), str) or not HEAD_SHA.fullmatch(run["head_sha"])
            or type(run.get("run_attempt")) is not int or run["run_attempt"] < 1
            or not valid_outcome(run) or not optional_strings(run, "created_at")
            or not all(isinstance(run.get(field), str) and run[field] for field in ("path", "event"))):
        raise HostedUnavailable("invalid run identity or metadata")
    attempt = run["run_attempt"]
    pages = hosted_read("--paginate", "--slurp", f"{endpoint}/attempts/{attempt}/jobs?per_page=100")
    if not isinstance(pages, list) or not pages or any(
        not isinstance(page, dict) or type(page.get("total_count")) is not int or not isinstance(page.get("jobs"), list)
        for page in pages
    ):
        raise HostedUnavailable("attempt job inventory unavailable")
    jobs = [job for page in pages for job in page["jobs"]]
    if (any(page["total_count"] != len(jobs) for page in pages) or any(
        not isinstance(job, dict) or type(job.get("id")) is not int or job["id"] < 1
        or type(job.get("run_id")) is not int or job["run_id"] != run_id
        or not isinstance(job.get("head_sha"), str) or not HEAD_SHA.fullmatch(job["head_sha"])
        or not isinstance(job.get("name"), str) or not job["name"]
        or not valid_outcome(job) or not valid_job_fields(job, attempt)
        for job in jobs
    ) or len({job["id"] for job in jobs}) != len(jobs)):
        raise HostedUnavailable("incomplete or mismatched attempt job inventory")
    return {"id": run_id, "head_sha": run["head_sha"], "attempt": attempt, "workflow": run["path"], "event": run["event"],
            "status": run["status"], "conclusion": run.get("conclusion"), "created_at": run.get("created_at"),
            "jobs": [hosted_job(run, job) for job in jobs]}


def hosted(run_ids: list[str], as_json: bool) -> int:
    runs = []
    for run_id in run_ids:
        try:
            runs.append(hosted_run(int(run_id)))
        except HostedUnavailable as error:
            warn(f"timing hosted: run {run_id}: {error}")
            return 1
    EDGE.result = {"repository": REPO, "runs": runs}
    if as_json:
        out(json.dumps({"repository": REPO, "runs": runs}, indent=1))
        return 0
    for run in runs:
        out(f"Run {run['id']} attempt {run['attempt']} {run['head_sha']} {ascii(run['workflow'])} ({run['status']})")  # noqa: RUF010
        if not run["jobs"]:
            out("  No job records")
        for job in run["jobs"]:
            dispatch, wall = ("n/a" if job[k] is None else f"{job[k]:g}s" for k in ("dispatch_to_start_s", "job_wall_s"))
            out(f"  Job {job['id']} {job['head_sha']} {ascii(job['name'])} ({job['conclusion'] or job['status']}, {job['timing']})"  # noqa: RUF010
                  f" | dispatch to start {dispatch} | job wall {wall}")
    return 0


def stage_bead(bead: str) -> Any:
    try:
        rows = json.loads(sh("bd", "--directory", BEAD_ROOT, "--readonly", "--sandbox", "show", bead, "--json"))
    except (OSError, ValueError):
        rows = None
    if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
            or rows[0].get("id") != bead):
        raise StagesUnavailable("exact bead record unavailable")
    return rows[0].get("created_at")


def stage_api(endpoint: str, unavailable: str) -> dict[str, Any]:
    try:
        response = gh("api", "--hostname", "github.com", f"repos/{REPO}/{endpoint}")
    except (OSError, ValueError):
        response = None
    if not isinstance(response, dict):
        raise StagesUnavailable(unavailable)
    return response


def stage_publication(tag: str, merge_sha: Any) -> tuple[Any, str]:
    if not isinstance(merge_sha, str) or not HEAD_SHA.fullmatch(merge_sha):
        raise StagesUnavailable("exact PR merge commit unavailable")
    release = stage_api(f"releases/tags/{tag}", "GitHub release record unavailable")
    if release.get("tag_name") != tag or release.get("draft") is not False or release.get("prerelease") is not False:
        raise StagesUnavailable("exact final GitHub release unverified")
    ref = stage_api(f"git/ref/tags/{tag}", "exact GitHub tag ref unavailable")
    source = ref.get("object") if ref.get("ref") == f"refs/tags/{tag}" else None
    if (isinstance(source, dict) and source.get("type") == "tag"
            and isinstance(source.get("sha"), str) and HEAD_SHA.fullmatch(source["sha"])):
        annotated = stage_api(f"git/tags/{source['sha']}", "annotated GitHub tag unavailable")
        source = annotated.get("object") if annotated.get("sha") == source["sha"] else None
    if (not isinstance(source, dict) or source.get("type") != "commit"
            or not isinstance(source.get("sha"), str) or not HEAD_SHA.fullmatch(source["sha"])):
        raise StagesUnavailable("exact GitHub tag commit unavailable")
    tag_sha = source["sha"]
    comparison = stage_api(f"compare/{merge_sha}...{tag_sha}?per_page=1", "GitHub ancestry comparison unavailable")
    base = comparison.get("merge_base_commit")
    if (comparison.get("status") != ("identical" if merge_sha == tag_sha else "ahead")
            or not isinstance(base, dict) or base.get("sha") != merge_sha):
        raise StagesUnavailable("GitHub comparison does not prove the release tag contains the merge")
    return release.get("published_at"), tag_sha


def stage_interval(start: Any, end: Any, pending_reason: str | None = None) -> dict[str, Any]:
    seconds = elapsed_seconds(start, end)
    if seconds is None:
        reason = ("end timestamp precedes start timestamp"
                  if elapsed_seconds(start, start) is not None and elapsed_seconds(end, end) is not None
                  else "missing or invalid timestamp")
        return {"state": "unavailable", "seconds": None, "reason": reason}
    row = {"state": "measured", "seconds": seconds, "start_at": start, "end_at": end}
    if pending_reason:
        row.update(state="pending", reason=pending_reason)
    return row


def stage_snapshot(bead: str, number: int, tag: str | None) -> dict[str, Any]:
    created = stage_bead(bead)
    pr = stage_api(f"pulls/{number}", "Core PR record unavailable")
    base = pr.get("base")
    repo = base.get("repo") if isinstance(base, dict) else None
    if (type(pr.get("number")) is not int or pr["number"] != number or not isinstance(repo, dict)
            or repo.get("full_name") != REPO or pr.get("state") not in ("open", "closed")
            or type(pr.get("merged")) is not bool or (pr["merged"] and pr["state"] != "closed")):
        raise StagesUnavailable("invalid or mismatched Core PR record")
    now = datetime.fromtimestamp(EDGE.clock.now(), timezone.utc).isoformat()  # noqa: UP017
    opened, merged = pr.get("created_at"), pr.get("merged_at")
    intervals = {"bead_to_pr": stage_interval(created, opened)}
    no_merge = {"state": "not_applicable", "seconds": None, "reason": "PR has no merge"}
    publication = no_merge
    tag_sha = None
    if pr["merged"]:
        intervals["pr_to_merge"] = stage_interval(opened, merged)
        try:
            if not tag:
                raise StagesUnavailable("no exact release tag supplied")
            published, tag_sha = stage_publication(tag, pr.get("merge_commit_sha"))
            publication = stage_interval(merged, published)
        except StagesUnavailable as error:
            publication = stage_interval(merged, now, str(error))
    elif pr["state"] == "open":
        intervals["pr_open_age"] = stage_interval(opened, now, "PR is open")
    else:
        intervals["pr_to_merge"] = {**no_merge, "reason": "PR closed without a merge"}
    intervals["merge_to_publication"] = publication
    return {"repository": REPO, "bead": bead, "pr": number, "release_tag": tag, "tag_commit": tag_sha,
            "observed_at": now, "intervals": intervals, "bead_time_basis": "creation proxy for owner ask"}


def stages(bead: str, number: int, tag: str | None, as_json: bool) -> int:
    event = {"bead": bead, "pr": number, "tag": tag}
    warn(json.dumps({"event": "wait.stages.start", **event}))
    try:
        snapshot = stage_snapshot(bead, number, tag)
    except StagesUnavailable as error:
        EDGE.result = None
        warn(json.dumps({"event": "wait.stages.outcome", **event, "outcome": "unavailable", "reason": str(error)}))
        return 1
    intervals = snapshot["intervals"]
    incomplete = any(row["state"] == "unavailable" for row in intervals.values())
    EDGE.result = snapshot
    warn(json.dumps({"event": "wait.stages.outcome", **event, "outcome": "partial" if incomplete else "ok",
                     "intervals": {key: row["state"] for key, row in intervals.items()}}))
    if as_json:
        out(json.dumps(snapshot, indent=1))
    else:
        out(f"Bead {bead} and Core PR #{number}" + (f" and release {tag}" if tag else ""))
        labels = {"bead_to_pr": "Bead creation to PR (ask time proxy)", "pr_to_merge": "PR to merge",
                  "pr_open_age": "Open PR age", "merge_to_publication": "Merge to GitHub publication"}
        for key, row in intervals.items():
            duration = f"{row['seconds']:g}s" if row["seconds"] is not None else "unavailable"
            if row["state"] == "pending":
                duration += " elapsed (pending verification)" if key == "merge_to_publication" else " elapsed"
            if row.get("reason"):
                duration += f" ({row['reason']})"
            out(f"  {labels[key]}  {duration}")
    return int(incomplete)

