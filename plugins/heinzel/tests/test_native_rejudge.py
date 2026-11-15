"""`sm head sonar` and `sm ci merge-gate rejudge`, and how `sm ci why` and `sm ci sonar-close --apply` use them.
GitHub is a FakeGh, Sonar a fake HTTP function: no test starts a process or reads the network."""

import json
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github.testing import FakeGh
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel.native import ci_ops, ci_read, rejudge
from steuermaschine_heinzel.plugin import plugin

REPO = "example/core"
PROJECT = "example_core"
HEAD = "2c6e817a" + "0" * 32
CI, GATE = 43438000001, 43438981284
GATE_URL = f"https://github.com/{REPO}/actions/runs/{GATE}"


class Web:
    """Sonar's web API: the answer of the first needle the URL contains."""

    def __init__(self, gate="OK", ratings=("1.0", "1.0", "1.0"), issues=(), down=False):
        self.gate, self.ratings, self.issues, self.down, self.asked = gate, ratings, list(issues), down, []

    def __call__(self, url, headers, timeout):
        self.asked.append(url)
        if self.down:
            return 0, "no route"
        if "qualitygates/project_status" in url:
            conditions = [] if self.gate == "OK" else [{"metricKey": "new_reliability_rating", "status": "ERROR", "actualValue": "5", "comparator": "GT", "errorThreshold": "1"}]
            return 200, json.dumps({"projectStatus": {"status": self.gate, "conditions": conditions}})
        if "measures/component" in url:
            names = list(rejudge.RATINGS)
            return 200, json.dumps({"component": {"measures": [{"metric": n, "period": {"value": v}} for n, v in zip(names, self.ratings, strict=True)]}})
        if "issues/search" in url:
            return 200, json.dumps({"total": len(self.issues), "issues": self.issues})
        return 404, ""


def runtime_for(tmp_path, gh, web=None, **settings):
    stack = stack_of(plugin)
    validated, _ = stack.validate({"heinzel": {"repo": REPO, "sonar_project": PROJECT, "legacy_state_dir": str(tmp_path / "state"), **settings}}, Path("."))
    providers = fake_providers(commands=gh, extra={"http_fetch": web or Web()}, tracker=None)
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=True), stack, validated, _providers=providers)


def gate_run(status="completed", conclusion="failure", attempt=1):
    return {"id": GATE, "name": "Merge gate", "path": ".github/workflows/merge-gate.yml", "head_sha": HEAD, "status": status, "conclusion": conclusion, "run_attempt": attempt}


def checks(conclusion="failure"):
    return {"check_runs": [{"name": n, "status": "completed", "conclusion": conclusion, "details_url": GATE_URL} for n in ("merge-gate", "coverage-floor")]  # the kit policy's merge-gate checks
            + [{"name": "CI OK", "status": "completed", "conclusion": "success", "details_url": f"https://github.com/{REPO}/actions/runs/{CI}/job/1"}]}


def github(ci_status="completed", ci_conclusion="success", gate_answers=None, check_answers=None, rerun=None) -> FakeGh:
    gh = FakeGh()
    gh.on("pulls/803", {"state": "open", "head": {"sha": HEAD}})
    gh.on("actions/runs?head_sha=", {"workflow_runs": [{"id": CI - 5, "name": "CI", "head_sha": HEAD, "status": "completed", "conclusion": "failure"},
                                                        {"id": CI, "name": "CI", "head_sha": HEAD, "status": ci_status, "conclusion": ci_conclusion},
                                                        {"id": CI + 9, "name": "CodeQL", "head_sha": HEAD, "status": "in_progress", "conclusion": None}]})
    gh.on(f"commits/{HEAD}/check-runs", *(check_answers or [checks()]))
    gh.on(f"actions/runs/{GATE}", *(gate_answers or [gate_run()]))
    gh.on(f"actions/runs/{GATE}/rerun", rerun if rerun is not None else {})
    return gh


