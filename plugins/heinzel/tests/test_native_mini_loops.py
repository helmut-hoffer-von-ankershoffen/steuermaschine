"""`sm mini loops`: freshness by the installed sm release, never by script mtimes; read-only (no kickstart)."""

import json

import pytest
from sm_runtime_kit import runtime_with

from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, invoke
from steuermaschine_heinzel.native import loops

P = "org.example."
LIST = (f"PID\tStatus\tLabel\n101\t0\t{P}sm-queue\n102\t0\t{P}sm-train\n-\t0\t{P}sm-leakwatch\n103\t0\t{P}guard\n"
        f"104\t0\t{P}heinzel.daemon\n")


@pytest.fixture
def install(tmp_path):
    root = tmp_path / "stable"
    for rel in ("aaa", "bbb"):
        (root / "releases" / rel / "venv" / "bin").mkdir(parents=True)
    (root / "current").symlink_to("releases/bbb")
    return root


def ps(root, rel, args="daemon queue --loop 1"):
    return Result(0, f"Fri Nov 13 22:06:33 2026     {root}/releases/{rel}/venv/bin/python {root}/releases/{rel}/venv/bin/steuermaschine {args}\n", "")


def build(tmp_path, install, labels, answers=None, listing=Result(0, LIST, "")):
    commands = FakeCommands()
    commands.on(["launchctl", "list"], listing)
    answers = answers if answers is not None else {101: ps(install, "bbb"), 102: ps(install, "aaa", "daemon train --loop 60"),
                                                   103: Result(0, "Fri Nov 13 08:00:00 2026 /usr/bin/python3 /x/old-guard\n", "")}
    for pid, result in answers.items():
        commands.on(["ps", "-o", "lstart=,command=", "-p", str(pid)], result)
    conf = {"mini_loops_labels": [P + n for n in labels], "mini_loops_release_pointer": str(install / "current")}
    return runtime_with(tmp_path, conf, commands=commands), commands


def test_each_loop_is_judged_by_the_release_it_runs(tmp_path, install):
    runtime, commands = build(tmp_path, install, ["sm-queue", "sm-train", "sm-leakwatch", "guard", "gone", "heinzel.daemon"])
    done = invoke(runtime, ["mini", "loops"])
    assert done.exit_code == 1
    lines = done.stdout.splitlines()
    assert lines[0].split()[:2] == ["sm-queue", "fresh"] and "runs the installed release bbb" in lines[0]
    assert lines[1].split()[:2] == ["sm-train", "stale"] and "runs release aaa, installed is bbb" in lines[1]
    assert lines[2].split()[:3] == ["sm-leakwatch", "not", "running"]
    assert lines[3].split()[:2] == ["guard", "foreign"]
    assert lines[4].split()[:3] == ["gone", "not", "loaded"]
    assert lines[5].split()[:2] == ["daemon", "skipped"]
    assert any(line.startswith("STALE:") for line in lines) and any(line.startswith("FOREIGN:") for line in lines)
    assert all(call[0] in ("launchctl", "ps") for call in commands.calls)  # read-only: never kickstart, kill or bootout
    assert ["launchctl", "list"] in commands.calls and not any("kickstart" in call for call in commands.calls)
    assert ["ps", "-o", "lstart=,command=", "-p", "104"] not in commands.calls  # the live service is never read


def test_all_fresh_is_exit_0(tmp_path, install):
    runtime, _ = build(tmp_path, install, ["sm-queue", "sm-leakwatch"])
    done = invoke(runtime, ["mini", "loops"])
    assert done.exit_code == 0 and "STALE" not in done.stdout


def test_script_mtimes_do_not_matter(tmp_path, install):
    """The former check called a loop stale when a file was newer than the process; a new file in the release changes nothing."""
    (install / "releases" / "bbb" / "venv" / "bin" / "steuermaschine").write_text("touched after the start")
    runtime, _ = build(tmp_path, install, ["sm-queue"])
    assert invoke(runtime, ["mini", "loops"]).exit_code == 0


def test_an_unreadable_process_is_exit_1(tmp_path, install):
    runtime, _ = build(tmp_path, install, ["sm-queue"], answers={101: Result(1, "", "no such process")})
    done = invoke(runtime, ["mini", "loops"])
    assert done.exit_code == 1 and "unreadable" in done.stdout


def test_in_json(tmp_path, install):
    runtime, _ = build(tmp_path, install, ["sm-queue", "sm-train"])
    doc = loops.Loops.model_validate(json.loads(invoke(runtime, ["--format", "json", "mini", "loops"]).stdout))
    assert doc.installed == "bbb" and [(r.state, r.release, r.pid) for r in doc.loops] == [("fresh", "bbb", 101), ("stale", "aaa", 102)]


def test_refuses_without_labels(tmp_path, install):
    runtime = runtime_with(tmp_path, {"mini_loops_release_pointer": str(install / "current")})
    done = invoke(runtime, ["mini", "loops"])
    assert done.exit_code == 2 and "mini_loops_labels" in done.stderr


def test_refuses_without_a_release_pointer(tmp_path):
    runtime = runtime_with(tmp_path, {"mini_loops_labels": [P + "sm-queue"]})
    done = invoke(runtime, ["mini", "loops"])
    assert done.exit_code == 2 and "mini_loops_release_pointer" in done.stderr


def test_the_launchers_pointer_is_used_when_no_setting_names_one(tmp_path, install, monkeypatch):
    monkeypatch.setenv("STEUERMASCHINE_RELEASE", str(install / "current"))
    commands = FakeCommands()
    commands.on(["launchctl", "list"], Result(0, LIST, ""))
    commands.on(["ps"], ps(install, "bbb"))
    runtime = runtime_with(tmp_path, {"mini_loops_labels": [P + "sm-queue"]}, commands=commands)
    assert invoke(runtime, ["mini", "loops"]).exit_code == 0


def test_a_pointer_that_names_nothing_refuses(tmp_path):
    runtime = runtime_with(tmp_path, {"mini_loops_labels": [P + "sm-queue"], "mini_loops_release_pointer": str(tmp_path / "nope")})
    assert invoke(runtime, ["mini", "loops"]).exit_code == 1


def test_a_failing_launchctl_judges_nothing(tmp_path, install):
    runtime, commands = build(tmp_path, install, ["sm-queue"], listing=Result(1, "", "boom"))
    done = invoke(runtime, ["mini", "loops"])
    assert done.exit_code == 1 and "launchctl list failed" in done.stderr and commands.calls == [["launchctl", "list"]]


def test_launchd_rows_and_short_ps_lines():
    assert loops.launchd_pids("PID\tStatus\tLabel\n12\t0\ta\n-\t-15\tb\nbroken line\n") == {"Label": None, "a": 12, "b": None}
    assert loops.judge("x", 1, "Fri Nov 13", loops.Path("/nowhere")).state == "unreadable"
