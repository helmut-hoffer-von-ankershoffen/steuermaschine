"""`sm ci sonar-close`. Mock the CLI boundary: these tests never read or mutate real Sonar issues.

The first part is the former closing tool's test file, moved: every test body and assertion is unchanged. Only the
fixture `setup` changed (the edge): `close.main(argv)` now runs `sm ci sonar-close ARGV` through the whole command
tree and returns its exit code (a usage error, exit 2, is raised as SystemExit(2) as argparse did), and
`close.subprocess.run` is what the `Commands` provider of the runtime calls for every `sonar` process.
The second part adds what every write command has: text, json, the review gate, the exact calls in order.
"""

import json
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from ci_ops_kit import runtime
from steuermaschine_heinzel.native import ci_ops

from steuermaschine.protocols import Result
from steuermaschine.testing import invoke

PROJECT = "owner_project"
KEY = "reviewed-key"


class Close:
    """Stands where the former tests had the loaded script."""

    def __init__(self, config):
        self.LIST, self.rt, self.last = config, None, None
        self.subprocess = SimpleNamespace(run=None)

    def main(self, argv):
        self.last = done = invoke(self.rt, ["review", "sonar", *argv])
        sys.stdout.write(done.stdout)
        sys.stderr.write(done.stderr)
        if done.exit_code == 2:
            raise SystemExit(2)
        return done.exit_code


class Cli:
    """The `Commands` provider of these tests: every process goes to `close.subprocess.run`, with the former keywords."""

    def __init__(self, close):
        self.close = close

    def run(self, argv, cwd=None, timeout=None, env=None):
        done = self.close.subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
        return Result(done.returncode, done.stdout, done.stderr)


def row(**changes):
    return {"pull_request": 485, "issue": KEY, "file": "src/web.py", "rule": "python:S5332", "transition": "falsepositive", "reason": "Reviewed loopback-only announcement.", **changes}


def finding(**changes):
    return {"key": KEY, "project": PROJECT, "component": PROJECT + ":src/web.py", "rule": "python:S5332", "status": "OPEN", "type": "VULNERABILITY", **changes}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config = tmp_path / "closures.toml"
    close = Close(config)
    close.rt = runtime(tmp_path, monkeypatch, Cli(close))
    calls = []
    responses = []

    def cli(command, **kwargs):
        assert command[:2] == ["sonar", "api"]
        assert kwargs == {"capture_output": True, "text": True, "timeout": 60, "check": False}
        calls.append(command)
        if command[2] == "get":
            response = responses.pop(0)
            return subprocess.CompletedProcess(command, 0, json.dumps(response), "")
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(close.subprocess, "run", cli)

    def prepare(rows=None, issues=None, total=None, project=PROJECT):
        entries = [row()] if rows is None else rows
        text = "project = " + json.dumps(project) + "\n"
        for entry in entries:
            text += "\n[[closure]]\n" + "\n".join(f"{key} = {json.dumps(value)}" for key, value in entry.items()) + "\n"
        config.write_text(text)
        found = [finding()] if issues is None else issues
        responses.append({"total": len(found) if total is None else total, "issues": found})
        return close, calls, responses

    return prepare


def pr_args(*extra):
    return ["--pull-request", "485", "--issue", KEY, *extra]


def test_dry_run_scopes_both_pr_and_exact_keys_and_never_posts(setup, capsys):
    close, calls, _ = setup()
    assert close.main(pr_args()) == 0
    assert len(calls) == 1
    query = parse_qs(urlsplit(calls[0][3]).query)
    assert query == {"componentKeys": [PROJECT], "types": ["VULNERABILITY,BUG"], "issueStatuses": ["OPEN,CONFIRMED"], "ps": ["500"], "pullRequest": ["485"], "issues": [KEY]}
    assert "would close reviewed-key" in capsys.readouterr().out


