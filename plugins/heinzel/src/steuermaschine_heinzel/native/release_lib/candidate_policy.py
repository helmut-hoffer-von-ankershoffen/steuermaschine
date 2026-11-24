"""Require the independently reviewed full Core candidate before the existing exact-head merge."""

import re

from ..queue_lib import core_policy, hzlib, job_proof, release_candidate
from . import edge  # moved: records, processes and what used to be separate scripts

# Source qualification: the workflow and the job inventory are Core's reviewed release policy at the exact release head
# (queue_lib/core_policy), never a copy in this plugin.

# Tag-on-green-main (core ADR 0144): off until the push-event candidate route is built and independently reviewed;
# while it is None, verify_commit refuses.
TAG_ON_MAIN_SOURCES: dict | None = None

# Owner waiver (owner 2026-11-07, v0.9.29): one candidate job that failed on attempt 1 by an in-date KNOWN flake and
# passed when only the failed jobs were rerun (attempt 2). Everything else stays first-attempt evidence.
HARD = ("failure", "timed_out", "startup_failure")
BEAD = re.compile(r"[a-z0-9]{3}")


# Core's merge-gate workflow (workflow_run after CI) POSTs check runs on the PR head through the github-actions app: the
# required contexts of Core's release policy other than the CI aggregate. GitHub may attach them to the CI run's own
# check suite; the Actions jobs API then lists them as jobs of the candidate run (v0.9.32, run 42480597872). They are
# API check runs, never CI workload: drop only those, by exact name and shape.
def api_check_runs(policy: core_policy.Policy) -> frozenset[str]:
    """The API-posted check runs of `policy`: its required contexts except the CI aggregate job."""
    return policy.required_contexts - {policy.aggregate}


def _is_api_check_run(job: object, run_id: int, names: frozenset[str]) -> bool:
    """True only for a check run POSTed through the API: exact name, no runner, no steps, no labels, no job URL."""
    if not isinstance(job, dict) or type(job.get("id")) is not int:
        return False
    url = job.get("html_url")
    return (job.get("name") in names
            and job.get("runner_id") is None and job.get("runner_name") is None
            and job.get("steps") == [] and job.get("labels") == []
            and job.get("run_id") == run_id
            and url == f"https://github.com/{release_candidate.REPOSITORY}/runs/{job['id']}"
            and f"/actions/runs/{run_id}/job/" not in str(job.get("check_run_url", "")) + str(url))


def without_api_check_runs(get, names: frozenset[str]):
    """A reader whose first-attempt job inventory omits only merge-gate/coverage-floor API check runs.

    The raw pages go through the reviewed reader's job_inventory first (totals, pagination, unique IDs), so the
    filtered view is one complete page. Every other job, missing name, extra name or short matrix still refuses."""
    pattern = re.compile(r"actions/runs/([1-9][0-9]*)/attempts/1/jobs\?per_page=100&page=([1-9][0-9]*)")

    def reader(path: str):
        found = pattern.fullmatch(path)
        if not found:
            return get(path)
        if found.group(2) != "1":
            raise release_candidate.CandidateError("candidate job pagination after the filtered inventory")
        run_id = int(found.group(1))
        jobs = [job_proof.log_proven(job, run_id, job_log) for job in release_candidate.job_inventory(get, run_id) if not _is_api_check_run(job, run_id, names)]
        if not jobs:
            raise release_candidate.CandidateError("candidate job inventory is only API check runs")
        return {"total_count": len(jobs), "jobs": jobs}

    return reader


def _api(path: str):
    value = hzlib.ghj("api", f"repos/{hzlib.CORE}/{path}", default=None)
    if value is None:
        raise release_candidate.CandidateError("candidate API evidence unavailable")
    return value


def job_log(job_id: int) -> bytes:
    """One candidate job's log, read only when GitHub returned a success job with a runner but empty steps."""
    done = edge.run(["gh", "api", f"repos/{hzlib.CORE}/actions/jobs/{job_id}/logs"], capture_output=True, check=False, timeout=60)
    if done.returncode:
        raise release_candidate.CandidateError("candidate job log unavailable")
    return done.stdout


