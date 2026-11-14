"""Core ADR 0149 item d in the queue and the CI reads: a replaced main run is not red, a green rerun repairs a red one,
the batch run may be a rerun, and a TEST failure outside release/* is rerun first. Each only with the merge tier at main;
the older rules otherwise. GitHub is the fake's."""

import base64

import pytest

from steuermaschine.github.testing import FakeGh
from steuermaschine_heinzel.native import ci_read, merge_tier
from steuermaschine_heinzel.native.ci_read import FailedJob, RunFailure
from steuermaschine_heinzel.native.dispatch_lib import focused
from steuermaschine_heinzel.native.queue_lib import hzlib, main_batch
from steuermaschine_heinzel.native.queue_lib import queue_daemon as qd

SHA = [c * 40 for c in "abcdef"]
TIER = {"encoding": "base64", "content": base64.b64encode(b"WHOLE_SUITES = {}\n").decode()}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HZ_QUEUE_STATE", str(tmp_path / "queue.json"))
    monkeypatch.setenv("HZ_ALARM_STATE", str(tmp_path / "alarm.json"))
    monkeypatch.setenv("HZ_ALARM_NOTIFY", "off")
    (tmp_path / ".cache").mkdir()
    monkeypatch.setattr(hzlib, "note", lambda _t: None)
    monkeypatch.setattr(hzlib, "MERGE_TIER_MODE", "auto")


def row(n, conclusion, status="completed", sha=None):
    return {"databaseId": n, "headSha": sha or SHA[n % 6], "status": status, "conclusion": conclusion, "displayTitle": "x", "attempt": 1}


def reads(monkeypatch, rows, tier=True):
    calls = []

    def ghj(*args, default=None):
        calls.append(args)
        if args[:2] == ("run", "list"):
            return rows
        if "contents/scripts/focused_plan.py" in args[1]:
            return TIER if tier else {"encoding": "base64", "content": base64.b64encode(b'VITEST_ALL = "x"\n').decode()}
        return default

    monkeypatch.setattr(hzlib, "ghj", ghj)
    monkeypatch.setattr(hzlib, "resolve_main_runs", lambda r, **_: r)
    return calls


def test_a_replaced_main_run_is_not_red_in_the_merge_tier(monkeypatch):
    rows = [row(2, "failure"), row(1, "cancelled")]
    qd.save({"items": [], "batch": {"shas": [SHA[1][:8]]}})
    reads(monkeypatch, rows)
    assert qd.main_state() == "red"  # the newest run failed: red either way
    qd.save({"items": [], "batch": {"shas": [SHA[1][:8]]}})
    rows[:] = [row(3, "success"), row(1, "cancelled")]
    assert qd.main_state() == "green"
    rows[:] = [row(4, "cancelled", sha=SHA[4]), row(1, "cancelled")]
    assert qd.main_state() == "unknown"  # the newest cancelled run is not replaced: no evidence


def test_the_older_core_keeps_cancelled_unknown(monkeypatch):
    qd.save({"items": [], "batch": {"shas": [SHA[1][:8]]}})
    reads(monkeypatch, [row(2, "failure"), row(1, "cancelled")], tier=False)
    assert qd.main_state() == "unknown"  # a cancelled run of the batch is no proof either way
    qd.save({"items": []})
    calls = reads(monkeypatch, [row(3, "success")], tier=False)
    assert qd.main_state() == "green" and not any("contents" in str(c) for c in calls)  # read only when a rule needs it


def test_a_green_rerun_of_the_pinned_run_repairs_main_only_in_the_merge_tier(monkeypatch):
    pinned = row(5, "failure")
    for tier, expected in ((False, "red"), (True, "green")):
        qd.save({"items": [], "main_failure": pinned})
        reads(monkeypatch, [row(5, "success")], tier=tier)
        assert qd.main_state() == expected
    assert qd.load()["main_failure"] == {}