@pytest.mark.parametrize("transition", ["falsepositive", "accept"])
def test_apply_posts_only_exact_reviewed_key_reason_transition(setup, transition):
    reviewed = row(transition=transition)
    close, calls, responses = setup(rows=[reviewed])
    responses.append({"total": 0, "issues": []})
    assert close.main(pr_args("--apply")) == 0
    assert [c[2] for c in calls] == ["get", "post", "post", "get"]
    assert calls[1][3] == "/api/issues/add_comment"
    assert json.loads(calls[1][5]) == {"issue": KEY, "text": reviewed["reason"]}
    assert calls[2][3] == "/api/issues/do_transition"
    assert json.loads(calls[2][5]) == {"issue": KEY, "transition": transition}
    assert parse_qs(urlsplit(calls[-1][3]).query)["pullRequest"] == ["485"]


@pytest.mark.parametrize("change", [
    {"project": "wrong"}, {"component": "wrong:src/web.py"},
    {"component": PROJECT + ":other/src/web.py"}, {"rule": "javascript:S5332"},
    {"rule": "python:S5443"}, {"key": "unreviewed-key"}, {"pullRequest": "486"},
    {"status": "CLOSED"}, {"issueStatus": "FIXED"}, {"type": "CODE_SMELL"},
])
def test_response_identity_status_or_type_mismatch_never_mutates(setup, change):
    close, calls, _ = setup(issues=[finding(**change)])
    assert close.main(pr_args("--apply")) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("issues,total", [([], 0), ([finding(), finding()], 2), ([finding()], 2)])
def test_missing_duplicate_or_partial_search_never_mutates(setup, issues, total):
    close, calls, _ = setup(issues=issues, total=total)
    assert close.main(pr_args("--apply")) == 1
    assert len(calls) == 1


def test_validate_all_requested_issues_before_first_post(setup):
    close, calls, _ = setup(rows=[row(), row(issue="second")], issues=[finding(), finding(key="second", rule="python:S0000")])
    assert close.main(pr_args("--issue", "second", "--apply")) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("rows", [
    [row(pull_request=486)], [row(issue="other")], [row(), row()],
    [row(transition="resolve")], [row(reason="")], [row(reason=" padded ")],
    [row(rule="S5332")], [row(pull_request=True)], [row(pull_request=0)],
    [row(issue="one,two")], [row(issue="one two")], [row(pull_requset=485)],
    [{k: v for k, v in row().items() if k != "pull_request"}],
    [{k: v for k, v in row().items() if k != "issue"}],
    [{k: v for k, v in row().items() if k != "transition"}],
])
def test_unreviewed_or_ambiguous_selection_rejected_before_cli(setup, rows):
    close, calls, _ = setup(rows=rows)
    assert close.main(pr_args("--apply")) == 1
    assert calls == []


@pytest.mark.parametrize("args", [
    ["--issue", KEY], ["--pull-request", "485"], ["--pull-request", "0", "--issue", KEY],
    pr_args("--issue", KEY), pr_args("--issue", "one,two"), pr_args("--issue", "one two"),
])
def test_invalid_scope_arguments_never_call_cli(setup, args):
    close, calls, _ = setup()
    assert close.main(args + ["--apply"]) == 1  # noqa: RUF005
    assert calls == []


@pytest.mark.parametrize("flag", ["--aply", "--a", "--app", "--appl", "--pull-requst", "--pull"])
def test_typo_or_unknown_arguments_are_not_ignored(setup, flag):
    close, calls, _ = setup()
    with pytest.raises(SystemExit) as error:
        close.main([flag])
    assert error.value.code == 2
    assert calls == []


def test_main_mode_keeps_legacy_rows_and_does_not_select_pr_rows(setup):
    legacy = {"file": "src/main.py", "rule": "S5332", "reason": "Legacy reviewed reason."}
    close, calls, responses = setup(rows=[legacy, row()], issues=[finding(), finding(key="legacy", component=PROJECT + ":src/main.py")])
    responses.append({"total": 0, "issues": []})
    assert close.main(["--apply"]) == 0
    assert "pullRequest" not in parse_qs(urlsplit(calls[0][3]).query)
    assert [json.loads(c[5])["issue"] for c in calls if c[2] == "post"] == ["legacy", "legacy"]
    assert json.loads(calls[2][5])["transition"] == "falsepositive"


def test_pr_mode_does_not_select_legacy_main_rows(setup):
    close, calls, _ = setup(rows=[{"file": "src/web.py", "rule": "S5332", "reason": "Main review."}])
    assert close.main(pr_args("--apply")) == 1
    assert calls == []


