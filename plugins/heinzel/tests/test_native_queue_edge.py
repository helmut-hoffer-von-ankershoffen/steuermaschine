"""The edges of the moved queue code: process runner, note, bead stamp, alarm state, cross-tool loads. No script of
the old tool set is started anywhere; these are the functions that replaced the five reaches the queue had."""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import steuermaschine_heinzel
from steuermaschine.github.testing import FakeGh
from steuermaschine.native import NativeRuntime, using
from steuermaschine.protocols import Result
from steuermaschine.testing import fake_providers
from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native.queue_lib import edge, hzlib, stall


@pytest.fixture(autouse=True)
def throwaway(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HZ_ALARM_STATE", str(tmp_path / "alarm.json"))
    monkeypatch.setenv("HZ_ALARM_NOTIFY", "off")
    (tmp_path / "records").mkdir()
    (tmp_path / "records" / "AGENTS.md").write_text("## 9. Log\n")
    edge.configure(tmp_path / "records")
    yield
    edge.configure(None)


def inside(commands):
    return using(NativeRuntime(fake_providers(commands=commands), {}, None))


def test_no_module_of_the_package_reaches_a_script_directory_or_loads_a_sibling_from_one():
    package = Path(steuermaschine_heinzel.__file__).parent
    for path in package.rglob("*.py"):
        if path.name == "moved_map.py":  # holds the old text of the replaced lines, as anchors of the move
            continue
        text = path.read_text()
        assert "SourceFileLoader" not in text or path.name == "main_checkpoint_reader.py", path
        assert not re.search(r"edge\.(sibling|scripts_dir)|sys\.path\.(insert|append)\(.*(TOOLS|tools)", text), path
        assert not re.search(r"""\[str\([^\]]*/ ["']hz-""", text), path  # no argv that starts an old script


def test_a_cross_tool_load_returns_a_fresh_moved_module_and_an_unmoved_tool_raises():
    first, second = hzlib.load("hz-merge-train"), hzlib.load("hz-merge-train")
    assert first is not second and first.__file__ == second.__file__ and first.__file__.endswith("queue_lib/merge_train.py")
    assert hzlib.load("hz-flakes").read_attempt and hzlib.load("hz-diff-guard").check
    with pytest.raises(LookupError, match="hz-tick is not part of this package"):
        hzlib.load("hz-tick")


def test_note_appends_the_line_the_note_tool_wrote(tmp_path):
    assert edge.note("  queue:   batch:\n 801  ") is True and edge.note("") is False
    lines = (tmp_path / "records" / "AGENTS.md").read_text().splitlines()
    assert lines[0] == "## 9. Log" and len(lines) == 2
    assert re.fullmatch(r"- \d\d-\d\d \d\d:\d\d queue: batch: 801", lines[1]) and lines[1][2:13] == time.strftime("%m-%d %H:%M")
    assert (statedir.root() / "note.lock").exists()
    hzlib.note("second")
    assert (tmp_path / "records" / "AGENTS.md").read_text().endswith(" second\n")


def test_a_note_that_cannot_be_written_never_fails_the_caller(tmp_path):
    edge.configure(tmp_path / "missing" / "deeper")
    assert edge.note("x") is False


def test_raise_alarm_writes_the_alarm_state_format_and_clear_removes_only_the_queue_source(tmp_path):
    entry = edge.raise_alarm("stall:801", "ALERT", "#801 queued 20 min, BLOCKED with green checks")
    assert entry["key"] == "stall:801" and entry["new"] is True and entry["pushed"] is False
    data = json.loads((tmp_path / "alarm.json").read_text())
    assert set(data["stall:801"]) == {"first", "last", "sources", "notified", "acked", "cls", "text"}
    assert (data["stall:801"]["cls"], data["stall:801"]["sources"], data["stall:801"]["acked"]) == ("ALERT", ["queue"], False)
    stall.process([{"cls": "ALERT", "key": "stall:801", "text": "t"}], "watch", complete=False)
    assert edge.clear_alarm("stall:801") is True
    assert json.loads((tmp_path / "alarm.json").read_text())["stall:801"]["sources"] == ["watch"]
    assert edge.clear_alarm("stall:801") is False and edge.clear_alarm("stall:801", "watch") is True
    assert json.loads((tmp_path / "alarm.json").read_text()) == {}


def test_stamp_merged_writes_note_and_label_through_the_runner_and_swallows_every_failure(capsys):
    bd = FakeGh().on("state,mergeCommit,mergedAt", {"state": "MERGED", "mergeCommit": {"oid": "a" * 40}, "mergedAt": "2026-11-12T21:00:00Z"})
    bd.on("show heinzel-management-abc", {"labels": []}).on("update heinzel-management-abc", "").on("label add heinzel-management-abc", "")
    with inside(bd):
        assert edge.stamp_merged("abc", "801") == 0
        assert [c[0] for c in bd.calls] == ["gh", "bd", "bd", "bd"]
        assert bd.calls[2][1:4] == ["update", "heinzel-management-abc", "--append-notes"] and bd.calls[2][4].endswith("merged_in: PR #801 sha=aaaaaaaa at=2026-11-12T21:00:00Z")
        assert bd.calls[3][1:] == ["label", "add", "heinzel-management-abc", "merged-801"]
        assert edge.stamp_merged("abc", "not-a-number") is None
        bd.on("state,mergeCommit,mergedAt", {"state": "OPEN"})
        assert edge.stamp_merged("abc", "801") == 1  # refused: not merged; nothing written
        assert len([c for c in bd.calls if c[0] == "bd"]) == 3
    assert capsys.readouterr() == ("", "")  # as when the stamp was a process whose output nobody read


def test_run_goes_through_the_runner_inside_a_native_command_and_keeps_the_exceptions_the_moved_code_catches():
    class Runner:
        def __init__(self, result):
            self.result, self.calls = result, []

        def run(self, argv, cwd=None, timeout=None, env=None):
            self.calls.append((argv, cwd, timeout, env))
            return self.result

    ok = Runner(Result(0, "out", "err"))
    with inside(ok):
        done = edge.run(["gh", "api", "x"], capture_output=True, text=True, check=False, timeout=15, env={"A": "1"})
        assert (done.returncode, done.stdout, done.stderr, done.args) == (0, "out", "err", ["gh", "api", "x"])
        assert ok.calls == [(["gh", "api", "x"], None, 15, {"A": "1"})]
    with inside(Runner(Result(124, "", "timed out after 15s: gh"))), pytest.raises(subprocess.TimeoutExpired):
        edge.run(["gh"], capture_output=True, text=True, timeout=15)  # review finding S1: the moved retry on a timeout stays alive
    with inside(Runner(Result(127, "", "[Errno 2] No such file or directory: 'gh'"))), pytest.raises(FileNotFoundError):
        edge.run(["gh"], capture_output=True, text=True)
    with inside(Runner(Result(1, "", "boom"))), pytest.raises(subprocess.CalledProcessError):
        edge.run(["gh"], capture_output=True, text=True, check=True)


def test_the_moved_admission_read_retries_once_after_a_timeout_through_the_runner():
    jobs = {"total_count": 1, "jobs": [{"name": "CI OK", "status": "completed", "conclusion": "success"}]}
    gh = FakeGh().on("actions/runs/7/attempts/1/jobs", Result(124, "", "timed out after 15s: gh"), Result(0, json.dumps(jobs), ""))
    with inside(gh):
        assert hzlib._restoration_admission_jobs("actions/runs/7/attempts/1/jobs?per_page=100", 7, 1) == jobs["jobs"]
    assert len(gh.calls) == 2


def test_run_outside_a_native_command_and_for_calls_the_runner_cannot_express_is_the_plain_call(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: seen.append((argv, k)) or subprocess.CompletedProcess(argv, 0, "", ""))
    edge.run(["gh", "x"], capture_output=True, text=True)
    with inside(FakeGh()):
        edge.run(["gh", "api", "--input", "-"], input="{}", capture_output=True, text=True)
        edge.run(["gh", "api", "zip"], stdout=subprocess.PIPE)
    assert [argv for argv, _ in seen] == [["gh", "x"], ["gh", "api", "--input", "-"], ["gh", "api", "zip"]]


# ----------------------------------------------------------------------------- review findings B3 and D1
def test_the_checkpoint_reader_starts_as_a_program_of_its_own_and_gets_past_its_imports():
    """The queue starts this file isolated (`python -I FILE`, no package around it). With a relative import in it the
    reader died before reading its request and the checkpoint could never permit a merge (review finding B3)."""
    from steuermaschine_heinzel.native.queue_lib import main_checkpoint, main_checkpoint_reader, moved_map

    reader = Path(main_checkpoint_reader.__file__)
    assert "main_checkpoint_reader.py" in moved_map.STANDALONE
    assert "from . import" not in reader.read_text() and "edge." not in reader.read_text()
    done = subprocess.run([sys.executable, "-I", str(reader)], input=b"{}", capture_output=True, timeout=60, check=False)
    assert b"relative import" not in done.stderr and b"ImportError" not in done.stderr and b"ModuleNotFoundError" not in done.stderr, done.stderr
    assert b"KeyError: 'reader_head'" in done.stderr  # it read the (empty) request: every import worked
    source = Path(main_checkpoint.__file__).read_text()
    assert 'str(Path(__file__).with_name("main_checkpoint_reader.py"))' in source  # the queue still starts the file beside it


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_the_checkpoint_reader_starts_exactly_as_the_queue_starts_it(tmp_path, monkeypatch):
    """The same argument list as main_checkpoint.isolated_reader, a real process, nothing patched."""
    from steuermaschine_heinzel.native.queue_lib import main_checkpoint, main_checkpoint_reader

    seen = {}
    real = subprocess.run

    def spy(argv, **kwargs):
        seen["argv"] = list(argv)
        return real(argv, **{**kwargs, "env": {**os.environ, "UV_OFFLINE": os.environ.get("UV_OFFLINE", "")}})

    monkeypatch.setattr(subprocess, "run", spy)
    with pytest.raises(ValueError, match="protected focused reader refused"):
        main_checkpoint.isolated_reader({})
    assert seen["argv"][:8] == ["uv", "run", "--no-project", "--no-build", "--with", "coverage==7.17.0", "python", "-I"]
    assert seen["argv"][8] == main_checkpoint_reader.__file__
    monkeypatch.undo()
    done = subprocess.run(seen["argv"], input=b"{}", capture_output=True, timeout=180, check=False)
    if b"relative import" in done.stderr:
        pytest.fail(done.stderr.decode(errors="replace"))
    if b"KeyError: 'reader_head'" not in done.stderr:
        pytest.skip("uv could not provide the reader's environment here (offline): " + done.stderr.decode(errors="replace")[-200:])


@pytest.mark.parametrize("code", [0, 1])
def test_a_merge_call_whose_output_is_not_utf8_is_decoded_leniently_as_before(tmp_path, monkeypatch, code):
    """Review finding D1: `gh pr merge` is read with errors="replace". With the core's strict runner in between the
    tick died with UnicodeDecodeError after the merge had happened."""
    from steuermaschine.defaults import Subprocess
    from steuermaschine_heinzel.native.queue_lib import merge_train

    fake = tmp_path / "bin" / "gh"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\nprintf 'merged \\377 ok\\n'\nprintf 'refused \\377\\n' >&2\nexit {code}\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    with inside(Subprocess()):
        got, text = merge_train.gh("pr", "merge", "801", "-R", "o/r", "--squash", "--match-head-commit", "a" * 40)
    assert got == code and "\ufffd" in text and text.startswith("refused" if code else "merged")
    with inside(FakeGh().on("pr merge 801", "merged")):  # a scripted runner still sees the call
        assert merge_train.gh("pr", "merge", "801") == (0, "merged")


def test_a_strict_call_still_goes_through_the_core_runner(tmp_path, monkeypatch):
    from steuermaschine.defaults import Subprocess

    class Counting(Subprocess):
        calls = 0

        def run(self, argv, cwd=None, timeout=None, env=None):
            Counting.calls += 1
            return super().run(argv, cwd=cwd, timeout=timeout, env=env)

    with inside(Counting()):
        done = edge.run([sys.executable, "-c", "print('x')"], capture_output=True, text=True, check=False, errors=None)
        assert done.stdout == "x\n" and Counting.calls == 1
        lenient = edge.run([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff')"], capture_output=True, text=True, errors="replace")
        assert lenient.stdout == "\ufffd" and Counting.calls == 1


def test_the_records_checkout_has_no_default_and_refuses_naming_the_key():
    from steuermaschine.engine import Refuse
    from steuermaschine_heinzel.native.queue_lib import edge

    refusing = edge.REFUSING_RECORDS_DIR  # conftest keeps the production function here
    saved = list(edge._records)
    try:
        edge.configure(None)
        with pytest.raises(Refuse, match="queue_records_dir"):
            refusing()
        edge.configure("/r")
        assert refusing() == Path("/r")
    finally:
        edge._records[:] = saved
