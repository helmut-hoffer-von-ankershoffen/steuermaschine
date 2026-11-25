"""Read-only evidence for a never-acquired exact-main macOS test job.

This is an original-attempt assessment, not a merge or release verdict. No
retry or rerun admission is performed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..release_lib import candidate_capacity
from . import core_policy, main_capacity, main_checkpoint, main_lanes
from .release_candidate import job_inventory, require, valid_sha

GREEN = ("success", "skipped", "neutral")
READY = (1, "completed", "success")


@dataclass(frozen=True)
class MainCapacityRecovery:
    """Pin a complete original result before a future targeted retry."""

    head: str
    push_run: int
    macos_run: int
    aggregate_job: int
    jobs: tuple[int, ...]
    inventory_sha256: str


def exact_jobs(api: Callable[[str], Any], run: int, head: str) -> dict[str, dict[str, Any]]:
    """Require a uniquely bound inventory from the complete attempt reader."""
    jobs = job_inventory(api, run)
    found = {}
    ids = set()
    for job in jobs:
        name, identifier = job.get("name"), job.get("id")
        require(isinstance(name, str) and name and name not in found
                and type(identifier) is int and identifier > 0 and identifier not in ids
                and type(job.get("run_id")) is int and job["run_id"] == run
                and type(job.get("run_attempt")) is int and job["run_attempt"] == 1
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
    logger = main_capacity.dependency_check(api, policy, head)
    main_capacity.check_aggregate(aggregate, logger, cancelled, lambda _: raw_log)
    require(main_checkpoint.main_ci_inventory(api, head) == runs
            and api("git/ref/heads/main")["object"]["sha"] == head, "main or run inventory changed")
    identity = candidate_capacity.digest(
        {"head": head, "push": linux_jobs, "macos": mac_jobs, "annotations": original_annotations,
         "aggregate_log": raw_log.decode()}
    )
    return MainCapacityRecovery(head, push_run, macos_run, aggregate["id"],
                                tuple(sorted(mac_jobs[name]["id"] for name in cancelled)), identity)
