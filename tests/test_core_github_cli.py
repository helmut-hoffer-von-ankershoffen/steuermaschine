"""`sm ci ...` and `sm pr ...`: the generic commands on the reader Protocols, through the whole command tree."""

import json
from pathlib import Path

import pytest
from test_core_github_forge import JOBS, PR, RULES, RUN, check

from steuermaschine import USAGE, helpdoc
from steuermaschine.app import OUT, Runtime, command_tree
from steuermaschine.config import Project
from steuermaschine.github import cli as github_cli
from steuermaschine.github import models as m
from steuermaschine.github.sonar import Sonar
from steuermaschine.github.testing import FakeGh
from steuermaschine.protocols import Result
from steuermaschine.testing import fake_providers, invoke


@pytest.fixture(autouse=True)
def text_format(monkeypatch):
    for name in ("SM_FORMAT", "SM_GITHUB__REPO", "SM_SONAR__PROJECT_KEY", "SM_GITHUB__MAX_PAGES"):
        monkeypatch.delenv(name, raising=False)
    OUT["format"] = "text"
    yield
    OUT["format"] = "text"


def rt(fake=None, tables=None, **over):
    layers = [("command line", {}), ("environment", {}), ("project file x.toml", {"github": {"repo": "o/r"}} if tables is None else tables), ("user file y.toml", {"github": {"timeout_seconds": 5}})]
    project = Project(name="proj", plugin="none", root=Path("."), layers=layers, allow_unreviewed=True)  # `ci cancel` is under the review gate
    return Runtime(project, None, {}, {}, fake_providers(commands=fake or FakeGh(), **over))


def gh():
    fake = FakeGh()
    fake.on("pr view 12", PR).on("pr list -R o/r --head feat/x", [PR]).on("pr list -R o/r --state open", [PR, {**PR, "number": 13, "isDraft": True}])
    fake.on("pr checks 12", [check("CI OK", "pass"), check("lint", "fail")])
    fake.on("pr checks 12 -R o/r --json name,state,bucket,link,workflow,event,startedAt,completedAt --required", [check("CI OK", "pass")])
    fake.on("rules/branches/main", RULES).on("protection/required_status_checks", 404).on("compare/main...", {"behind_by": 0})
    fake.on("api graphql", {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}})
    fake.on("actions/runs", {"total_count": 1, "workflow_runs": [RUN]}).on("actions/runs/7", RUN).on("actions/runs/7/jobs", JOBS).on("actions/runs/7/cancel", {})
    fake.on("actions/jobs/1/logs", "2026-01-01T00:00:00.0Z setup\n2026-01-01T00:00:01.0Z ##[error]tests failed\n2026-01-01T00:00:02.0Z Cleaning up orphan processes\n")
    fake.on("actions/jobs/2/logs", 404)
    return fake


COMMANDS = [(["ci", "active"], m.RunList), (["ci", "runs", "--commit", "abc", "--branch", "feat/x"], m.RunList), (["ci", "failed", "7"], m.FailedReport),
            (["ci", "cancel", "7"], m.CancelResult), (["pr", "status", "12"], m.PullRequest), (["pr", "status", "feat/x"], m.PullRequest), (["pr", "list"], m.PullRequestList),
            (["pr", "checks", "12"], m.ChecksRollup), (["pr", "why", "12"], m.WhyReport)]


@pytest.mark.parametrize("args,model", COMMANDS, ids=[" ".join(a) for a, _ in COMMANDS])
def test_every_command_prints_text_and_exactly_one_json_document_of_its_model(args, model):
    fake = gh()
    text = invoke(rt(fake), args)
    assert text.exit_code == 0 and text.stdout.strip() and not text.stdout.lstrip().startswith("{"), text
    out = invoke(rt(fake), ["--format", "json", *args])
    assert out.exit_code == 0, out
    model.model_validate(json.loads(out.stdout))  # one document, and it fits the published model
    assert fake.writes() == []  # nothing here writes without --live


def test_the_models_are_published_as_schemas():
    r = rt()
    names = json.loads(invoke(r, ["--format", "json", "self", "schema"]).stdout)
    assert {"PullRequest", "ChecksRollup", "WhyReport", "RunList", "FailedReport", "CancelResult", "QualityReport"} <= set(names)
    assert json.loads(invoke(r, ["self", "schema", "WhyReport"]).stdout)["title"] == "WhyReport"