def test_bad_project_config_fails_before_search(setup):
    close, calls, _ = setup(project="")
    assert close.main(pr_args("--apply")) == 1
    assert calls == []


def test_explicit_manifest_scopes_a_separate_project_without_changing_default(setup, tmp_path):
    close, calls, responses = setup()
    default = close.LIST.read_bytes()
    alternate = tmp_path / "other-project.toml"
    alternate.write_text(close.LIST.read_text().replace(PROJECT, "other_project"))
    responses[0] = {"total": 1, "issues": [finding(project="other_project", component="other_project:src/web.py")]}
    responses.append({"total": 0, "issues": []})
    assert close.main(pr_args("--config", str(alternate), "--apply")) == 0
    assert parse_qs(urlsplit(calls[0][3]).query)["componentKeys"] == ["other_project"]
    assert close.LIST.read_bytes() == default


def test_alternate_manifest_cannot_close_an_issue_in_another_project(setup, tmp_path):
    close, calls, _ = setup()
    alternate = tmp_path / "other-project.toml"
    alternate.write_text(close.LIST.read_text().replace(PROJECT, "other_project"))
    assert close.main(pr_args("--config", str(alternate), "--apply")) == 1
    assert len(calls) == 1


def test_alternate_manifest_still_requires_an_exact_reviewed_issue(setup, tmp_path):
    close, calls, _ = setup()
    alternate = tmp_path / "unreviewed.toml"
    alternate.write_text('project = "other_project"\nclosure = []\n')
    assert close.main(pr_args("--config", str(alternate), "--apply")) == 1
    assert calls == []


def test_cli_error_stops_before_any_mutation(setup, monkeypatch):
    close, calls, _ = setup()
    monkeypatch.setattr(close.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, "", "read failed"))
    assert close.main(pr_args("--apply")) == 1
    assert calls == []


def test_comment_failure_never_attempts_transition(setup, monkeypatch):
    close, calls, _ = setup()
    mocked = close.subprocess.run

    def fail_comment(command, **kwargs):
        if command[2] == "post":
            calls.append(command)
            return subprocess.CompletedProcess(command, 1, "", "comment rejected")
        return mocked(command, **kwargs)

    monkeypatch.setattr(close.subprocess, "run", fail_comment)
    assert close.main(pr_args("--apply")) == 1
    assert [c[3] for c in calls if c[2] == "post"] == ["/api/issues/add_comment"]


# ----------------------------------------------------------------------------- through the command tree: what every write command has
def test_preview_in_text_and_json(setup):
    close, calls, responses = setup(issues=[finding(line=12)])
    assert close.main(pr_args()) == 0
    assert close.last.stdout.splitlines() == ["open before: 1", "would close reviewed-key src/web.py:12 python:S5332"]
    responses.append({"total": 1, "issues": [finding(line=12)]})
    doc = json.loads(invoke(close.rt, ["--format", "json", "review", "sonar", *pr_args()]).stdout)
    result = ci_ops.SonarClose.model_validate(doc)
    assert not result.live and result.project == PROJECT and result.pull_request == 485 and result.open_before == 1 and result.open_after is None
    closure, = result.closures
    assert (closure.issue, closure.file, closure.line, closure.rule, closure.transition, closure.state) == (KEY, "src/web.py", 12, "python:S5332", "falsepositive", "would_close")
    assert closure.reason == "Reviewed loopback-only announcement."
    assert result.calls == [f"sonar api post /api/issues/add_comment (issue {KEY}, the reviewed reason)", f"sonar api post /api/issues/do_transition (issue {KEY}, falsepositive)"]
    assert len(calls) == 2 and all(call[2] == "get" for call in calls)  # two previews, two reads, no write


