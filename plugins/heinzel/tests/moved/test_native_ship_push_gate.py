from datetime import datetime, timezone

from steuermaschine_heinzel.native.ship_lib.push_gate_rules import OK, WAIT, decide, fix_prs, release_hold, release_run_active, oldest_ready, parse_keep, queue_age_min

NOW = datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc)


def pr(n, created, conclusions, draft=False):
    return {"number": n, "createdAt": created, "isDraft": draft, "statusCheckRollup": [{"status": "COMPLETED", "conclusion": c} for c in conclusions]}


def test_parse_keep_numbers_comments_auto():
    assert parse_keep("603, 598 # fix\n# 1\nauto\n600") == ({603, 598, 600}, True)
    assert parse_keep("603") == ({603}, False)
    assert parse_keep(None) == (set(), True) and parse_keep("  \n") == (set(), True)


def test_fix_prs_from_known_failures():
    assert fix_prs('note = "fix PR #603; web-2"\nnote = "Fix PR #7"') == {603, 7}
    assert fix_prs(None) == set()


def test_queue_age():
    assert queue_age_min([], NOW) is None
    assert queue_age_min(["2026-11-07T17:42:08Z", "2026-11-07T17:50:00Z"], NOW) == 17


def test_oldest_ready_skips_draft_failed_pending():
    prs = [pr(5, "2026-11-07T10:00:00Z", ["FAILURE"]), pr(6, "2026-11-07T11:00:00Z", ["SUCCESS"], draft=True),
           pr(8, "2026-11-07T12:00:00Z", ["SUCCESS", "SKIPPED"]), pr(7, "2026-11-07T11:30:00Z", ["SUCCESS"])]
    prs.append({"number": 4, "createdAt": "2026-11-07T09:00:00Z", "isDraft": False, "statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": ""}]})
    assert oldest_ready(prs) == 7 and oldest_ready([]) is None


def test_decide_ok_when_not_starved():
    assert decide(9, None, 15, set())[0] == OK
    assert decide(9, 14, 15, set())[0] == OK


def test_decide_wait_when_starved_and_not_kept():
    code, msg = decide(9, 88, 15, {603}, auto_ready=598)
    assert code == WAIT == 75 and "88 min" in msg and "PR #9" in msg and "ci-keep.txt" in msg


def test_decide_keep_and_oldest_ready_pass_and_new_branch_waits():
    assert decide(603, 88, 15, {603})[0] == OK
    assert decide(598, 88, 15, set(), auto_ready=598)[0] == OK
    assert decide(None, 88, 15, {603}, auto_ready=598)[0] == WAIT


def test_release_hold():
    NOW = datetime(2026, 11, 8, 16, 0, tzinfo=timezone.utc)
    runs = [{"databaseId": 7, "headBranch": "release/v0.9.34", "status": "in_progress", "createdAt": "2026-11-08T15:40:00Z"},
            {"databaseId": 8, "headBranch": "main", "status": "queued", "createdAt": "2026-11-08T15:00:00Z"}]
    f = release_run_active(runs, "feat/x", NOW)
    assert f == (7, 20, False)
    code, msg = release_hold(f)
    assert code == WAIT and "7" in msg
    code, msg = release_hold(f, waived=True)  # P0 waiver
    assert code == OK and "7" in msg
    for b in ("release/v0.9.34", "bump/x", "main"):  # never held
        assert release_run_active(runs, b, NOW) is None
    old = release_run_active(runs, "feat/x", datetime(2026, 11, 8, 16, 30, tzinfo=timezone.utc))  # 50 min: cap reached
    assert old[2] and release_hold(old)[0] == OK and "WARNING" in release_hold(old)[1]
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
        assert ns["release_check"](5, None, keep={5}) == (0, "")  # keep-list fix PR never held
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
