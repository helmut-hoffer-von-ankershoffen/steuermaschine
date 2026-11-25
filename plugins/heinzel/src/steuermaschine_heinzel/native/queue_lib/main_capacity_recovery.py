"""Verify macOS runner capacity failure and start one fresh official main lane."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from steuermaschine.protocols import Store

from ..release_lib import candidate_capacity
from . import core_policy, hzlib, main_capacity, main_checkpoint, main_lanes
from .release_candidate import job_inventory, require, source_bytes, valid_sha

GREEN = ("success", "skipped", "neutral")
READY = (1, "completed", "success")
DISPATCH = "main-capacity-dispatch-v1"


@dataclass(frozen=True)
class MainCapacityRecovery:
    """Bind the original capacity failure to a fresh full macOS dispatch."""

    head: str
    push_run: int
    macos_run: int
    aggregate_job: int
    jobs: tuple[int, ...]
    inventory_sha256: str
    workflow: str
    workflow_id: int


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

    A fresh success, competing run or newer main makes the replacement dispatch moot
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
    dispatch_jobs = policy.expected_jobs("workflow_dispatch")
    capacity_eligible = {name for name, verdict in dispatch_jobs.items()
                         if verdict == "success" and main_lanes.is_macos_cell(name)}
    require(
        all(job.get("conclusion") in GREEN for job in linux_jobs.values())
        and {policy.aggregate, main_lanes.DISPATCH_JOB, *(policy.test_jobs - mac_required)} <= linux
        and mac_required and mac_required <= mac | cancelled
        and policy.aggregate in failed and mac_jobs[policy.aggregate].get("conclusion") == "failure"
        and cancelled and cancelled <= capacity_eligible,
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
    logger = main_capacity.dependency_check(api, policy, head, workflow=workflow)
    verify_original_workload(workflow, policy, linux_jobs, mac_jobs, cancelled, logger)
    main_capacity.check_aggregate(aggregate, logger, cancelled, lambda _: raw_log)
    require(main_checkpoint.main_ci_inventory(api, head) == runs
            and api("git/ref/heads/main")["object"]["sha"] == head, "main or run inventory changed")
    evidence = {"head": head, "push": linux_jobs, "macos": mac_jobs, "annotations": original_annotations,
                "aggregate_log": raw_log.decode()}
    identity = candidate_capacity.digest(evidence)
    return MainCapacityRecovery(head, push_run, macos_run, aggregate["id"],
                                tuple(sorted(mac_jobs[name]["id"] for name in cancelled)), identity, policy.workflow, push[0])


def verify_original_workload(workflow: str, policy: core_policy.Policy, linux: dict, macos: dict,
                             cancelled: set[str], logger: main_capacity.DependencyCheck) -> None:
    """An original job's green conclusion counts only with its actual required workload."""
    step = main_capacity.test_step(workflow)
    for name in sorted(policy.test_jobs - cancelled):
        lane = macos if main_lanes.is_macos_cell(name) else linux
        job = lane[name]
        candidate_capacity.executed(job)
        main_capacity.passed_steps(job, step)
    aggregate = linux[policy.aggregate]
    candidate_capacity.executed(aggregate)
    main_capacity.passed_steps(aggregate, logger.step, main_capacity.aggregate_workload_step(workflow))


def verified_dispatch(store: Store, key: str, request: dict[str, Any], api: Callable[[str], Any]) -> dict[str, Any]:
    received = store.get_receipt(DISPATCH, f"{key}.reply")
    require(isinstance(received, dict) and received.get("request_sha256") == candidate_capacity.digest(request),
            "dispatch outcome is unbound; inspect GitHub before further action")
    rid = received.get("run")
    require(type(rid) is int and rid > 0 and rid not in (request["push_run"], request["macos_run"])
            and received.get("url") == f"https://github.com/{hzlib.CORE}/actions/runs/{rid}",
            "dispatch run identity unavailable")
    run = api(f"actions/runs/{rid}")
    head = request["head"]
    require(isinstance(run, dict) and run.get("id") == rid and run.get("head_sha") == head
            and run.get("head_branch") == "main" and run.get("path") == request["workflow"]
            and run.get("workflow_id") == request["workflow_id"] and run.get("event") == "workflow_dispatch"
            and type(run.get("run_attempt")) is int and run["run_attempt"] == 1
            and run.get("display_title") == f"CI macOS main {head}"
            and run.get("status") in ("queued", "in_progress", "waiting", "requested", "pending", "completed")
            and all(isinstance(run.get(key), dict) and run[key].get("full_name") == hzlib.CORE
                    for key in ("repository", "head_repository")),
            f"returned run {rid} is not the first-attempt full macOS lane on the requested SHA")
    require(api("git/ref/heads/main")["object"]["sha"] == head,
            f"main moved during dispatch of run {rid}; no additional dispatch will be sent")
    return {"run": rid, "url": received["url"], "head": head, "status": run.get("status"),
            "conclusion": run.get("conclusion"), "original_run": request["macos_run"]}


def dispatch(store: Store, head: str, push_run: int, macos_run: int, *, api: Callable[[str], Any],
             read_log: Callable[[int], bytes], now: Callable[[], datetime],
             authorize: Callable[[], None], post: Callable[[str], Any]) -> dict[str, Any]:
    """Request one new official macOS lane per main SHA. Only the returned run ID is read back."""
    require(valid_sha(head) and all(type(rid) is int and rid > 0 for rid in (push_run, macos_run)),
            "invalid main capacity dispatch identity")
    with store.lock(DISPATCH):
        if prior := store.get_receipt(DISPATCH, head):
            require((prior.get("head"), prior.get("push_run"), prior.get("macos_run")) == (head, push_run, macos_run),
                    "another capacity dispatch is already recorded for this main SHA")
            return verified_dispatch(store, head, prior, api)
        proof = assess(head, push_run, macos_run, api=api, read_log=read_log)
        authorize()
        require(api("git/ref/heads/main")["object"]["sha"] == head, "main moved before dispatch")
        timestamp = now()
        require(timestamp.utcoffset() is not None, "dispatch clock has no timezone")
        request = asdict(proof) | {"requested_at": timestamp.isoformat(), "ref": "main", "lane": "macos"}
        request["jobs"] = list(proof.jobs)
        store.put_receipt(DISPATCH, head, request)
        try:
            reply = post(proof.workflow.rsplit("/", 1)[-1])
        except OSError:
            reply = None
        rid = reply.get("workflow_run_id") if isinstance(reply, dict) else None
        link = f"https://github.com/{hzlib.CORE}/actions/runs/{rid}"
        require(type(rid) is int and rid > 0 and rid not in (push_run, macos_run)
                and reply.get("html_url") == link, "dispatch outcome is unbound; no request will be repeated")
        store.put_receipt(DISPATCH, f"{head}.reply",
                          {"run": rid, "url": link, "request_sha256": candidate_capacity.digest(request)})
        return verified_dispatch(store, head, request, api)