def test_help_contract_and_read_only_marks():
    rows = {row["path"]: row for row in helpdoc.tree(command_tree(rt()), "sm")}
    mine = [p for p in rows if p.startswith(("sm ci", "sm pr")) and not p.startswith("sm ci window")]  # `ci window status`: commands/window.py
    assert sorted(mine) == ["sm ci", "sm ci active", "sm ci cancel", "sm ci failed", "sm ci runs", "sm pr", "sm pr checks", "sm pr list", "sm pr sonar", "sm pr status", "sm pr why"]
    assert rows["sm ci window status"]["read_only"] and not rows["sm ci window status"]["missing"]
    assert not [p for p in mine if rows[p]["missing"]]
    assert [p for p in mine if not rows[p]["read_only"]] == ["sm ci", "sm ci cancel"]  # never offered as a tool without confirmation
    assert "--live" in rows["sm ci cancel"]["options"]
    assert invoke(rt(), ["self", "lint"]).exit_code == 0


def test_help_runs_no_gh():
    fake = FakeGh()
    assert invoke(rt(fake), ["pr", "why", "-h"]).exit_code == 0 and invoke(rt(fake), ["ci", "--help"]).exit_code == 0 and fake.calls == []


def test_pr_why_text_names_every_cause_and_its_next_step():
    fake = gh()
    fake.on("pr checks 12", [check("CI OK", "fail")])
    out = invoke(rt(fake), ["pr", "why", "12"])
    assert out.exit_code == 0 and "PR 12 is NOT mergeable" in out.stdout
    assert "missing_required: required contexts that have not reported on this head: gate" in out.stdout and "red_required: required checks are red: CI OK" in out.stdout
    assert "-> sm pr checks 12" in out.stdout


def test_pr_checks_text_sets_required_apart():
    out = invoke(rt(gh()), ["pr", "checks", "#12"]).stdout
    assert "PR 12 checks: pending" in out and "MISSING  gate" in out and "green    CI OK   (required)" in out and "red      lint   (optional)" in out


def test_unknown_is_exit_1_with_the_model_and_the_reason():
    fake = FakeGh().on("pr view", Result(1, "", "GraphQL: Could not resolve to a PullRequest with the number of 99.")).on("actions/runs", 403)
    out = invoke(rt(fake), ["--format", "json", "pr", "status", "99"])
    assert out.exit_code == 1 and json.loads(out.stdout)["state"].startswith("unknown: GraphQL: Could not resolve")
    assert invoke(rt(fake), ["pr", "why", "99"]).exit_code == 1 and invoke(rt(fake), ["pr", "checks", "99"]).exit_code == 1
    out = invoke(rt(fake), ["ci", "active"])
    assert out.exit_code == 1 and "unknown: Resource not accessible" in out.stdout
    assert invoke(rt(fake), ["ci", "failed", "7"]).exit_code == 1


def test_a_pr_number_is_required_where_a_head_cannot_be_used():
    out = invoke(rt(gh()), ["--format", "json", "pr", "checks", "feat/x"])
    assert out.exit_code == USAGE and json.loads(out.stdout)["hint"] == "find it with: sm pr status feat/x"


def test_pr_status_none_for_a_head_without_a_pull_request():
    fake = FakeGh().on("pr list", [])
    out = invoke(rt(fake), ["pr", "status", "feat/none"])
    assert out.exit_code == 0 and "no pull request has the head feat/none" in out.stdout


def test_ci_failed_shows_failed_and_cancelled_jobs_with_log_tails_and_bounds_the_logs():
    fake = gh()
    out = invoke(rt(fake), ["ci", "failed", "7", "--lines", "2"])
    assert out.exit_code == 0
    assert "FAILED unit   (job 1)   failed step: Run tests" in out.stdout and "  | setup\n  | ##[error]tests failed" in out.stdout and "Cleaning up" not in out.stdout
    assert "CANCELLED e2e   (job 2)" in out.stdout and "log: unknown: Not Found (HTTP 404)" in out.stdout
    doc = json.loads(invoke(rt(fake), ["--format", "json", "ci", "failed", "7", "--jobs", "1"]).stdout)
    assert [j["job"]["name"] for j in doc["failed"]] == ["unit"] and doc["cancelled"] == [] and doc["omitted"] == ["e2e"] and doc["failed"][0]["log"]["ends_at"] == "error"
    fake.on("actions/runs/7/jobs", {"jobs": [JOBS["jobs"][2]]})
    assert "no failed or cancelled job so far: the run is not finished" in invoke(rt(fake), ["ci", "failed", "7"]).stdout


