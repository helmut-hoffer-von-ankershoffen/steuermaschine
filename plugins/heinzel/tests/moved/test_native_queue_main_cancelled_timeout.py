"""Required provider timeouts on original main CI attempts."""

import base64
import copy
from dataclasses import replace
from pathlib import Path

import pytest
from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native.queue_lib import main_checkpoint

RUN = 42906326430
SHA = "e3dc6ee2325505e542b1f707a371ef8896e17903"
JOB = 130145283910
SUITE = 116201260346


@pytest.fixture
def world(tool, home, monkeypatch):
    state = home / ".cache/isolated-timeout-queue.json"
    monkeypatch.setenv("HZ_QUEUE_STATE", str(state))
    q = tool("hz-queue")
    assert Path.home() == home
    assert q.state_path().resolve() == state.resolve()
    assert not state.exists()
    pin = {"databaseId": 41, "headSha": "a" * 40, "status": "completed", "conclusion": "failure"}
    q.update(main_failure=pin, red_batch={"prs": ["old"], "shas": ["aaaaaaaa"]},
             progress_history=[{"run": 41, "reason": "original failure"}])
    before = state.read_bytes()
    rows = [{"databaseId": RUN, "headSha": SHA, "status": "completed", "conclusion": "cancelled", "attempt": 1},
            {"databaseId": 42, "headSha": "b" * 40, "status": "completed", "conclusion": "success", "attempt": 1,
             "displayTitle": "CI prior"}]
    job = {"id": JOB, "name": "Secret scan", "run_id": RUN, "run_attempt": 1, "head_sha": SHA,
           "status": "completed", "conclusion": "cancelled", "runner_id": 1000117321,
           "runner_name": "GitHub Actions 1000117321", "runner_group_name": "GitHub Actions",
           "started_at": "2026-11-10T21:34:37Z", "completed_at": "2026-11-10T21:37:43Z",
           "check_run_url": f"https://api.github.com/repos/{q.hzlib.CORE}/check-runs/{JOB}",
           "steps": [{"name": "Scan the history for credentials", "status": "completed", "conclusion": "cancelled",
                      "started_at": "2026-11-10T21:35:28Z", "completed_at": "2026-11-10T21:37:40Z"}]}
    aggregate = {**job, "id": JOB + 1, "name": "CI OK", "conclusion": "failure",
                 "steps": [{"name": "Fail if any required job failed", "status": "completed", "conclusion": "failure"}]}
    run = {"id": RUN, "head_sha": SHA, "head_branch": "main", "event": "push", "path": ".github/workflows/ci.yml",
           "run_attempt": 1, "status": "completed", "conclusion": "cancelled", "check_suite_id": SUITE,
           "repository": {"full_name": q.hzlib.CORE}, "head_repository": {"full_name": q.hzlib.CORE}}
    check = {"id": JOB, "name": "Secret scan", "head_sha": SHA, "status": "completed", "conclusion": "cancelled",
             "app": {"id": 15368, "slug": "github-actions"}, "check_suite": {"id": SUITE},
             "output": {"annotations_count": 2}}
    annotation = {"annotation_level": "failure", "path": ".github", "title": "",
                  "blob_href": f"https://github.com/{q.hzlib.CORE}/blob/{SHA}/.github"}
    responses = {
        "tip": {"ref": "refs/heads/main", "object": {"sha": SHA, "type": "commit",
                "url": f"https://api.github.com/repos/{q.hzlib.CORE}/git/commits/{SHA}"}},
        "run": run, "jobs": {"total_count": 2, "jobs": [job, aggregate]}, "check": check,
        "annotations": [{**annotation, "message": "The job has exceeded the maximum execution time of 3m0s"},
                        {**annotation, "message": "The operation was canceled."}],
    }
    prefix = f"repos/{q.hzlib.CORE}"
    endpoints = {f"{prefix}/git/ref/heads/main": "tip", f"{prefix}/actions/runs/{RUN}": "run",
                 f"{prefix}/actions/runs/{RUN}/attempts/1/jobs?per_page=100": "jobs",
                 f"{prefix}/check-runs/{JOB}": "check",
                 f"{prefix}/check-runs/{JOB}/annotations?per_page=100": "annotations"}
    calls = []

    def read(*args, default=None):
        calls.append(args)
        if args[:2] == ("run", "list"):
            return rows
        assert args[0] == "api" and not any(a in args for a in ("--method", "-X", "-f", "-F"))
        if args[1] == f"{prefix}/actions/runs/41/jobs?per_page=100":
            return {"jobs": [{"name": "Historical failure", "conclusion": "failure"}]}
        return copy.deepcopy(responses.get(endpoints.get(args[1]), default))

    monkeypatch.setattr(q.hzlib, "ghj", read)
    return {"queue": q, "state": state, "before": before, "rows": rows, "responses": responses, "calls": calls}


