"""ci merge-gate and ci main, native. The answers are written by hand in the shape GitHub, Sonar and the coverage badge
give (the shapes of the moved queue modules' own tests); no test here starts a process or reads the network."""

import json
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, fake_providers, invoke
from steuermaschine_heinzel.native import ci_evidence
from steuermaschine_heinzel.native.queue_lib import merge_gate
from steuermaschine_heinzel.plugin import plugin

OWNER = merge_gate.hzlib.OWNER


def runtime_for(tmp_path, commands, **settings):
    stack = stack_of(plugin)
    table = {"legacy_state_dir": str(tmp_path / "state"), "queue_records_dir": str(tmp_path / "records"),
             "queue_state_file": str(tmp_path / "queue.json"), **settings}
    validated, _ = stack.validate({"heinzel": table}, Path("."))
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, validated, _providers=fake_providers(commands=commands))


# ------------------------------------------------------------------ ci merge-gate
def sonar_and_badge(red_repo: str = ""):
    def http_json(url):
        if "qualitygates" in url:
            return {"projectStatus": {"status": "OK"}}
        if "measures" in url:
            return {"component": {"measures": [{"metric": m, "value": "1.0"} for m in merge_gate.RATINGS]}}
        if "endpoint.json" in url:
            return {"message": "91%", "color": "red" if red_repo and f"/{red_repo}/" in url else "brightgreen"}
        return None

    return http_json


def gate_commands(bypass: bool = False) -> FakeCommands:
    c = FakeCommands()
    ruleset = {"id": 7, "enforcement": "active", "bypass_actors": [{"actor_id": 5}] if bypass else [],
               "rules": [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "merge-gate"}]}}]}
    for repo in merge_gate.PUBLIC:
        c.on(["gh", "api", f"repos/{OWNER}/{repo}/rulesets"], Result(0, json.dumps([{"id": 7}])))
        c.on(["gh", "api", f"repos/{OWNER}/{repo}/rulesets/7"], Result(0, json.dumps(ruleset)))
    return c


def test_merge_gate_in_text_is_the_audit_line_for_line(tmp_path, monkeypatch):
    monkeypatch.setattr(merge_gate, "http_json", sonar_and_badge(red_repo="heinzel-acme"))
    commands = gate_commands()
    out = invoke(runtime_for(tmp_path, commands), ["ci", "merge-gate"])
    assert out.exit_code == 0, out
    lines = out.stdout.splitlines()
    assert lines[0] == ("heinzel: gate OK, reliability A security A maintainability A; coverage 91% brightgreen; required: merge-gate; "
                        "not yet: coverage-floor")
    assert lines[2].endswith("<<< RED") and len(lines) == len(merge_gate.PUBLIC) + len(merge_gate.PRIVATE)
    assert all(argv[:2] == ["gh", "api"] and "-X" not in argv for argv in commands.calls)


def test_merge_gate_in_json_validates_and_names_the_marks(tmp_path, monkeypatch):
    monkeypatch.setattr(merge_gate, "http_json", lambda url: None)
    out = invoke(runtime_for(tmp_path, gate_commands(bypass=True)), ["--format", "json", "ci", "merge-gate"])
    assert out.exit_code == 0, out
    doc = ci_evidence.CiMergeGate.model_validate_json(out.stdout)
    first = doc.repos[0]
    assert first.repo == "heinzel" and first.marks == ["NOT A", "RED", "BYPASS"] and doc.read_only
    assert all(r.marks == [] for r in doc.repos if r.repo in merge_gate.PRIVATE)


def test_gate_rows_parse_a_missing_ruleset():
    rows = ci_evidence.gate_rows(["x: Sonar UNAVAILABLE; coverage UNKNOWN; required: neither; not yet: merge-gate  <<< NOT A, RED, NO RULESET", "y: fine"])
    assert rows[0].marks == ["NOT A", "RED", "NO RULESET"] and rows[1].marks == [] and rows[1].repo == "y"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """The moved gate module reads Sonar with urllib: a test that forgets to answer must not reach the network."""
    monkeypatch.setattr(merge_gate.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("no network in tests")))


# ------------------------------------------------------------------ ci main
RED, GREEN, PENDING = "a" * 40, "b" * 40, "c" * 40


def run_row(run, sha, status="completed", conclusion="success", title="feat: x (#1)"):
    return {"databaseId": run, "headSha": sha, "status": status, "conclusion": conclusion if status == "completed" else None,
            "displayTitle": title, "attempt": 1, "createdAt": f"2026-11-13T10:{run:02d}:00Z"}


def main_commands(runs, jobs=None) -> FakeCommands:
    c = FakeCommands()
    c.on(["gh", "run", "list"], Result(0, json.dumps(runs)))
    c.on(["gh", "api"], Result(0, json.dumps(jobs or {"total_count": 0, "jobs": []})))
    return c


def test_ci_main_names_the_failing_jobs_and_the_queue_verdict_and_writes_nothing(tmp_path):
    jobs = {"total_count": 2, "jobs": [{"name": "web-2 (macos-latest)", "conclusion": "failure"}, {"name": "CI OK", "conclusion": "failure"}]}
    commands = main_commands([run_row(12, RED, conclusion="failure"), run_row(11, GREEN)], jobs)
    out = invoke(runtime_for(tmp_path, commands), ["why", "main"])
    assert out.exit_code == 0, out
    lines = out.stdout.splitlines()
    assert lines[0] == "aaaaaaaa run 12 completed/failure 2026-11-13T10:12:00Z feat: x (#1)  FAILED: web-2 (macos-latest), CI OK"
    assert lines[1] == "bbbbbbbb run 11 completed/success 2026-11-13T10:11:00Z feat: x (#1)"
    assert lines[2] == "merge queue main_state: red" and "main_failure" in lines[3]
    assert not (tmp_path / "queue.json").exists() and not [a for a in commands.calls if a[0] != "gh" or "-X" in a]


def test_ci_main_in_json_explains_a_cancelled_run(tmp_path):
    runs = [run_row(13, PENDING, status="in_progress"), run_row(12, RED, conclusion="cancelled"), run_row(11, GREEN)]
    out = invoke(runtime_for(tmp_path, main_commands(runs)), ["--format", "json", "why", "main"])
    assert out.exit_code == 0, out
    doc = ci_evidence.CiMain.model_validate_json(out.stdout)
    assert doc.notes == ["run 12: cancelled, ignored (newer run pending)"] and doc.queue_state == "green"
    assert [r.head for r in doc.runs] == ["cccccccc", "aaaaaaaa", "bbbbbbbb"] and doc.queue_would_record == {}


def test_ci_main_without_runs_is_unknown(tmp_path):
    out = invoke(runtime_for(tmp_path, main_commands([])), ["why", "main"])
    assert out.stdout.splitlines() == ["  note: no runs listed (gh failed or empty)", "merge queue main_state: unknown"]


def test_explain_sees_malformed_and_unproven_rows():
    from steuermaschine_heinzel.native.ci_evidence_lib.main_evidence import explain
    from steuermaschine_heinzel.native.queue_lib import queue_daemon

    rows = [{"databaseId": 3, "batch_unknown": True}, {"databaseId": 2, "status": "odd"}, run_row(1, RED, conclusion="cancelled")]
    assert explain(rows, queue_daemon) == ["run 3: batch_unknown (admission/delegation proof missing or malformed)",
                                           "run 2: malformed row (sha/status/conclusion)",
                                           "run 1: cancelled with no newer green or pending run -> UNKNOWN"]
    assert explain([run_row(2, RED, status="queued")], queue_daemon) == ["no completed run yet (only pending runs)"]
