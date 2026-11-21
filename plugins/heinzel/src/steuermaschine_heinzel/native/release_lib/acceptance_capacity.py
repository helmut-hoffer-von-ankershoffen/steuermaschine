"""Replace one tagged public acceptance run whose Mac job never acquired a runner."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import uuid
import zipfile
from typing import Any

from . import candidate_capacity as remote

DIGEST = re.compile(r"[a-f0-9]{64}")
SHA = re.compile(r"[a-f0-9]{40}")
MARKER = re.compile(r"release-[a-f0-9]{32}")
LINUX = "Acceptance (ubuntu-24.04)"
MAC = "Acceptance (macos-latest)"
WORKFLOW = "acceptance.yml"
SCHEMA = "published-acceptance-capacity/v1"
# Core's sharded acceptance.yml: one pins job, four shards per OS, and one reducer per OS that keeps the job name and
# the receipt artifact the release runner reads (LINUX, MAC, acceptance-result-<OS>).
PINS = "Acceptance pins"
SHARDS = ("public", "update", "plugins", "browser")
LINUX_SHARDS = tuple(f"Acceptance shard (ubuntu-24.04, {name})" for name in SHARDS)
MAC_SHARDS = tuple(f"Acceptance shard (macos-latest, {name})" for name in SHARDS)
SHARDED_JOBS = {PINS, *LINUX_SHARDS, *MAC_SHARDS, LINUX, MAC}
LINUX_RECEIPT = "acceptance-result-Linux"
RUN_FIELDS = ("id", "run_attempt", "head_sha", "head_branch", "event", "path", "display_title",
              "status", "conclusion", "workflow_id", "check_suite_id", "created_at")


def exact_run(run: dict[str, Any], core: str, identity: dict[str, Any], marker: str, run_id: int, attempt: int) -> None:
    repo, head_repo = run.get("repository"), run.get("head_repository")
    remote.require(
        type(run.get("id")) is int and run["id"] == run_id
        and type(run.get("run_attempt")) is int and run["run_attempt"] == attempt
        and run.get("head_sha") == identity["commit"] and run.get("head_branch") == identity["ref"]
        and run.get("event") == "workflow_dispatch" and run.get("path") == f".github/workflows/{WORKFLOW}"
        and run.get("display_title") == f"Acceptance {identity['version']} {marker}"
        and isinstance(repo, dict) and isinstance(head_repo, dict)
        and repo.get("full_name") == core and head_repo.get("full_name") == core
        and type(repo.get("id")) is int and repo["id"] > 0 and head_repo.get("id") == repo["id"],
        "acceptance workflow identity changed",
    )


def validate_replacement(data: dict[str, Any], run: dict[str, Any], core: str, run_id: int) -> None:
    """Independently bind a discovered or persisted fresh capacity dispatch before normal verdict."""
    intent = data.get("acceptance_capacity")
    if intent is None:
        return
    remote.require(isinstance(intent, dict) and intent.get("schema") == SCHEMA
                   and isinstance(intent.get("identity"), dict)
                   and isinstance(intent.get("marker"), str) and MARKER.fullmatch(intent["marker"])
                   and type(intent.get("failed_run")) is int and intent["failed_run"] > 0,
                   "capacity replacement intent is invalid")
    if intent["marker"] != data.get("acceptance_marker"):
        remote.require(data.get("acceptance_identity") != intent["identity"],
                       "capacity replacement marker changed")
        return  # A later independently reviewed harness change uses the ordinary replacement route.
    remote.require(type(run_id) is int and run_id > 0 and run_id != intent["failed_run"],
                   "capacity replacement is not a fresh run")
    remote.require(data.get("acceptance_identity") == intent["identity"], "capacity replacement snapshot changed")
    exact_run(run, core, intent["identity"], intent["marker"], run_id, 1)


def _receipt_artifact(rows: list[dict[str, Any]], sharded: bool) -> dict[str, Any]:
    """The one Linux receipt artifact; a sharded run may also hold its shards' receipts, never a Mac verdict."""
    if not sharded:
        remote.require(len(rows) == 1, "expected only the original Linux receipt artifact")
        return rows[0]
    allowed = {LINUX_RECEIPT} | {f"acceptance-shard-{os_name}-{name}" for os_name in ("Linux", "macOS") for name in SHARDS}
    names = [row.get("name") for row in rows]
    remote.require(names.count(LINUX_RECEIPT) == 1 and set(names) <= allowed and len(set(names)) == len(names),
                   "expected the Linux receipt and only shard receipts of the original run")
    return next(row for row in rows if row.get("name") == LINUX_RECEIPT)