def test_apply_makes_exactly_these_calls_in_this_order(setup):
    second = row(issue="second", file="src/api.py", transition="accept", reason="Accepted: test-only endpoint.")
    close, calls, responses = setup(rows=[row(), second], issues=[finding(), finding(key="second", component=PROJECT + ":src/api.py")])
    responses.append({"total": 0, "issues": []})
    done = invoke(close.rt, ["--format", "json", "review", "sonar", *pr_args("--issue", "second", "--apply")])
    assert done.exit_code == 0, done
    assert [(c[0], c[1], c[2], c[3].split("?")[0]) for c in calls] == [
        ("sonar", "api", "get", "/api/issues/search"),  # the whole plan is read and checked first
        ("sonar", "api", "post", "/api/issues/add_comment"), ("sonar", "api", "post", "/api/issues/do_transition"),
        ("sonar", "api", "post", "/api/issues/add_comment"), ("sonar", "api", "post", "/api/issues/do_transition"),
        ("sonar", "api", "get", "/api/issues/search")]
    assert [json.loads(c[5]) for c in calls[1:5]] == [
        {"issue": KEY, "text": "Reviewed loopback-only announcement."}, {"issue": KEY, "transition": "falsepositive"},
        {"issue": "second", "text": "Accepted: test-only endpoint."}, {"issue": "second", "transition": "accept"}]
    assert all(c[4] == "--data" for c in calls[1:5]) and all(len(c) == 4 for c in (calls[0], calls[5]))
    result = ci_ops.SonarClose.model_validate(json.loads(done.stdout))
    assert result.live and result.open_before == 2 and result.open_after == 0 and [c.state for c in result.closures] == ["closed", "closed"]
    assert result.lines == ["open before: 2", "closing reviewed-key src/web.py:None python:S5332", "closing second src/api.py:None python:S5332", "open after: 0"]


def test_a_failure_half_way_says_what_was_already_written(setup, monkeypatch):
    close, calls, _ = setup(rows=[row(), row(issue="second")], issues=[finding(), finding(key="second")])
    mocked = close.subprocess.run

    def fail_second_comment(command, **kwargs):
        if command[2] == "post" and "second" in command[5]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 1, "", "comment rejected")
        return mocked(command, **kwargs)

    monkeypatch.setattr(close.subprocess, "run", fail_second_comment)
    assert close.main(pr_args("--issue", "second", "--apply")) == 1
    assert [c[3].split("?")[0] for c in calls] == ["/api/issues/search", "/api/issues/add_comment", "/api/issues/do_transition", "/api/issues/add_comment"]
    assert "comment rejected" in close.last.stderr
    assert f"already written to Sonar, in this order: comment on {KEY}, transition on {KEY}" in close.last.stderr


def test_a_missing_manifest_refuses_before_any_call(setup, tmp_path):
    close, calls, _ = setup()
    assert close.main(["--config", str(tmp_path / "missing.toml")]) == 1 and calls == []
    close.LIST.write_text("project = [unclosed\n")
    assert close.main([]) == 1 and calls == []


@pytest.mark.parametrize("change", [{"type": "CODE_SMELL"}, {"status": "CLOSED"}, {"project": "other_project"}])
def test_main_route_checks_each_planned_issue_and_never_writes_on_a_mismatch(setup, change):
    # review C5: the main route no longer trusts the search filter for type, status and project
    legacy = {"file": "src/main.py", "rule": "S5332", "reason": "Legacy reviewed reason."}
    close, calls, _ = setup(rows=[legacy], issues=[finding(key="legacy", component=PROJECT + ":src/main.py", **change)])
    assert close.main(["--apply"]) == 1
    assert [c for c in calls if c[2] == "post"] == []


def test_main_route_matches_the_rule_exactly(setup):
    rows = [{"file": "src/main.py", "rule": "S533", "reason": "A shorter key."}, {"file": "src/main.py", "rule": "java:S5332", "reason": "Another language."}]
    close, _, _ = setup(rows=rows, issues=[finding(key="legacy", component=PROJECT + ":src/main.py")])
    assert close.main([]) == 0
    assert "would close" not in close.last.stdout


def test_without_a_manifest_setting_or_config_it_refuses(tmp_path, monkeypatch):
    rt = runtime(tmp_path, monkeypatch, Cli(Close(tmp_path / "x.toml")), ci_ops_sonar_closures_file=None)
    done = invoke(rt, ["review", "sonar"])
    assert done.exit_code == 2 and "ci_ops_sonar_closures_file" in done.stderr
