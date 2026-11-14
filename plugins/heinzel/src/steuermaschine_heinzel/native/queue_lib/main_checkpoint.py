"""Merge-only continuity from a fully verified merged release and focused main segments.

The stored configuration identifies evidence; it is never a success receipt.
Every decision re-authenticates that evidence. Nothing here merges, clears pins,
changes holds, or declares published acceptance/live verification.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from time import monotonic_ns
from typing import Callable

from . import hzlib
from . import release_candidate as rc
from . import core_policy
from . import job_proof
from . import edge  # moved: records, processes and what used to be separate scripts

MAX_SEGMENTS = 20
MAX_CONFIG = 8192
MAX_REGISTRY = 48 * 1024
MAX_ARCHIVE = 1024 * 1024
MAX_JOB_LOG = 128 * 1024 * 1024  # a job log is read only to prove a success job with empty steps ran


def require(condition, reason):
    if not condition:
        raise rc.CandidateError(reason)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def checkpoint_event(event):
    try:
        print(json.dumps(event), file=sys.stderr, flush=True)
    except (OSError, ValueError):
        # A lost timing sample must not change the proof or its refusal.
        pass


@contextmanager
def timed_phase(phase: str, *, segment: int | None = None, report=checkpoint_event):
    """Report elapsed time, including refusal paths, without granting authority."""
    start = monotonic_ns()
    try:
        yield
    finally:
        event = {
            "event": "queue.checkpoint.phase",
            "phase": phase,
            "elapsed_ms": (monotonic_ns() - start) // 1_000_000,
        }
        if segment is not None:
            event["segment"] = segment
        report(event)


def timed_reader(verify_segment, args, number, samples):
    with timed_phase("isolated_reader", segment=number, report=samples.append):
        return verify_segment(*args)


def config_path(state: Path) -> Path:
    return state.with_suffix(".checkpoint.json")


def read_config(state: Path) -> dict | None:
    path = config_path(state)
    if not path.exists():
        return None
    require(
        not path.is_symlink() and path.stat().st_size <= MAX_CONFIG,
        "unsafe checkpoint configuration",
    )
    config = json.loads(path.read_bytes(), object_pairs_hook=rc.unique_object)
    require(
        isinstance(config, dict)
        and set(config) == {"schema", "pr", "run", "core_repo"}
        and type(config["schema"]) is int
        and config["schema"] == 2  # 1 carried source pins: Core's commit and its verdict bind the sources now
        and type(config["pr"]) is int
        and config["pr"] > 0
        and type(config["run"]) is int
        and config["run"] > 0
        and isinstance(config["core_repo"], str)
        and Path(config["core_repo"]).is_absolute(),
        "checkpoint configuration differs from independently reviewed source policy",
    )
    return config


def get(path: str):
    value = hzlib.ghj("api", f"repos/{hzlib.CORE}/{path}", default=None)
    require(value is not None, "checkpoint GitHub evidence unavailable")
    return value


def download(path: str, limit: int) -> bytes:
    """Bound binary output while gh follows the authenticated artifact redirect."""
    with tempfile.TemporaryFile() as output:
        process = edge.run(
            ["gh", "api", f"repos/{hzlib.CORE}/{path}"],
            stdout=output,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        require(
            process.returncode == 0 and output.tell() <= limit,
            "checkpoint archive unavailable or oversized",
        )
        output.seek(0)
        return output.read(limit + 1)


def merged_candidate(config: dict, api: Callable, read: Callable) -> dict:
    """Authenticate historical MERGED release evidence without pretending the PR is open."""
    run_id, number = config["run"], config["pr"]
    run, pr = api(f"actions/runs/{run_id}"), api(f"pulls/{number}")
    head, squash = pr.get("head", {}).get("sha"), pr.get("merge_commit_sha")
    require(
        rc.valid_sha(head) and rc.valid_sha(squash),
        "release head or actual squash missing",
    )
    # The workflow and the inventory are Core's reviewed release policy at the exact release head; a release whose
    # head has no policy (older than the manifest) cannot be a checkpoint.
    policy = core_policy.at_commit_api(api, head)
    require(
        pr.get("number") == number
        and pr.get("state") == "closed"
        and pr.get("merged") is True
        and pr.get("draft") is False
        and isinstance(pr.get("merged_at"), str),
        "release is not authenticated merged PR",
    )
    version = rc.release_version("pull_request", hzlib.CORE, "", {"pull_request": pr})
    require(
        version and run.get("head_branch") == f"release/v{version}",
        "noncanonical merged release",
    )
    require(
        run.get("id") == run_id
        and type(run.get("run_attempt")) is int
        and run["run_attempt"] == 1
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("event") == "pull_request"
        and run.get("head_sha") == head
        and run.get("repository", {}).get("full_name") == hzlib.CORE
        and run.get("head_repository", {}).get("full_name") == hzlib.CORE
        and run.get("path") == policy.workflow,
        "release checkpoint requires genuine original candidate success",
    )
    require(
        isinstance(run.get("created_at"), str)
        and isinstance(run.get("updated_at"), str),
        "candidate timestamps missing",
    )
    require(
        (
            datetime.fromisoformat(run["updated_at"])
            - datetime.fromisoformat(run["created_at"])
        ).total_seconds()
        >= 0,
        "candidate timestamps reversed",
    )
    original = api(f"actions/runs/{run_id}/attempts/1")
    require(
        all(
            original.get(key) == run.get(key)
            for key in (
                "id",
                "head_sha",
                "run_attempt",
                "event",
                "path",
                "repository",
                "head_repository",
                "status",
                "conclusion",
            )
        ),
        "candidate original attempt identity changed",
    )
    artifacts = api(f"actions/runs/{run_id}/artifacts?per_page=100")
    require(
        type(artifacts.get("total_count")) is int
        and artifacts["total_count"] == len(artifacts.get("artifacts", [])),
        "truncated candidate artifacts",
    )
    matches = [
        item for item in artifacts["artifacts"] if item.get("name") == rc.ARTIFACT
    ]
    require(len(matches) == 1, "candidate source artifact missing or duplicated")
    artifact = matches[0]
    require(
        artifact.get("expired") is False
        and type(artifact.get("id")) is int
        and artifact["id"] > 0
        and artifact.get("workflow_run", {}).get("id") == run_id
        and artifact.get("workflow_run", {}).get("head_sha") == head,
        "wrong candidate source artifact",
    )
    archive = read(f"actions/artifacts/{artifact['id']}/zip", MAX_ARCHIVE)
    require(
        len(archive) <= MAX_ARCHIVE
        and artifact.get("digest") == "sha256:" + hashlib.sha256(archive).hexdigest()
        and artifact.get("size_in_bytes") == len(archive),
        "candidate immutable artifact digest mismatch",
    )
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        require(
            zipped.namelist() == [rc.PROOF_FILE]
            and zipped.getinfo(rc.PROOF_FILE).file_size <= rc.MAX_PROOF,
            "unexpected candidate source archive",
        )
        proof = json.loads(
            zipped.read(rc.PROOF_FILE), object_pairs_hook=rc.unique_object
        )
    require(
        isinstance(proof, dict)
        and all(
            rc.valid_sha(proof.get(key))
            for key in ("source_commit", "head", "base", "tree")
        ),
        "malformed candidate source proof",
    )
    require(
        proof.get("schema") == 1
        and proof.get("repository") == hzlib.CORE
        and proof.get("event") == "pull_request"
        and proof.get("pr") == number
        and proof.get("version") == version
        and proof.get("run_id") == run_id
        and proof.get("run_attempt") == 1
        and proof["head"] == head
        and proof["base"] == pr.get("base", {}).get("sha"),
        "candidate source identity or reviewed pins differ",
    )
    source, released, merged = (
        api(f"commits/{sha}") for sha in (proof["source_commit"], head, squash)
    )
    for row, sha in (
        (source, proof["source_commit"]),
        (released, head),
        (merged, squash),
    ):
        require(
            row.get("sha") == sha
            and row.get("commit", {}).get("tree", {}).get("sha") == proof["tree"],
            "candidate and actual release squash trees differ",
        )
    require(
        [p.get("sha") for p in source.get("parents", [])] == [proof["base"], head]
        and [p.get("sha") for p in merged.get("parents", [])] == [proof["base"]],
        "release source or squash parent differs",
    )
    jobs = rc.quarantine_lane([job_proof.log_proven(job, run_id, lambda job_id: read(f"actions/jobs/{job_id}/logs", MAX_JOB_LOG))
                               for job in rc.job_inventory(api, run_id)], run_id, head, policy)
    inventory = policy.expected_jobs("pull_request")
    require(
        len(jobs) == len(inventory)
        and {job.get("name") for job in jobs} == set(inventory),
        "candidate workload inventory differs",
    )
    for job in jobs:
        require(
            job.get("run_id") == run_id
            and job.get("run_attempt") == 1
            and job.get("head_sha") == head
            and job.get("status") == "completed"
            and job.get("conclusion") == inventory[job["name"]],
            "candidate workload failed, changed, skipped or reran",
        )
        if inventory[job["name"]] == "success":
            require(
                type(job.get("runner_id")) is int
                and job["runner_id"] > 0
                and job.get("steps"),
                "candidate workload never started",
            )
    require(
        api(f"actions/runs/{run_id}") == run and api(f"pulls/{number}") == pr,
        "candidate changed during validation",
    )
    return {
        "head": squash,
        "tree": proof["tree"],
        "base": proof["base"],
        "run": run_id,
        "candidate_head": head,
        "artifact_sha256": hashlib.sha256(archive).hexdigest(),
        "jobs": len(jobs),
    }


def main_run(api: Callable, head: str) -> int:
    data = api(f"actions/workflows/ci.yml/runs?head_sha={head}&event=push&per_page=1")
    rows = data.get("workflow_runs", [])
    require(len(rows) == 1, "missing or ambiguous resulting-main workflow")
    run = rows[0]
    require(
        run.get("head_sha") == head
        and run.get("head_branch") == "main"
        and run.get("event") == "push"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and type(run.get("run_attempt")) is int
        and run["run_attempt"] == 1
        and type(run.get("id")) is int
        and run["id"] > 0,
        "latest resulting-main attempt is not successful original evidence",
    )
    return run["id"]


def actual_main_ci(api: Callable, head: str, push_id: int) -> dict:
    """Bind the actual-main CI runs across events to the canonical push proof."""
    data = api(f"actions/workflows/ci.yml/runs?branch=main&head_sha={head}&per_page=100")
    require(isinstance(data, dict), "invalid actual-main CI response")
    rows = data.get("workflow_runs")
    require(
        isinstance(rows, list)
        and type(data.get("total_count")) is int
        and 0 < len(rows) == data["total_count"] <= 100,
        "incomplete actual-main CI run inventory",
    )
    runs = {}
    seen = set()
    for row in rows:
        require(isinstance(row, dict), "invalid actual-main CI run")
        identifier, event = row.get("id"), row.get("event")
        repository, source = row.get("repository"), row.get("head_repository")
        require(
            type(identifier) is int and identifier > 0 and identifier not in seen
            and row.get("head_sha") == head and row.get("head_branch") == "main"
            and row.get("path") == rc.WORKFLOW
            and isinstance(repository, dict) and repository.get("full_name") == rc.REPOSITORY,
            "mismatched actual-main CI run identity",
        )
        seen.add(identifier)
        if event == "pull_request":
            continue  # A PR can have a source branch called main.
        workflow, attempt = row.get("workflow_id"), row.get("run_attempt")
        status, conclusion, created = row.get("status"), row.get("conclusion"), row.get("created_at")
        require(
            event in ("push", "schedule", "workflow_dispatch")
            and isinstance(source, dict) and source.get("full_name") == rc.REPOSITORY
            and type(workflow) is int and workflow > 0 and type(attempt) is int and attempt > 0
            and status in ("completed", "queued", "in_progress", "waiting", "requested", "pending")
            and (isinstance(conclusion, str) if status == "completed" else conclusion is None)
            and isinstance(created, str),
            "invalid actual-main CI run metadata",
        )
        created_at = datetime.fromisoformat(created)
        require(created_at.utcoffset() is not None, "actual-main CI run timestamp has no timezone")
        runs[identifier] = (workflow, event, attempt, status, conclusion, created_at)
    require(push_id in runs, "canonical push absent from actual-main CI")
    workflow, event, attempt, status, conclusion, push_created = runs[push_id]
    require(
        (event, attempt, status, conclusion) == ("push", 1, "completed", "success")
        and all(run[0] == workflow for run in runs.values()),
        "actual-main CI disagrees with the canonical push",
    )
    for identifier, (_, event, _, status, conclusion, run_created) in runs.items():
        require(
            run_created < push_created or (status, conclusion) == ("completed", "success"),
            f"actual-main CI run {identifier} ({event}) at or after the canonical push is {conclusion or status}",
        )
    return runs


def selected_main(api: Callable, registry: dict, head: str) -> tuple[dict, str]:
    commit = api(f"commits/{head}")
    tree = commit.get("commit", {}).get("tree", {}).get("sha")
    require(
        commit.get("sha") == head and rc.valid_sha(tree),
        "main commit identity unavailable",
    )
    plan = registry.get(f"push:{head}", registry.get(f"push-tree:{tree}"))
    require(
        isinstance(plan, dict)
        and rc.valid_sha(plan.get("base"))
        and plan["base"] != head,
        "missing exact resulting-main manifest",
    )
    return plan, tree


def verify_progress(
    config: dict,
    pin: dict,
    registry: dict,
    api: Callable,
    read: Callable,
    verify_segment: Callable,
    *,
    check_registry: Callable[[str], None] | None = None,
) -> dict:
    """Re-authenticate a full checkpoint and every contiguous focused interval."""
    require(
        rc.valid_sha(pin.get("headSha")), "missing retained original failure identity"
    )
    with timed_phase("tip_snapshot"):
        tip = api("git/ref/heads/main")["object"]["sha"]
        require(rc.valid_sha(tip), "invalid current main")
        tip_run = main_run(api, tip)
        tip_ci = actual_main_ci(api, tip, tip_run)
    with timed_phase("candidate_anchor"):
        anchor = merged_candidate(config, api, read)
        ancestry = api(f"compare/{pin['headSha']}...{anchor['head']}")
        require(
            ancestry.get("status") == "ahead"
            and ancestry.get("merge_base_commit", {}).get("sha") == pin["headSha"],
            "release checkpoint does not descend from the retained incident",
        )
        # The actual release squash also needs its own exact focused main proof.
        anchor_plan, anchor_tree = selected_main(api, registry, anchor["head"])
        require(
            anchor_plan["base"] == anchor["base"] and anchor_tree == anchor["tree"],
            "release main scope differs from full checkpoint",
        )
    with timed_phase("chain_discovery"):
        links = []
        cursor = tip
        seen = set()
        while cursor != anchor["head"]:
            require(
                cursor not in seen and len(links) < MAX_SEGMENTS,
                "checkpoint chain cycles or exceeds bound",
            )
            seen.add(cursor)
            plan, tree = selected_main(api, registry, cursor)
            comparison = api(f"compare/{plan['base']}...{cursor}")
            require(
                comparison.get("status") == "ahead"
                and comparison.get("merge_base_commit", {}).get("sha") == plan["base"],
                "focused segment is not an ancestor-bound interval",
            )
            require(
                api(f"compare/{anchor['head']}...{plan['base']}").get("status")
                in {"identical", "ahead"},
                "focused interval skips before the release checkpoint",
            )
            links.append((cursor, plan["base"], tree, main_run(api, cursor), plan))
            cursor = plan["base"]
        links.append(
            (
                anchor["head"],
                anchor_plan["base"],
                anchor_tree,
                main_run(api, anchor["head"]),
                anchor_plan,
            )
        )
    proofs = []
    ordered = list(reversed(links))
    with ThreadPoolExecutor(max_workers=2) as readers:
        for offset in range(0, len(ordered), 2):
            pair = ordered[offset:offset + 2]
            pending = []
            launch_error = None
            for number, link in enumerate(pair, offset + 1):
                head, base, _, run, _ = link
                args = (config["core_repo"], tip, head, base, run, registry)
                samples = []
                try:
                    future = readers.submit(timed_reader, verify_segment, args, number, samples)
                except RuntimeError as error:
                    launch_error = error
                    pending.append((link, None, samples))
                    # submit can enqueue work before thread startup fails.
                    readers.shutdown(wait=True)
                    break
                pending.append((link, future, samples))
            # A refusal can leave one sibling in flight. Settle both before
            # reporting in lineage order. A refusal stops further pairs.
            wait([future for _, future, _ in pending if future is not None])
            for _, _, samples in pending:
                for sample in samples:
                    checkpoint_event(sample)
            try:
                for (head, base, tree, run, plan), future, _ in pending:
                    if future is None:
                        raise rc.CandidateError(
                            "protected focused reader could not start"
                        ) from launch_error
                    proof = future.result()
                    require(
                        proof
                        == {
                            "type": "focused-main/v1",
                            "base": base,
                            "head": head,
                            "tree": tree,
                            "run": run,
                            "attempt": 1,
                            "manifest_sha256": hashlib.sha256(canonical(plan)).hexdigest(),
                        },
                        "protected focused reader returned mismatched evidence",
                    )
                    proofs.append(proof)
            except Exception:
                if len(pair) == 2:
                    checkpoint_event({
                        "event": "queue.checkpoint.pair_refused",
                        "first_segment": offset + 1,
                        "max_in_flight_siblings": 1,
                    })
                raise
    if check_registry is not None:
        check_registry(tip)
    with timed_phase("final_main_guard"):
        require(
            main_run(api, tip) == tip_run
            and actual_main_ci(api, tip, tip_run) == tip_ci
            and api("git/ref/heads/main")["object"]["sha"] == tip,
            "main or latest workflow advanced during checkpoint verification",
        )
    return {
        "type": "merged-release-focused-continuity/v1",
        "head": tip,
        "anchor": anchor,
        "segments": proofs,
        "incident": {"head": pin["headSha"], "run": pin.get("databaseId")},
        "authority": "merges only; published acceptance and live state are not inferred",
    }


def isolated_reader(request):
    result = edge.run(
        [
            "uv",
            "run",
            "--no-project",
            "--no-build",
            "--with",
            "coverage==7.17.0",
            "python",
            "-I",
            str(Path(__file__).with_name("main_checkpoint_reader.py")),
        ],
        input=canonical(request),
        capture_output=True,
        timeout=180,
        check=False,
    )
    require(
        result.returncode == 0 and len(result.stdout) <= 5 * 1024 * 1024,
        "protected focused reader refused: "
        + result.stderr.decode("utf-8", "replace")[-1500:],
    )
    return json.loads(result.stdout, object_pairs_hook=rc.unique_object)


def isolated_segment(core_repo, reader_head, head, base, run, registry):
    return isolated_reader(
        {
            "core_repo": core_repo,
            "reader_head": reader_head,
            "head": head,
            "base": base,
            "run": run,
            "registry": registry,
        }
    )


def transports():
    data = get("actions/variables?per_page=100")
    rows = data.get("variables", [])
    require(
        type(data.get("total_count")) is int
        and data["total_count"] == len(rows) <= 100,
        "incomplete focused variable inventory",
    )
    require(
        len({row["name"] for row in rows}) == len(rows), "duplicate repository variable"
    )
    values = {row["name"]: row["value"] for row in rows}
    names = ["FOCUSED_CI_PLANS", *(f"FOCUSED_CI_PLANS_V2_{i}" for i in range(4))]
    result = {name: values.get(name, "") for name in names}
    require(
        all(
            isinstance(value, str)
            and len(value.encode()) <= (MAX_REGISTRY if name == names[0] else 40000)
            for name, value in result.items()
        ),
        "focused variable exceeds transport bound",
    )
    return result


def verify_configured(config, pin):
    with timed_phase("registry_snapshot"):
        before = transports()
        reader = get("git/ref/heads/main")["object"]["sha"]
    with timed_phase("registry_decode"):
        registry = isolated_reader(
            {
                "mode": "registry",
                "core_repo": config["core_repo"],
                "reader_head": reader,
                "legacy": before["FOCUSED_CI_PLANS"],
                "slots": [before[f"FOCUSED_CI_PLANS_V2_{i}"] for i in range(4)],
            }
        )

    def check_registry(tip):
        with timed_phase("final_registry_guard"):
            require(
                tip == reader and transports() == before,
                "protected source or registry changed during verification",
            )

    return verify_progress(
        config, pin, registry, get, download, isolated_segment, check_registry=check_registry
    )


def establish(state, pin, number, run, core_repo, *, apply=False):
    """Official verifier command: write only configuration after a fresh complete proof.

    Caller must hold the queue lock. An edited configuration never grants progress:
    permits() independently revalidates all authenticated evidence on each use.
    """
    require(not hzlib.merge_frozen(), "merge freeze or release hold is present")
    config = {
        "schema": 2,
        "pr": number,
        "run": run,
        "core_repo": str(Path(core_repo).resolve()),
    }
    proof = verify_configured(config, pin)
    with timed_phase("final_hold_guard"):
        require(not hzlib.merge_frozen(), "merge freeze or release hold appeared")
    if apply:
        target = config_path(state)
        require(
            not target.is_symlink() and not target.parent.is_symlink(),
            "unsafe checkpoint configuration path",
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=".checkpoint-", delete=False
        ) as output:
            temp = Path(output.name)
            output.write(canonical(config) + b"\n")
        try:
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
    return proof


def permits(state: Path, pin: dict) -> bool:
    """Fail closed without mutating queue state; retain exact incident evidence."""
    if not config_path(state).exists() and not config_path(state).is_symlink():
        return False
    print(json.dumps({"event": "queue.checkpoint.start"}), file=sys.stderr, flush=True)
    try:
        require(not hzlib.merge_frozen(), "merge freeze or release hold is present")
        config = read_config(state)
        require(config is not None, "checkpoint configuration disappeared")
        proof = verify_configured(config, pin)
        with timed_phase("final_hold_guard"):
            require(
                not hzlib.merge_frozen(),
                "merge freeze or release hold appeared during proof",
            )
        print(
            json.dumps(
                {
                    "event": "queue.checkpoint.outcome",
                    "outcome": "ok",
                    "head": proof["head"],
                    "anchor": proof["anchor"]["head"],
                    "segments": len(proof["segments"]),
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        return True
    except (
        ValueError,
        KeyError,
        TypeError,
        OSError,
        subprocess.SubprocessError,
        zipfile.BadZipFile,
    ) as error:
        print(
            json.dumps(
                {
                    "event": "queue.checkpoint.outcome",
                    "outcome": "refused",
                    "reason": str(error),
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        return False
