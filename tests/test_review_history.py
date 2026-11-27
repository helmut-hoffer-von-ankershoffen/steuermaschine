"""Exact-head history and executable workflow regressions adapted from reviewed Core."""
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]

def scaled(seconds):
    return seconds

def _gate_review():
    return yaml.safe_load((REPO / ".github" / "workflows" / "gate-review.yml").read_text(encoding="utf-8"))


def _gate_scope_script():
    return next(step["run"] for step in _gate_review()["jobs"]["scope"]["steps"] if step.get("id") == "scope")


def _gate_judge_script():
    return next(
        step["run"]
        for step in _gate_review()["jobs"]["gate-review"]["steps"]
        if step.get("name") == "Judge the review and comment"
    )


@pytest.mark.parametrize(
    ("enabled", "mode"), [("false", "paused"), ("true", "review"), ("", "refuse"), ("False", "refuse")]
)
def test_gate_review_operator_switch(tmp_path, enabled, mode):
    jobs = _gate_review()["jobs"]
    scope = next(step for step in jobs["scope"]["steps"] if step.get("id") == "scope")
    assert scope["env"]["ENABLED"] == "${{ vars.CLAUDE_REVIEW }}"
    output = tmp_path / "output"
    environment = dict(os.environ, ENABLED=enabled, GITHUB_OUTPUT=str(output), CHANGED_FILES="1")
    script = 'gh() { echo ".github/workflows/ci.yml"; }\n' + scope["run"]
    subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], env=environment, check=True, timeout=scaled(5))
    assert output.read_text().strip() == f"mode={mode}"


@pytest.mark.parametrize(
    ("scope", "model", "history", "skipped"),
    [
        ("success", "skipped", "success", True),
        ("success", "skipped", "failure", False),
        ("success", "skipped", "skipped", False),
        ("failure", "skipped", "success", False),
        ("success", "failure", "success", False),
    ],
)
def test_gate_review_records_only_a_successful_operator_pause(tmp_path, scope, model, history, skipped):
    job = _gate_review()["jobs"]["gate-review"]
    judge = next(step for step in job["steps"] if step.get("name") == "Judge the review and comment")
    assert judge["env"]["SCOPE_RESULT"] == "${{ needs.scope.result }}"
    summary = tmp_path / "summary"
    environment = dict(
        os.environ,
        MODE="paused",
        SCOPE_RESULT=scope,
        MODEL=model,
        HISTORY_OUTCOME=history,
        GITHUB_STEP_SUMMARY=str(summary),
    )
    result = subprocess.run(["bash", "-e", "-c", judge["run"]], env=environment, check=False, timeout=scaled(5))
    assert (result.returncode == 0) is skipped
    assert summary.exists() is skipped
    if skipped:
        assert "Claude review skipped: an operator paused it with CLAUDE_REVIEW=false." in summary.read_text()


@pytest.mark.parametrize("case", ["first", "prior_clear", "prior_reject", "unreadable"])
def test_operator_pause_judges_actual_history(tmp_path, case):
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "review_history.py"
    spec = importlib.util.spec_from_file_location("pause_history", path)
    history = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(history)
    head = "a" * 40

    def run(number):
        return {
            "id": number,
            "run_attempt": 1,
            "head_sha": head,
            "event": "pull_request_target",
            "path": history.WORKFLOW,
            "status": "completed",
            "conclusion": "failure",
        }

    def read(endpoint):
        if "/workflows/" in endpoint:
            runs = [run(100)] if case == "first" else [run(99), run(100)]
            return {"total_count": len(runs), "workflow_runs": runs}
        if case == "unreadable":
            return {"total_count": 1, "jobs": None}
        job = {
            "id": 99,
            "name": "Gate review",
            "run_id": 99,
            "run_attempt": 1,
            "head_sha": head,
            "status": "completed",
            "conclusion": "failure",
            "steps": [
                {
                    "name": history.REJECT_STEP,
                    "status": "completed",
                    "conclusion": "success" if case == "prior_reject" else "skipped",
                }
            ],
        }
        return {"total_count": 1, "jobs": [job]}

    try:
        history.check(read, "owner/repo", head, 100, 1)
        outcome = "success"
    except ValueError:
        outcome = "failure"
    summary = tmp_path / "summary"
    env = dict(
        os.environ,
        MODE="paused",
        SCOPE_RESULT="success",
        MODEL="skipped",
        HISTORY_OUTCOME=outcome,
        GITHUB_STEP_SUMMARY=str(summary),
    )
    result = subprocess.run(["bash", "-e", "-c", _gate_judge_script()], env=env, check=False, timeout=scaled(5))
    assert (result.returncode == 0) == (case in {"first", "prior_clear"})