# ------------------------------------------------------------------ sm head sonar
def test_head_sonar_reads_gate_ratings_and_open_bugs_read_only(tmp_path):
    issue = {"key": "AaHVNZ5", "type": "BUG", "severity": "BLOCKER", "component": f"{PROJECT}:src/x.py", "line": 7, "rule": "python:S1", "message": "bad"}
    web = Web(gate="ERROR", ratings=("5.0", "1.0", "1.0"), issues=[issue])
    gh = FakeGh()
    out = invoke(runtime_for(tmp_path, gh, web), ["head", "sonar", "803"])
    assert out.exit_code == 0, out
    lines = out.stdout.splitlines()
    assert lines[0] == "SONAR    PR 803: gate ERROR; reliability E, security A, maintainability A"
    assert lines[1] == "FAILING  new_reliability_rating 5 (GT 1)"
    assert lines[2] == "ISSUE    BUG BLOCKER AaHVNZ5 src/x.py:7 python:S1 bad"
    assert lines[-1].startswith("NEXT     sm ci sonar-close --pull-request 803 --issue KEY")
    assert all("pullRequest=803" in url and PROJECT in url for url in web.asked) and "types=BUG%2CVULNERABILITY" in web.asked[-1]
    assert gh.calls == []


def test_head_sonar_clean_names_the_rejudge_and_unread_is_unknown(tmp_path):
    clean = invoke(runtime_for(tmp_path, FakeGh()), ["--format", "json", "head", "sonar", "803"])
    report = rejudge.SonarHead.model_validate_json(clean.stdout)
    assert report.clean and report.ratings == {"reliability": "A", "security": "A", "maintainability": "A"} and report.open_issues == 0
    assert "sm ci merge-gate rejudge 803" in invoke(runtime_for(tmp_path, FakeGh()), ["head", "sonar", "803"]).stdout
    down = invoke(runtime_for(tmp_path, FakeGh(), Web(down=True)), ["head", "sonar", "803"])
    assert down.exit_code == 0 and "UNKNOWN  gate: no answer" in down.stdout and "NEXT     sm head sonar 803" in down.stdout


# ------------------------------------------------------------------ sm ci merge-gate rejudge
def test_rejudge_reruns_the_gate_run_the_details_url_names_and_prints_new_verdicts(tmp_path):
    gh = github(gate_answers=[gate_run(), gate_run("in_progress", None, 2), gate_run("completed", "success", 2)], check_answers=[checks(), checks("success")])
    rt = runtime_for(tmp_path, gh)
    out = invoke(rt, ["ci", "merge-gate", "rejudge", "803"])
    assert out.exit_code == 0, out
    assert gh.writes() == [f"api -X POST repos/{REPO}/actions/runs/{GATE}/rerun"]
    lines = out.stdout.splitlines()
    assert lines[0] == f"GATE     PR 803 head 2c6e817a: Merge gate run {GATE} (from the check's details URL, after CI run {CI}): re-run finished"
    assert "VERDICT  merge-gate: completed success" in lines and "VERDICT  coverage-floor: completed success" in lines
    assert lines[-1] == "NEXT     sm queue why 803"
    assert rt.providers.clock.slept == [rejudge.POLL_SECONDS, rejudge.POLL_SECONDS]
    assert not [c for c in gh.lines() if "claude" in c.lower()]


def test_explicit_gate_dry_run_preserves_the_verified_run_in_next_command(tmp_path):
    gh = github(gate_answers=[gate_run() | {"head_sha": "f" * 40, "event": "workflow_run"}])
    gh.on(f"run view {GATE}", workflow_input_log())
    out = invoke(runtime_for(tmp_path, gh), ["ci", "merge-gate", "rejudge", "803", "--gate-run", str(GATE), "--dry-run"])
    assert out.exit_code == 0 and not gh.writes()
    assert out.stdout.splitlines()[-1] == f"NEXT     sm ci merge-gate rejudge 803 --gate-run {GATE}"