def _original_linux_receipt(rel: Any, run: dict[str, Any], identity: dict[str, Any], sharded: bool = False) -> dict[str, Any]:
    run_id = run["id"]
    rows = remote.paged(f"actions/runs/{run_id}/artifacts", "artifacts")
    artifact = _receipt_artifact(rows, sharded)
    binding = artifact.get("workflow_run") or {}
    repo = run["repository"]["id"]
    remote.require(
        artifact.get("name") == "acceptance-result-Linux" and artifact.get("expired") is False
        and binding.get("id") == run_id and binding.get("head_sha") == identity["commit"]
        and binding.get("head_branch") == identity["ref"]
        and binding.get("repository_id") == repo and binding.get("head_repository_id") == repo,
        "original Linux receipt artifact identity changed",
    )
    raw = remote.download(artifact["id"])
    digest = hashlib.sha256(raw).hexdigest()
    remote.require(artifact.get("digest") == f"sha256:{digest}", "original Linux receipt artifact digest differs")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            remote.require(archive.namelist() == ["result.json"]
                           and archive.getinfo("result.json").file_size <= 1024 * 1024,
                           "unexpected original Linux receipt archive")
            result = rel.release_rejection.decode(archive.read("result.json"))
    except zipfile.BadZipFile as exc:
        raise remote.InvalidCapacity("original Linux receipt archive is invalid") from exc
    remote.require(rel.acceptance_receipt_matches(result, run_id, 1, identity, "Linux"),
                   "the original Linux result did not accept the exact published snapshot")
    return {"id": artifact["id"], "sha256": digest, "verdict": result["verdict"]}


def _hosted_linux_success(job: dict[str, Any], steps: tuple[str, ...]) -> None:
    """A Linux job of the original run that succeeded on a hosted runner and ran each named step once."""
    remote.require(job.get("conclusion") == "success" and job.get("labels") == ["ubuntu-24.04"]
                   and job.get("runner_group_id") == 0 and job.get("runner_group_name") == "GitHub Actions"
                   and str(job.get("runner_name", "")).startswith("GitHub Actions "),
                   "the original Linux acceptance job did not succeed on a hosted runner")
    remote.executed(job)
    for step in steps:
        remote.require(sum(s.get("name") == step and s.get("conclusion") == "success" for s in job["steps"]) == 1,
                       "the original Linux acceptance execution is incomplete")


def _mac_capacity(by_name: dict[str, Any], head: str) -> list[dict[str, Any]]:
    """Sharded: at least one Mac shard never acquired a runner; every other one succeeded; no Mac verdict passed.

    A Mac shard that ran and failed is a real rejection, never capacity, so it refuses the recovery.
    """
    annotations, starved = [], 0
    for name in MAC_SHARDS:
        job = by_name[name]
        if job.get("conclusion") == "success":
            remote.require(job.get("labels") == ["macos-latest"], "a Mac shard ran outside the hosted Mac ARM label")
            remote.executed(job)
            continue
        rows = remote.annotations(job)
        remote.never_acquired(job, rows, head)
        annotations += [row | {"job": job["id"]} for row in rows]
        starved += 1
    remote.require(starved > 0, "no Mac shard was starved of a hosted runner")
    remote.require(by_name[MAC].get("conclusion") in ("failure", "cancelled", "skipped"),
                   "the Mac verdict job did not fail; this is no capacity rejection")
    return annotations