@pytest.mark.parametrize(
    ("changes", "previous_rejection", "skip"),
    [
        ({}, False, True),
        ({"result": "You've hit your weekly spend limit. Try again later."}, False, True),
        ({"result": "Rate limit exceeded. Try again later."}, False, False),
        ({"api_error_status": 401}, False, False),
        ({"api_error_origin": "other"}, False, False),
        ({"is_error": False}, False, False),
        ({}, True, False),
    ],
)
def test_gate_review_only_waives_a_confirmed_spend_limit(tmp_path, changes, previous_rejection, skip):
    review = _gate_review()["jobs"]
    collect = next(step["run"] for step in review["model"]["steps"] if step.get("id") == "result")
    execution_file = tmp_path / "execution.json"
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    terminal = {
        "type": "result",
        "is_error": True,
        "terminal_reason": "api_error",
        "api_error_type": "rate_limit_error",
        "api_error_status": 429,
        "api_error_origin": "first_party",
        "result": "You've hit your monthly spend limit. Try again later.",
    }
    events = []
    if previous_rejection:
        events.append(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Finding.\nVERDICT: REJECT"}]}}
        )
    execution_file.write_text(json.dumps([*events, terminal | changes]), encoding="utf-8")
    env = os.environ | {
        "OUTCOME": "failure",
        "CLAUDE_CONCLUSION": "failure",
        "EXECUTION_FILE": str(execution_file),
        "GITHUB_OUTPUT": str(output),
    }
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", collect],
        check=False,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if skip:
        assert result.returncode == 0, result.stderr
        assert output.read_text(encoding="utf-8") == "spend_limit=true\n"
        judge = _gate_judge_script()
        result = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", judge],
            check=False,
            env=env
            | {
                "MODE": "review",
                "SCOPE_RESULT": "success",
                "MODEL": "success",
                "SPEND_LIMIT": "true",
                "HISTORY_OUTCOME": "success",
                "VERDICT_LINE": "",
                "RESULT": "",
                "GITHUB_STEP_SUMMARY": str(summary),
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert "review skipped" in summary.read_text(encoding="utf-8")
    else:
        assert result.returncode == 1
        assert "Gate review failed" in result.stdout
        if previous_rejection:
            assert output.read_text(encoding="utf-8") == "explicit_rejection=true\n"
        else:
            assert not output.exists()


def test_gate_review_does_not_accept_approval_after_explicit_rejection(tmp_path):
    reject = {"type": "assistant", "message": {"content": [{"type": "text", "text": "VERDICT: REJECT"}]}}
    approve = {"type": "result", "is_error": False, "result": "VERDICT: APPROVE"}
    execution = tmp_path / "execution.json"
    execution.write_text(json.dumps([reject, approve]), encoding="utf-8")
    output = tmp_path / "output"
    collect = next(step["run"] for step in _gate_review()["jobs"]["model"]["steps"] if step.get("id") == "result")
    env = os.environ | {
        "OUTCOME": "success",
        "CLAUDE_CONCLUSION": "success",
        "EXECUTION_FILE": str(execution),
        "GITHUB_OUTPUT": str(output),
    }
    args = ["bash", "-e", "-o", "pipefail", "-c"]
    subprocess.run([*args, collect], env=env, check=True, timeout=10)
    assert output.read_text(encoding="utf-8").splitlines()[:2] == [
        "explicit_rejection=true",
        "verdict_line=VERDICT: APPROVE",
    ]
    result = subprocess.run(
        [*args, "gh() { return 0; }\n" + _gate_judge_script()],
        env=env
        | {
            "MODE": "review",
                "SCOPE_RESULT": "success",
            "MODEL": "success",
            "VERDICT_LINE": "VERDICT: APPROVE",
            "RESULT": "VERDICT: APPROVE",
            "EXPLICIT_REJECTION": "true",
            "SPEND_LIMIT": "",
            "RUNNER_TEMP": str(tmp_path),
            "PR": "1",
            "GITHUB_REPOSITORY": "owner/repo",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "explicitly rejected" in result.stdout




def _history():
    spec = importlib.util.spec_from_file_location("history_probe", REPO / "scripts/review_history.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("case", ["clean", "wrong_head", "missing_current", "wrong_attempt", "unreadable", "opaque_failed_judge", "rejected", "clear_marker"])
def test_exact_head_inventory_and_attempt_membership(case):
    history = _history()
    head = "a" * 40
    def get(endpoint):
        if "/workflows/" in endpoint:
            assert f"event=pull_request_target&head_sha={head}&per_page=100&page=1" in endpoint
            if case == "unreadable":
                return {"total_count": 1, "workflow_runs": None}
            rows = [{"id": 100, "run_attempt": 2 if case == "wrong_attempt" else 1,
                     "head_sha": "b" * 40 if case == "wrong_head" else head,
                     "path": history.WORKFLOW, "event": "pull_request_target", "status": "completed", "conclusion": "failure"}]
            if case == "missing_current":
                rows = []
            if case in {"opaque_failed_judge", "rejected", "clear_marker"}:
                rows.append(dict(rows[0], id=99))
            return {"total_count": len(rows), "workflow_runs": rows}
        assert endpoint.endswith("/runs/99/attempts/1/jobs?per_page=100")
        step = {"name": history.LEGACY_JUDGE if case == "opaque_failed_judge" else history.REJECT_STEP,
                "status": "completed", "conclusion": "success" if case == "rejected" else "skipped"}
        return {"total_count": 1, "jobs": [{"id": 99, "name": "Gate review", "run_id": 99, "run_attempt": 1,
                "head_sha": head, "status": "completed", "conclusion": "failure", "steps": [step]}]}
    if case in {"clean", "clear_marker"}:
        history.check(get, "owner/repo", head, 100, 1)
    else:
        with pytest.raises(ValueError):
            history.check(get, "owner/repo", head, 100, 1)


@pytest.mark.parametrize("checkout_matches, workflow_matches", [(True, True), (False, True), (True, False)])
def test_protected_base_bootstrap_executes_only_after_checkout_verification(tmp_path, checkout_matches, workflow_matches):
    steps = _gate_review()["jobs"]["gate-review"]["steps"]
    checkout = next(step for step in steps if "actions/checkout@" in step.get("uses", ""))
    assert "ref" not in checkout["with"]
    assert checkout["with"]["persist-credentials"] is False
    step = next(step for step in steps if step.get("id") == "history")
    script = 'git() { echo "$CHECKOUT"; }; python3() { echo checked > "$PROBE"; };\n' + step["run"]
    probe = tmp_path / "probe"
    env = {"PATH": os.environ["PATH"], "CHECKOUT": "a" * 40 if checkout_matches else "b" * 40,
           "TRUSTED_BASE": "a" * 40, "GITHUB_SHA": "a" * 40 if workflow_matches else "b" * 40, "PROBE": str(probe)}
    result = subprocess.run(["bash", "-e", "-c", script], env=env, check=False, timeout=5)
    assert (result.returncode == 0) == (checkout_matches and workflow_matches)
    assert probe.exists() == (checkout_matches and workflow_matches)


def test_positive_approval_marker_excludes_skips_and_rejections():
    jobs = _gate_review()["jobs"]
    marker = next(step for step in jobs["gate-review"]["steps"]
                  if step.get("name") == "Record explicit Gate review approval")
    for mode, model, spend, reject, verdict, approved in [
        ("review", "success", "", "", "VERDICT: APPROVE", True),
        ("paused", "skipped", "", "", "", False),
        ("review", "success", "true", "", "", False),
        ("review", "success", "", "true", "VERDICT: APPROVE", False),
        ("review", "success", "", "true", "VERDICT: REJECT", False),
        ("review", "failure", "", "", "VERDICT: APPROVE", False),
    ]:
        expression = marker["if"]
        values = {"needs.scope.result": "success", "needs.scope.outputs.mode": mode,
                  "needs.model.result": model, "needs.model.outputs.spend_limit": spend,
                  "needs.model.outputs.explicit_rejection": reject, "needs.model.outputs.verdict_line": verdict}
        for key in sorted(values, key=len, reverse=True):
            expression = expression.replace(key, repr(values[key]))
        expression = expression.replace("success()", "True").replace("&&", " and ")
        assert bool(eval(expression, {"__builtins__": {}})) == approved
    # Every remaining model run step must be collection, never deliberate failure after a classified skip.
    assert [step.get("id") for step in jobs["model"]["steps"] if "run" in step] == [None, "result"]


@pytest.mark.parametrize("broken", ["none", "checkout", "workflow", "head"])
def test_fetched_pr_input_is_verified_before_review(tmp_path, broken):
    steps = _gate_review()["jobs"]["model"]["steps"]
    checkout = next(step for step in steps if "actions/checkout@" in step.get("uses", ""))
    assert "ref" not in checkout["with"]
    assert checkout["with"]["persist-credentials"] is False
    fetch = next(step["run"] for step in steps if step.get("name") == "Fetch the head as git objects only")
    probe = tmp_path / "fetched"
    script = ('git() { if [[ "$1" == rev-parse ]]; then '
              'if [[ "$2" == HEAD ]]; then echo "$CHECKOUT"; else echo "$FETCHED"; fi; '
              'else echo fetched > "$PROBE"; fi; };\n' + fetch)
    env = {"PATH": os.environ["PATH"], "GH_TOKEN": "fixture", "PR": "12", "PR_HEAD": "c" * 40,
           "TRUSTED_BASE": "a" * 40, "GITHUB_SHA": "b" * 40 if broken == "workflow" else "a" * 40,
           "CHECKOUT": "b" * 40 if broken == "checkout" else "a" * 40,
           "FETCHED": "b" * 40 if broken == "head" else "c" * 40, "PROBE": str(probe)}
    result = subprocess.run(["bash", "-e", "-c", script], env=env, check=False, timeout=5, capture_output=True)
    assert (result.returncode == 0) == (broken == "none")
    assert probe.exists() == (broken in {"none", "head"})


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        path = tmp_path / key.lower()
        path.mkdir()
        monkeypatch.setenv(key, str(path))


@pytest.mark.parametrize("broken", ["none", "changed_total", "duplicate", "missing_page", "missing_count"])
def test_exact_head_history_pages_are_complete_and_unique(broken):
    history = _history()
    head = "a" * 40
    rows = [{"id": i, "run_attempt": 1, "path": history.WORKFLOW,
             "event": "pull_request_target", "head_sha": head, "status": "completed", "conclusion": "success"} for i in range(1, 102)]

    def get(path):
        assert "/workflows/gate-review.yml/runs?event=pull_request_target&head_sha=" in path
        if path.endswith("page=1"):
            return {"total_count": 101, "workflow_runs": rows[:100]}
        if broken == "missing_count":
            return {"workflow_runs": rows[100:]}
        return {"total_count": 102 if broken == "changed_total" else 101,
                "workflow_runs": [] if broken == "missing_page" else rows[:1] if broken == "duplicate" else rows[100:]}

    if broken == "none":
        assert len(history.inventory(get, "owner/repo", head)) == 101
    else:
        with pytest.raises(ValueError):
            history.inventory(get, "owner/repo", head)


def test_only_current_attempt_is_excluded_from_history():
    history = _history()
    head = "a" * 40
    calls = []

    def get(path):
        if "/workflows/" in path:
            return {"total_count": 1, "workflow_runs": [{"id": 10, "run_attempt": 2,
                "path": history.WORKFLOW, "event": "pull_request_target", "head_sha": head,
                "status": "completed", "conclusion": "success"}]}
        calls.append(path)
        attempt = 1 if "/attempts/1/" in path else 2
        return {"total_count": 1, "jobs": [{"id": attempt, "name": "Gate review", "run_id": 10,
            "run_attempt": attempt, "head_sha": head, "status": "completed", "conclusion": "success",
            "steps": [{"name": history.REJECT_STEP, "status": "completed", "conclusion": "skipped"}]}]}

    history.check(get, "owner/repo", head, 10, 2)
    assert len(calls) == 1 and "/attempts/1/" in calls[0]
    calls.clear()
    history.verify_history(get, "owner/repo", head)
    assert len(calls) == 2 and "/attempts/2/" in calls[1]
