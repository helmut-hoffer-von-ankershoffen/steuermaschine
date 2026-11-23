"""PR 70 review findings 1, 2 and 5: auto_arm applies the old merge rule and red-main hold, and `hold` reads GitHub."""

import pytest
from test_native_queue_commands import ARM, CORE, HEAD, MAIN, MAIN_RUNS, PR, gh, items, nothing_real, queue_one, rt  # noqa: F401 - fixtures

from steuermaschine.testing import invoke
from steuermaschine_heinzel.native import queue as area
from steuermaschine_heinzel.native.queue_lib import main_plan

RED_MAIN = [{**MAIN_RUNS[0], "conclusion": "failure", "databaseId": 501, "headSha": "2" * 40}]


def tick(runtime):
    area.reset()
    return invoke(runtime, ["daemon", "queue"])


def merges(gh):  # noqa: F811
    return [line for line in gh.lines() if line.startswith("pr merge")]


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        ("merge rule: #801 (e9c94381) is behind against main 11111111: update the branch and let CI test it again", "needs work"),
        ("merge rule: #801 (e9c94381): no verdict is recorded: changed protected paths .github/workflows/ci.yml", "needs work"),
        ("merge rule: #801 (e9c94381): required checks not green: CI OK", "queued"),
        ("merge rule: cannot read the required status checks of main's ruleset", "queued"),
    ],
)
def test_the_merge_rule_gates_the_arm(rt, gh, monkeypatch, reason, status):  # noqa: F811
    queue_one(rt)
    gh.on("pr list", []).on("pr merge 801", "")
    seen = []
    monkeypatch.setattr(main_plan, "gap", lambda pr, head="": seen.append((pr, head)) or reason)
    assert tick(rt).exit_code == 0
    row = items(rt)[-1]
    assert not merges(gh) and row["status"] == status and reason in row["detail"] and seen == [("801", HEAD)]


def test_a_pr_that_meets_the_merge_rule_is_armed(rt, gh):  # noqa: F811
    queue_one(rt)
    gh.on("pr list", []).on("pr merge 801", "")
    assert tick(rt).exit_code == 0 and merges(gh) == [ARM]


def test_a_red_main_arms_nothing_except_the_pr_that_fixes_it(rt, gh):  # noqa: F811
    gh.on("run list", RED_MAIN).on("pr list", []).on("pr merge", "")
    queue_one(rt)
    queue_one(rt, key="802", pr="802", fixes_main=True)
    gh.on("pr view 802", {**PR})
    assert tick(rt).exit_code == 0
    assert merges(gh) == [ARM.replace("801", "802")]
    held = items(rt)[-2]
    assert held["key"] == "801" and held["detail"].startswith("not armed: main's latest run is not green")


def test_a_red_main_switches_off_an_armed_pr_that_does_not_fix_it(rt, gh):  # noqa: F811
    gh.on("run list", RED_MAIN).on("pr list", []).on("pr merge", "")
    gh.on("pr view 801", {**PR, "autoMergeRequest": {"mergeMethod": "SQUASH"}})
    queue_one(rt)
    assert tick(rt).exit_code == 0
    assert merges(gh) == [f"pr merge 801 -R {CORE} --disable-auto"]


PUSH = {**MAIN_RUNS[0], "event": "push", "conclusion": "failure"}
SCHEDULE = {**PUSH, "event": "schedule", "conclusion": "cancelled", "databaseId": 502, "attempt": 2}
MACOS = {**MAIN_RUNS[0], "event": "workflow_dispatch", "displayTitle": "CI macOS main " + MAIN, "databaseId": 501}
RECOVERY_JOB = {"name": "End-to-end cli (ubuntu-24.04)", "status": "completed", "conclusion": "failure"}
RECOVERY_JOBS = {"total_count": 2, "jobs": [RECOVERY_JOB, {**RECOVERY_JOB, "name": "CI OK"}]}


