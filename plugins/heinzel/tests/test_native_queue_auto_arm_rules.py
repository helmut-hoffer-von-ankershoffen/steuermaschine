"""PR 70 review findings 1, 2 and 5: auto_arm applies the old merge rule and red-main hold, and `hold` reads GitHub."""

import json

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



def test_owned_reviewed_release_freeze_only_arms_exact_candidate(rt, gh, monkeypatch):  # noqa: F811
    from steuermaschine_heinzel.native.release_lib import exact_queue

    candidate = exact_queue.Candidate("0.9.55", "1" * 32, 801, HEAD, MAIN, "3" * 40, 42)
    queue_one(rt, reviewed_head=HEAD)
    queue_one(rt, key="802", pr="802")
    queue_one(rt, key="plugins #803", pr="803", repo=PLUGINS)
    gh.on("pr list", []).on("pr merge", "").on("pr view 802", PR).on("pr view 803", PR)
    gh.on(f"repos/{CORE}/git/matching-refs/tags/v0.9.55", [])
    monkeypatch.setattr(exact_queue, "reviewed_freeze_active", lambda: True)
    monkeypatch.setattr(exact_queue, "queue_candidate", lambda item: candidate if item["key"] == "801" else None)
    verified = []
    monkeypatch.setattr(exact_queue, "verify_open", verified.append)
    assert tick(rt).exit_code == 0
    route = f"repos/{CORE}/pulls/801/merge"
    direct = [line for line in gh.lines() if route in line]
    assert direct == [f"api --method PUT {route} -f merge_method=squash -f sha={HEAD} -f commit_title=chore(release): v0.9.55"]
    assert not merges(gh) and verified == [candidate]
    assert not any(line.startswith("pr update-branch") for line in gh.lines())

    gh.on("pr view 801", {**PR, "autoMergeRequest": {"commitHeadline": candidate.subject}})
    assert tick(rt).exit_code == 0
    assert merges(gh)[-1] == f"pr merge 801 -R {CORE} --disable-auto"

    gh.on("pr view 801", PR).on(f"repos/{CORE}/git/matching-refs/tags/v0.9.55", [{"ref": "refs/tags/v0.9.55"}])
    assert tick(rt).exit_code == 0
    assert [line for line in gh.lines() if route in line] == direct
    assert "tag exists" in next(row["detail"] for row in items(rt) if row["key"] == "801")


def test_release_authority_loss_after_verification_prevents_merge(rt, gh, monkeypatch):  # noqa: F811
    from steuermaschine_heinzel.native.release_lib import exact_queue

    candidate = exact_queue.Candidate("0.9.55", "1" * 32, 801, HEAD, MAIN, "3" * 40, 42)
    queue_one(rt, reviewed_head=HEAD)
    gh.on("pr list", []).on("pr merge", "")
    gh.on(f"repos/{CORE}/git/matching-refs/tags/v0.9.55", [])
    auth = iter([candidate, None])
    monkeypatch.setattr(exact_queue, "queue_candidate", lambda item: next(auth))
    monkeypatch.setattr(exact_queue, "verify_open", lambda candidate: None)
    assert tick(rt).exit_code == 0
    assert not merges(gh)
    assert not any(line.startswith("api --method PUT ") for line in gh.lines())
    assert "approval or owned hold changed" in items(rt)[-1]["detail"]


def test_reviewed_release_stops_when_core_auto_merge_cannot_be_disarmed(rt, gh, monkeypatch):  # noqa: F811
    from steuermaschine_heinzel.native.queue_lib import auto_arm
    from steuermaschine_heinzel.native.release_lib import exact_queue

    candidate = exact_queue.Candidate("0.9.55", "1" * 32, 801, HEAD, MAIN, "3" * 40, 42)
    queue_one(rt, reviewed_head=HEAD)
    gh.on("pr list", []).on("pr merge", "")
    disarms = []
    monkeypatch.setattr(auto_arm, "disarm_all", lambda: disarms.append(True) or ["#999"])
    monkeypatch.setattr(exact_queue, "queue_candidate", lambda item: candidate)
    monkeypatch.setattr(exact_queue, "verify_open", lambda _: pytest.fail("proof ran before successful disarm"))
    assert tick(rt).exit_code == 0
    assert disarms == [True] and not merges(gh)
    assert not any(line.startswith("api --method PUT ") for line in gh.lines())
    assert "auto-merge still armed" in items(rt)[-1]["detail"]