def test_rejudge_dry_run_changes_nothing_and_a_red_verdict_exits_1(tmp_path):
    gh = github()
    dry = invoke(runtime_for(tmp_path, gh), ["--format", "json", "ci", "merge-gate", "rejudge", "803", "--dry-run"])
    result = rejudge.Rejudge.model_validate_json(dry.stdout)
    assert dry.exit_code == 0 and result.state == "would_rerun" and result.gate_run == str(GATE) and not gh.writes()
    red = github(gate_answers=[gate_run(), gate_run("completed", "failure", 2)], check_answers=[checks(), checks("failure")])
    assert invoke(runtime_for(tmp_path, red), ["ci", "merge-gate", "rejudge", "803"]).exit_code == 1


def test_dynamic_ci_title_is_identified_by_workflow_path_not_a_spoofed_name(tmp_path):
    gh = github()
    gh.on("actions/runs?head_sha=", {"workflow_runs": [
        {"id": CI, "name": "CI merge-tree", "path": ".github/workflows/ci.yml", "head_sha": HEAD,
         "status": "completed", "conclusion": "success"},
        {"id": CI + 1, "name": "CI", "path": ".github/workflows/other.yml", "head_sha": HEAD,
         "status": "in_progress"},
    ]})
    out = invoke(runtime_for(tmp_path, gh), ["ci", "merge-gate", "rejudge", "803", "--dry-run"])
    assert out.exit_code == 0 and f"after CI run {CI}" in out.stdout and not gh.writes()


def workflow_input_log(head=HEAD, ci=str(CI), pr="803"):
    prefix = "Merge gates\tVerify the original workload and applicable coverage evidence\t2026-11-15T21:32:59.788Z "
    lines = ["##[group]Run uv run --no-build --frozen --script scripts/focused_gate.py", "env:"]
    lines += [f"  {key}: {value}" for key, value in (("PR_HEAD_SHA", head), ("CI_RUN_ID", ci), ("PR_NUMBER", pr))]
    return "\n".join(prefix + line for line in [*lines, "##[endgroup]"]).replace("\t2026-", "\t\ufeff2026-", 1)


def test_explicit_workflow_gate_binds_trigger_inputs_instead_of_default_branch_sha(tmp_path):
    gh = github(gate_answers=[gate_run() | {"head_sha": "f" * 40, "event": "workflow_run"}])
    gh.on(f"run view {GATE}", workflow_input_log())
    out = invoke(runtime_for(tmp_path, gh), ["ci", "merge-gate", "rejudge", "803", "--gate-run", str(GATE), "--wait", "0"])
    assert out.exit_code == 0, out
    assert gh.writes() == [f"api -X POST repos/{REPO}/actions/runs/{GATE}/rerun"]


@pytest.mark.parametrize("log", [workflow_input_log(head="f" * 40), workflow_input_log(ci="1"),
                                  workflow_input_log(pr="804"), "",
                                  workflow_input_log().replace("  PR_NUMBER: 803", "  PR_NUMBER: 803\nMerge gates\t"
                                                              "Verify the original workload and applicable coverage evidence\t"
                                                              "2026-11-15T21:32:59.788Z   PR_NUMBER: 804"),
                                  workflow_input_log().replace("Verify the original workload and applicable coverage evidence", "unrelated"),
                                  workflow_input_log().replace("env:", "##[endgroup]\nenv:")])
def test_explicit_workflow_gate_refuses_missing_wrong_or_ambiguous_inputs(tmp_path, log):
    gh = github(gate_answers=[gate_run() | {"head_sha": "f" * 40, "event": "workflow_run"}])
    gh.on(f"run view {GATE}", log)
    out = invoke(runtime_for(tmp_path, gh), ["ci", "merge-gate", "rejudge", "803", "--gate-run", str(GATE)])
    assert out.exit_code == 1 and "does not bind exactly" in out.stderr and not gh.writes(), out