def verify_pr(number: int, waiver: dict | None = None) -> dict:
    """Read genuine first-attempt source/job evidence; this neither dispatches CI nor changes a check.

    With an owner `waiver` ({"job", "bead", "owner_quote"}), the single waived job (and the aggregate it failed)
    may have failed on attempt 1 when the run is exactly at attempt 2 and that job passed there; see waived_view."""
    get = _api

    def download(artifact_id: int) -> bytes:
        done = edge.run(["gh", "api", f"repos/{hzlib.CORE}/actions/artifacts/{artifact_id}/zip"],
                              capture_output=True, check=False, timeout=30)
        if done.returncode:
            raise release_candidate.CandidateError("candidate source artifact unavailable")
        return done.stdout

    pr = get(f"pulls/{number}")
    head = pr.get("head", {}).get("sha", "")
    if not isinstance(head, str) or not release_candidate.SHA.fullmatch(head):
        raise release_candidate.CandidateError("release PR has no exact source head")
    commit = get(f"commits/{head}")
    tree = commit.get("commit", {}).get("tree", {}).get("sha", "")
    if not isinstance(tree, str) or not release_candidate.SHA.fullmatch(tree):
        raise release_candidate.CandidateError("release PR has no exact source tree")
    policy = core_policy.at_commit_api(_api, head)  # the reviewed facts of the exact release head, read first
    record = None
    if waiver is not None:
        get, record = waived_view(get, head, waiver, hzlib.classify_attempt, aggregate=policy.aggregate,
                                  runs=policy.workflow_runs)
    get = without_api_check_runs(get, api_check_runs(policy))
    run_id = release_candidate.discover(get, head, policy)
    expected = release_candidate.ExpectedCandidate(run_id, number, head, tree)
    evidence = release_candidate.verify(get, download, expected, policy)
    if record is not None:
        evidence = evidence | {"owner_waiver": record}
    return evidence


def require_base_sources(base: str, head: str) -> None:
    """The candidate cannot redefine the workload or the workflow and recorder used to prove it."""
    policy = core_policy.at_commit_api(_api, base)
    pinned = (core_policy.PATH, *policy.pinned)
    for path in pinned:
        before = release_candidate.source_bytes(_api, path, base, pinned)
        after = release_candidate.source_bytes(_api, path, head, pinned)
        release_candidate.require(before == after, f"candidate changed its evidence source: {path}")


def verify_commit(sha: str, waiver: dict | None = None) -> dict:
    """Tag-on-green-main: the first-attempt full main push run of the release commit `sha` is the candidate. Same
    reader, same first-attempt and waiver rules as verify_pr; no PR, the commit must be on main."""
    if TAG_ON_MAIN_SOURCES is None or not hasattr(release_candidate, "pushed_release_version"):
        raise release_candidate.CandidateError("no independently reviewed tag-on-green-main CI sources are pinned")
    get = _api

    def download(artifact_id: int) -> bytes:
        done = edge.run(["gh", "api", f"repos/{hzlib.CORE}/actions/artifacts/{artifact_id}/zip"],
                              capture_output=True, check=False, timeout=30)
        if done.returncode:
            raise release_candidate.CandidateError("candidate source artifact unavailable")
        return done.stdout

    if not isinstance(sha, str) or not release_candidate.SHA.fullmatch(sha):
        raise release_candidate.CandidateError("release commit has no exact identity")
    commit = get(f"commits/{sha}")
    tree = commit.get("commit", {}).get("tree", {}).get("sha", "")
    if not isinstance(tree, str) or not release_candidate.SHA.fullmatch(tree):
        raise release_candidate.CandidateError("release commit has no exact source tree")
    policy = core_policy.at_commit_api(_api, sha)
    record = None
    if waiver is not None:
        get, record = waived_view(get, sha, waiver, hzlib.classify_attempt, event="push", aggregate=policy.aggregate,
                                  runs=policy.workflow_runs)
    get = without_api_check_runs(get, api_check_runs(policy))
    run_id = release_candidate.discover(get, sha, policy)
    expected = release_candidate.ExpectedCandidate(run_id, None, sha, tree)
    evidence = release_candidate.verify(get, download, expected, policy)
    if record is not None:
        evidence = evidence | {"owner_waiver": record}
    return evidence


def _require(condition: object, message: str) -> None:
    if not condition:
        raise release_candidate.CandidateError(f"owner waiver refused: {message}")