@pytest.mark.parametrize("disarm_result", ["", 403])
def test_unreadable_release_authority_disarms_when_possible(rt, gh, monkeypatch, disarm_result):  # noqa: F811
    from steuermaschine_heinzel.native.release_lib import exact_queue

    def unavailable(_):
        raise exact_queue.ExactReleaseError("candidate receipt was unreadable")

    queue_one(rt, reviewed_head=HEAD)
    gh.on("pr view 801", {**PR, "autoMergeRequest": {"mergeMethod": "SQUASH"}})
    gh.on("pr list", []).on("--disable-auto", disarm_result)
    monkeypatch.setattr(exact_queue, "queue_candidate", unavailable)
    result = tick(rt)
    assert result.exit_code == 0
    assert merges(gh) == [f"pr merge 801 -R {CORE} --disable-auto"]
    assert "cannot verify the exact release queue approval" in result.stdout
    assert ("could not be switched off" in result.stdout) == (disarm_result == 403)
    assert not any(line.startswith("api --method PUT ") for line in gh.lines())


def test_release_merge_conflict_is_resolved_by_readback_before_another_write(rt, gh, monkeypatch):  # noqa: F811
    from steuermaschine_heinzel.native.release_lib import exact_queue

    candidate = exact_queue.Candidate("0.9.55", "1" * 32, 801, HEAD, MAIN, "3" * 40, 42)
    queue_one(rt, reviewed_head=HEAD)
    route = f"repos/{CORE}/pulls/801/merge"
    gh.on("pr list", []).on(f"repos/{CORE}/git/matching-refs/tags/v0.9.55", []).on(route, 409)
    monkeypatch.setattr(exact_queue, "queue_candidate", lambda _: candidate)
    monkeypatch.setattr(exact_queue, "verify_open", lambda _: None)
    assert tick(rt).exit_code == 0
    assert "needs GitHub read-back" in items(rt)[-1]["detail"]
    gh.on("pr view 801", {**PR, "state": "MERGED", "mergeCommit": {"oid": "4" * 40}})
    assert tick(rt).exit_code == 0
    assert items(rt)[-1]["status"] == "merged"
    assert len([line for line in gh.lines() if line.startswith("api --method PUT ")]) == 1


def test_release_candidate_stays_queued_while_merging_checks_are_pending(rt, gh, monkeypatch):  # noqa: F811
    from steuermaschine_heinzel.native.release_lib import exact_queue

    candidate = exact_queue.Candidate("0.9.55", "1" * 32, 801, HEAD, MAIN, "3" * 40, 42)
    queue_one(rt, reviewed_head=HEAD)
    pending = {"name": "extra", "status": "IN_PROGRESS", "conclusion": ""}
    gh.on("pr view 801", {**PR, "statusCheckRollup": [*PR["statusCheckRollup"], pending]})
    monkeypatch.setattr(exact_queue, "queue_candidate", lambda _: candidate)
    monkeypatch.setattr(exact_queue, "verify_open", lambda _: pytest.fail("merge-time proof ran before checks settled"))
    assert tick(rt).exit_code == 0
    assert items(rt)[-1]["detail"] == "not armed: checks not settled"
    assert not merges(gh) and not any(line.startswith("api --method PUT ") for line in gh.lines())


