"""Bounded throughput samples, never a repository crawl.

At most three CI samples and one release sample; at most 15 GitHub calls, no pagination.
Completed run/attempt responses are cached. PR metadata and bounded CI counts are live snapshots.
`record_eligible` records a caller-attested observation in the local measurement journal;
it neither decides merge eligibility nor changes the queue. Invoke only after the caller's gates pass.
The acceptance metric is successful workflow completion, not proof of an accepted artifact verdict.

Moved from the former throughput tool: everything below the imports is the original code (check:
plugins/heinzel/tests/coord_verbatim.py), except that `Api` has no default runner: the caller passes one that goes
through the one process runner. The cache directory layer opens files relative to directory descriptors and never
follows a symbolic link; the journal append holds an exclusive lock on the journal file itself.
"""
# mypy: disable-error-code="no-untyped-def,no-untyped-call,type-arg,attr-defined,misc,union-attr,operator,index,arg-type,var-annotated"

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import statistics
from urllib.parse import urlencode
from ..queue_lib import core_facts

CORE = "helmut-hoffer-von-ankershoffen/heinzel"
MAX_CALLS = 15
MAX_SAMPLES = 3
ADVISORY = {"review", "attempt", "Claude review"}
LIMITS = [
    "Small, explicitly selected samples; no population throughput or 10x claim.",
    "Job-created to job-start includes scheduler overhead; overlapping job sums are not wall time.",
    "Check-ready and caller-attested eligibility observations are not historical proof of queue eligibility.",
    "Acceptance workflow success is not an independently verified accepted-artifact verdict.",
]


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def seconds(start, end):
    if not start or not end:
        return None
    delta = (datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
    return round(delta, 3) if delta >= 0 else None


def selection(value):
    if not re.fullmatch(r"[1-9]\d*:[1-9]\d*:[1-9]\d*", value):
        raise argparse.ArgumentTypeError("use PR:RUN:ATTEMPT, all positive integers")
    return tuple(map(int, value.split(":")))


class Unavailable(Exception):
    pass


def valid_repo(repo):
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)) and all(p not in (".", "..") for p in repo.split("/"))


@contextmanager
def directory(root, parts=()):
    """All cache descendants are opened relative to pinned, non-symlink directory descriptors."""
    root.mkdir(parents=True, exist_ok=True)
    descriptors = [os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
    try:
        for part in parts:
            if part in ("", ".", "..") or "/" in part:
                raise ValueError("invalid cache component")
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptors[-1])
            except FileExistsError:
                pass
            descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptors[-1]))
        yield descriptors[-1]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read_json_at(descriptor, name):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
    with os.fdopen(handle) as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024 * 1024:
            raise ValueError("invalid cache file")
        return json.load(file)