@pytest.mark.parametrize("change", [{"event": "pull_request"}, {"path": ".github/workflows/other.yml"}])
def test_explicit_workflow_gate_refuses_another_workflow_or_event(tmp_path, change):
    gh = github(gate_answers=[gate_run() | {"event": "workflow_run"} | change])
    out = invoke(runtime_for(tmp_path, gh), ["ci", "merge-gate", "rejudge", "803", "--gate-run", str(GATE)])
    assert out.exit_code == 1 and "not the trusted" in out.stderr and not gh.writes(), out


def test_rejudge_falls_back_to_the_newest_gate_run_of_the_head(tmp_path):
    gh = github(check_answers=[{"check_runs": [{"name": "merge-gate", "conclusion": "failure", "details_url": "https://github.com/x/runs/131"}]}])
    gh.on("workflows/merge-gate.yml/runs", {"workflow_runs": [gate_run() | {"id": GATE - 50}, gate_run(), gate_run() | {"head_sha": "f" * 40, "id": GATE + 1}]})
    out = invoke(runtime_for(tmp_path, gh), ["--format", "json", "ci", "merge-gate", "rejudge", "803", "--dry-run"])
    result = rejudge.Rejudge.model_validate_json(out.stdout)
    assert result.gate_run == str(GATE) and result.found_by == "the newest Merge gate run of the head"


def test_rejudge_still_running_after_the_wait_says_so(tmp_path):
    gh = github(gate_answers=[gate_run(), gate_run("in_progress", None, 2)])
    rt = runtime_for(tmp_path, gh)
    out = invoke(rt, ["ci", "merge-gate", "rejudge", "803", "--wait", "1"])
    assert out.exit_code == 0 and "re-run started, still running" in out.stdout and "NEXT     sm ci why 803" in out.stdout
    assert len(gh.writes()) == 1 and rt.providers.clock.slept == [30, 30]


@pytest.mark.parametrize(("gh", "web", "code", "said"), [
    (github(ci_status="in_progress", ci_conclusion=None), None, 75, "is in_progress: the Merge gate judges after it"),
    (github(ci_conclusion="cancelled"), None, 1, "was cancelled"),
    (github(gate_answers=[gate_run("in_progress", None)]), None, 75, "it is judging now"),
    (github(), Web(gate="ERROR", ratings=("5.0", "1.0", "1.0")), 1, "Sonar is not clean for PR 803"),
    (github(), Web(ratings=("1.0", "1.0", "2.0")), 1, "maintainability B"),
    (github(), Web(down=True), 1, "Sonar is not clean"),
    (github(rerun=403), None, 1, "GitHub refused the re-run"),
])
def test_rejudge_refuses_or_waits_before_anything_is_rerun(tmp_path, gh, web, code, said):
    out = invoke(runtime_for(tmp_path, gh, web), ["ci", "merge-gate", "rejudge", "803"])
    assert out.exit_code == code and said in out.stderr, out
    assert [w for w in gh.writes() if "rerun" in w] == ([f"api -X POST repos/{REPO}/actions/runs/{GATE}/rerun"] if "refused" in said else [])


def test_rejudge_refuses_a_closed_pr_a_head_without_ci_and_without_a_gate_run(tmp_path):
    closed = github()
    closed.on("pulls/803", {"state": "closed", "head": {"sha": HEAD}})
    assert "not open" in invoke(runtime_for(tmp_path, closed), ["ci", "merge-gate", "rejudge", "803"]).stderr
    no_ci = github()
    no_ci.on("actions/runs?head_sha=", {"workflow_runs": []})
    assert "no CI run for head" in invoke(runtime_for(tmp_path, no_ci), ["ci", "merge-gate", "rejudge", "803"]).stderr
    no_gate = github(check_answers=[{"check_runs": []}])
    no_gate.on("workflows/merge-gate.yml/runs", {"workflow_runs": []})
    out = invoke(runtime_for(tmp_path, no_gate), ["ci", "merge-gate", "rejudge", "803"])
    assert out.exit_code == 1 and "no Merge gate run for head 2c6e817a" in out.stderr and not no_gate.writes()