def recovery_forge(gh, rows, ref, jobs):  # noqa: F811
    gh.on("run list", rows).on(f"repos/{CORE}/git/ref/heads/main", ref)
    gh.on(f"repos/{CORE}/actions/runs/500/attempts/1/jobs?per_page=100", jobs)
    gh.on("pr list", []).on("pr merge", "")


def test_cancelled_schedule_preserves_exact_failed_push_for_reviewed_recovery(rt, gh):  # noqa: F811
    recovery_forge(gh, [SCHEDULE, MACOS, PUSH], {"object": {"sha": MAIN}}, RECOVERY_JOBS)
    queue_one(rt)
    queue_one(rt, key="802", pr="802", fixes_main=True)
    queue_one(rt, key="803", pr="803", reviewed_head=HEAD, mitigation_main="reviewed exact revert")
    gh.on("pr view 802", PR).on("pr view 803", PR)
    out = tick(rt)
    assert out.exit_code == 0 and merges(gh) == [ARM.replace("801", "803")]
    assert all("main CI evidence is unknown" in row["detail"] for row in items(rt)[-3:-1])
    assert f"failed push run 500 attempt 1 at {MAIN}; later scheduled run 502 cancelled" in out.stdout


@pytest.mark.parametrize(
    ("rows", "ref", "jobs"),
    [
        (None, {"object": {"sha": MAIN}}, RECOVERY_JOBS),
        ([SCHEDULE, PUSH], None, RECOVERY_JOBS),
        ([SCHEDULE, PUSH], {"object": {"sha": "2" * 40}}, RECOVERY_JOBS),
        ([{**SCHEDULE, "headSha": "2" * 40}, PUSH], {"object": {"sha": MAIN}}, RECOVERY_JOBS),
        ([{**SCHEDULE, "databaseId": 499}, PUSH], {"object": {"sha": MAIN}}, RECOVERY_JOBS),
        ([{**SCHEDULE, "status": "in_progress", "conclusion": ""}, PUSH], {"object": {"sha": MAIN}}, RECOVERY_JOBS),
        ([SCHEDULE, {**PUSH, "status": "queued", "conclusion": "", "databaseId": 501}, PUSH], {"object": {"sha": MAIN}}, RECOVERY_JOBS),
        ([SCHEDULE, {**PUSH, "conclusion": "success", "databaseId": 501}, PUSH], {"object": {"sha": MAIN}}, RECOVERY_JOBS),
        ([SCHEDULE, PUSH], {"object": {"sha": MAIN}}, None),
        ([SCHEDULE, PUSH], {"object": {"sha": MAIN}}, {**RECOVERY_JOBS, "total_count": 3}),
        ([SCHEDULE, PUSH], {"object": {"sha": MAIN}}, {"total_count": 1, "jobs": [{**RECOVERY_JOB, "name": "CI OK"}]}),
    ],
)
def test_unknown_or_replaced_push_evidence_cannot_authorize_recovery(rt, gh, rows, ref, jobs):  # noqa: F811
    from steuermaschine.native import NativeRuntime, using

    recovery_forge(gh, rows, ref, jobs)
    with using(NativeRuntime(rt.providers, rt.settings, rt.project)):
        assert area._queue().cancelled_schedule_red_push() == ""


def test_a_hold_switches_off_every_pr_armed_on_github_not_only_the_recorded_ones(rt, gh):  # noqa: F811
    queue_one(rt, detail="")  # sm's record says nothing is armed (the arm call failed on our side but worked on GitHub)
    gh.on("pr list", [{"number": 801, "autoMergeRequest": {"mergeMethod": "SQUASH"}}, {"number": 999, "autoMergeRequest": {"mergeMethod": "SQUASH"}}, {"number": 5, "autoMergeRequest": None}]).on(
        "pr merge", ""
    )
    invoke(rt, ["queue", "hold", "--reason", "stop"])
    area.reset()
    assert invoke(rt, ["daemon", "queue"]).exit_code == 0
    assert sorted(merges(gh)) == [f"pr merge 801 -R {CORE} --disable-auto", f"pr merge 999 -R {CORE} --disable-auto"]
    assert items(rt)[-1]["detail"] == "merge freeze: auto-merge off until it lifts"


