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
    monkeypatch.setattr(main_plan, "gap", lambda pr, head="", gate_in_ruleset=False: seen.append((pr, head, gate_in_ruleset)) or reason)
    assert tick(rt).exit_code == 0
    row = items(rt)[-1]
    assert not merges(gh) and row["status"] == status and reason in row["detail"] and seen == [("801", HEAD, True)]


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
