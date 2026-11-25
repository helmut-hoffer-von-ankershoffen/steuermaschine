# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Bind one full release-candidate CI run to its exact source tree and complete first-attempt job inventory."""

from __future__ import annotations

import base64
import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from .core_policy import Policy

REPOSITORY = "helmut-hoffer-von-ankershoffen/heinzel"
ARTIFACT = "release-candidate-source"
PROOF_FILE = "release-candidate.json"
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
SHA = re.compile(r"[0-9a-f]{40}")
MAX_JOBS = 1000
MAX_PROOF = 64 * 1024


class CandidateError(ValueError):
    """Candidate evidence is incomplete, changed or outside the reviewed release contract."""


def require(condition: object, message: str) -> None:
    """Refuse unknown evidence instead of inferring a successful test execution.

    Raises:
        CandidateError: The supplied evidence does not satisfy the contract.
    """
    if not condition:
        raise CandidateError(message)


@dataclass(frozen=True, slots=True)
class ExpectedCandidate:
    """The coordinator's exact run, open PR and reviewed source head to verify."""

    run_id: int
    pr_number: int
    head: str
    tree: str


def valid_sha(value: object) -> bool:
    """Recognize a complete immutable Git identity at an untyped API boundary."""
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def release_version(event: str, repository: str, ref: str, payload: dict[str, Any]) -> str | None:
    """Select full CI only for canonical release branches in the owner repository; this grants no merge authority."""
    if repository != REPOSITORY:
        return None
    if event == "pull_request":
        pr = payload.get("pull_request", {})
        if (
            pr.get("head", {}).get("repo", {}).get("full_name") != REPOSITORY
            or pr.get("base", {}).get("repo", {}).get("full_name") != REPOSITORY
            or pr.get("base", {}).get("ref") != "main"
        ):
            return None
        ref = pr.get("head", {}).get("ref", "")
    elif event != "workflow_dispatch":
        return None
    version = ref.removeprefix("release/v") if ref.startswith("release/v") else ""
    if not VERSION.fullmatch(version):
        return None
    if event == "pull_request" and payload["pull_request"].get("title") != f"chore(release): v{version}":
        return None
    return version


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Refuse a source artifact that hides repeated provenance fields behind duplicate JSON keys."""
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate candidate source JSON key")
        result[key] = value
    return result


def quarantine_lane(jobs: list[dict[str, Any]], run_id: object, head: str, policy: Policy) -> list[dict[str, Any]]:
    """Check the quarantine lane's jobs and return the others for the exact inventory.

    The lane (tests/quarantine.toml, scripts/quarantine.py) is absent from a workflow that predates it. Otherwise the
    required registry check passed, and the non-blocking plan and lane jobs ran on this run's first attempt: both OS
    jobs, whatever their tests did, or the one skipped placeholder when nothing is quarantined. A lane failure never
    refuses a candidate; a missing, extra or rerun lane job does.

    Raises:
        CandidateError: The lane's jobs are incomplete, duplicated, rerun or the registry check did not pass.
    """
    lane_jobs = policy.quarantine
    names = {lane_jobs.registry, lane_jobs.plan, lane_jobs.idle, *lane_jobs.runs}
    lane = [job for job in jobs if job.get("name") in names]
    if not lane:
        return jobs
    found = sorted(str(job.get("name")) for job in lane)
    require(
        found
        in (
            sorted({lane_jobs.registry, lane_jobs.plan, *lane_jobs.runs}),
            sorted({lane_jobs.registry, lane_jobs.plan, lane_jobs.idle}),
        ),
        "missing, extra or duplicated quarantine lane jobs",
    )
    for job in lane:
        # A lane job that hit its time limit ends cancelled; it still never blocks the candidate.
        allowed = {"success"} if job["name"] == lane_jobs.registry else {"success", "failure", "skipped", "cancelled"}
        if job["name"] == lane_jobs.idle:
            allowed = {"skipped"}
        require(
            job.get("run_id") == run_id
            and job.get("run_attempt") == 1
            and job.get("head_sha") == head
            and job.get("status") == "completed"
            and job.get("conclusion") in allowed,
            "quarantine lane job failed its registry check, changed or reran",
        )
    return [job for job in jobs if job.get("name") not in names]


def job_inventory(get: Callable[[str], Any], run_id: int) -> list[dict[str, Any]]:
    """Exhaust the immutable first-attempt job pages and refuse inconsistent totals or duplicate IDs.

    Raises:
        CandidateError: The complete bounded inventory cannot be collected.
    """
    rows: list[Any] = []
    total = None
    for page in range(1, 11):
        data = get(f"actions/runs/{run_id}/attempts/1/jobs?per_page=100&page={page}")
        require(
            isinstance(data, dict) and type(data.get("total_count")) is int and isinstance(data.get("jobs"), list),
            "malformed candidate job page",
        )
        if total is None:
            total = data["total_count"]
        require(data["total_count"] == total and 0 < total <= MAX_JOBS, "candidate job count changed or exceeds bound")
        rows.extend(data["jobs"])
        require(len(rows) <= total, "extra jobs beyond declared inventory")
        if len(rows) == total:
            require(all(isinstance(row, dict) and type(row.get("id")) is int for row in rows), "malformed job identity")
            require(len({row["id"] for row in rows}) == total, "duplicate candidate job IDs")
            return rows
        require(data["jobs"], "truncated candidate job inventory")
    message = "candidate job pagination exceeded bound"
    raise CandidateError(message)


def validate(evidence: dict[str, Any], expected: ExpectedCandidate, policy: Policy) -> dict[str, Any]:
    """Consume real completed first-attempt evidence only, without aliasing checks or replacing any main gate.

    Every fact of Core's CI (the workflow, the job inventory) comes from `policy`, the reviewed manifest read at the
    exact release head (core_policy), never from this plugin. The commit and its review verdict bind it, no checksum."""
    run, jobs, proof, pr, source, head = (evidence[key] for key in ("run", "jobs", "proof", "pr", "source", "head"))
    expected_head = expected.head
    expected_tree = expected.tree
    require(
        valid_sha(expected_head) and valid_sha(expected_tree),
        "invalid expected head or tree",
    )
    require(
        policy.commit == expected_head and policy.repository == REPOSITORY,
        "candidate policy is not the release head's",
    )
    require(
        type(run.get("id")) is int
        and run["id"] == expected.run_id
        and run["id"] > 0
        and type(run.get("run_attempt")) is int
        and run["run_attempt"] == 1,
        "candidate must be the original attempt",
    )
    require(
        run.get("repository", {}).get("full_name") == REPOSITORY
        and run.get("head_repository", {}).get("full_name") == REPOSITORY
        and run.get("path") == policy.workflow,
        "wrong candidate repository or workflow",
    )
    require(
        run.get("status") == "completed" and run.get("conclusion") == "success",
        "candidate is not completed successfully",
    )
    event = run.get("event")
    require(event in ("pull_request", "workflow_dispatch"), "candidate event is not a PR or explicit dispatch")
    require(run.get("head_sha") == expected_head, "candidate run is not the exact expected head")
    require(
        pr.get("number") == expected.pr_number
        and pr.get("state") == "open"
        and pr.get("draft") is False
        and pr.get("head", {}).get("sha") == expected_head,
        "release PR is no longer the expected open head",
    )
    version = release_version("pull_request", REPOSITORY, "", {"pull_request": pr})
    require(version and run.get("head_branch") == f"release/v{version}", "candidate branch/title is not canonical")
    require(
        proof.get("schema") == 1
        and proof.get("repository") == REPOSITORY
        and proof.get("event") == event
        and proof.get("run_id") == run["id"]
        and proof.get("run_attempt") == 1
        and proof.get("head") == expected_head
        and proof.get("tree") == expected_tree
        and proof.get("version") == version,
        "candidate source artifact differs from reviewed run or release identity",
    )
    require(proof.get("base") == pr.get("base", {}).get("sha"), "release base changed after candidate execution")
    require(
        head.get("sha") == expected_head and head.get("commit", {}).get("tree", {}).get("sha") == expected_tree,
        "live release head tree differs",
    )
    require(
        source.get("sha") == proof.get("source_commit")
        and source.get("commit", {}).get("tree", {}).get("sha") == expected_tree,
        "tested checkout tree differs",
    )
    if event == "pull_request":
        require(
            proof.get("pr") == pr.get("number")
            and [p.get("sha") for p in source.get("parents", [])] == [proof["base"], expected_head],
            "PR merge source does not bind its exact base and head",
        )
    else:
        require(
            proof.get("pr") is None and proof.get("source_commit") == expected_head,
            "dispatch source differs from the exact release head",
        )
    jobs = quarantine_lane(jobs, run["id"], expected_head, policy)
    inventory = policy.expected_jobs(event)
    require(
        len(jobs) == len(inventory) and {job.get("name") for job in jobs} == set(inventory),
        "missing, extra or short-matrix candidate jobs",
    )
    require(len({job.get("id") for job in jobs}) == len(jobs), "duplicate candidate job IDs")
    for job in jobs:
        require(
            job.get("run_id") == run["id"]
            and job.get("run_attempt") == 1
            and job.get("head_sha") == expected_head
            and job.get("status") == "completed"
            and job.get("conclusion") == inventory[job["name"]],
            "candidate job failed, changed, reran or skipped workload",
        )
        if inventory[job["name"]] == "success":
            require(
                type(job.get("runner_id")) is int and job["runner_id"] > 0 and job.get("steps"),
                "candidate workload never acquired a runner",
            )
    created, finished = run.get("created_at"), run.get("updated_at")
    require(isinstance(created, str) and isinstance(finished, str), "candidate timestamps missing")
    duration = (datetime.fromisoformat(finished) - datetime.fromisoformat(created)).total_seconds()
    require(duration >= 0, "candidate timestamps reversed")
    return {
        "schema": 1,
        "run_id": run["id"],
        "run_attempt": 1,
        "event": event,
        "head": expected_head,
        "tree": expected_tree,
        "base": proof["base"],
        "source_commit": proof["source_commit"],
        "job_count": len(jobs),
        "created_at": created,
        "updated_at": finished,
        "workflow_wall_seconds": duration,
        "policy_commit": policy.commit,
    }


def discover(get: Callable[[str], Any], expected_head: str, policy: Policy) -> int:
    """Require one original CI run on this exact head; never select a later green from competing attempts.

    Raises:
        CandidateError: Run identity is ambiguous, incomplete or outside the bounded listing.
    """
    require(valid_sha(expected_head), "invalid release head for candidate discovery")
    rows = []
    total = None
    for page in range(1, 11):
        data = get(f"{policy.workflow_runs}?head_sha={expected_head}&per_page=100&page={page}")
        require(
            isinstance(data, dict)
            and type(data.get("total_count")) is int
            and isinstance(data.get("workflow_runs"), list),
            "malformed candidate run listing",
        )
        if total is None:
            total = data["total_count"]
        require(data["total_count"] == total and 0 < total <= MAX_JOBS, "candidate run listing changed or is empty")
        rows.extend(data["workflow_runs"])
        require(len(rows) <= total, "extra candidate runs beyond listing total")
        if len(rows) == total:
            require(
                len(rows) == 1
                and type(rows[0].get("id")) is int
                and rows[0]["id"] > 0
                and rows[0].get("head_sha") == expected_head
                and rows[0].get("run_attempt") == 1,
                "ambiguous, changed or rerun candidate; retain all original runs",
            )
            return int(rows[0]["id"])
        require(data["workflow_runs"], "truncated candidate run listing")
    message = "candidate run listing exceeded bound"
    raise CandidateError(message)


def contents_bytes(get: Callable[[str], Any], path: str, ref: str, missing: str) -> bytes | None:
    """One file at an exact commit through the contents API; None when the API has no file there.

    A file over the contents API's 1 MB limit comes back without content (encoding "none"); it is then read as the git
    blob the same answer names, by its id (the id is the content's own hash).
    """
    data = get(f"contents/{path}?ref={ref}")
    if not isinstance(data, dict) or data.get("type") != "file":
        return None
    if data.get("encoding") == "none" and data.get("content") == "" and isinstance(data.get("sha"), str) and SHA.fullmatch(data["sha"]):
        data = get(f"git/blobs/{data['sha']}")
        require(isinstance(data, dict) and data.get("encoding") == "base64", missing)
    require(data.get("encoding") == "base64" and isinstance(data.get("content"), str), missing)
    return base64.b64decode("".join(data["content"].split()), validate=True)


def source_bytes(get: Callable[[str], Any], path: str, ref: str, pinned: Iterable[str]) -> bytes:
    """Decode an exact official source blob, never a moving branch or an unchecked path (only a `pinned` one)."""
    require(path in tuple(pinned) and SHA.fullmatch(ref), "invalid reviewed source request")
    blob = contents_bytes(get, path, ref, "source blob missing")
    require(blob is not None, "source blob missing")
    return blob  # type: ignore[return-value]


def verify(
    get: Callable[[str], Any],
    download: Callable[[int], bytes],
    expected: ExpectedCandidate,
    policy: Policy,
) -> dict[str, Any]:
    """Read every original job and the unique same-run source artifact, then re-fetch the terminal run identity."""
    run_id, pr_number, expected_head = expected.run_id, expected.pr_number, expected.head
    require(
        type(run_id) is int
        and run_id > 0
        and type(pr_number) is int
        and pr_number > 0
        and valid_sha(expected_head)
        and valid_sha(expected.tree),
        "invalid expected candidate identity",
    )
    run = get(f"actions/runs/{run_id}")
    require(isinstance(run, dict) and run.get("status") == "completed", "candidate run is not terminal")
    inventory = job_inventory(get, run_id)
    data = get(f"actions/runs/{run_id}/artifacts?per_page=100")
    require(
        isinstance(data, dict) and data.get("total_count") == len(data.get("artifacts", [])),
        "truncated artifact inventory",
    )
    matches = [a for a in data["artifacts"] if a.get("name") == ARTIFACT]
    require(len(matches) == 1 and matches[0].get("expired") is False, "candidate source artifact absent or duplicated")
    artifact = matches[0]
    require(
        artifact.get("workflow_run", {}).get("id") == run_id
        and artifact.get("workflow_run", {}).get("head_sha") == expected_head,
        "artifact belongs to another run or head",
    )
    require(type(artifact.get("id")) is int and artifact["id"] > 0, "malformed source artifact ID")
    archive = download(artifact["id"])
    require(len(archive) <= 1024 * 1024, "candidate source artifact exceeds bound")
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        require(zipped.namelist() == [PROOF_FILE], "unexpected candidate source archive contents")
        require(zipped.getinfo(PROOF_FILE).file_size <= MAX_PROOF, "candidate source proof exceeds bound")
        proof = json.loads(zipped.read(PROOF_FILE), object_pairs_hook=unique_object)
    require(
        isinstance(proof, dict)
        and all(
            isinstance(proof.get(key), str) and SHA.fullmatch(proof[key])
            for key in ("source_commit", "head", "base", "tree")
        ),
        "malformed candidate source identities",
    )
    pr = get(f"pulls/{pr_number}")
    head = get(f"commits/{expected_head}")
    source = get(f"commits/{proof['source_commit']}")
    require(get(f"actions/runs/{run_id}") == run, "candidate run changed during evidence collection")
    return validate(
        {"run": run, "jobs": inventory, "proof": proof, "pr": pr, "source": source, "head": head},
        expected,
        policy,
    )
