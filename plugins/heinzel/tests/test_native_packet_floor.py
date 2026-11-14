import json
import os
import subprocess

from packet_paths import tool  # noqa: F401  (the fixture that imports a moved module)

HEAD, MAIN, BASE = "a" * 40, "b" * 40, "c" * 40
PROTECTED = ["scripts/focused_plan.py", "uv.lock"]


def facts(**kw):
    base = {"pr": {"number": 778, "state": "OPEN", "isDraft": False, "baseRefName": "main", "headRefOid": HEAD},
            "head": HEAD, "main": MAIN, "protected": PROTECTED, "base_is_ancestor": True,
            "blobs": {p: "1" * 40 for p in PROTECTED},
            "plan": {"head": HEAD, "base": MAIN, "production": {"src/heinzel/x.py": "2" * 40}, "selection": {"python": ["tests/x_test.py"], "web": ["web/a.test.ts"]}, "execution": {p: "1" * 40 for p in PROTECTED}},
            "checks": [{"name": "Focused tests (python)", "conclusion": "success"}, {"name": "Focused tests (web)", "conclusion": "success"}, {"name": "Coverage", "conclusion": "skipped"}]}
    base.update(kw)
    return base


def test_ready(tool):
    assert tool("floor_check").evaluate(facts()) == []


def test_draft_pr_is_the_778_failure(tool):
    f = facts()
    f["pr"]["isDraft"] = True
    reasons = tool("floor_check").evaluate(f)
    assert len(reasons) == 1 and "draft" in reasons[0] and "gh pr ready 778" in reasons[0] and "rerun merge-gate" in reasons[0]


def test_missing_pr_plan_and_stale_head(tool):
    check = tool("floor_check")
    assert "no open PR" in check.evaluate(facts(pr=None))[0]
    assert any("no focused plan" in r for r in check.evaluate(facts(plan=None)))
    f = facts()
    f["pr"]["headRefOid"] = "d" * 40
    assert any("not the local head" in r for r in check.evaluate(f))


def test_plan_base_must_be_main_or_ancestor(tool):
    check = tool("floor_check")
    moved = facts(plan={**facts()["plan"], "base": BASE})
    assert check.evaluate(moved) == []
    moved["base_is_ancestor"] = False
    assert any("re-admit" in r for r in check.evaluate(moved))


def test_protected_pin_drift(tool):
    f = facts(blobs={"scripts/focused_plan.py": "9" * 40, "uv.lock": "1" * 40})
    assert tool("floor_check").evaluate(f) == ["protected file differs from the plan pin: scripts/focused_plan.py"]


def test_coverage_floor_evidence(tool):
    check = tool("floor_check")
    assert any("`Focused tests (python)` on the head is absent" in r for r in check.evaluate(facts(checks=[])))
    skipped = [{"name": "Focused tests (python)", "conclusion": "success"}, {"name": "Focused tests (web)", "conclusion": "failure"}]
    assert check.evaluate(facts(checks=skipped)) == ["coverage floor evidence missing: `Focused tests (web)` on the head is failure"]
    assert any("coverage-floor check" in r for r in check.evaluate(facts(checks=[*facts()["checks"], {"name": "coverage-floor", "conclusion": "failure"}])))
    assert any("no python coverage lane" in r for r in check.evaluate(facts(plan={**facts()["plan"], "selection": {"web": ["w"]}})))


def whole(selection, **kw):
    return facts(plan={**facts()["plan"], "selection": selection}, **kw)


def test_whole_vitest_suite_needs_main_support_and_stands_alone(tool):
    check = tool("floor_check")
    ok = {"python": ["tests/x_test.py"], "web": ["web/ALL"]}
    assert check.evaluate(whole(ok, vitest_all="web/ALL")) == []
    assert any("has no VITEST_ALL" in r for r in check.evaluate(whole(ok)))
    mixed = {"python": ["tests/x_test.py"], "web": ["web/ALL", "web/a.test.ts"]}
    assert any("only a web lane on its own" in r for r in check.evaluate(whole(mixed, vitest_all="web/ALL")))
    other = {"python": ["tests/x_test.py", "web/ALL"], "web": ["web/a.test.ts"]}
    assert any("only a web lane on its own" in r for r in check.evaluate(whole(other, vitest_all="web/ALL")))
    no_run = [{"name": "Focused tests (python)", "conclusion": "success"}]
    assert check.evaluate(whole(ok, vitest_all="web/ALL", checks=no_run)) == [
        "coverage floor evidence missing: `Focused tests (web)` on the head is absent"]


def test_gather_with_fakes_makes_no_network_call(tool):
    check = tool("floor_check")
    calls = []

    def git(cmd):
        calls.append(cmd)
        if "rev-parse" in cmd and cmd[-1].endswith("^{commit}"):
            return (HEAD if cmd[-1].startswith("feat") else MAIN) + "\n"
        if "rev-parse" in cmd:
            return "1" * 40 + "\n"
        return ""

    def gh(cmd):
        calls.append(cmd)
        if cmd[1] == "pr":
            return json.dumps([{"number": 5, "state": "OPEN", "isDraft": True, "baseRefName": "main", "headRefOid": HEAD}])
        if "variables" in cmd[2]:
            return json.dumps({"variables": [{"name": "FOCUSED_CI_PLANS", "value": "{}"}]})
        return json.dumps({"check_runs": []})

    decode = lambda core, legacy, slots, key: ({"head": HEAD, "base": MAIN, "production": {}, "execution": {p: "1" * 40 for p in PROTECTED}}, PROTECTED)
    got = check.gather("feat/x", "/core", "o/r", "origin/main", git, gh, decode)
    assert got["pr"]["isDraft"] and got["head"] == HEAD and got["base_is_ancestor"]
    assert [c[0] for c in calls if c[0] == "gh"] and all(c[0] in {"git", "gh"} for c in calls)
    assert check.evaluate(got) and "draft" in check.evaluate(got)[0]
    assert got["vitest_all"] is None  # an older two-value decoder: no whole-suite support
    three = lambda *args: (*decode(*args), "web/ALL")
    assert check.gather("feat/x", "/core", "o/r", "origin/main", git, gh, three)["vitest_all"] == "web/ALL"

