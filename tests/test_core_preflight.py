"""The preflight framework: ordered checks, skips named in the verdict, opt-in checks, checks that need a green run
before them, the READY / NOT READY verdict with numbered failures and fixes, and the report as text and as a document."""

import json

import pytest

from steuermaschine.preflight import BLOCKED, FAIL, PASS, SKIP, WARN, Preflight, PreflightReport, Step, verdict


def passing(name, detail=""):
    return lambda ctx: Step(name, detail=detail)


def failing(name, message="it is broken\n       | evidence", fix="repair it"):
    return lambda ctx: Step(name).fail(message, fix)


def test_checks_run_in_the_order_they_were_registered_and_get_the_context_unchanged():
    checks, seen, said = Preflight(), [], []

    @checks.check("tree")
    def tree(ctx):
        seen.append(("tree", ctx))
        return Step("tree", detail="2 commit(s) ahead")

    checks.add("lint", lambda ctx: seen.append(("lint", ctx)) or Step("lint"))
    ctx = object()
    steps, skipped, not_run = checks.run(ctx, say=said.append)
    assert checks.names == ("tree", "lint") and seen == [("tree", ctx), ("lint", ctx)] and said == ["... tree", "... lint"]
    assert [(s.name, s.status, s.detail) for s in steps] == [("tree", PASS, "2 commit(s) ahead"), ("lint", PASS, "")] and (skipped, not_run) == ([], [])
    assert tree(ctx).name == "tree"  # the decorator hands the function back
    with pytest.raises(ValueError, match="already registered"):
        checks.add("tree", passing("tree"))


def test_a_step_fails_with_a_message_and_a_fix_and_a_warning_never_fails_it():
    step = Step("coverage")
    assert step.warn("margin is only 1.2 points") is step and step.status == WARN and step.data["warnings"] == ["margin is only 1.2 points"] and not step.failures
    assert step.fail("below 90 %", "cover the missing lines") is step and step.status == FAIL and step.failures == [("below 90 %", "cover the missing lines")]
    step.warn("another")
    step.fail("second", "fix two")
    assert step.status == FAIL and len(step.failures) == 2 and len(step.data["warnings"]) == 2


def test_a_skipped_check_does_not_run_and_the_verdict_names_every_skipped_one():
    checks, ran = Preflight(), []
    for name in ("tree", "lint", "typos"):
        checks.add(name, lambda ctx, name=name: ran.append(name) or Step(name))
    said = []
    steps, skipped, not_run = checks.run(None, skip=["typos", "tree"], say=said.append)
    assert ran == ["lint"] and said == ["... lint"] and skipped == ["tree", "typos"]  # in the order of the checks, not of the flags
    assert [(s.name, s.status, s.detail) for s in steps] == [("tree", SKIP, "skipped by --skip"), ("lint", PASS, ""), ("typos", SKIP, "skipped by --skip")]
    assert verdict(steps, skipped, not_run) == ("READY (SKIPPED by flag: tree, typos)", [])
    assert checks.unknown(["lint", "nothing", "tree", "else"]) == ["nothing", "else"]


def test_an_opt_in_check_runs_only_when_asked_for_and_is_named_when_it_did_not_run():
    checks = Preflight()
    checks.add("tree", passing("tree"))
    checks.add("hosted", passing("hosted", "two lanes green"), opt_in=True)
    steps, _, not_run = checks.run(None, not_run=["consumers"])
    assert [s.name for s in steps] == ["tree"] and not_run == ["consumers", "hosted"]
    assert verdict(steps, [], not_run)[0] == "READY (opt-in not run: consumers, hosted)"
    steps, _, not_run = checks.run(None, opted={"hosted"})
    assert [(s.name, s.detail) for s in steps] == [("tree", ""), ("hosted", "two lanes green")] and not_run == []