@pytest.mark.parametrize("existing", ["pin", "empty", "red_batch_only"])
def test_started_required_provider_timeout_is_red_and_maps_the_exact_latest_run_without_writing(world, existing):
    q = world["queue"]
    if existing != "pin":
        q.update(main_failure={}, red_batch={} if existing == "empty" else {"prs": ["old"]})
    before = world["state"].read_bytes()
    assert q.main_state() == "red"
    assert q.main_red_jobs() == {"run": RUN, "sha": SHA, "jobs": ["Secret scan"]}
    assert world["state"].read_bytes() == before
    assert world["rows"][0]["conclusion"] == "cancelled"
    assert not q.main_is_green()
    assert not q.later_main_passes()


@pytest.mark.parametrize("target,path,value", [
    ("tip", ("object", "sha"), "c" * 40),
    ("tip", ("ref",), "refs/heads/other"),
    ("run", ("id",), RUN + 1),
    ("run", ("head_sha",), "c" * 40),
    ("run", ("head_branch",), "other"),
    ("run", ("event",), "workflow_dispatch"),
    ("run", ("path",), ".github/workflows/other.yml"),
    ("run", ("run_attempt",), 2),
    ("run", ("run_attempt",), True),
    ("run", ("status",), "in_progress"),
    ("run", ("repository",), None),
    ("run", ("head_repository", "full_name"), "example/fork"),
    ("run", ("check_suite_id",), None),
    ("jobs", ("total_count",), 3),
    ("jobs", ("jobs", 0, "id"), None),
    ("jobs", ("jobs", 0, "run_id"), RUN + 1),
    ("jobs", ("jobs", 0, "head_sha"), "c" * 40),
    ("jobs", ("jobs", 0, "run_attempt"), 2),
    ("jobs", ("jobs", 0, "run_attempt"), True),
    ("jobs", ("jobs", 0, "status"), "in_progress"),
    ("jobs", ("jobs", 0, "name"), "Advisory scan"),
    ("jobs", ("jobs", 0, "runner_id"), 0),
    ("jobs", ("jobs", 0, "runner_name"), ""),
    ("jobs", ("jobs", 0, "runner_group_name"), "self-hosted"),
    ("jobs", ("jobs", 0, "check_run_url"), "https://example.invalid/check"),
    ("jobs", ("jobs", 0, "started_at"), None),
    ("jobs", ("jobs", 0, "completed_at"), "2026-11-10T21:34:37Z"),
    ("jobs", ("jobs", 0, "completed_at"), "2026-11-10T21:37:30Z"),
    ("jobs", ("jobs", 0, "steps"), []),
    ("jobs", ("jobs", 0, "steps", 0, "completed_at"), "bad"),
    ("jobs", ("jobs", 0, "steps", 0, "status"), "in_progress"),
    ("jobs", ("jobs", 0, "steps", 0, "conclusion"), "failure"),
    ("jobs", ("jobs", 1, "steps", 0, "name"), "Unrelated failure"),
    ("jobs", ("jobs", 1, "steps"), None),
    ("jobs", ("jobs", 1, "conclusion"), "success"),
    ("check", ("id",), JOB + 1),
    ("check", ("head_sha",), "c" * 40),
    ("check", ("app",), None),
    ("check", ("app", "id"), 1),
    ("check", ("check_suite", "id"), SUITE + 1),
    ("check", ("output", "annotations_count"), 3),
    ("annotations", (0, "annotation_level"), "notice"),
    ("annotations", (0, "message"), "The operation was canceled."),
    ("annotations", (0, "message"), "The job was not acquired by Runner of type hosted even after multiple attempts"),
    ("annotations", (0, "message"), "The job has exceeded the maximum execution time of 5m0s"),
    ("annotations", (0, "blob_href"), "https://example.invalid/blob"),
])
def test_incomplete_wrong_or_unproven_timeout_evidence_stays_unknown_and_preserves_the_pin(world, target, path, value):
    node = world["responses"][target]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    q = world["queue"]
    assert q.latest_required_timeout(world["rows"]) == {}
    assert q.main_state() == "unknown"
    assert q.main_red_jobs() == {"run": 41, "sha": "a" * 40, "jobs": ["Historical failure"]}
    assert world["state"].read_bytes() == world["before"]


