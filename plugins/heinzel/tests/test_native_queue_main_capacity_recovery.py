"""The original exact-main capacity assessment is read-only and fails closed."""

import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from steuermaschine.defaults import FileStore
from steuermaschine.engine import Refuse
from steuermaschine.native import NativeRuntime, using
from steuermaschine.testing.fakes import MemoryStore, fake_providers
from steuermaschine_heinzel.native import queue_main
from steuermaschine_heinzel.native.queue_lib import hzlib, main_lanes
from steuermaschine_heinzel.native.queue_lib import main_capacity_recovery as recovery
from steuermaschine_heinzel.native.queue_lib.release_candidate import CandidateError

HEAD = "a" * 40
LINUX = "Test (ubuntu-24.04, Python 3.15, integration)"
MACOS = "Test (macos-latest, Python 3.15, integration)"
LINUX_UNIT = "Test (ubuntu-24.04, Python 3.15, unit)"
ELECTRON = "Electron (macos-latest)"
MACOS_UNIT = "Test (macos-latest, Python 3.15, unit)"
TEST_STEP = "Unit and integration tests (unit first, 4 workers, stop at the first failure)"
AGG = "CI OK"
GUARD = "Fail if any required job failed, naming each with its verdict"
DIRECT = "Require the complete direct CI workload"
FOCUSED = "Require successful focused workload and all owning checks"
WORKFLOW = r'''name: CI
jobs:
  test:
    name: Test (${{ matrix.os }}, Python ${{ matrix.python }}, ${{ matrix.level }})
    steps:
      - name: Unit and integration tests (unit first, 4 workers, stop at the first failure)
        run: make test-parallel
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
      - name: Require successful focused workload and all owning checks
        if: needs.changes.outputs.focused_mode != ''
        run: |
          required=(focused-tests)
          for job in "${required[@]}"; do
            jq -e --arg job "$job" '.[$job].result == "success"' <<< "$RESULTS"
          done
      - name: Require the complete direct CI workload
        run: |
          required=(test)
          for job in "${required[@]}"; do
            jq -e --arg job "$job" '.[$job].result == "success"' <<< "$RESULTS"
          done
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
    policy = SimpleNamespace(aggregate=AGG, test_jobs=frozenset({LINUX, MACOS, LINUX_UNIT, MACOS_UNIT}),
                             workflow=".github/workflows/ci.yml", workflow_runs="actions/workflows/ci.yml/runs")
    policy.expected_jobs = lambda event: {name: "success" for name in policy.test_jobs | {ELECTRON}}
    monkeypatch.setattr(recovery.core_policy, "at_commit_api", lambda *_: policy)
    hosted = recovery.candidate_capacity
    hold = job(20, MACOS, 2, "cancelled", runner_id=0, runner_name="", steps=[], labels=["macos-latest"],
               check_run_url=f"https://api.github.com/repos/{hzlib.CORE}/check-runs/20")
    aggregate = job(21, AGG, 2, "failure", steps=[
        {"number": i, "name": name, "status": "completed", "conclusion": verdict}
        for i, (name, verdict) in enumerate((
            ("Set up job", "success"), (GUARD, "failure"), (FOCUSED, "skipped"),
            (DIRECT, "skipped"), ("Complete job", "success"),
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
    ready = [execution(job(10, LINUX, 1), TEST_STEP), execution(job(11, AGG, 1), GUARD, DIRECT),
             execution(job(12, main_lanes.DISPATCH_JOB, 1)), execution(job(13, LINUX_UNIT, 1), TEST_STEP)]
    other = execution(job(22, MACOS_UNIT, 2), TEST_STEP)
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


@pytest.mark.parametrize("defect", ["", "optional", "different dependency"])
def test_electron_failure_is_capacity_only_when_policy_and_aggregate_bind_it(evidence, defect, monkeypatch):
    responses, _, hold, _, _, log = evidence
    policy = recovery.core_policy.at_commit_api(None, HEAD)
    if defect == "optional":
        monkeypatch.setattr(policy, "expected_jobs", lambda _: {ELECTRON: "skipped"})
    hold["name"] = ELECTRON
    inventory = responses["actions/runs/2/attempts/1/jobs?per_page=100&page=1"]
    inventory["jobs"].append(execution(job(23, MACOS, 2), TEST_STEP))
    inventory["total_count"] += 1
    workflow = WORKFLOW.replace("  ci-ok:", "  electron:\n    name: Electron (${{ matrix.os }})\n  ci-ok:")
    workflow = workflow.replace("needs: [test]", "needs: [test, electron]")
    responses[f"contents/{policy.workflow}?ref={HEAD}"]["content"] = base64.b64encode(workflow.encode()).decode()
    failed = "test" if defect == "different dependency" else "electron"
    needs = {name: {"result": "abandoned" if name == failed else "success"} for name in ("test", "electron")}
    log[1] = "  NEEDS: " + json.dumps(needs, indent=2)
    log[3] = f"##[error]Required job {failed} ended with abandoned"
    if defect:
        with pytest.raises(CandidateError):
            assess(evidence)
    else:
        assert assess(evidence).jobs == (20,)


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
        responses["actions/runs/2/attempts/1/jobs?per_page=100&page=2"] = {"jobs": [], "total_count": 4}
    elif defect == "not split":
        responses["actions/runs/1/attempts/1/jobs?per_page=100&page=1"]["jobs"][-1]["conclusion"] = "skipped"
    elif defect == "other dispatch":
        lane["display_title"] = f"CI {HEAD}"
    with pytest.raises((CandidateError, recovery.candidate_capacity.InvalidCapacity)):
        assess(evidence)

def dispatch(store, evidence, post, authorize=lambda: None):
    responses, _, _, _, _, lines = evidence
    log = "\n".join(f"2026-11-25T12:45:00.0000000Z {line}" for line in lines).encode()
    return recovery.dispatch(store, HEAD, 1, 2, api=responses.__getitem__, read_log=lambda _: log,
                             now=lambda: datetime(2026, 11, 25, 13, tzinfo=UTC), authorize=authorize, post=post)


def replacement(evidence, **overrides):
    responses = evidence[0]
    run = responses["actions/runs/2"] | {"id": 3, "status": "queued", "conclusion": None} | overrides
    responses["actions/runs/3"] = run
    return {"workflow_run_id": 3, "html_url": f"https://github.com/{hzlib.CORE}/actions/runs/3"}


def test_one_fresh_dispatch_persists_and_reuses_only_the_response_id(evidence, tmp_path):
    store = FileStore(tmp_path)
    sent = []

    def post(workflow):
        intent = store.get_receipt(recovery.DISPATCH, HEAD)
        assert (intent["head"], intent["lane"], intent["jobs"]) == (HEAD, "macos", [20])
        sent.append(workflow)
        return replacement(evidence)

    got = dispatch(store, evidence, post)
    assert got["run"] == 3 and got["head"] == HEAD and got["status"] == "queued"
    assert dispatch(FileStore(tmp_path), evidence, post) == got
    assert sent == ["ci.yml"]


@pytest.mark.parametrize("wrong", ["timeout", "no-id", "url", "head", "branch", "event", "attempt", "name", "repo", "moved", "not-visible"])
def test_unbound_or_mismatched_dispatch_never_sends_a_second_post(evidence, wrong):
    store = MemoryStore()
    calls = []

    def post(workflow):
        calls.append(workflow)
        if wrong == "timeout":
            raise TimeoutError("response lost")
        if wrong == "no-id":
            return {}
        if wrong == "url":
            return {"workflow_run_id": 3, "html_url": "unbound"}
        if wrong == "moved":
            evidence[0]["git/ref/heads/main"]["object"]["sha"] = "b" * 40
        if wrong == "not-visible":
            return {"workflow_run_id": 3, "html_url": f"https://github.com/{hzlib.CORE}/actions/runs/3"}
        changes = {
            "head": {"head_sha": "b" * 40}, "branch": {"head_branch": "other"}, "event": {"event": "schedule"},
            "attempt": {"run_attempt": 2}, "name": {"display_title": f"CI {HEAD}"}, "repo": {"repository": {}},
        }
        return replacement(evidence, **changes.get(wrong, {}))

    for _ in range(2):
        with pytest.raises((CandidateError, KeyError)):
            dispatch(store, evidence, post)
    assert calls == ["ci.yml"]


def test_hold_blocks_dispatch_before_creating_intent(evidence):
    store = MemoryStore()

    def authorize():
        raise CandidateError("operator hold")

    with pytest.raises(CandidateError, match="operator hold"):
        dispatch(store, evidence, lambda _: pytest.fail("must not post"), authorize)
    assert not store.receipts


@pytest.mark.parametrize(("run", "name", "witness"), [(1, LINUX, TEST_STEP), (1, LINUX_UNIT, TEST_STEP),
                                                   (2, MACOS_UNIT, TEST_STEP), (1, AGG, GUARD), (1, AGG, DIRECT)])
@pytest.mark.parametrize("damage", ["skip", "remove"])
def test_original_workload_must_have_run_before_dispatch(evidence, run, name, witness, damage):
    page = evidence[0][f"actions/runs/{run}/attempts/1/jobs?per_page=100&page=1"]
    original = next(row for row in page["jobs"] if row["name"] == name)
    step = next(step for step in original["steps"] if step["name"] == witness)
    if damage == "skip":
        step["conclusion"] = "skipped"
    else:
        original["steps"].remove(step)
    with pytest.raises((CandidateError, recovery.candidate_capacity.InvalidCapacity)):
        dispatch(MemoryStore(), evidence, lambda _: pytest.fail("must not post"))

@pytest.mark.parametrize("response", ["bound", "empty", "malformed"])
def test_native_dispatch_uses_versioned_gh_arguments_and_binds_only_a_real_reply(evidence, monkeypatch, response):
    responses, _, _, _, _, lines = evidence
    reply = replacement(evidence)
    stdout = {"bound": json.dumps(reply), "empty": "", "malformed": "not json"}[response]
    sent = []
    monkeypatch.setattr(queue_main.hzlib, "gh", lambda *args: (sent.append(args) or 0, stdout))
    monkeypatch.setattr(queue_main.hzlib, "merge_frozen", lambda: False)
    monkeypatch.setattr(queue_main.queue_hold, "current", lambda _rt: None)
    monkeypatch.setattr(queue_main.main_checkpoint, "get", responses.__getitem__)
    log = "\n".join(f"2026-11-25T12:45:00.0000000Z {line}" for line in lines).encode()
    monkeypatch.setattr(queue_main.main_capacity, "read_aggregate_log", lambda _job: log)
    with using(NativeRuntime(fake_providers(), {}, None)):
        for _ in range(2):
            if response == "bound":
                got = queue_main.queue_main_capacity(HEAD, 1, 2, dispatch=True)
                assert got.proof["run"] == 3 and got.proof["head"] == HEAD
            else:
                with pytest.raises(Refuse, match="dispatch outcome is unbound"):
                    queue_main.queue_main_capacity(HEAD, 1, 2, dispatch=True)
    assert sent == [(
        "api", "--method", "POST", "-H", "X-GitHub-Api-Version: 2026-03-10",
        f"repos/{hzlib.CORE}/actions/workflows/ci.yml/dispatches", "-f", "ref=main",
        "-f", "inputs[lane]=macos",
    )]