def _jobs(get, run_id: int, attempt: int) -> list[dict]:
    data = get(f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page=1")
    _require(isinstance(data, dict) and isinstance(data.get("jobs"), list) and data.get("total_count") == len(data["jobs"])
             and all(isinstance(j, dict) and isinstance(j.get("name"), str) for j in data["jobs"]),
             f"attempt {attempt} job list incomplete")
    names = [j["name"] for j in data["jobs"]]
    _require(len(names) == len(set(names)), f"attempt {attempt} has duplicate job names")
    return data["jobs"]


def waived_view(get, head: str, waiver: dict, classify, event: str | None = None, *, aggregate: str, runs: str) -> tuple:
    """Check the waiver against live evidence, then return a reader that shows attempt 1 with only the waived jobs
    replaced by their passing attempt-2 outcome, plus the record for the receipt. Refuses anything else."""
    job, bead, quote = (waiver.get(k) for k in ("job", "bead", "owner_quote"))
    _require(isinstance(job, str) and job and job != aggregate, "a single named job is required")
    _require(isinstance(bead, str) and BEAD.fullmatch(bead), "bead id must be 3 lowercase characters")
    _require(isinstance(quote, str) and len(quote.strip()) >= 10, "the owner's own words are required")
    only = f"&event={event}" if event else ""
    listing = get(f"{runs}?head_sha={head}{only}&per_page=100&page=1")
    rows = listing.get("workflow_runs") if isinstance(listing, dict) else None
    _require(isinstance(rows, list) and listing.get("total_count") == 1 and len(rows) == 1, "exactly one CI run on the head")
    current = rows[0]
    run_id = current.get("id")
    _require(type(run_id) is int and run_id > 0 and current.get("head_sha") == head, "run identity")
    _require(current.get("run_attempt") == 2, "the run must be at exactly attempt 2 (one rerun of failed jobs)")
    _require(current.get("status") == "completed" and current.get("conclusion") == "success", "attempt 2 is not green")
    first = get(f"actions/runs/{run_id}/attempts/1")
    _require(isinstance(first, dict) and first.get("id") == run_id and first.get("run_attempt") == 1
             and first.get("head_sha") == head and first.get("status") == "completed", "attempt 1 identity")
    one, two = _jobs(get, run_id, 1), _jobs(get, run_id, 2)
    failed = {j["name"] for j in one if j.get("conclusion") in HARD or j.get("conclusion") == "cancelled"}
    _require(failed - {aggregate} == {job}, f"attempt 1 failed jobs {sorted(failed)} are not exactly the waived job")
    by_name = {j["name"]: j for j in two}
    for name in failed:
        rerun = by_name.get(name)
        _require(isinstance(rerun, dict) and rerun.get("run_attempt") == 2 and rerun.get("head_sha") == head
                 and rerun.get("status") == "completed" and rerun.get("conclusion") == "success",
                 f"{name} did not pass on attempt 2")
    rerun = by_name[job]
    _require(type(rerun.get("runner_id")) is int and rerun["runner_id"] > 0 and rerun.get("steps"),
             "the waived job's rerun never ran on a runner")
    verdict, tags = classify(run_id, 1)
    _require(verdict == "known" and f"KNOWN({bead})" in tags,
             f"attempt 1 is not an in-date KNOWN failure of bead {bead} ({verdict}: {tags})")
    run_view = first | {"conclusion": "success"}
    patched = {name: by_name[name]["conclusion"] for name in failed}

    def reader(path: str):
        if path.startswith(f"{runs}?"):
            return {"total_count": 1, "workflow_runs": [run_view]}
        if path == f"actions/runs/{run_id}":
            return run_view
        if path.startswith(f"actions/runs/{run_id}/attempts/1/jobs?"):
            data = get(path)
            return data | {"jobs": [j | {"conclusion": patched[j["name"]]} if j.get("name") in patched else j
                                     for j in data.get("jobs", [])]}
        return get(path)

    record = {"job": job, "bead": bead, "owner_quote": quote.strip(), "run_id": run_id, "run_attempt": 2,
              "attempt1_failed": sorted(failed), "rerun_job_id": rerun.get("id"), "known": tags}
    return reader, record