@pytest.mark.parametrize("target", ["tip", "run", "jobs", "check", "annotations"])
def test_unavailable_api_evidence_stays_unknown(world, target):
    world["responses"][target] = None
    assert world["queue"].latest_required_timeout(world["rows"]) == {}
    assert world["queue"].main_state() == "unknown"
    assert world["state"].read_bytes() == world["before"]


@pytest.mark.parametrize("status,conclusion", [("in_progress", None), ("completed", "success"), ("completed", "failure")])
def test_a_newer_main_run_supersedes_the_timeout(world, status, conclusion):
    world["rows"].insert(0, {"databaseId": RUN + 1, "headSha": "d" * 40, "status": status, "conclusion": conclusion})
    assert world["queue"].latest_required_timeout(world["rows"]) == {}
    assert not world["calls"]


def test_intentionally_deferred_cancelled_run_cannot_become_timeout_evidence(world):
    deferred = statedir.root() / "ci-deferred.txt"
    deferred.write_text(f"{RUN} intentionally deferred\n")
    assert world["queue"].main_state() == "unknown"
    assert world["queue"].latest_required_timeout(world["rows"]) == {}
    assert world["state"].read_bytes() == world["before"]


def test_another_unsuccessful_job_or_extra_annotation_cannot_be_hidden(world):
    responses = world["responses"]
    extra = {**responses["jobs"]["jobs"][0], "id": JOB + 2, "name": "Other unknown cancellation"}
    responses["jobs"]["jobs"].append(extra)
    responses["jobs"]["total_count"] += 1
    assert world["queue"].main_state() == "unknown"
    responses["jobs"]["jobs"].pop()
    responses["jobs"]["total_count"] -= 1
    responses["annotations"].append(responses["annotations"][0])
    assert world["queue"].main_state() == "unknown"
    assert world["state"].read_bytes() == world["before"]


