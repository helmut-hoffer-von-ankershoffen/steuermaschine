"""The original exact-main capacity assessment is read-only and fails closed."""

import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from steuermaschine.defaults import FileStore
from steuermaschine.native import NativeRuntime, using
from steuermaschine.testing.fakes import MemoryStore, fake_providers
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


def execution(row, *names, retry=False):
    start = "2026-11-25T13:01:00Z" if retry else "2026-11-25T12:08:00Z"
    end = "2026-11-25T13:02:00Z" if retry else "2026-11-25T12:09:00Z"
    return row | {"runner_id": 99, "runner_name": "Hosted Agent", "started_at": start, "completed_at": end, "steps": [
        {"name": name, "conclusion": "success", "status": "completed", "started_at": start, "completed_at": end}
        for name in (names or ("Real workload",))
    ]}


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
    ready = [execution(job(10, LINUX, 1)), execution(job(11, AGG, 1)), execution(job(12, main_lanes.DISPATCH_JOB, 1))]
    other = execution(job(22, "More successful work", 2))
    listing = {"total_count": 2, "workflow_runs": [lane, push]}
    responses = {
        "git/ref/heads/main": {"object": {"sha": HEAD, "type": "commit"}},
        f"{policy.workflow_runs}?branch=main&head_sha={HEAD}&per_page=100": listing,
        "actions/runs/2": lane,
        "actions/runs/1/attempts/1/jobs?per_page=100&page=1": {"total_count": len(ready), "jobs": ready},
        "actions/runs/2/attempts/1/jobs?per_page=100&page=1": {"total_count": 3, "jobs": [hold, aggregate, other]},
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
    "product failure", "incomplete jobs", "not split", "other dispatch", "extra dependency",
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
        responses["actions/runs/2/attempts/1/jobs?per_page=100&page=2"] = {"jobs": [], "total_count": 4}
    elif defect == "not split":
        responses["actions/runs/1/attempts/1/jobs?per_page=100&page=1"]["jobs"][-1]["conclusion"] = "skipped"
    elif defect == "other dispatch":
        lane["display_title"] = f"CI {HEAD}"
    elif defect == "extra dependency":
        workflow = responses[f"contents/.github/workflows/ci.yml?ref={HEAD}"]
        workflow["content"] = base64.b64encode((WORKFLOW + "  more-work:\n    needs: ci-ok\n").encode()).decode()
    with pytest.raises((CandidateError, recovery.candidate_capacity.InvalidCapacity)):
        assess(evidence)


def prepare(store, evidence, *, head=HEAD, push=1):
    responses, _, _, _, _, lines = evidence
    log = "\n".join(f"2026-11-25T12:45:00.0000000Z {line}" for line in lines).encode()
    return recovery.prepare(store, head, push, 2, api=responses.__getitem__, read_log=lambda _: log,
                            now=datetime(2026, 11, 25, 13, tzinfo=UTC))


def retry_rows(evidence, *, carrier=False):
    responses, listing, _, _, _, _ = evidence
    lane = responses["actions/runs/2"]
    new = lane | {"run_attempt": 2, "conclusion": "success", "triggering_actor": {"id": 100},
                  "previous_attempt_url": f"https://api.github.com/repos/{hzlib.CORE}/actions/runs/2/attempts/1"}
    listing["workflow_runs"][0] = new
    responses["actions/runs/2/attempts/2"] = new
    replacement = execution(
        job(30, MACOS, 2) | {"run_attempt": 2},
        "Install Python 3.15 dependencies with the verified PyYAML compatibility build",
        "Unit and integration tests (unit first, 4 workers, stop at the first failure)", retry=True,
    )
    aggregate = execution(job(31, AGG, 2) | {"run_attempt": 2}, GUARD, DIRECT, retry=True)
    jobs = [replacement, aggregate]
    if carrier:
        old = responses["actions/runs/2/attempts/1/jobs?per_page=100&page=1"]["jobs"][-1]
        jobs.append(old | {"id": 32, "run_attempt": 2})
    responses["actions/runs/2/attempts/2/jobs?per_page=100&page=1"] = {"total_count": len(jobs), "jobs": jobs}
    return new, jobs


def test_intent_is_exact_create_once_and_never_sends(evidence, tmp_path):
    saved = prepare(FileStore(tmp_path), evidence)
    store = FileStore(tmp_path)
    assert (saved["expected_attempt"], saved["status"]) == (2, "prepared")
    assert saved["request_path"] == f"repos/{hzlib.CORE}/actions/jobs/20/rerun"
    assert prepare(store, evidence) == saved
    with pytest.raises(CandidateError, match="conflicting original request"):
        prepare(store, evidence, head="b" * 40)
    with pytest.raises(CandidateError, match=r"actual-main CI run 2 .* failure"):
        recovery.inspect_attempt(store, 2, api=evidence[0].__getitem__)


@pytest.mark.parametrize("carrier", [False, True])
def test_completed_retry_stays_diagnostic_without_request_provenance(evidence, carrier):
    store = MemoryStore()
    saved = prepare(store, evidence)
    retry_rows(evidence, carrier=carrier)
    result = recovery.inspect_attempt(store, 2, api=evidence[0].__getitem__)
    assert result == recovery.ObservedMainRetry(HEAD, 2, 30, 31, saved["intent_sha256"])
    assert store.get_receipt(recovery.INTENT, "2") == saved


@pytest.mark.parametrize("defect", [
    "new main", "new run", "another attempt", "duplicate", "product failure", "skipped target step",
    "missing aggregate judge", "changed original", "changed carrier", "receipt tampering",
])
def test_retry_reader_refuses_ambiguous_or_nonexecuting_evidence(evidence, defect):
    store = MemoryStore()
    saved = prepare(store, evidence)
    run, jobs = retry_rows(evidence, carrier=True)
    responses, listing, _, _, _, _ = evidence
    if defect == "new main":
        responses["git/ref/heads/main"]["object"]["sha"] = "b" * 40
    elif defect == "new run":
        listing["workflow_runs"].append(run | {"id": 8})
        listing["total_count"] += 1
    elif defect == "another attempt":
        run["run_attempt"] = 3
    elif defect == "duplicate":
        jobs[0]["id"] = jobs[1]["id"]
    elif defect == "product failure":
        jobs[0]["conclusion"] = "failure"
    elif defect == "skipped target step":
        jobs[0]["steps"][1]["conclusion"] = "skipped"
    elif defect == "missing aggregate judge":
        jobs[1]["steps"].pop()
    elif defect == "changed original":
        responses["actions/runs/2/attempts/1/jobs?per_page=100&page=1"]["jobs"][-1]["runner_id"] = 40
    elif defect == "changed carrier":
        jobs[-1]["runner_name"] = "Another runner"
    else:
        store.receipts[(recovery.INTENT, "2")] = saved | {"request_path": "/invalid"}
    with pytest.raises((CandidateError, recovery.candidate_capacity.InvalidCapacity)):
        recovery.inspect_attempt(store, 2, api=responses.__getitem__)


@pytest.mark.parametrize("acknowledged", [True, False, None])
def test_request_is_recorded_before_one_exact_post_and_never_repeated(evidence, tmp_path, acknowledged):
    store = FileStore(tmp_path)
    saved = prepare(store, evidence)
    sent = []
    responses, _, _, _, _, lines = evidence
    log = "\n".join(f"2026-11-25T12:45:00.0000000Z {line}" for line in lines).encode()

    def post(path):
        assert store.get_receipt(recovery.REQUEST, "2")["path"] == path
        sent.append(path)
        if acknowledged is None:
            raise TimeoutError("connection closed after request")
        return acknowledged

    def request():
        return recovery.request_once(store, 2, saved["intent_sha256"], api=responses.__getitem__,
                                     read_log=lambda _: log, now=datetime(2026, 11, 25, 13, tzinfo=UTC), post=post)

    if acknowledged:
        assert request()["attempt_origin"] == "unproven"
    else:
        with pytest.raises(CandidateError, match="request outcome ambiguous"):
            request()
        assert store.get_receipt(recovery.REQUEST, "2-ack") is None
    with pytest.raises(CandidateError, match="request already intended"):
        request()
    assert sent == [f"repos/{hzlib.CORE}/actions/jobs/20/rerun"]
    retry_rows(evidence)
    assert recovery.inspect_attempt(store, 2, api=responses.__getitem__).attempt_origin == "unproven"

def test_queue_recovery_is_scoped_to_the_existing_exact_pin(evidence, monkeypatch):
    from steuermaschine_heinzel.native.queue_lib import queue_daemon as queue

    store = MemoryStore()
    prepare(store, evidence)
    retry_rows(evidence)
    pin = {"databaseId": 2, "attempt": 1, "headSha": HEAD, "event": "workflow_dispatch",
           "displayTitle": f"CI macOS main {HEAD}", "status": "completed", "conclusion": "failure"}
    monkeypatch.setattr(queue, "load", lambda: {"main_failure": pin})
    monkeypatch.setattr(queue.main_checkpoint, "get", evidence[0].__getitem__)
    monkeypatch.setattr(queue, "update", lambda **_: pytest.fail("must retain original pin"))
    rt = NativeRuntime(fake_providers(store=store), {}, None)
    with using(rt):
        assert queue.recovered_main_capacity()
        pin["headSha"] = "b" * 40
        assert not queue.recovered_main_capacity()
        pin["headSha"] = HEAD
        pin["event"] = "schedule"
        assert not queue.recovered_main_capacity()

def test_exact_release_base_uses_verified_attempt_two_without_touching_candidate(evidence, monkeypatch):
    from steuermaschine_heinzel.native.release_lib import exact_queue

    store = MemoryStore()
    prepare(store, evidence)
    _, jobs = retry_rows(evidence)
    monkeypatch.setattr(exact_queue, "_api", evidence[0].__getitem__)
    monkeypatch.setattr(exact_queue, "store", lambda: store)
    monkeypatch.setattr(exact_queue.core_facts, "ci_file", lambda: "ci.yml")
    monkeypatch.setattr(exact_queue.hzlib, "ghj", lambda *_, **__: [
        {"databaseId": 1, "event": "push", "headSha": HEAD, "attempt": 1, "status": "completed", "conclusion": "success"},
        {"databaseId": 2, "event": "workflow_dispatch", "displayTitle": f"CI macOS main {HEAD}",
         "headSha": HEAD, "attempt": 2, "status": "completed", "conclusion": "success"},
    ])
    assert exact_queue.base_ready(HEAD)
    assert not exact_queue.base_ready("b" * 40)
    jobs[0]["steps"][1]["conclusion"] = "skipped"
    assert not exact_queue.base_ready(HEAD)