def test_ci_cancel_is_dry_by_default_and_live_cancels_once():
    fake = gh()
    out = invoke(rt(fake), ["ci", "cancel", "7"])
    assert out.exit_code == 0 and "dry run, nothing was cancelled" in out.stdout and "gh api -X POST repos/o/r/actions/runs/7/cancel" in out.stdout and fake.writes() == []
    out = invoke(rt(fake), ["--format", "json", "ci", "cancel", "7", "--live"])
    assert out.exit_code == 0 and json.loads(out.stdout)["state"] == "cancelled" and fake.writes() == ["api -X POST repos/o/r/actions/runs/7/cancel"]
    fake.on("actions/runs/7", {**RUN, "status": "completed", "conclusion": "failure"})
    out = invoke(rt(fake), ["ci", "cancel", "7", "--live"])
    assert out.exit_code == 1 and "cannot be cancelled" in out.stdout and len(fake.writes()) == 1


@pytest.mark.parametrize("run", ["7/../../pulls/1", "abc", "1e3", "7 ", "", "٣"])
def test_ci_cancel_refuses_a_run_that_is_not_digits_before_any_call(run):
    fake = gh()
    out = invoke(rt(fake), ["ci", "cancel", run, "--live"])
    assert out.exit_code == 2 and "is not a run id" in out.stderr + out.stdout and fake.calls == [] and fake.writes() == []


def test_ci_runs_passes_the_filters():
    fake = gh()
    invoke(rt(fake), ["ci", "runs", "--commit", "abc", "--branch", "feat/x", "--status", "failure", "--limit", "3"])
    assert fake.lines()[-1] == "api repos/o/r/actions/runs?head_sha=abc&branch=feat%2Fx&status=failure&per_page=100&page=1"


def test_settings_are_layered_environment_over_project_file_over_user_file(monkeypatch):
    r = rt()
    assert github_cli.table(r, "github") == {"repo": "o/r", "timeout_seconds": 5.0, "max_pages": 3, "merge_method": "squash", "variable_writes": False}
    monkeypatch.setenv("SM_GITHUB__REPO", "env/repo")
    monkeypatch.setenv("SM_GITHUB__MAX_PAGES", "1")
    assert (github_cli.table(r, "github")["repo"], github_cli.table(r, "github")["max_pages"]) == ("env/repo", 1)
    fake = FakeGh().on("pr view", PR)
    invoke(rt(fake), ["pr", "status", "12"])
    assert "-R env/repo" in fake.lines()[0]


def test_an_unknown_setting_is_a_usage_error():
    out = invoke(rt(tables={"github": {"repo": "o/r", "tokn": "x"}}), ["--format", "json", "pr", "status", "1"])
    assert out.exit_code == USAGE and "[github] is not valid: tokn" in json.loads(out.stdout)["error"]


def test_the_repository_comes_from_gh_when_none_is_configured():
    fake = FakeGh().on("repo view", "found/here\n").on("pr view", PR)
    assert invoke(rt(fake, tables={}), ["pr", "status", "12"]).exit_code == 0 and "-R found/here" in fake.lines()[-1]


class Reader:
    """A plugin's own forge and CI: no gh at all."""

    def __init__(self):
        self.asked = []

    def pr(self, ref):
        self.asked.append(("pr", ref))
        return m.PullRequest(state="open", number=5, title="from the plugin")

    def open_prs(self, limit=50):
        return m.PullRequestList()

    def pr_checks(self, number):
        return m.ChecksRollup(pr=number, state="green")

    def why(self, number):
        return m.WhyReport(pr=number, mergeable="yes")

    def branch_tip(self, branch):
        return m.BranchTip(branch=branch, state="missing")

    def run(self, run_id):
        return m.Run(id=run_id, status="completed", conclusion="success")

    def runs(self, commit="", branch="", status="", limit=20):
        return m.RunList()

    def active(self, limit=100):
        self.asked.append(("active", limit))
        return m.RunList(runs=[m.Run(id="1", status="queued")])

    def jobs(self, run_id):
        return m.RunJobs(run=run_id)

    def log_tail(self, job_id, lines=40):
        return m.LogTail(job=job_id)

    def cancel_run(self, run_id, live=False):
        return m.CancelResult(run=run_id, state="dry_run")


