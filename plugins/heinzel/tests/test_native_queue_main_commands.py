"""`sm queue main-rerun` and `sm queue checkpoint` through the command tree, on a scripted `gh` and a copy of the real
queue state. No network, no real process, every path in a sandbox."""

import fcntl
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github.testing import FakeGh
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native import queue as queue_area
from steuermaschine_heinzel.native import queue_main as area
from steuermaschine_heinzel.plugin import plugin

REAL = Path(__file__).parent / "fixtures" / "queue" / "state-2026-11-12.json"
CORE = "helmut-hoffer-von-ankershoffen/heinzel"
MAIN = "1" * 40
GREEN_MAIN = [{"status": "completed", "conclusion": "success", "databaseId": 500, "headSha": MAIN, "attempt": 1, "displayTitle": "feat: x (#700)"}]


@pytest.fixture(autouse=True)
def nothing_real(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".cache").mkdir()
    monkeypatch.setenv("HZ_ALARM_NOTIFY", "off")
    monkeypatch.delenv("HZ_QUEUE_STATE", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: pytest.fail(f"a real process was started: {argv}"))
    yield
    queue_area.reset()


@pytest.fixture
def gh():
    return FakeGh()


@pytest.fixture
def rt(tmp_path, gh):
    state = tmp_path / ".cache" / "queue.json"
    shutil.copy(REAL, state)
    records = tmp_path / "records"
    (records / "research").mkdir(parents=True)
    (records / "AGENTS.md").write_text("## 9. Log\n")
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": {"queue_state_file": str(state), "queue_records_dir": str(records)}}, Path("."))
    runtime = Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=True), stack, settings,
                      _providers=fake_providers(commands=gh))
    runtime.state_file, runtime.records = state, records
    return runtime


def test_main_rerun_dry_run_says_nothing_to_rerun_on_a_green_main_and_writes_nothing(rt, gh):
    gh.on("run list", GREEN_MAIN)
    before = rt.state_file.read_text()
    out = invoke(rt, ["--format", "json", "queue", "main-rerun", "--dry-run"])
    doc = area.QueueMain.model_validate(json.loads(out.stdout))
    assert out.exit_code == 0 and doc.lines == ["main-rerun: nothing to rerun: main's latest run 500 is success"], out
    assert not gh.writes() and not any("rerun" in c for c in gh.calls) and rt.state_file.read_text() == before


def test_main_rerun_refuses_with_exit_1_when_main_ci_cannot_be_read_and_starts_nothing(rt, gh):
    out = invoke(rt, ["queue", "main-rerun"])
    assert out.exit_code == 1 and "main-rerun: refused: main CI evidence is unknown" in out.stderr and out.stdout.strip() == ""
    assert not any(c[:3] == ["gh", "run", "rerun"] for c in gh.calls)


def test_main_rerun_leaves_the_rerun_to_the_loop_that_holds_the_lock(rt, gh, tmp_path):
    gh.on("run list", GREEN_MAIN)
    with (statedir.root() / "queue.lock").open("a+") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        out = invoke(rt, ["queue", "main-rerun"])
    assert out.exit_code == 1 and "queue lock unavailable; let the automatic tick act" in out.stderr and not gh.calls


def test_checkpoint_needs_the_idle_queue_lock(rt, gh, tmp_path):
    with (statedir.root() / "queue.lock").open("a+") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        out = invoke(rt, ["queue", "checkpoint", "--pr", "795", "--run", "7", "--core-repo", str(tmp_path)])
    assert out.exit_code == 1 and "sm queue: checkpoint requires the idle queue lock" in out.stderr and not gh.calls


def test_checkpoint_refuses_evidence_it_cannot_accept_as_a_refusal_not_a_traceback(rt, gh, tmp_path):
    before = rt.state_file.read_text()
    out = invoke(rt, ["queue", "checkpoint", "--pr", "795", "--run", "7", "--core-repo", str(tmp_path), "--apply"])
    assert out.exit_code == 1 and "checkpoint refused:" in out.stderr and "Traceback" not in out.stderr, out
    assert rt.state_file.read_text() == before and not (tmp_path / ".cache" / "hz-queue.checkpoint.json").exists()
    assert not gh.writes()




def test_a_checkpoint_whose_last_line_is_not_json_has_no_proof_and_the_model_renders_its_lines(monkeypatch):
    monkeypatch.setattr(area.area, "_queue", lambda: object())
    monkeypatch.setattr(area.area, "_run", lambda module, argv: (0, ["established", "not json"], []))
    monkeypatch.setattr(area.area, "_translate", lambda lines: lines)
    monkeypatch.setattr(area, "runtime", lambda: type("R", (), {"say": lambda self, line: None})())
    result = area._call("checkpoint", ["checkpoint"])
    assert result.proof is None and result.render() == ["established", "not json"] and result.exit_code == 0