@pytest.mark.parametrize(("blocked_by", "expected"), [
    ("main", "main is red"),
    ("required check", "required by the tools, not passed"),
    ("merge rule", "the reviewed head is behind"),
])
def test_disarm_failure_does_not_clear_a_blocked_pr(rt, gh, monkeypatch, blocked_by, expected):  # noqa: F811
    from steuermaschine_heinzel.native.queue_lib import auto_arm, hzlib

    queue_one(rt, detail=auto_arm.ARMED)
    gh.on("pr list", []).on("--disable-auto", 403)
    gh.on("pr view 801", {**PR, "autoMergeRequest": {"mergeMethod": "SQUASH"}})
    if blocked_by == "main":
        monkeypatch.setattr(auto_arm, "main_hold", lambda *_: "main is red")
    elif blocked_by == "required check":
        monkeypatch.setattr(hzlib, "tool_required_checks", lambda _: {"additional release check"})
    else:
        monkeypatch.setattr(main_plan, "gap", lambda *_: expected)
    result = tick(rt)
    assert result.exit_code == 0
    assert merges(gh) == [f"pr merge 801 -R {CORE} --disable-auto"]
    assert expected in result.stdout and "could not be switched off" in result.stdout
    assert items(rt)[-1]["status"] == "queued"
    assert not any(line.startswith("api --method PUT ") for line in gh.lines())


def test_github_armed_pr_is_reconciled_with_local_queue_on_restart(rt, gh):  # noqa: F811
    queue_one(rt)
    gh.on("pr list", []).on("pr view 801", {**PR, "autoMergeRequest": {"mergeMethod": "SQUASH"}})
    assert tick(rt).exit_code == 0
    assert items(rt)[-1]["detail"] == "auto-merge armed"
    assert tick(rt).exit_code == 0 and not merges(gh)


def test_a_freeze_race_reports_a_failed_disarm_and_retries(rt, gh, monkeypatch):  # noqa: F811
    from steuermaschine_heinzel.native.queue_lib import auto_arm

    queue_one(rt)
    gh.on("pr list", []).on("pr merge", "").on("--disable-auto", 403)
    freeze = iter(["", "release freeze"])
    monkeypatch.setattr(auto_arm, "freeze_without_candidate", lambda _: next(freeze))
    result = tick(rt)
    assert result.exit_code == 0
    assert merges(gh) == [ARM, f"pr merge 801 -R {CORE} --disable-auto"]
    assert "auto-merge could not be switched off" in result.stdout
    assert items(rt)[-1]["detail"] == "auto-merge armed"


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
RECOVERY_JOB = {"name": "End-to-end cli (ubuntu-24.04)", "status": "completed", "conclusion": "failure",
                "id": 1, "run_id": 500, "head_sha": MAIN, "run_attempt": 1}
RECOVERY_JOBS = {"total_count": 2, "jobs": [RECOVERY_JOB, {**RECOVERY_JOB, "id": 2, "name": "CI OK"}]}
SCHEDULE_JOBS = {"total_count": 2, "jobs": [
    {**RECOVERY_JOB, "id": 3, "run_id": 502, "run_attempt": 2, "conclusion": "cancelled"},
    {**RECOVERY_JOB, "id": 4, "run_id": 502, "run_attempt": 2, "name": "CI OK"},
]}
REF = {"ref": "refs/heads/main", "object": {"sha": MAIN, "type": "commit",
       "url": f"https://api.github.com/repos/{CORE}/git/commits/{MAIN}"}}
EVIDENCE = {"push_run": 500, "push_attempt": 1, "head": MAIN, "schedule_run": 502, "schedule_attempt": 2}
MITIGATION = {"reviewed_head": HEAD, "mitigation_main": "reviewed exact revert", "mitigation_evidence": "verified prior tree",
              "fixes_main": False}


def direct_run(row):
    return {"id": row["databaseId"], "run_attempt": row["attempt"], "head_sha": row["headSha"], "head_branch": "main",
            "event": row["event"], "path": ".github/workflows/ci.yml", "status": row["status"], "conclusion": row["conclusion"],
            "repository": {"full_name": CORE}, "head_repository": {"full_name": CORE}}


def recovery_forge(gh, rows, ref=REF, jobs=RECOVERY_JOBS):  # noqa: F811
    gh.on("run list", rows).on(f"repos/{CORE}/git/ref/heads/main", ref)
    gh.on(f"repos/{CORE}/actions/runs/500", direct_run(PUSH)).on(f"repos/{CORE}/actions/runs/502", direct_run(SCHEDULE))
    gh.on(f"repos/{CORE}/actions/runs/500/attempts/1/jobs?per_page=100", jobs)
    gh.on(f"repos/{CORE}/actions/runs/502/attempts/2/jobs?per_page=100", SCHEDULE_JOBS)
    gh.on("pr list", []).on("pr merge", "")