def test_a_check_that_needs_a_green_run_is_skipped_after_a_failure_and_says_why():
    def build(first):
        checks, ran = Preflight(), []
        checks.add("lint", first)
        checks.add("hosted", lambda ctx: ran.append("hosted") or Step("hosted"), after_green=True, blocked="not run: no hosted minutes for a head that fails locally")
        checks.add("later", lambda ctx: ran.append("later") or Step("later"), after_green=True)
        return checks, ran

    checks, ran = build(failing("lint"))
    steps, skipped, not_run = checks.run(None)
    assert ran == [] and skipped == [] and not_run == ["hosted", "later"]
    assert [(s.name, s.status, s.detail) for s in steps[1:]] == [("hosted", SKIP, "not run: no hosted minutes for a head that fails locally"), ("later", SKIP, BLOCKED)]
    assert verdict(steps, skipped, not_run)[0] == "NOT READY: 1 failure(s) (opt-in not run: hosted, later)"
    checks, ran = build(lambda ctx: Step("lint").warn("slow"))  # a warning is not a failure
    assert checks.run(None)[2] == [] and ran == ["hosted", "later"]


def test_the_verdict_is_ready_or_a_numbered_list_of_exact_failures_each_with_its_fix():
    checks = Preflight()
    checks.add("tree", lambda ctx: Step("tree").fail("working tree is not clean (3 paths)", "commit the change").fail("head is not ahead", "commit on the branch"))
    checks.add("lint", lambda ctx: Step("lint", detail="make lint green").warn("slow today"))
    checks.add("typos", failing("typos"))
    checks.add("dup", passing("dup"))
    steps, skipped, not_run = checks.run(None, skip={"dup"}, not_run=["consumers"])
    report = PreflightReport.of("preflight of abc123", steps, skipped, not_run)
    assert not report.ready and report.exit_code == 1
    assert report.verdict == "NOT READY: 3 failure(s) (SKIPPED by flag: dup) (opt-in not run: consumers)"
    assert [(f.number, f.step, f.fix) for f in report.failures] == [(1, "tree", "commit the change"), (2, "tree", "commit on the branch"), (3, "typos", "repair it")]
    assert report.render() == [
        "preflight of abc123",
        "  [FAIL] tree: working tree is not clean (3 paths)",  # no summary line: the first line of its first failure
        "  [WARN] lint: make lint green",
        "         warning: slow today",
        "  [FAIL] typos: it is broken",
        "  [SKIP] dup: skipped by --skip",
        "NOT READY: 3 failure(s) (SKIPPED by flag: dup) (opt-in not run: consumers)",
        "  1. tree: working tree is not clean (3 paths)",
        "     fix: commit the change",
        "  2. tree: head is not ahead",
        "     fix: commit on the branch",
        "  3. typos: it is broken\n       | evidence",
        "     fix: repair it",
    ]


def test_a_ready_report_exits_zero_and_a_failure_without_text_still_renders():
    checks = Preflight()
    checks.add("tree", passing("tree", "clean"))
    report = PreflightReport.of("t", *checks.run(None))
    assert report.ready and report.exit_code == 0 and report.verdict == "READY" and report.render() == ["t", "  [PASS] tree: clean", "READY"]
    empty = PreflightReport.of("t", [Step("x").fail("", "fix")], [], [])
    assert empty.render()[1] == "  [FAIL] x: " and empty.exit_code == 1


def test_the_summary_document_is_the_report_as_json_and_a_plugin_adds_its_own_fields(tmp_path):
    class HeadReport(PreflightReport):
        head: str = ""

        def render(self) -> list[str]:
            return [*super().render(), f"head: {self.head}"]

    steps = [Step("coverage", detail="95 %", data={"percent": 95.0, "files": ["src/x.py"]}).warn("close"), Step("deps").fail("changed", "relock")]
    report = HeadReport.of("t", steps, [], ["hosted"], head="a" * 40)
    path = report.save(tmp_path / "deep" / "summary.json")
    document = json.loads(path.read_text())
    assert HeadReport.model_validate(document) == report and document == report.model_dump(mode="json") and report.render()[-1] == "head: " + "a" * 40
    assert document["ready"] is False and document["not_run"] == ["hosted"] and document["failures"] == [{"number": 1, "step": "deps", "message": "changed", "fix": "relock"}]
    assert document["steps"][0] == {"name": "coverage", "status": "WARN", "detail": "95 %", "data": {"percent": 95.0, "files": ["src/x.py"], "warnings": ["close"]}}
