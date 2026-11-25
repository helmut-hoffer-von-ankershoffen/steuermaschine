"""The original exact-main capacity assessment is read-only and fails closed."""

import base64
import json
from types import SimpleNamespace

import pytest

from steuermaschine_heinzel.native.queue_lib import hzlib, main_lanes
from steuermaschine_heinzel.native.queue_lib import main_capacity_recovery as recovery
from steuermaschine_heinzel.native.queue_lib.release_candidate import CandidateError

HEAD = "a" * 40
LINUX = "Test (ubuntu-24.04, Python 3.15, integration)"
MACOS = "Test (macos-latest, Python 3.15, integration)"
AGG = "CI OK"
GUARD = "Fail if any required job failed, naming each with its verdict"
DIRECT = "Require the complete direct CI workload"
WORKFLOW = r'''name: CI
jobs:
  test:
    name: Test (${{ matrix.os }}, Python ${{ matrix.python }}, ${{ matrix.level }})
  ci-ok:
    name: CI OK
    needs: [test]
    steps:
      - name: Fail if any required job failed, naming each with its verdict
        env:
          NEEDS: ${{ toJSON(needs) }}
        run: |
          jq -r 'to_entries[] | select(.value.result != "success" and .value.result != "skipped")
            | "::error::Required job \(.key) ended with \(.value.result)"' <<< "$NEEDS"
          for result in $RESULTS; do
            *) echo "A required job ended with: $result"; exit 1 ;;
      - name: Require the complete direct CI workload
'''
COMMAND = next(line.strip() for line in WORKFLOW.splitlines() if "jq -r" in line)


def job(identifier, name, run, conclusion="success", **extra):
    return {
        "id": identifier, "name": name, "run_id": run, "run_attempt": 1, "head_sha": HEAD,
        "status": "completed", "conclusion": conclusion, **extra,
    }


@pytest.fixture
def evidence(monkeypatch):
    policy = SimpleNamespace(aggregate=AGG, test_jobs=frozenset({LINUX, MACOS}),
                             workflow=".github/workflows/ci.yml", workflow_runs="actions/workflows/ci.yml/runs")
    monkeypatch.setattr(recovery.core_policy, "at_commit_api", lambda *_: policy)
    hosted = recovery.candidate_capacity
    hold = job(20, MACOS, 2, "cancelled", runner_id=0, runner_name="", steps=[], labels=["macos-latest"],
               check_run_url=f"https://api.github.com/repos/{hzlib.CORE}/check-runs/20")
    aggregate = job(21, AGG, 2, "failure", steps=[
        {"number": i, "name": name, "status": "completed", "conclusion": verdict}
        for i, (name, verdict) in enumerate((
            ("Set up job", "success"), (GUARD, "failure"), (DIRECT, "skipped"), ("Complete job", "success"),
        ), 1)
    ])
    notes = [
        {"path": ".github", "blob_href": f"https://github.com/{hzlib.CORE}/blob/{HEAD}/.github",
         "annotation_level": level, "message": message}
        for level, message in (("failure", hosted.NOT_ACQUIRED), ("notice", hosted.CAPACITY))
    ]
    repo = {"full_name": hzlib.CORE}
    bound = {"workflow_id": 42, "head_sha": HEAD, "head_branch": "main", "path": policy.workflow,
             "run_attempt": 1, "status": "completed", "repository": repo, "head_repository": repo}
    push = bound | {"id": 1, "event": "push", "conclusion": "success", "created_at": "2026-11-25T12:00:00Z"}
    lane = bound | {"id": 2, "event": "workflow_dispatch", "conclusion": "failure",
                    "created_at": "2026-11-25T12:01:00Z", "display_title": f"CI macOS main {HEAD}"}
    ready = [job(10, LINUX, 1), job(11, AGG, 1), job(12, main_lanes.DISPATCH_JOB, 1)]
    listing = {"total_count": 2, "workflow_runs": [lane, push]}
    responses = {
        "git/ref/heads/main": {"object": {"sha": HEAD}},
        f"{policy.workflow_runs}?branch=main&head_sha={HEAD}&per_page=100": listing,
        "actions/runs/2": lane,
        "actions/runs/1/attempts/1/jobs?per_page=100&page=1": {"total_count": len(ready), "jobs": ready},
        "actions/runs/2/attempts/1/jobs?per_page=100&page=1": {"total_count": 2, "jobs": [hold, aggregate]},
        "check-runs/20/annotations?per_page=100&page=1": notes,
        f"contents/{policy.workflow}?ref={HEAD}": {"type": "file", "encoding": "base64", "content": base64.b64encode(WORKFLOW.encode()).decode()},
    }
    log = [
        f"##[group]Run {COMMAND}", "  NEEDS: " + json.dumps({"test": {"result": "abandoned"}}, indent=2),
        "##[endgroup]", "##[error]Required job test ended with abandoned",
        "A required job ended with: abandoned", "##[error]Process completed with exit code 1.",
    ]
    return responses, listing, hold, aggregate, notes, log


def assess(evidence):
    responses, _, _, _, _, lines = evidence
    log = "\n".join(f"2026-11-25T12:45:00.0000000Z {line}" for line in lines).encode()
    return recovery.assess(HEAD, 1, 2, api=responses.__getitem__, read_log=lambda _: log)


def test_original_exact_pair_produces_only_a_read_only_assessment(evidence):
    result = assess(evidence)
    assert result == assess(evidence)
    assert (result.head, result.push_run, result.macos_run, result.aggregate_job, result.jobs) == (HEAD, 1, 2, 21, (20,))
    assert len(result.inventory_sha256) == 64


@pytest.mark.parametrize("defect", [
    "head", "extra run", "attempt", "runner", "steps", "annotation", "log", "aggregate",
    "product failure", "incomplete jobs", "not split", "other dispatch",
])
def test_ambiguous_or_product_failures_produce_no_assessment(evidence, defect):
    responses, listing, hold, aggregate, notes, log = evidence
    lane = responses["actions/runs/2"]
    mac_jobs = responses["actions/runs/2/attempts/1/jobs?per_page=100&page=1"]
    if defect == "head":
        responses["git/ref/heads/main"]["object"]["sha"] = "b" * 40
    elif defect == "extra run":
        listing["total_count"] += 1
        listing["workflow_runs"].append(lane | {"id": 3})
    elif defect == "attempt":
        lane["run_attempt"] = 2
    elif defect == "runner":
        hold["runner_id"] = 5
    elif defect == "steps":
        hold["steps"] = [{"conclusion": "failure"}]
    elif defect == "annotation":
        notes[0]["message"] = "User cancelled workflow"
    elif defect == "log":
        log[1] = "  NEEDS: " + json.dumps({"test": {"result": "success"}}, indent=2)
    elif defect == "aggregate":
        aggregate["steps"][2]["conclusion"] = "failure"
    elif defect == "product failure":
        mac_jobs["jobs"].append(job(22, "Test (macos-latest, Python 3.15, unit)", 2, "failure"))
        mac_jobs["total_count"] += 1
    elif defect == "incomplete jobs":
        mac_jobs["total_count"] += 1
        responses["actions/runs/2/attempts/1/jobs?per_page=100&page=2"] = {"jobs": [], "total_count": 3}
    elif defect == "not split":
        responses["actions/runs/1/attempts/1/jobs?per_page=100&page=1"]["jobs"][-1]["conclusion"] = "skipped"
    elif defect == "other dispatch":
        lane["display_title"] = f"CI {HEAD}"
    with pytest.raises((CandidateError, recovery.candidate_capacity.InvalidCapacity)):
        assess(evidence)