@pytest.mark.parametrize("previous_pin", [{}, {**PUSH, "headSha": "2" * 40, "databaseId": 499}])
def test_cancelled_schedule_pins_failed_push_before_reviewed_recovery(rt, gh, previous_pin):  # noqa: F811
    recovery_forge(gh, [SCHEDULE, MACOS, PUSH])
    data = json.loads(rt.state_file.read_text())
    data["main_failure"] = previous_pin
    rt.state_file.write_text(json.dumps(data))
    queue_one(rt, fixes_main=False)
    queue_one(rt, key="802", pr="802", fixes_main=True)
    queue_one(rt, key="803", pr="803", **MITIGATION)
    gh.on("pr view 802", PR).on("pr view 803", PR)
    out = tick(rt)
    assert out.exit_code == 0 and merges(gh) == [ARM.replace("801", "803")]
    assert all("only a reviewed fix or measured mitigation is armed" in row["detail"] for row in items(rt)[-3:-1])
    listed = invoke(rt, ["queue", "list", "--active"])
    assert f"failed push run 500 attempt 1 at {MAIN}; later scheduled run 502 attempt 2 cancelled" in listed.stdout
    pin = {**(previous_pin or PUSH), "cancelled_schedule": EVIDENCE}
    assert json.loads(rt.state_file.read_text())["main_failure"] == pin

    # GitHub reports the mitigation merged and its main run was green. Neither outcome clears the hold.
    merged = "3" * 40
    gh.on("pr view 803", {**PR, "state": "MERGED", "mergeCommit": {"oid": merged}})
    gh.on("run list", [{**MAIN_RUNS[0], "event": "push", "headSha": merged, "databaseId": 503}])
    assert tick(rt).exit_code == 0 and merges(gh) == [ARM.replace("801", "803")]
    data = json.loads(rt.state_file.read_text())
    assert data["main_failure"] == pin and data["items"][-1]["status"] == "merged"
    assert "only a reviewed fix or measured mitigation" in data["items"][-3]["detail"]


def test_reviewed_recovery_fix_requires_exact_merged_head_and_first_push_pass(rt, gh):  # noqa: F811
    from steuermaschine.native import NativeRuntime, using

    recovery_forge(gh, [SCHEDULE, PUSH])
    admitted = invoke(rt, ["queue", "add", "--fixes-main", "--reviewed", "reviewed exact fix", "--reviewed-head", HEAD, "801"])
    assert admitted.exit_code == 0 and items(rt)[-1]["reviewed_head"] == HEAD
    assert tick(rt).exit_code == 0 and merges(gh) == [ARM]
    sha = "3" * 40
    gh.on("pr view 801", {**PR, "state": "MERGED", "baseRefName": "main", "headRefOid": "4" * 40, "mergeCommit": {"oid": sha}})
    assert tick(rt).exit_code == 0 and items(rt)[-1]["status"] == "queued"
    assert not json.loads(rt.state_file.read_text())["main_failure"].get("fixed_by")
    gh.on("pr view 801", {**PR, "state": "MERGED", "baseRefName": "main"})
    assert tick(rt).exit_code == 0 and items(rt)[-1]["status"] == "queued"
    gh.on("pr view 801", {**PR, "state": "MERGED", "baseRefName": "main", "mergeCommit": {"oid": sha}})
    assert tick(rt).exit_code == 0 and items(rt)[-1]["status"] == "merged"
    assert json.loads(rt.state_file.read_text())["main_failure"]["fixed_by"] == [sha]

    green = {**MAIN_RUNS[0], "event": "push", "headSha": sha, "databaseId": 503}
    with using(NativeRuntime(rt.providers, rt.settings, rt.project)):
        queue = area._queue()
        gh.on("run list", [{**green, "event": "schedule"}])
        assert not queue.main_is_green()
        gh.on("run list", [{**green, "attempt": 2}])
        assert not queue.main_is_green()
        gh.on("run list", [green])
        assert queue.main_is_green()
    assert not json.loads(rt.state_file.read_text())["main_failure"]


