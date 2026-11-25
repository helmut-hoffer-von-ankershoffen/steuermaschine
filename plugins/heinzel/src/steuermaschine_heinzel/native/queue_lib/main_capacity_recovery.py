"""Read-only evidence for a never-acquired exact-main macOS test job.

This is an original-attempt assessment, not a merge or release verdict. No
retry or rerun admission is performed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from subprocess import SubprocessError
from typing import Any

from steuermaschine.protocols import Store

from ..release_lib import candidate_capacity
from . import core_policy, hzlib, main_capacity, main_checkpoint, main_lanes
from .release_candidate import job_inventory, require, source_bytes, valid_sha

GREEN = ("success", "skipped", "neutral")
READY = (1, "completed", "success")
INTENT = "main-capacity-retry-intent-v1"
REQUEST = "main-capacity-retry-request-v1"


@dataclass(frozen=True)
class MainCapacityRecovery:
    """Pin a complete original result before a future targeted retry."""

    head: str
    push_run: int
    macos_run: int
    aggregate_job: int
    jobs: tuple[int, ...]
    inventory_sha256: str
    original: dict[str, Any]


def exact_jobs(api: Callable[[str], Any], run: int, head: str, *, attempt: int = 1) -> dict[str, dict[str, Any]]:
    """Require a uniquely bound inventory from the complete attempt reader."""
    jobs = job_inventory(api, run, attempt=attempt)
    found = {}
    ids = set()
    for job in jobs:
        name, identifier = job.get("name"), job.get("id")
        require(isinstance(name, str) and name and name not in found
                and type(identifier) is int and identifier > 0 and identifier not in ids
                and type(job.get("run_id")) is int and job["run_id"] == run
                and type(job.get("run_attempt")) is int and job["run_attempt"] in (1, attempt)
                and job.get("head_sha") == head and job.get("status") == "completed",
                "unbound or duplicate original main job")
        ids.add(identifier)
        found[name] = job
    return found


def assess(head: str, push_run: int, macos_run: int, *, api: Callable[[str], Any],
           read_log: Callable[[int], bytes]) -> MainCapacityRecovery:
    """Prove the exact original main pair and explain the sole failed dependency.

    A fresh success, competing run or newer main makes the narrow retry moot
    or unsafe. The shared exact-attempt reader rejects incomplete pages.
    """
    require(valid_sha(head) and api("git/ref/heads/main")["object"]["sha"] == head, "main moved")
    require(all(type(run) is int and run > 0 for run in (push_run, macos_run)) and push_run != macos_run,
            "invalid original pair IDs")
    policy = core_policy.at_commit_api(api, head)
    runs = main_checkpoint.main_ci_inventory(api, head)
    require(set(runs) == {push_run, macos_run}, "missing or competing exact-main runs")
    push, macos = runs[push_run], runs[macos_run]
    require(push[1:5] == ("push", *READY) and macos[1:5] == ("workflow_dispatch", 1, "completed", "failure")
            and push[0] == macos[0] and push[-1] <= macos[-1], "not the original split main pair")
    lane = api(f"actions/runs/{macos_run}")
    require(isinstance(lane, dict) and lane.get("id") == macos_run
            and lane.get("head_sha") == head and lane.get("head_branch") == "main"
            and lane.get("workflow_id") == macos[0] and lane.get("path") == policy.workflow
            and lane.get("event") == "workflow_dispatch" and type(lane.get("run_attempt")) is int
            and (lane["run_attempt"], lane.get("status"), lane.get("conclusion")) == (1, "completed", "failure")
            and lane.get("display_title") == f"CI macOS main {head}", "not the coalesced macOS dispatch")

    linux_jobs = exact_jobs(api, push_run, head)
    mac_jobs = exact_jobs(api, macos_run, head)
    linux = {name for name, job in linux_jobs.items() if job.get("conclusion") == "success"}
    mac = {name for name, job in mac_jobs.items() if job.get("conclusion") == "success"}
    failed = {name for name, job in mac_jobs.items() if job.get("conclusion") not in GREEN}
    cancelled = failed - {policy.aggregate}
    mac_required = {name for name in policy.test_jobs if main_lanes.is_macos_cell(name)}
    require(
        all(job.get("conclusion") in GREEN for job in linux_jobs.values())
        and {policy.aggregate, main_lanes.DISPATCH_JOB, *(policy.test_jobs - mac_required)} <= linux
        and mac_required and mac_required <= mac | cancelled
        and policy.aggregate in failed and mac_jobs[policy.aggregate].get("conclusion") == "failure"
        and cancelled and cancelled <= mac_required
        and all(name.startswith("Test (macos-latest, Python ") for name in cancelled),
        "missing required jobs or observed a non-capacity failure",
    )
    original_annotations = {}
    for name in sorted(cancelled):
        job = mac_jobs[name]
        rows = candidate_capacity.annotations(job, api=api)
        candidate_capacity.never_acquired(job, rows, head)
        original_annotations[name] = rows
    aggregate = mac_jobs[policy.aggregate]
    raw_log = read_log(aggregate["id"])
    workflow = source_bytes(api, policy.workflow, head, (policy.workflow,)).decode()
    require(main_capacity.retry_dependents(workflow, "test") == {"ci-ok"},
            "targeted test rerun has unreviewed dependent jobs")
    logger = main_capacity.dependency_check(api, policy, head)
    main_capacity.check_aggregate(aggregate, logger, cancelled, lambda _: raw_log)
    require(main_checkpoint.main_ci_inventory(api, head) == runs
            and api("git/ref/heads/main")["object"]["sha"] == head, "main or run inventory changed")
    evidence = {"head": head, "push": linux_jobs, "macos": mac_jobs, "annotations": original_annotations,
                "aggregate_log": raw_log.decode()}
    identity = candidate_capacity.digest(evidence)
    return MainCapacityRecovery(head, push_run, macos_run, aggregate["id"],
                                tuple(sorted(mac_jobs[name]["id"] for name in cancelled)), identity, evidence)


def prepare(store: Store, head: str, push_run: int, macos_run: int, *, api: Callable[[str], Any],
            read_log: Callable[[int], bytes], now: datetime) -> dict[str, Any]:
    """Persist exactly one original job and its request target. No network write runs here."""
    require(valid_sha(head) and all(type(run) is int and run > 0 for run in (push_run, macos_run))
            and push_run != macos_run, "invalid main request identity")
    key = str(macos_run)
    with store.lock(INTENT):
        prior = store.get_receipt(INTENT, key)
        if prior is not None:
            require(prior.get("head") == head and prior.get("push_run") == push_run,
                    "conflicting original request; never retry automatically")
            return validate_intent(prior)
        require(now.utcoffset() is not None, "request clock must include timezone")
        proof = assess(head, push_run, macos_run, api=api, read_log=read_log)
        require(len(proof.jobs) == 1, "a single exact job is required for targeted retry")
        draft = asdict(proof) | {
            "jobs": list(proof.jobs),
            "expected_attempt": 2,
            "prepared_at": now.isoformat(),
            "request_path": f"repos/{hzlib.CORE}/actions/jobs/{proof.jobs[0]}/rerun",
            "status": "prepared",
        }
        saved = draft | {"intent_sha256": candidate_capacity.digest(draft)}
        store.put_receipt(INTENT, key, saved)
        return saved


def validate_intent(data: dict[str, Any]) -> dict[str, Any]:
    """Refuse a corrupt, incompatible, or altered receipt."""
    require(data.get("status") == "prepared" and type(data.get("expected_attempt")) is int
            and data["expected_attempt"] == 2 and valid_sha(data.get("head"))
            and all(type(data.get(key)) is int and data[key] > 0 for key in ("push_run", "macos_run", "aggregate_job"))
            and data["push_run"] != data["macos_run"], "invalid exact main retry intent")
    jobs = data.get("jobs")
    require(isinstance(jobs, list) and len(jobs) == 1 and type(jobs[0]) is int and jobs[0] > 0
            and data.get("request_path") == f"repos/{hzlib.CORE}/actions/jobs/{jobs[0]}/rerun",
            "retry intent does not target exactly one original job")
    require(isinstance(data.get("original"), dict) and data["original"].get("head") == data["head"]
            and candidate_capacity.digest(data["original"]) == data.get("inventory_sha256")
            and candidate_capacity.digest({k: v for k, v in data.items() if k != "intent_sha256"}) == data.get("intent_sha256"),
            "main retry receipt fingerprint differs")
    return data


def request_once(store: Store, macos_run: int, expected: str, *, api: Callable[[str], Any],
                 read_log: Callable[[int], bytes], now: datetime, post: Callable[[str], bool]) -> dict[str, Any]:
    """Record the sole outbound intention before submitting through the supplied adapter."""
    require(type(macos_run) is int and macos_run > 0, "invalid retry run")
    key = str(macos_run)
    with store.lock(INTENT):
        require(store.get_receipt(REQUEST, key) is None, "request already intended; inspect the run, never resubmit")
        saved = store.get_receipt(INTENT, key)
        require(saved is not None, "no durable main retry intent")
        data = validate_intent(saved)
        require(data["macos_run"] == macos_run and data["intent_sha256"] == expected,
                "approved retry intent fingerprint differs")
        fresh = assess(data["head"], data["push_run"], macos_run, api=api, read_log=read_log)
        require(fresh.inventory_sha256 == data["inventory_sha256"], "original job evidence changed")
        require(now.utcoffset() is not None and now >= candidate_capacity.clock(data["prepared_at"]),
                "request clock predates original preparation")
        request = {"intent_sha256": expected, "attempt": 2, "path": data["request_path"], "requested_at": now.isoformat()}
        store.put_receipt(REQUEST, key, request)
        try:
            accepted = post(request["path"])
        except (OSError, SubprocessError):
            accepted = False
        require(accepted is True, "request outcome ambiguous; inspect the run, never resubmit")
        acknowledgement = request | {"transport": "accepted", "attempt_origin": "unproven"}
        store.put_receipt(REQUEST, f"{key}-ack", acknowledgement)
        return acknowledgement


@dataclass(frozen=True)
class ObservedMainRetry:
    """Verified execution evidence with independent, unproven request attribution."""

    head: str
    run: int
    replacement_job: int
    aggregate_job: int
    intent_sha256: str
    attempt_origin: str = "unproven"


def inspect_attempt(store: Store, macos_run: int, *, api: Callable[[str], Any]) -> ObservedMainRetry:
    """Verify physical execution and retained jobs without asserting which request caused them."""
    require(type(macos_run) is int and macos_run > 0, "invalid retry run")
    saved = store.get_receipt(INTENT, str(macos_run))
    require(saved is not None, "no durable main retry intent")
    data = validate_intent(saved)
    head, push = data["head"], data["push_run"]
    require(data["macos_run"] == macos_run and api("git/ref/heads/main")["object"]["sha"] == head, "main or run moved")
    runs = main_checkpoint.actual_main_ci(api, head, push)
    require(set(runs) == {push, macos_run} and runs[macos_run][1:5] == ("workflow_dispatch", 2, "completed", "success"),
            "expected one successful same-run attempt two")
    run = api(f"actions/runs/{macos_run}/attempts/2")
    previous = f"https://api.github.com/repos/{hzlib.CORE}/actions/runs/{macos_run}/attempts/1"
    require(run.get("previous_attempt_url") == previous and run.get("id") == macos_run
            and run.get("head_sha") == head and run.get("head_branch") == "main" and run.get("event") == "workflow_dispatch"
            and run.get("workflow_id") == runs[macos_run][0] and run.get("display_title") == f"CI macOS main {head}"
            and type(run.get("run_attempt")) is int and run["run_attempt"] == 2
            and run.get("status") == "completed" and run.get("conclusion") == "success",
            "replacement attempt identity differs")
    original = data["original"]
    before = exact_jobs(api, macos_run, head)
    require(before == original["macos"] and exact_jobs(api, push, head) == original["push"], "original executions changed")
    target = [name for name, job in before.items() if job["id"] == data["jobs"][0]]
    policy = core_policy.at_commit_api(api, head)
    require(len(target) == 1 and before[policy.aggregate]["id"] == data["aggregate_job"], "original target or aggregate moved")
    name = target[0]
    requested = {name, policy.aggregate}
    after = exact_jobs(api, macos_run, head, attempt=2)
    require(set(after) in (requested, set(before)), "extra or incomplete replacement jobs")
    original_ids = {job["id"] for jobs in (original["push"], before) for job in jobs.values()}
    intent = candidate_capacity.clock(data["prepared_at"])
    for original_jobs in (original["push"], before):
        for job in original_jobs.values():
            if job["conclusion"] == "success":
                candidate_capacity.executed(job)
                candidate_capacity.execution_boundary(job, intent, reused=True)
    for job_name, job in after.items():
        if job_name not in requested:
            old = before[job_name]
            if job["run_attempt"] == 1:
                require(job == old, "an unrequested original job changed")
            else:
                require(job["id"] not in original_ids, "carrier reused an original job ID")
                if old["conclusion"] == "skipped":
                    candidate_capacity.skipped_carrier(job, old)
                else:
                    require(candidate_capacity.execution_tuple(job) == candidate_capacity.execution_tuple(old),
                            "an unrequested execution changed")
            continue
        require(job["run_attempt"] == 2 and job["id"] not in original_ids and job["conclusion"] == "success",
                "target and aggregate must actually succeed in attempt two")
        candidate_capacity.executed(job)
        candidate_capacity.execution_boundary(job, intent, reused=False)
    candidate_capacity.replacement_steps(after[name])
    logger = main_capacity.dependency_check(api, policy, head)
    required = "Require the complete direct CI workload"
    require(required in logger.steps and all(
        sum(s.get("name") == step and s.get("conclusion") == "success" for s in after[policy.aggregate]["steps"]) == 1
        for step in (logger.step, required)
    ), "replacement aggregate did not verify the complete workload")
    require(main_checkpoint.actual_main_ci(api, head, push) == runs
            and api("git/ref/heads/main")["object"]["sha"] == head, "main or run inventory changed")
    return ObservedMainRetry(head, macos_run, after[name]["id"], after[policy.aggregate]["id"], data["intent_sha256"])