def test_merge_tier_mode_forces_the_rules(monkeypatch):
    calls = reads(monkeypatch, [], tier=False)
    monkeypatch.setattr(hzlib, "MERGE_TIER_MODE", "on")
    assert hzlib.merge_tier() and not calls
    monkeypatch.setattr(hzlib, "MERGE_TIER_MODE", "auto")
    assert not hzlib.merge_tier()
    monkeypatch.setattr(hzlib, "ghj", lambda *a, default=None: default)
    assert not hzlib.merge_tier()
    probe = qd.MergeTier()
    monkeypatch.setattr(hzlib, "merge_tier", lambda: True)
    assert probe() and probe.found is True


def test_the_batch_run_may_be_a_rerun_only_in_the_merge_tier():
    members = [[1, SHA[0]]]
    run = {"id": 7, "run_attempt": 2, "event": "push", "head_branch": "main", "head_sha": SHA[0], "path": ".github/workflows/ci.yml",
           "repository": {"full_name": main_batch.REPOSITORY}, "status": "completed", "conclusion": "success"}
    assert not main_batch.valid_run(run, {"run": 7, "prs": members})  # older Core: scripts/main_batch.py needs attempt 1
    assert main_batch.valid_run({**run, "run_attempt": 1}, {"run": 7, "prs": members})
    assert main_batch.valid_run(run, {"run": 7, "prs": members}, reruns=True)
    assert not main_batch.valid_run({**run, "run_attempt": 0}, {"run": 7, "prs": members}, reruns=True)


def test_a_main_rerun_needs_no_force_only_in_the_merge_tier(monkeypatch):
    run = {"id": 9, "head_branch": "main", "status": "completed", "path": ".github/workflows/ci.yml"}
    monkeypatch.setattr(focused, "run_state", lambda rid: run)
    monkeypatch.setattr(focused, "workflow_path", lambda r: ".github/workflows/ci.yml")
    monkeypatch.setattr(focused, "jobs_of", lambda rid: [])
    from types import SimpleNamespace

    args = SimpleNamespace(target="200000", job=None, repeat=1, wait=False, json=False, force=False, merge_tier=lambda: False)
    with pytest.raises(SystemExit, match="main or a release candidate"):
        focused.cmd_rerun(args)
    args.merge_tier = lambda: True
    with pytest.raises(SystemExit, match="no failed job to re-run"):
        focused.cmd_rerun(args)
    run["head_branch"] = "release/v1"
    with pytest.raises(SystemExit, match="main or a release candidate"):
        focused.cmd_rerun(args)


def failure(cause="TEST"):
    return [RunFailure(run=9, jobs=[FailedJob(name="Focused tests (web)", cause=cause)])]


def test_ci_why_reruns_a_test_failure_first_in_the_merge_tier():
    _line, nxt, trap = ci_read.decide(5, "pr", failure(), [], [], None, rerun_repairs=True)
    assert nxt == "sm ci one rerun 5 --wait" and "valid repair" in trap
    assert ci_read.decide(5, "pr", failure(), [], [], None)[1] == "sm ci faillog 9"
    assert ci_read.decide(5, "pr", failure("UNKNOWN"), [], [], None, rerun_repairs=True)[1] == "sm ci faillog 9"


def world(gh):
    from steuermaschine_heinzel.native.go_lib import probes

    return probes.World(commands=gh, clock=None, state_dir=None, core="", repo="o/core")


@pytest.mark.parametrize(("kind", "path", "answer", "expected"), [
    ("pr", "pulls/5", {"head": {"ref": "fix/x"}}, True),
    ("pr", "pulls/5", {"head": {"ref": "release/v1"}}, False),
    ("run", "actions/runs/5", {"head_branch": "main"}, True),
    ("run", "actions/runs/5", 404, False),
])
def test_rerun_repairs_reads_the_branch_and_main(kind, path, answer, expected):
    gh = FakeGh().on(f"repos/o/core/{path}", answer).on(f"contents/{merge_tier.PLAN_PATH}", TIER)
    assert ci_read.rerun_repairs(world(gh), 5, kind) is expected

