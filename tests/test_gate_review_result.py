import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("gate_review_result", ROOT / "bin" / "gate_review_result.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

LIMIT = {"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error", "api_error_type": "rate_limit_error",
         "api_error_status": 429, "api_error_origin": "first_party", "result": "You've hit your monthly spend limit · more details"}


def test_specific_provider_limit_has_a_distinct_outcome():
    kind, public = gate.classify([{"type": "system"}, LIMIT], "failure")
    assert kind == "skipped-provider-spend-limit" and "Independent review was skipped" in public
    assert "VERDICT" not in public and "more details" not in public


@pytest.mark.parametrize("override,outcome", [
    ({"result": "429 Too many requests"}, "failure"),
    ({"terminal_reason": "tool_error"}, "failure"),
    ({"api_error_type": "authentication_error", "api_error_status": 401}, "failure"),
    ({"api_error_origin": "third_party"}, "failure"),
    ({"api_error_status": None}, "failure"),
    ({"result": "Authentication failed\nYou've hit your monthly spend limit"}, "failure"),
    ({}, "success"),
    ({}, "cancelled"),
])
def test_weak_or_mixed_error_evidence_never_passes(override, outcome):
    with pytest.raises(ValueError):
        gate.classify([{**LIMIT, **override}], outcome)


def test_rejection_already_emitted_before_limit_blocks():
    reject = {"type": "assistant", "message": {"content": [{"type": "text", "text": "A finding\nVERDICT: REJECT"}]}}
    assert gate.classify([reject, LIMIT], "failure")[0] == "rejected"
    assert gate.classify([reject, {"type": "result", "is_error": True}], "failure")[0] == "rejected"
    assert gate.classify([{**LIMIT, "result": "You've hit your monthly spend limit\nVERDICT: REJECT"}], "failure")[0] == "rejected"


@pytest.mark.parametrize("answer,kind", [("Looks fine\nVERDICT: APPROVE\n\n", "approved"), ("VERDICT: REJECT", "rejected"),
                                          ("VERDICT: APPROVE\ntext after", "invalid"), ("VERDICT: REJECT\nVERDICT: APPROVE", "rejected")])
def test_normal_opus_verdict_path_is_still_exact(answer, kind):
    assert gate.classify([{"type": "result", "subtype": "success", "is_error": False, "result": answer}], "success") == (kind, answer)


def test_collects_the_pinned_actions_failing_file_without_leaking_provider_details(tmp_path, monkeypatch, capsys):
    execution = tmp_path / "claude-execution-output.json"
    execution.write_text(json.dumps([LIMIT]))
    output = tmp_path / "output"
    for name, value in {"RUNNER_TEMP": tmp_path, "EXECUTION_FILE": execution, "GITHUB_OUTPUT": output, "OUTCOME": "failure"}.items():
        monkeypatch.setenv(name, str(value))
    assert gate.main() == 0
    data = output.read_text()
    assert "kind=skipped-provider-spend-limit\n" in data and "more details" not in data and not capsys.readouterr().err
    execution.unlink()
    assert gate.main() == 1


@pytest.mark.parametrize("kind,ready,exit_code", [("approved", "", 0), ("rejected", "true", 1),
                                                    ("skipped-provider-spend-limit", "true", 0), ("skipped-provider-spend-limit", "", 1)])
def test_gate_judge_rollout_switch_cannot_pass_a_rejection(tmp_path, kind, ready, exit_code):
    import yaml

    flow = yaml.safe_load((ROOT / ".github/workflows/gate-review.yml").read_text())
    rejection, _checkout, history, judge, marker = flow["jobs"]["gate-review"]["steps"]
    assert rejection["if"] == "needs.model.outputs.kind == 'rejected'"
    assert history["if"] == "needs.model.outputs.kind == 'skipped-provider-spend-limit' && vars.GATE_SPEND_LIMIT_READY == 'true'"
    assert marker["if"] == "needs.model.outputs.kind == 'skipped-provider-spend-limit'"
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n")
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "RUNNER_TEMP": str(tmp_path), "MODE": "review",
           "MODEL": "success", "KIND": kind, "RESULT": "test outcome", "PR": "42", "GITHUB_REPOSITORY": "owner/repo", "SPEND_LIMIT_READY": ready}
    assert subprocess.run(["bash", "-e", "-c", judge["run"]], env=env, check=False).returncode == exit_code