def write_json_at(descriptor, name, data):
    temporary = ".velocity-" + secrets.token_hex(16)
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
    try:
        with os.fdopen(handle, "w") as file:
            json.dump(data, file)
        os.replace(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
    finally:
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass


def valid_run(data, run_id, attempt):
    return isinstance(data, dict) and data.get("id") == run_id and data.get("run_attempt") == attempt and data.get("status") == "completed"


def valid_jobs(data, run_id, attempt):
    return isinstance(data, dict) and isinstance(data.get("jobs"), list) and type(data.get("total_count")) is int and data["total_count"] >= len(data["jobs"]) and all(isinstance(j, dict) and type(j.get("id")) is int and j["id"] > 0 and j.get("run_id") == run_id and j.get("run_attempt") == attempt and j.get("status") == "completed" for j in data["jobs"])


class Api:
    def __init__(self, cache, runner):
        self.cache, self.runner, self.calls, self.cache_hits = cache, runner, 0, 0

    def read(self, path, query=None):
        if self.calls >= MAX_CALLS:
            raise Unavailable("15-call budget reached")
        self.calls += 1
        args = ["gh", "api", path]
        if query is not None:
            args += ["-f", "query=" + query]
        result = self.runner(args, capture_output=True, text=True, check=False)
        if result.returncode:
            raise Unavailable("GitHub request failed: " + path.split("?")[0])
        try:
            data = json.loads(result.stdout)
        except (ValueError, TypeError) as error:
            raise Unavailable("GitHub returned unreadable JSON") from error
        if isinstance(data, dict) and data.get("errors"):
            raise Unavailable("GitHub returned GraphQL errors")
        return data

    def attempt(self, repo, run_id, attempt, jobs=False):
        if not valid_repo(repo) or not isinstance(run_id, int) or run_id <= 0 or not isinstance(attempt, int) or attempt <= 0:
            raise ValueError("invalid run/cache identity")
        validator = valid_jobs if jobs else valid_run
        name = "jobs.json" if jobs else "run.json"
        with directory(self.cache, (*repo.split("/"), str(run_id), str(attempt))) as folder:
            try:
                cached = read_json_at(folder, name)
                if isinstance(cached, dict) and cached.get("format") == 2 and cached.get("run_id") == run_id and cached.get("attempt") == attempt and validator(cached.get("data"), run_id, attempt):
                    self.cache_hits += 1
                    return cached["data"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
        path = f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}"
        data = self.read(path + ("/jobs?per_page=100" if jobs else ""))
        # Jobs are cached only after this exact attempt has completed. Run metadata is independently validated.
        if not jobs:
            if data.get("id") != run_id or data.get("run_attempt") != attempt:
                raise Unavailable("run/attempt identity mismatch")
            completed = valid_run(data, run_id, attempt)
        else:
            run = self.attempt(repo, run_id, attempt)
            if not valid_jobs(data, run_id, attempt):
                raise Unavailable("jobs do not belong to the selected completed run/attempt")
            completed = valid_run(run, run_id, attempt)
        if completed:
            with directory(self.cache, (*repo.split("/"), str(run_id), str(attempt))) as folder:
                write_json_at(folder, name, {"format": 2, "run_id": run_id, "attempt": attempt, "data": data})
        return data


def pr_metadata(api, repo, numbers):
    owner, name = repo.split("/")
    fields = "number title state isDraft createdAt mergedAt headRefName headRefOid baseRepository{nameWithOwner databaseId} headRepository{nameWithOwner databaseId} mergeCommit{oid} commits(last:1){nodes{commit{statusCheckRollup{contexts(first:100){nodes{... on CheckRun{name status conclusion completedAt} ... on StatusContext{context state createdAt}} pageInfo{hasNextPage}}}}}}"
    items = " ".join(f"p{number}:pullRequest(number:{number}){{{fields}}}" for number in sorted(set(numbers)))
    query = f"query{{repository(owner:{json.dumps(owner)},name:{json.dumps(name)}){{{items}}}}}"
    found = api.read("graphql", query).get("data", {}).get("repository") or {}
    return {item["number"]: item for item in found.values() if item}


def job_metrics(payload):
    jobs = payload.get("jobs", [])
    complete = payload.get("total_count") == len(jobs)
    executed = [j for j in jobs if j.get("runner_name") and j.get("conclusion") != "skipped"]
    waits = [v for j in executed if (v := seconds(j.get("created_at"), j.get("started_at"))) is not None]
    runtimes = [v for j in executed if (v := seconds(j.get("started_at"), j.get("completed_at"))) is not None]
    return {
        "job_sample_complete": complete,
        "jobs_observed": len(jobs),
        "jobs_total": payload.get("total_count"),
        "executed_jobs_observed": len(executed),
        "macos_executed_jobs_observed": sum("macos" in j.get("name", "").lower() for j in executed),
        "never_started_jobs_observed": sum(j.get("conclusion") == "cancelled" and not j.get("runner_name") and not j.get("steps") for j in jobs),
        "job_queue_median_s": statistics.median(waits) if waits else None,
        "job_queue_tail_max_s": max(waits, default=None),
        "job_queue_sum_s": sum(waits),
        "job_runtime_sum_s": sum(runtimes),
    }


def check_ready(pr):
    contexts = ((pr.get("commits", {}).get("nodes") or [{}])[-1].get("commit", {}).get("statusCheckRollup") or {}).get("contexts") or {}
    if contexts.get("pageInfo", {}).get("hasNextPage"):
        return None
    times = []
    for check in contexts.get("nodes", []):
        if check.get("name") in ADVISORY:
            continue
        if "context" in check:
            if check.get("state") != "SUCCESS":
                return None
            times.append(check.get("createdAt"))
        elif check.get("conclusion") == "SKIPPED":
            continue
        elif check.get("status") == "COMPLETED" and check.get("conclusion") == "SUCCESS":
            times.append(check.get("completedAt"))
        else:
            return None
    return max(filter(None, times), default=None)


@contextmanager
def journal(cache):
    with directory(cache) as folder:
        handle = os.open("eligibility.jsonl", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=folder)
        with os.fdopen(handle, "a+") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise ValueError("invalid journal file")
            fcntl.flock(file, fcntl.LOCK_EX)
            file.seek(0)
            yield file


def record_eligible(cache, repo, number, head, at=None):
    """First caller-attested observation only; this function has no merge authority."""
    if not valid_repo(repo) or not isinstance(number, int) or number <= 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("invalid eligibility identity")
    with journal(cache) as file:
        for line in file:
            event = json.loads(line)
            if (event.get("repo"), event.get("pr"), event.get("head")) == (repo, number, head):
                return event
        event = {"repo": repo, "pr": number, "head": head, "eligible_observed_at": at or utc_now(), "source": "caller-attested; first observation, not exact eligibility time"}
        file.write(json.dumps(event) + "\n")
        return event


def eligible_observation(cache, repo, pr):
    try:
        with journal(cache) as file:
            events = [json.loads(line) for line in file]
    except FileNotFoundError:
        return None
    return next((e for e in events if (e.get("repo"), e.get("pr"), e.get("head")) == (repo, pr["number"], pr["headRefOid"])), None)


def count_ci(api, repo, pr, observed_at):
    until = pr.get("mergedAt") or observed_at
    params = urlencode({"branch": pr["headRefName"], "event": "pull_request", "per_page": 100, "created": f"{pr['createdAt']}..{until}"})
    data = api.read(f"repos/{repo}/actions/workflows/{core_facts.ci_file()}/runs?{params}")
    runs = data.get("workflow_runs", [])
    return {"pr_ci_runs_observed": len(runs), "pr_ci_attempts_observed": sum(r.get("run_attempt", 1) for r in runs), "pr_ci_run_count_complete": data.get("total_count") == len(runs), "count_binding": "branch and PR lifetime; branch reuse remains a limitation"}


def repository_matches(value, repo, database_id=None):
    if not isinstance(value, dict):
        return False
    if database_id is not None and value.get("id") == database_id:
        return True
    return value.get("full_name", "").lower() == repo.lower() or value.get("url", "").rstrip("/").lower() == "https://api.github.com/repos/" + repo.lower()


def ci_binding(run, pr, repo, observed_at):
    if run.get("event") != "pull_request" or run.get("head_branch") != pr["headRefName"] or run.get("path", "").split("@")[0] != core_facts.ci_path() or not repository_matches(run.get("repository"), repo):
        raise Unavailable("CI run does not match selected PR repository/branch/workflow")
    associations = run.get("pull_requests") or []
    base_id = (pr.get("baseRepository") or {}).get("databaseId")
    if any(p.get("number") == pr["number"] and repository_matches((p.get("base") or {}).get("repo"), repo, base_id) for p in associations):
        return True, "explicit PR number and base-repository association"
    if associations:
        raise Unavailable("CI run is associated with a different PR or base repository")
    # GitHub often removes PR associations from old runs. Preserve these only as a separately labelled proxy cohort.
    head_repo = (pr.get("headRepository") or {}).get("nameWithOwner")
    if not head_repo or not repository_matches(run.get("head_repository"), head_repo) or run.get("head_sha") != pr["headRefOid"] or seconds(pr.get("createdAt"), run.get("created_at")) is None or seconds(run.get("created_at"), pr.get("mergedAt") or observed_at) is None:
        raise Unavailable("PR association absent and exact repository/head/lifetime proxy unavailable")
    return False, "exact source head/repository/branch/lifetime proxy; explicit PR association unavailable"


def collect(api, repo, samples, release=None, cache=None, observed_at=None):
    if not valid_repo(repo):
        raise ValueError("invalid repository slug")
    if not 1 <= len(samples) <= MAX_SAMPLES:
        raise ValueError("choose one to three CI samples")
    identities = [(r, a) for _, r, a in samples] + ([(release[1], release[2])] if release else [])
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate run/attempt selection, including conflicting PR assignments")
    observed_at = observed_at or utc_now()
    cache = cache or api.cache
    numbers = [s[0] for s in samples] + ([release[0]] if release else [])
    prs = pr_metadata(api, repo, numbers)
    report = {"repo": repo, "observed_at": observed_at, "samples": [], "release": None, "missing": [], "limits": LIMITS}
    for number, run_id, attempt in samples:
        row = {"pr": number, "run_id": run_id, "attempt": attempt}
        report["samples"].append(row)
        try:
            pr = prs[number]
            run = api.attempt(repo, run_id, attempt)
            associated, binding = ci_binding(run, pr, repo, observed_at)
            row |= {"status": run.get("status"), "conclusion": run.get("conclusion"), "head_sha": run.get("head_sha"), "matches_final_pr_head": run.get("head_sha") == pr["headRefOid"], "pr_association_verified": associated, "binding": binding}
            if run.get("status") != "completed":
                row["pending"] = True
                continue
            row["run_lifetime_proxy_s"] = seconds(run.get("created_at"), run.get("updated_at"))
            row["attempt_elapsed_proxy_s"] = seconds(run.get("run_started_at"), run.get("updated_at"))
            row |= job_metrics(api.attempt(repo, run_id, attempt, jobs=True))
            row |= count_ci(api, repo, pr, observed_at)
            ready = check_ready(pr) if row["matches_final_pr_head"] else None
            row |= {"check_ready_proxy_at": ready, "check_ready_proxy_to_merge_s": seconds(ready, pr.get("mergedAt"))}
            event = eligible_observation(cache, repo, pr)
            row["eligible_observation"] = event
            row["eligible_observed_to_merge_s"] = seconds(event.get("eligible_observed_at"), pr.get("mergedAt")) if event else None
        except (KeyError, Unavailable, OSError, ValueError) as error:
            row["missing"] = str(error)
    if release:
        number, run_id, attempt = release
        row = report["release"] = {"pr": number, "run_id": run_id, "attempt": attempt}
        try:
            pr, run = prs[number], api.attempt(repo, run_id, attempt)
            version = re.fullmatch(r"chore\(release\): v(\d+\.\d+\.\d+)", pr["title"])
            if not version or not repository_matches(run.get("repository"), repo) or run.get("display_title") != "Acceptance " + version[1] or run.get("path", "").split("@")[0] != ".github/workflows/acceptance.yml" or run.get("event") != "workflow_dispatch" or run.get("head_sha") != (pr.get("mergeCommit") or {}).get("oid") or seconds(pr.get("mergedAt"), run.get("created_at")) is None:
                raise Unavailable("acceptance version/workflow/source identity does not match the release PR")
            row |= {"version": version[1], "status": run.get("status"), "conclusion": run.get("conclusion"), "merge_to_acceptance_workflow_success_s": seconds(pr.get("mergedAt"), run.get("updated_at")) if run.get("status") == "completed" and run.get("conclusion") == "success" else None, "accepted_artifact_verdict_verified": False}
        except (KeyError, Unavailable, OSError, ValueError) as error:
            row["missing"] = str(error)
    def cohort(associated):
        return [r["attempt_elapsed_proxy_s"] for r in report["samples"] if r.get("pr_association_verified") is associated and r.get("conclusion") == "success" and r.get("job_sample_complete") and r.get("attempt_elapsed_proxy_s") is not None]
    primary, proxy = cohort(True), cohort(False)
    report["summary"] = {"primary_completed_successful_ci_samples": len(primary), "primary_median_attempt_elapsed_proxy_s": statistics.median(primary) if primary else None, "association_unavailable_proxy_samples": len(proxy), "proxy_median_attempt_elapsed_proxy_s": statistics.median(proxy) if proxy else None, "tenfold_improvement_established": False}
    report |= {"api_calls": api.calls, "cache_hits": api.cache_hits}
    return report