@pytest.mark.parametrize(
    "details",
    [
        {**MITIGATION, "reviewed_head": ""},
        {**MITIGATION, "mitigation_evidence": " "},
        {**MITIGATION, "safety_fix_main": "conflicting admission purpose"},
    ],
)
def test_underspecified_recovery_stays_held_on_proven_red(rt, gh, details):  # noqa: F811
    recovery_forge(gh, [SCHEDULE, PUSH])
    queue_one(rt, **details)
    assert tick(rt).exit_code == 0 and not merges(gh)
    assert "only a reviewed fix or measured mitigation" in items(rt)[-1]["detail"]


@pytest.mark.parametrize(
    ("rows", "ref", "jobs"),
    [
        (None, REF, RECOVERY_JOBS),
        ([SCHEDULE, PUSH], None, RECOVERY_JOBS),
        ([SCHEDULE, PUSH], {"object": {"sha": "2" * 40}}, RECOVERY_JOBS),
        ([{**SCHEDULE, "headSha": "2" * 40}, PUSH], REF, RECOVERY_JOBS),
        ([{**SCHEDULE, "databaseId": 499}, PUSH], REF, RECOVERY_JOBS),
        ([SCHEDULE, {**PUSH, "status": "queued", "conclusion": "", "databaseId": 501}, PUSH], REF, RECOVERY_JOBS),
        ([SCHEDULE, {**PUSH, "conclusion": "success", "databaseId": 501}, PUSH], REF, RECOVERY_JOBS),
        ([SCHEDULE, PUSH], REF, None),
        ([SCHEDULE, PUSH], REF, {**RECOVERY_JOBS, "total_count": 3}),
        ([SCHEDULE, PUSH], REF, {"total_count": 1, "jobs": [{**RECOVERY_JOB, "name": "CI OK"}]}),
        ([SCHEDULE, PUSH], REF, {**RECOVERY_JOBS, "jobs": [{**RECOVERY_JOB, "head_sha": "2" * 40}, RECOVERY_JOBS["jobs"][1]]}),
    ],
)
def test_unknown_or_replaced_push_evidence_cannot_authorize_recovery(rt, gh, rows, ref, jobs):  # noqa: F811
    recovery_forge(gh, rows, ref, jobs)
    queue_one(rt, **MITIGATION)
    assert tick(rt).exit_code == 0 and not merges(gh)
    assert "main CI evidence is unknown" in items(rt)[-1]["detail"]


@pytest.mark.parametrize(
    ("run", "field", "value"),
    [
        (500, "repository", {"full_name": "other/repo"}),
        (500, "head_repository", None),
        (500, "event", "schedule"),
        (500, "path", ".github/workflows/other.yml"),
        (500, "head_branch", "other"),
        (500, "head_sha", "2" * 40),
        (500, "run_attempt", 2),
        (502, "conclusion", "success"),
        (502, "head_sha", "2" * 40),
    ],
)
def test_mismatched_direct_run_metadata_holds_recovery(rt, gh, run, field, value):  # noqa: F811
    recovery_forge(gh, [SCHEDULE, PUSH])
    gh.on(f"repos/{CORE}/actions/runs/{run}", {**direct_run(PUSH if run == 500 else SCHEDULE), field: value})
    queue_one(rt, **MITIGATION)
    assert tick(rt).exit_code == 0 and not merges(gh)
    assert "main CI evidence is unknown" in items(rt)[-1]["detail"]


def test_cancelled_schedule_with_its_own_failed_workload_is_not_inconclusive(rt, gh):  # noqa: F811
    recovery_forge(gh, [SCHEDULE, PUSH])
    jobs = [*SCHEDULE_JOBS["jobs"], {**SCHEDULE_JOBS["jobs"][0], "id": 5, "name": "lint", "conclusion": "failure"}]
    gh.on(f"repos/{CORE}/actions/runs/502/attempts/2/jobs?per_page=100", {"total_count": 3, "jobs": jobs})
    queue_one(rt, **MITIGATION)
    assert tick(rt).exit_code == 0 and not merges(gh)
    assert "main CI evidence is unknown" in items(rt)[-1]["detail"]


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