@pytest.fixture
def full_dispatch(world, monkeypatch):
    q = world["queue"]
    responses = world["responses"]
    run, check = responses["run"], responses["check"]
    run.update(event="workflow_dispatch", display_title=f"CI {SHA}")
    world["rows"][0].update(event="workflow_dispatch", displayTitle=run["display_title"])
    guard = "Fail if any required job failed, naming each with its verdict"
    job, aggregate = responses["jobs"]["jobs"]
    job.update(name="Plugin template", labels=["ubuntu-24.04"], completed_at="2026-11-10T21:44:43Z")
    job["steps"][0].update(name="Run make template-test AUDIT=1", completed_at="2026-11-10T21:44:40Z")
    aggregate["steps"][0]["name"] = guard
    check["name"] = job["name"]
    responses["annotations"][0]["message"] = "The job has exceeded the maximum execution time of 10m0s"
    policy = q.core_facts.at(SHA)
    expected = policy.expected_jobs("workflow_dispatch") | {"Plugin template": "success", "Release candidate source": "success"}
    policy = replace(policy, jobs={**policy.jobs, "workflow_dispatch": expected})
    monkeypatch.setattr(q.core_facts, "at", lambda _: policy)
    lane = policy.quarantine
    observed = expected | {"Release candidate source": "skipped", lane.registry: "success", lane.plan: "success", lane.idle: "skipped"}
    for identifier, (name, conclusion) in enumerate(observed.items(), JOB + 2):
        if name not in (job["name"], aggregate["name"]):
            responses["jobs"]["jobs"].append({**job, "id": identifier, "name": name, "conclusion": conclusion})
    responses["jobs"]["total_count"] = len(responses["jobs"]["jobs"])
    world["workflow"] = rf'''name: CI
jobs:
  template:
    name: Plugin template
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - run: make template-test AUDIT=1
  ci-ok:
    name: CI OK
    needs: [template]
    steps:
      - name: {guard}
        env:
          NEEDS: ${{{{ toJSON(needs) }}}}
        run: |
          jq -r 'to_entries[] | select(.value.result != "success")
            | "::error::Required job \(.key) ended with \(.value.result)"' <<< "$NEEDS"
          for result in $RESULTS; do
            *) echo "A required job ended with: $result"; exit 1 ;;
'''

    def source(path):
        assert path == f"contents/{policy.workflow}?ref={SHA}"
        return {"type": "file", "encoding": "base64", "content": base64.b64encode(world["workflow"].encode()).decode()}

    monkeypatch.setattr(main_checkpoint, "get", source)
    return world


def test_full_dispatch_required_template_timeout_is_red_while_preserving_the_old_pin(full_dispatch):
    q = full_dispatch["queue"]
    assert q.main_state() == "red"
    assert q.main_red_jobs() == {"run": RUN, "sha": SHA, "jobs": ["Plugin template"]}
    assert not q.main_is_green()
    assert not q.later_main_passes()
    assert q.main_rerun_plan()[0] == "none"
    assert full_dispatch["state"].read_bytes() == full_dispatch["before"]


@pytest.mark.parametrize("damage", ["split", "missing required job", "wrong runner", "wrong workload",
                                    "extra aggregate failure", "different timeout", "changed workflow"])
def test_dispatch_timeout_requires_exact_full_inventory_and_hosted_workload(full_dispatch, damage):
    q = full_dispatch["queue"]
    responses = full_dispatch["responses"]
    jobs = responses["jobs"]["jobs"]
    if damage == "split":
        responses["run"]["display_title"] = f"CI macOS main {SHA}"
    elif damage == "missing required job":
        jobs.remove(next(job for job in jobs if job["name"].startswith("Test (macos")))
        responses["jobs"]["total_count"] -= 1
    elif damage == "wrong runner":
        jobs[0]["labels"] = ["ubuntu-latest"]
    elif damage == "wrong workload":
        jobs[0]["steps"][0]["name"] = "Run ./.github/actions/setup-uv"
    elif damage == "extra aggregate failure":
        jobs[1]["steps"].append({"name": "Another failure", "conclusion": "failure"})
    elif damage == "different timeout":
        responses["annotations"][0]["message"] = "The job has exceeded the maximum execution time of 5m0s"
    else:
        full_dispatch["workflow"] = full_dispatch["workflow"].replace("needs: [template]", "needs: []")
    assert q.latest_required_timeout(full_dispatch["rows"]) == {}
    assert q.main_state() == "unknown"
    assert full_dispatch["state"].read_bytes() == full_dispatch["before"]


def test_dispatch_timeout_refuses_when_protected_main_moves_during_proof(full_dispatch, monkeypatch):
    q = full_dispatch["queue"]
    lookup = q.hzlib.ghj
    tip_reads = 0

    def main_moves(*args, **kwargs):
        nonlocal tip_reads
        evidence = lookup(*args, **kwargs)
        if args[-1].endswith("/git/ref/heads/main"):
            tip_reads += 1
            if tip_reads == 2:
                evidence["object"]["sha"] = "f" * 40
        return evidence

    monkeypatch.setattr(q.hzlib, "ghj", main_moves)
    assert q.latest_required_timeout(full_dispatch["rows"]) == {}
    assert tip_reads == 2
    assert full_dispatch["state"].read_bytes() == full_dispatch["before"]