def test_a_plugins_reader_in_extras_answers_instead_of_github():
    reader, fake = Reader(), FakeGh()
    r = rt(fake, extra={"forge_reader": reader, "ci_runs": reader})
    assert "from the plugin" in invoke(r, ["pr", "status", "5"]).stdout and "queued" in invoke(r, ["ci", "active", "--limit", "9"]).stdout
    assert "no failed or cancelled job" in invoke(r, ["ci", "failed", "3"]).stdout and "no run" in invoke(r, ["ci", "runs"]).stdout
    assert "no open pull request" in invoke(r, ["pr", "list"]).stdout
    assert reader.asked == [("pr", "5"), ("active", 9)] and fake.calls == []


def test_the_projects_own_forge_and_ci_answer_when_they_satisfy_the_reader_protocols():
    reader, fake = Reader(), FakeGh()
    r = rt(fake, forge=reader, ci=reader)
    assert invoke(r, ["pr", "why", "5"]).exit_code == 0 and invoke(r, ["ci", "cancel", "5"]).exit_code == 0 and fake.calls == []


def test_without_a_project_file_the_commands_still_work(monkeypatch):
    from steuermaschine import defaults

    fake = FakeGh().on("repo view", "o/r\n").on("pr view", PR)
    monkeypatch.setattr(defaults.Subprocess, "run", lambda self, argv, cwd=None, timeout=None: fake.run(argv, cwd, timeout))
    out = invoke(Runtime(None, None, problem="no settings file"), ["pr", "status", "12"])
    assert out.exit_code == 0 and "PR 12 open" in out.stdout


SONAR = {"qualitygates/project_status": {"projectStatus": {"status": "OK", "conditions": []}},
         "measures/component": {"component": {"measures": [{"metric": "new_sqale_debt_ratio", "periods": [{"value": "0.5"}]}]}},
         "issues/search": {"total": 0, "issues": []}}


def http(url, headers, timeout):
    return next(((200, json.dumps(doc)) for part, doc in SONAR.items() if part in url), (404, "{}"))


def test_pr_sonar_text_and_json_from_the_quality_provider():
    r = rt(extra={"quality": Sonar("org_p", fetch=http)})
    out = invoke(r, ["pr", "sonar", "12"])
    assert out.exit_code == 0 and "PR 12 quality gate: passed" in out.stdout and "debt ratio 0.5%" in out.stdout and "open issues: 0" in out.stdout
    doc = m.QualityReport.model_validate(json.loads(invoke(r, ["--format", "json", "pr", "sonar", "12", "--issues", "3"]).stdout))
    assert (doc.gate, doc.debt_ratio, doc.duplication, doc.open_issues) == ("passed", 0.5, None, 0)


def test_pr_sonar_without_a_project_key_says_how_to_configure_it_and_with_one_builds_the_provider(monkeypatch):
    out = invoke(rt(), ["--format", "json", "pr", "sonar", "12"])
    assert out.exit_code == USAGE and "project_key" in json.loads(out.stdout)["hint"]
    from steuermaschine.github import sonar

    seen = []
    monkeypatch.setattr(sonar, "urllib_fetch", lambda url, headers, timeout: seen.append((url, headers, timeout)) or http(url, headers, timeout))
    monkeypatch.setenv("MY_TOKEN", "t0")
    r = rt(tables={"sonar": {"project_key": "org_p", "host": "https://sonar.example", "token_env": "MY_TOKEN", "timeout_seconds": 3}})
    assert invoke(r, ["pr", "sonar", "12"]).exit_code == 0
    assert seen[0][0].startswith("https://sonar.example/api/qualitygates/project_status?projectKey=org_p") and seen[0][1]["Authorization"] == "Bearer t0" and seen[0][2] == 3


def test_core_tables_are_accepted_in_a_settings_file(tmp_path, monkeypatch):
    from steuermaschine import config

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "none"))
    (tmp_path / "steuermaschine.toml").write_text('[project]\nname = "p"\nplugin = "none"\n[github]\nrepo = "o/r"\n[sonar]\nproject_key = "org_p"\n')
    project = config.load(str(tmp_path))
    r = Runtime(project, None)
    assert github_cli.table(r, "github")["repo"] == "o/r" and github_cli.table(r, "sonar")["project_key"] == "org_p"
    (tmp_path / "steuermaschine.toml").write_text('[project]\nname = "p"\nplugin = "none"\n[gitlab]\nrepo = "o/r"\n')
    with pytest.raises(config.ConfigError, match=r"\[gitlab\]"):
        config.load(str(tmp_path))