def _evidence(rel: Any, identity: dict[str, Any], run_id: int, marker: str) -> dict[str, Any]:
    path = f"actions/runs/{run_id}"
    run = remote.get(f"{path}/attempts/1")
    exact_run(run, rel.CORE, identity, marker, run_id, 1)
    remote.require(run.get("status") == "completed" and run.get("conclusion") in {"failure", "cancelled"},
                   "the bound attempt is not a completed rejection")
    jobs = remote.paged(f"{path}/attempts/1/jobs", "jobs")
    names = {job.get("name") for job in jobs}
    sharded = PINS in names
    expected = SHARDED_JOBS if sharded else {LINUX, MAC}
    remote.require(len(jobs) == len(expected) and names == expected,
                   "the original acceptance jobs are incomplete or unexpected")
    by_name = {job["name"]: job for job in jobs}
    for job in jobs:
        remote.job_shape(job, run_id, identity["commit"])
        remote.require(job["run_attempt"] == 1, "acceptance job is not from the original attempt")
    if sharded:
        for name, steps in ((PINS, ("Pin the release bytes once for every shard",)),
                            *((name, ("Run the acceptance harness", "Upload the shard receipt")) for name in LINUX_SHARDS),
                            (LINUX, ("Reduce the four shard receipts of this OS", "Upload the receipt"))):
            _hosted_linux_success(by_name[name], steps)
        annotations = _mac_capacity(by_name, identity["commit"])
    else:
        _hosted_linux_success(by_name[LINUX], ("Run the acceptance harness", "Upload the receipt"))
        mac = by_name[MAC]
        annotations = remote.annotations(mac)
        remote.never_acquired(mac, annotations, identity["commit"])
    linux_receipt = _original_linux_receipt(rel, run, identity, sharded)
    latest = remote.get(path)
    exact_run(latest, rel.CORE, identity, marker, run_id, 1)
    bound = {key: run.get(key) for key in RUN_FIELDS}
    remote.require({key: latest.get(key) for key in RUN_FIELDS} == bound
                   and latest["repository"]["id"] == run["repository"]["id"],
                   "the original run changed or was rerun")
    return {"run": bound, "jobs": sorted(jobs, key=lambda job: job["id"]),
            "mac_annotations": annotations, "linux_receipt": linux_receipt}


def _receipt(rel: Any, version: str, run_id: int, attempt: int, expected: str) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    path = rel.paths()[1]
    remote.require(path == rel.tail_receipt(version), "the exact release tail is required")
    raw = rel.release_rejection.private_read(path)
    remote.require(hashlib.sha256(raw).hexdigest() == expected, "release receipt changed")
    data = rel.release_rejection.decode(raw)
    remote.require(data.get("version") == version and data.get("finished") is True and data.get("accepted") is False
                   and data.get("absorbed_by") is None and data.get("acceptance_capacity") is None,
                   "the tail is not an eligible rejected release or already has a capacity request")
    remote.require(data.get("acceptance_snapshot_mode") is True and data.get("acceptance_requested") is True
                   and data.get("acceptance_run") == run_id and data.get("acceptance_run_id") == run_id
                   and type(data.get("acceptance_run_attempt")) is int and data["acceptance_run_attempt"] == attempt,
                   "tail does not bind the original failed acceptance run and attempt")
    identity, snapshot, marker = (data.get(key) for key in ("acceptance_identity", "snapshot", "acceptance_marker"))
    remote.require(isinstance(identity, dict) and isinstance(snapshot, dict) and isinstance(marker, str)
                   and MARKER.fullmatch(marker), "tail lacks a tagged snapshot and bound attempt marker")
    tag = f"v{version}"
    commit = snapshot.get("commit", "")
    remote.require(isinstance(commit, str) and SHA.fullmatch(commit) and snapshot.get("version") == version
                   and snapshot.get("tag") == tag and data.get("acceptance_commit") == commit
                   and identity == {"version": version, "app_version": version, "ref": tag,
                                    "commit": commit, "snapshot": snapshot}
                   and all(isinstance(snapshot.get(key), str) and DIGEST.fullmatch(snapshot[key])
                           for key in ("wheel_sha256", "profile_sha256")),
                   "tail does not bind the exact tagged harness and published snapshot")
    return raw, data, identity


