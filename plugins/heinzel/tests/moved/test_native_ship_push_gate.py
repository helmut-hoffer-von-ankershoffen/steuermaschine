from datetime import datetime, timezone

from steuermaschine_heinzel.native.ship_lib.push_gate_rules import OK, decide, release_hold, release_run_active, queue_age_min

NOW = datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc)


def pr(n, created, conclusions, draft=False):
    return {"number": n, "createdAt": created, "isDraft": draft, "statusCheckRollup": [{"status": "COMPLETED", "conclusion": c} for c in conclusions]}


def test_queue_age():
    assert queue_age_min([], NOW) is None
    assert queue_age_min(["2026-11-07T17:42:08Z", "2026-11-07T17:50:00Z"], NOW) == 17


def test_decide_is_always_ok_and_names_a_starved_main_queue_in_one_line():
    assert decide(None, 15) == (OK, "OK: main CI not starved (oldest queued none < 15): push")
    assert decide(14, 15)[0] == OK
    code, msg = decide(88, 15)
    assert code == OK and "88 min" in msg and "WAIT" not in msg


def test_release_hold():
    NOW = datetime(2026, 11, 8, 16, 0, tzinfo=timezone.utc)
    runs = [{"databaseId": 7, "headBranch": "release/v0.9.34", "status": "in_progress", "createdAt": "2026-11-08T15:40:00Z"},
            {"databaseId": 8, "headBranch": "main", "status": "queued", "createdAt": "2026-11-08T15:00:00Z"}]
    f = release_run_active(runs, "feat/x", NOW)
    assert f == (7, 20, False)
    code, msg = release_hold(f)
    assert code == OK and "run 7 is active" in msg  # an info line, never a WAIT
    for b in ("release/v0.9.34", "bump/x", "main"):  # never held
        assert release_run_active(runs, b, NOW) is None
    old = release_run_active(runs, "feat/x", datetime(2026, 11, 8, 16, 30, tzinfo=timezone.utc))  # 50 min: cap reached
    assert old[2] and release_hold(old)[0] == OK
    assert release_run_active([{"databaseId": 7, "headBranch": "release/v1", "status": "completed", "createdAt": "x"}], "x", NOW) is None
    assert release_hold(None) == (OK, "")


def test_gh_error_allows():
    import pathlib, subprocess
    from moved_support import load
    ns = vars(load("hz-push-gate"))  # moved: the gate is a module; importing it no longer runs it

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("gh", 5)
    from unittest import mock
    with mock.patch.object(ns["subprocess"], "run", boom):
        assert ns["release_check"](5, None) == (0, "")
    assert ns["subprocess"].run is not boom
    assert release_run_active([{"databaseId": 1, "headBranch": "release/v1", "status": "queued", "createdAt": "2026-11-08T15:40:00Z"}], "HEAD") is None


def test_auto_defer_cooldown_30_min_and_special_runs():
    from steuermaschine_heinzel.native.ship_lib import push_gate_rules as g
    assert g.auto_defer_plan(20, 15, set(), 1000.0, 1000.0 + 29 * 60) is None
    assert g.auto_defer_plan(20, 15, set(), 1000.0, 1000.0 + 31 * 60)
    assert g.auto_defer_plan(20, 15, set(), None, 5000.0, special_active=True) is None
    assert g.auto_defer_plan(20, 15, set(), None, 5000.0)
    assert g.special_run_active([{"workflowName": "Acceptance", "headBranch": "main", "status": "in_progress"}])
    for w in ("Release tag", "Publish", "Standard app composition", "Release one plugin", "Release bumped plugins"):
        assert g.special_run_active([{"workflowName": w, "headBranch": "main", "event": "workflow_dispatch", "status": "queued"}]), w
    assert not g.special_run_active([{"workflowName": "Acceptance", "headBranch": "main", "status": "completed"}, {"workflowName": "CI", "headBranch": "feat/x", "status": "queued"}])