# ------------------------------------------------------------------ sm ci why traces the gate check
def test_ci_why_traces_a_red_gate_check_to_its_merge_gate_run_and_names_the_rejudge(tmp_path):
    gh = github()
    rollup = [{"name": "merge-gate", "status": "COMPLETED", "conclusion": "FAILURE", "detailsUrl": GATE_URL, "workflowName": "Claude review"},
              {"name": "CI OK", "status": "COMPLETED", "conclusion": "SUCCESS", "detailsUrl": f"https://github.com/{REPO}/actions/runs/{CI}/job/1", "workflowName": "CI"}]
    gh.on(f"pr view 803 -R {REPO} --json statusCheckRollup", {"statusCheckRollup": rollup})
    gh.on(f"pr view 803 -R {REPO} --json state", {"state": "OPEN", "mergeStateStatus": "BLOCKED", "headRefOid": HEAD, "title": "fix: a title"})
    gh.on("check-runs", "")
    gh.on("run list", [])
    out = invoke(runtime_for(tmp_path, gh), ["--format", "json", "ci", "why", "803"])
    assert out.exit_code == 0, out
    report = ci_read.CiWhy.model_validate_json(out.stdout)
    assert report.runs == []  # the Merge gate run is not read as a failed CI run
    assert report.gates == [f"merge-gate: FAILURE, shown under the check suite of Claude review; judged by Merge gate run {GATE} (attempt 1, completed failure, "
                            "from the check's details URL); Sonar's quality gate and ratings: sm head sonar 803"]
    assert report.next == "sm head sonar 803   (clean: sm ci merge-gate rejudge 803)" and report.trap == ci_read.TRAPS["gate"]
    assert "new head" not in report.next and not gh.writes()


# ------------------------------------------------------------------ sm ci sonar-close --apply ends with the re-judge
def test_sonar_close_apply_ends_with_the_rejudge_and_never_fails_the_closure(tmp_path, monkeypatch):
    rt = runtime_for(tmp_path, github(), ci_ops_sonar_closures_file=str(tmp_path / "closures.toml"))
    asked = []
    monkeypatch.setattr(ci_ops.sonar_close, "close_reviewed", lambda args: asked.append((args.apply, args.pull_request)))
    monkeypatch.setattr(ci_ops, "closures_of", lambda edge, live: ([ci_ops.Closure(issue="K", file="f", rule="r", transition="falsepositive", reason="x", state="closed")], []))
    calls = []
    monkeypatch.setattr(rejudge, "rejudge", lambda pr: calls.append(pr) or rejudge.Rejudge(pr=pr, head=HEAD, ci_run="1", gate_run="2", found_by="x", state="running"))
    out = invoke(rt, ["ci", "sonar-close", "--pull-request", "803", "--issue", "K", "--apply"])
    assert out.exit_code == 0 and calls == [803] and "re-judge: GATE     PR 803" in out.stdout, out

    def refuse(pr):
        raise rejudge.Refuse("Sonar is not clean", "sm head sonar 803")

    monkeypatch.setattr(rejudge, "rejudge", refuse)
    out = invoke(rt, ["ci", "sonar-close", "--pull-request", "803", "--issue", "K", "--apply"])
    assert out.exit_code == 0 and "re-judge: not run: Sonar is not clean" in out.stdout and "re-judge: NEXT sm head sonar 803" in out.stdout

    def later(pr):
        raise rejudge.Wait("CI is running")

    monkeypatch.setattr(rejudge, "rejudge", later)
    assert "re-judge: not yet: CI is running" in invoke(rt, ["ci", "sonar-close", "--pull-request", "803", "--issue", "K", "--apply"]).stdout
    off = runtime_for(tmp_path, github(), ci_ops_sonar_closures_file=str(tmp_path / "closures.toml"), ci_ops_rejudge_after_close=False)
    assert "re-judge: off" in invoke(off, ["ci", "sonar-close", "--pull-request", "803", "--issue", "K", "--apply"]).stdout
    dry = invoke(rt, ["ci", "sonar-close", "--pull-request", "803", "--issue", "K"])
    assert "re-judge" not in dry.stdout and asked[-1] == (False, 803) and asked[0] == (True, 803)