def test_an_armed_pr_that_falls_behind_main_is_switched_off_again(rt, gh, monkeypatch):  # noqa: F811
    """Gate review finding 2 of PR 70: the merge rule is read on every tick, not only at the arm."""
    queue_one(rt, detail="auto-merge armed")
    gh.on("pr list", []).on("pr merge", "").on("pr view 801", {**PR, "autoMergeRequest": {"mergeMethod": "SQUASH"}})
    monkeypatch.setattr(main_plan, "gap", lambda pr, head="": "merge rule: #801 is behind against main: update the branch and let CI test it again")
    assert tick(rt).exit_code == 0
    assert merges(gh) == [f"pr merge 801 -R {CORE} --disable-auto"] and items(rt)[-1]["status"] == "needs work"


PLUGINS = "helmut-hoffer-von-ankershoffen/heinzel-plugins"
RED_CHECK = [{"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE", "completedAt": "2026-11-12T20:00:00Z"}]


def test_a_failing_check_stops_the_arm_in_every_repository(rt, gh):  # noqa: F811
    """Gate review of 4ef48f7, findings 1 and 3: the train's check rule applies to plugins too and to checks the ruleset does not name."""
    queue_one(rt, key="plugins #801", repo=PLUGINS)
    gh.on("pr list", []).on("pr merge", "").on("pr view 801", {**PR, "statusCheckRollup": PR["statusCheckRollup"] + RED_CHECK})
    assert tick(rt).exit_code == 0
    assert not merges(gh) and items(rt)[-1]["status"] == "needs work" and "lint" in items(rt)[-1]["detail"]


def test_checks_the_tools_require_are_demanded_before_the_arm(rt, gh, monkeypatch):  # noqa: F811
    """Finding 2: a private repository has no ruleset; `tool_required_checks` stays the rule."""
    from steuermaschine_heinzel.native.queue_lib import hzlib

    queue_one(rt)
    gh.on("pr list", []).on("pr merge", "")
    monkeypatch.setattr(hzlib, "tool_required_checks", lambda repo: {"coverage-floor", "extra-check"})
    assert tick(rt).exit_code == 0
    assert not merges(gh) and "required by the tools, not passed: extra-check" in items(rt)[-1]["detail"]


def test_one_core_pr_is_armed_at_a_time(rt, gh):  # noqa: F811
    """Finding 4: two PRs armed in one tick would both land on a main CI never tested; the next arms after the first merged."""
    gh.on("pr list", [{"number": 900, "autoMergeRequest": {"mergeMethod": "SQUASH"}}]).on("pr merge", "")
    queue_one(rt)
    assert tick(rt).exit_code == 0
    assert not merges(gh) and "#900 is armed; one Core PR at a time" in items(rt)[-1]["detail"]
    gh.on("pr list", "not json")  # unreadable fails closed
    assert tick(rt).exit_code == 0 and not merges(gh)


def test_reviewed_progress_needs_its_main_observation_unchanged_at_the_arm(rt, gh, monkeypatch):  # noqa: F811
    """Finding 5: the progress anchor is checked before the arm, as the train did before the merge."""
    queue_one(rt, reviewed_head=HEAD, reviewed_progress_main="why", progress_anchor={"completed": [], "pending": []})
    gh.on("pr list", []).on("pr merge", "")
    monkeypatch.setattr("steuermaschine_heinzel.native.queue_lib.queue_daemon.progress_anchor_unchanged", lambda anchor: False)
    assert tick(rt).exit_code == 0
    assert not merges(gh) and items(rt)[-1]["status"] == "needs work" and "observation changed" in items(rt)[-1]["detail"]
