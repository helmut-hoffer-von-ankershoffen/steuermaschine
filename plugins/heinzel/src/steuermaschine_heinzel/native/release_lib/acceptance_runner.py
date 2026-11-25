"""Explicit corrected-main acceptance runner selection from that commit's reviewed Core policy."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from ..queue_lib import core_facts, core_policy
from ..queue_lib.release_candidate import CandidateError, contents_bytes
from . import candidate_capacity as remote

KEYS = {"schema", "workflow", "workflow_sha256", "harness", "harness_sha256", "tagged_workflows", "runners", "shards", "jobs"}
JOBS = {"pins", "shard", "linux_runner", "macos_logical_runner", "linux_result", "macos_result"}
RUNNER = {"os", "image_os", "architecture", "python"}
SHA = re.compile(r"[a-f0-9]{40}")
DIGEST = re.compile(r"[a-f0-9]{64}")


def require(condition: Any, message: str) -> None:
    if not condition:
        raise CandidateError(message)


def request_state(data: dict[str, Any], version: str, label: str, head: str, failed_run: int) -> None:
    """Refuse before the release prelude can create anything; permit only this replacement or its resume."""
    identity = data.get("acceptance_identity")
    require(data.get("version") == version and data.get("absorbed_by") is None
            and data.get("acceptance_snapshot_mode") is True and isinstance(identity, dict)
            and isinstance(data.get("snapshot"), dict) and identity.get("snapshot") == data["snapshot"]
            and identity.get("version") == version and data["snapshot"].get("version") == version
            and type(failed_run) is int and failed_run > 0,
            "alternate acceptance requires the existing exact published snapshot receipt")
    existing = identity.get("runner")
    if existing is not None:
        history = data.get("acceptance_attempts", [])
        require(isinstance(existing, dict) and existing.get("label") == label and existing.get("head") == head
                and identity.get("ref") == "main" and identity.get("commit") == head
                and isinstance(history, list) and all(isinstance(row, dict) for row in history)
                and any(row.get("acceptance_run_id") == failed_run for row in history),
                "alternate acceptance resume identity changed")
    else:
        require(data.get("finished") is True and data.get("accepted") is False and data.get("acceptance_run_id") == failed_run,
                "alternate acceptance requires the exact finished rejected run")


def policy(value: Any, label: str) -> dict[str, Any]:
    require(isinstance(value, dict) and set(value) == KEYS and type(value.get("schema")) is int and value["schema"] == 1,
            "reviewed Core acceptance runner policy is absent or unknown")
    for key in ("workflow_sha256", "harness_sha256"):
        require(isinstance(value[key], str) and DIGEST.fullmatch(value[key]), "acceptance policy digest malformed")
    require(core_policy.repo_path(value["workflow"]) and value["workflow"].startswith(".github/workflows/")
            and core_policy.repo_path(value["harness"]), "acceptance policy source paths malformed")
    tags, shards, runners, jobs = (value[key] for key in ("tagged_workflows", "shards", "runners", "jobs"))
    require(isinstance(tags, list) and tags and all(isinstance(item, str) and DIGEST.fullmatch(item) for item in tags)
            and len(set(tags)) == len(tags), "acceptance tagged workflow policy malformed")
    require(isinstance(shards, list) and len(shards) == 4 and all(isinstance(item, str) and re.fullmatch(r"[a-z0-9-]+", item) for item in shards)
            and len(set(shards)) == 4, "acceptance policy requires four distinct VM shards")
    require(isinstance(runners, dict) and label in runners and isinstance(runners[label], dict)
            and set(runners[label]) == RUNNER, "requested acceptance runner is not approved by Core")
    selected = runners[label]
    require(re.fullmatch(r"[a-z0-9-]+", label) and selected["os"] == "macOS" and selected["architecture"] == "arm64"
            and isinstance(selected["image_os"], str) and re.fullmatch(r"macos\d+", selected["image_os"])
            and isinstance(selected["python"], str) and re.fullmatch(r"3[.]\d+", selected["python"]),
            "acceptance runner requires native Apple Silicon image and Python")
    require(isinstance(jobs, dict) and set(jobs) == JOBS
            and all(isinstance(item, str) and item and len(item) < 256 and not re.search(r"[\x00-\x1f\x7f]", item) for item in jobs.values()),
            "acceptance job policy malformed")
    require(jobs["shard"].count("{runner}") == jobs["shard"].count("{shard}") == 1
            and not set(jobs["shard"].replace("{runner}", "").replace("{shard}", "")) & {"{", "}"},
            "acceptance shard job template malformed")
    return value


def bind(rel: Any, snapshot: dict[str, Any], commit: str, expected_head: str, label: str) -> dict[str, Any]:
    """Read policy and both immutable source generations, never a working tree or mutable policy."""
    require(isinstance(expected_head, str) and SHA.fullmatch(expected_head) and commit == expected_head
            and commit != snapshot["commit"], "alternate acceptance requires the exact reviewed corrected-main head")
    reviewed = core_facts.at(commit)
    require(reviewed.repository == rel.CORE, "acceptance policy repository differs")
    selected = policy(reviewed.acceptance, label)
    require(selected["workflow"] == ".github/workflows/acceptance.yml", "acceptance policy does not describe the dispatched workflow")
    def get(path: str) -> Any:
        result = rel.hzlib.ghj("api", f"repos/{rel.CORE}/{path}", default=None)
        require(result is not None, "acceptance source unavailable")
        return result
    for path, source_commit, expected in (
        (selected["workflow"], commit, selected["workflow_sha256"]),
        (selected["harness"], commit, selected["harness_sha256"]),
    ):
        source = contents_bytes(get, path, source_commit, "acceptance source unavailable")
        require(source is not None and hashlib.sha256(source).hexdigest() == expected, "acceptance policy source digest differs")
    tagged = contents_bytes(get, selected["workflow"], snapshot["commit"], "tagged acceptance source unavailable")
    require(tagged is not None and hashlib.sha256(tagged).hexdigest() in selected["tagged_workflows"],
            "immutable tagged acceptance workflow is not approved")
    return {"label": label, "policy": selected, "head": commit}


def supports_snapshot(rel: Any, commit: str, workflow_digest: str | None = None) -> bool:
    """New tagged workflows opt in through their own exact, reviewed generated policy."""
    try:
        reviewed = core_facts.at(commit)
        raw = reviewed.acceptance
        require(isinstance(raw, dict) and isinstance(raw.get("runners"), dict) and raw["runners"], "acceptance policy missing")
        selected = policy(raw, next(iter(raw["runners"])))
        require(selected["workflow"] == ".github/workflows/acceptance.yml"
                and (workflow_digest is None or selected["workflow_sha256"] == workflow_digest),
                "acceptance policy does not describe the dispatched workflow")
        require(reviewed.repository == rel.CORE, "acceptance policy repository differs")
        def get(path: str) -> Any:
            value = rel.hzlib.ghj("api", f"repos/{rel.CORE}/{path}", default=None)
            require(value is not None, "acceptance source unavailable")
            return value
        return all(
            hashlib.sha256(contents_bytes(get, selected[path_key], commit, "acceptance source unavailable") or b"").hexdigest() == selected[hash_key]
            for path_key, hash_key in (("workflow", "workflow_sha256"), ("harness", "harness_sha256"))
        )
    except (CandidateError, KeyError, TypeError, ValueError):
        return False


def validate_run(data: dict[str, Any], run: dict[str, Any], core: str, run_id: int) -> None:
    identity = data.get("acceptance_identity") or {}
    binding = identity.get("runner")
    if binding is None:
        return
    require(isinstance(binding, dict) and set(binding) == {"label", "policy", "head"}
            and binding["head"] == identity.get("commit") and identity.get("ref") == "main",
            "alternate acceptance identity changed")
    selected = policy(binding["policy"], binding["label"])
    require(core_facts.at(binding["head"]).acceptance == selected, "stored acceptance policy differs from reviewed source")
    repo, head_repo = run.get("repository") or {}, run.get("head_repository") or {}
    require(type(run_id) is int and run_id > 0 and run.get("id") == run_id and run.get("run_attempt") == 1
            and run.get("head_sha") == binding["head"] and run.get("head_branch") == "main"
            and run.get("path") == selected["workflow"] and run.get("event") == "workflow_dispatch"
            and run.get("display_title") == f"Acceptance {identity['version']} {data.get('acceptance_marker')}"
            and repo.get("full_name") == head_repo.get("full_name") == core
            and type(repo.get("id")) is int and repo["id"] > 0 and head_repo.get("id") == repo["id"],
            "alternate acceptance run does not match the reviewed request")
    history = data.get("acceptance_attempts", [])
    require(isinstance(history, list) and history and all(isinstance(row, dict) for row in history)
            and not any(row.get("acceptance_run_id") == run_id for row in history),
            "alternate acceptance cannot reuse a rejected run")


def validate_jobs(identity: dict[str, Any], run_id: int) -> None:
    binding = identity.get("runner")
    if binding is None:
        return
    selected = policy(binding["policy"], binding["label"])
    require(core_facts.at(binding["head"]).acceptance == selected, "stored acceptance policy differs from reviewed source")
    job_policy = selected["jobs"]
    rows = remote.paged(f"actions/runs/{run_id}/attempts/1/jobs", "jobs")
    expected = {job_policy[key] for key in ("pins", "linux_result", "macos_result")}
    expected |= {job_policy["shard"].format(runner=runner, shard=shard)
                 for runner in (job_policy["linux_runner"], job_policy["macos_logical_runner"]) for shard in selected["shards"]}
    require(len(expected) == 11 and len(rows) == len(expected) and {row.get("name") for row in rows} == expected,
            "alternate acceptance job inventory differs")
    by_name = {row["name"]: row for row in rows}
    for row in rows:
        remote.job_shape(row, run_id, identity["commit"])
        require(row.get("run_attempt") == 1 and row.get("conclusion") == "success", "alternate acceptance job did not pass")
        remote.executed(row)
        require(row.get("runner_group_id") == 0 and row.get("runner_group_name") == "GitHub Actions"
                and str(row.get("runner_name", "")).startswith("GitHub Actions "), "alternate acceptance job is not hosted")
        name = row["name"]
        steps = ("Pin the release bytes once for every shard",) if name == job_policy["pins"] else (
            ("Reduce the four shard receipts of this OS", "Upload the receipt")
            if name in (job_policy["linux_result"], job_policy["macos_result"])
            else ("Run the acceptance harness", "Upload the shard receipt")
        )
        for step in steps:
            require(sum(item.get("name") == step and item.get("status") == "completed" and item.get("conclusion") == "success"
                        for item in row["steps"]) == 1, "alternate acceptance workload did not execute")
        if name in (job_policy["pins"], job_policy["linux_result"], job_policy["macos_result"]):
            require(row.get("labels") == [job_policy["linux_runner"]], "alternate acceptance reducer or pins runner differs")
    for logical, label in ((job_policy["linux_runner"], job_policy["linux_runner"]), (job_policy["macos_logical_runner"], binding["label"])):
        runners = set()
        for shard in selected["shards"]:
            job = by_name[job_policy["shard"].format(runner=logical, shard=shard)]
            remote.executed(job)
            require(job.get("labels") == [label] and job.get("runner_group_id") == 0
                    and job.get("runner_group_name") == "GitHub Actions"
                    and str(job.get("runner_name", "")).startswith("GitHub Actions "), "alternate acceptance shard runner differs")
            runners.add(job["runner_id"])
        require(len(runners) == 4, "alternate acceptance did not use four independent VMs per OS")


def receipt_matches(result: dict[str, Any], identity: dict[str, Any], os_name: str) -> bool:
    binding = identity.get("runner")
    if binding is None:
        return True
    selected = policy(binding["policy"], binding["label"])
    require(core_facts.at(binding["head"]).acceptance == selected, "stored acceptance policy differs from reviewed source")
    if result.get("harness_sha256") != selected["harness_sha256"]:
        return False
    ci = result.get("ci")
    if not isinstance(ci, dict):
        return False
    if os_name != "macOS":
        return ci.get("runner_arch") == "X64"
    config = selected["runners"][binding["label"]]
    hosts = result.get("host")
    if not isinstance(hosts, dict) or set(hosts) != set(selected["shards"]) or ci.get("runner_arch") != config["architecture"].upper():
        return False
    for host in hosts.values():
        if not isinstance(host, dict):
            return False
        version = host.get("macos", "")
        if not (host.get("runner_label") == binding["label"] and host.get("machine") == config["architecture"]
                and isinstance(version, str) and re.fullmatch(r"\d+[.]\d+(?:[.]\d+)?", version)
                and host.get("image_os") == config["image_os"] == "macos" + version.split(".")[0]
                and isinstance(host.get("image_version"), str) and host["image_version"]
                and isinstance(host.get("python_version"), str) and re.fullmatch(re.escape(config["python"]) + r"[.]\d+", host["python_version"])):
            return False
    return True