# ------------------------------------------------------------------ review follow-ups: no verdict, another head, not red
def test_rejudge_done_without_any_gate_check_is_not_success(tmp_path):
    gh = github(gate_answers=[gate_run(), gate_run("completed", "success", 2)], check_answers=[checks(), {"check_runs": []}])
    out = invoke(runtime_for(tmp_path, gh), ["ci", "merge-gate", "rejudge", "803"])
    assert out.exit_code == 1, out
    assert "re-run finished" in out.stdout and "VERDICT  none: no merge-gate or coverage-floor check found on head 2c6e817a" in out.stdout
    assert out.stdout.splitlines()[-1] == "NEXT     sm ci why 803"


def test_rejudge_refuses_a_details_url_run_of_another_head(tmp_path):
    gh = github(gate_answers=[gate_run() | {"head_sha": "f" * 40}])
    out = invoke(runtime_for(tmp_path, gh), ["ci", "merge-gate", "rejudge", "803"])
    assert out.exit_code == 1 and "judged head ffffffff, not the PR head 2c6e817a" in out.stderr, out
    assert not gh.writes()


@pytest.mark.parametrize("answer", [checks("success"), {"check_runs": [{"name": "merge-gate", "status": "in_progress", "conclusion": None, "details_url": GATE_URL}]}])
def test_rejudge_refuses_when_the_gate_is_not_red(tmp_path, answer):
    gh = github(check_answers=[answer], gate_answers=[gate_run(conclusion="success")])
    out = invoke(runtime_for(tmp_path, gh), ["ci", "merge-gate", "rejudge", "803", "--dry-run"])
    assert out.exit_code == 1 and "is not red" in out.stderr and "nothing to re-judge" in out.stderr, out
    assert not gh.writes()


def test_rejudge_without_gate_checks_follows_the_gate_run_conclusion():
    run = gate_run()
    assert rejudge.is_red([], run) and not rejudge.is_red([], run | {"conclusion": "success"}) and not rejudge.is_red([], run | {"conclusion": None})
    assert "no gate check; Merge gate run" in rejudge.gate_state([], run)


def test_sonar_close_reports_the_closures_when_the_rejudge_breaks_and_exits_1(tmp_path, monkeypatch):
    rt = runtime_for(tmp_path, github(), ci_ops_sonar_closures_file=str(tmp_path / "closures.toml"))
    monkeypatch.setattr(ci_ops.sonar_close, "close_reviewed", lambda args: None)
    monkeypatch.setattr(ci_ops, "closures_of", lambda edge, live: ([ci_ops.Closure(issue="K", file="f", rule="r", transition="falsepositive", reason="x", state="closed")], []))

    def broken(pr):
        raise KeyError("head")

    monkeypatch.setattr(rejudge, "rejudge", broken)
    out = invoke(rt, ["--format", "json", "ci", "sonar-close", "--pull-request", "803", "--issue", "K", "--apply"])
    assert out.exit_code == 1, out
    report = ci_ops.SonarClose.model_validate_json(out.stdout)
    assert report.rejudge_failed and [c.state for c in report.closures] == ["closed"]
    assert report.rejudge[0] == "re-judge: FAILED: KeyError: 'head'"
    text = invoke(rt, ["ci", "sonar-close", "--pull-request", "803", "--issue", "K", "--apply"])
    assert text.exit_code == 1 and "re-judge: FAILED" in text.stdout