def recover(rel: Any, version: str, run_id: int, attempt: int, receipt_sha: str, audit_sha: str | None) -> dict[str, Any]:
    """Audit in prepare mode. Request with a reviewed digest under the exact tail lock."""
    remote.require(type(run_id) is int and run_id > 0 and type(attempt) is int and attempt == 1
                   and DIGEST.fullmatch(receipt_sha), "exact original run, attempt 1 and receipt digest required")
    raw, data, identity = _receipt(rel, version, run_id, attempt, receipt_sha)
    marker = data["acceptance_marker"]
    remote.require(rel.release_snapshot.capture(version) == identity["snapshot"]
                   and rel.release_snapshot.workflow_supports_snapshot(identity["commit"]),
                   "published snapshot or tagged harness changed")
    evidence = _evidence(rel, identity, run_id, marker)
    audit = {"schema": SCHEMA, "receipt_sha256": receipt_sha, "identity": identity, "marker": marker,
             "failed_run": run_id, "failed_attempt": attempt, "evidence": evidence}
    digest = remote.digest(audit)
    remote.require(rel.release_rejection.private_read(rel.paths()[1]) == raw, "release receipt changed during audit")
    report = {"audit_sha256": digest, "replacement_marker": None, "head": identity["commit"],
              "mac_job": next(job["id"] for job in evidence["jobs"] if job["name"] == MAC),
              "linux_artifact": evidence["linux_receipt"]["id"]}
    if audit_sha is None:
        return report
    remote.require(digest == audit_sha, "reviewed acceptance capacity audit digest differs")

    replacement = "release-" + uuid.uuid4().hex
    remote.require(replacement != marker, "replacement marker collides with original")
    history = data.get("acceptance_attempts", [])
    remote.require(isinstance(history, list) and all(isinstance(entry, dict) for entry in history)
                   and not any(entry.get("acceptance_run_id") == run_id for entry in history),
                   "original acceptance attempt is already archived or history is invalid")
    archive = {key: value for key, value in data.items() if key.startswith("acceptance") and key != "acceptance_attempts"}
    archive.update(accepted=False, verdict=["Original rejection: hosted macOS runner not acquired."],
                   receipt_sha256=receipt_sha, receipt_base64=base64.b64encode(raw).decode())
    intent = {"schema": SCHEMA, "identity": identity, "failed_run": run_id, "marker": replacement, "audit_sha256": digest,
              "at": rel.now_iso(), "audit": audit}
    updated = data | {"finished": False, "acceptance_run": None, "acceptance_run_id": None,
                      "acceptance_run_attempt": None, "acceptance_requested": True,
                      "acceptance": rel.now_iso(-5), "acceptance_marker": replacement,
                      "acceptance_attempts": [*history, archive], "acceptance_capacity": intent}
    remote.require(len(json.dumps(updated).encode()) <= rel.release_rejection.LIMIT,
                   "capacity archive would exceed the release receipt size limit")
    rel.write_state(updated)
    # Write intent before crossing the network. Resume discovers only this marker; this action never retries a POST.
    pinned = identity["snapshot"]
    code, current = rel.hzlib.gh("api", f"repos/{rel.CORE}/commits/{identity['ref']}", "--jq", ".sha")
    remote.require(code == 0 and current.strip() == identity["commit"], "tag changed before dispatch; intent retained")
    code, _ = rel.hzlib.gh(
        "workflow", "run", WORKFLOW, "-R", rel.CORE, "--ref", identity["ref"],
        "-f", f"expected_version={version}", "-f", f"expected_app_version={identity['app_version']}",
        "-f", f"expected_wheel_sha256={pinned['wheel_sha256']}",
        "-f", f"expected_profile_sha256={pinned['profile_sha256']}", "-f", f"release_attempt={replacement}",
    )
    remote.require(code == 0, "dispatch outcome uncertain; intent retained, reconcile the exact marker")
    return report | {"replacement_marker": replacement}
