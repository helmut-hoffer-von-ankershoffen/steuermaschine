"""The run lock across real processes: the plugin's policy and the core lock started as real processes (lock_entry.py)
in a sandbox directory, with the load fixed by the test. Moved from the former gate's tests (same scenarios, same lock
files); the old environment switches are options and settings now. The command itself (`sm gate run`, text, json,
options) is tested in process at the end of this file."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ENTRY = [sys.executable, str(Path(__file__).with_name("lock_entry.py"))]
SM = " ".join(ENTRY)


@pytest.fixture
def home(tmp_path):
    (tmp_path / ".cache").mkdir()
    return tmp_path


def env_of(home, load="5", **settings):
    table = {"lock_directory": str(home / ".cache"), "lock_history": str(home / "history.jsonl"), "lock_allow_full_log": str(home / "allow-full.log"),
             **{f"lock_{key}": value for key, value in settings.items()}}
    return {"HOME": str(home), "PATH": "/usr/bin:/bin", "LOCK_TEST": json.dumps({"load": float(load), "settings": table})}


def gate(*args, home, load="5", timeout=30, extra=None, **settings):
    return subprocess.run([*ENTRY, *args], capture_output=True, text=True, timeout=timeout, cwd=home, env={**env_of(home, load, **settings), **(extra or {})})


def holder(home, seconds="12", load="5", **settings):
    proc = subprocess.Popen([*ENTRY, "run", "--", "sleep", seconds], cwd=home, env=env_of(home, load, **settings), stderr=subprocess.DEVNULL)
    lock = home / ".cache/gate.lock"
    while not lock.exists() or f"sleep {seconds}" not in lock.read_text():
        pass
    return proc


def test_gate_acquires_records_the_holder_and_returns_the_exit_code(home):
    done = gate("run", "--", "sh", "-c", "cat $HOME/.cache/gate.lock; exit 7", home=home)
    assert done.returncode == 7
    assert "sm gate: acquired after" in done.stderr
    assert "sh -c cat $HOME/.cache/gate.lock; exit 7" in done.stdout
    assert '"exit": 7' in (home / "history.jsonl").read_text()


def test_gate_waits_while_the_hold_file_is_fresh_unless_exempt(home):
    (home / ".cache/gate.hold").touch()
    with pytest.raises(subprocess.TimeoutExpired):
        gate("run", "--", "true", home=home, timeout=4)
    assert gate("run", "--hold-exempt", "--", "true", home=home).returncode == 0


def test_a_second_gate_cannot_run_alongside_the_first_and_gives_up_naming_the_holder(home):
    first = holder(home, heavy_slots=1)
    try:
        done = gate("run", "--max-wait", "1", "--", "echo", "ran", home=home, heavy_slots=1)
        assert done.returncode == 75 and "ran" not in done.stdout
        assert "the heavy slot is held by" in done.stderr and "sleep 12" in done.stderr and "Nothing ran" in done.stderr
    finally:
        first.kill()


def test_a_light_command_runs_beside_a_held_gate(home):
    first = holder(home)
    try:
        done = gate("run", "--light", "--", "true", home=home)
        assert done.returncode == 0 and "light lane" in done.stderr
    finally:
        first.kill()


def test_gated_commands_get_capped_workers_and_overrides_only_lower_them(home):
    show = "echo $VITEST_MAX_WORKERS $VITEST_MAX_THREADS $VITEST_MAX_FORKS $PYTEST_XDIST_AUTO_NUM_WORKERS"
    assert gate("run", "--", "sh", "-c", show, home=home).stdout.split()[-4:] == ["8", "8", "8", "8"]
    looser = {"VITEST_MAX_WORKERS": "32", "PYTEST_XDIST_AUTO_NUM_WORKERS": "auto"}
    assert gate("run", "--", "sh", "-c", show, home=home, extra=looser).stdout.split()[-4:] == ["8", "8", "8", "8"]
    stricter = {"VITEST_MAX_WORKERS": "1", "PYTEST_XDIST_AUTO_NUM_WORKERS": "1"}
    assert gate("run", "--", "sh", "-c", show, home=home, extra=stricter).stdout.split()[-4:] == ["1", "8", "8", "1"]


def test_no_gate_is_admitted_at_or_above_the_load_bar_even_with_priority(home):
    for level in ("0", "1", "2"):
        assert gate("run", "--priority", level, "--max-wait", "1", "--", "true", home=home, load="40").returncode == 75
    assert gate("run", "--", "true", home=home, load="39.9").returncode == 0
    assert gate("run", "--max-wait", "1", "--", "true", home=home, load="60", max_load=100).returncode == 75  # a setting cannot raise the bar


def test_a_gate_inside_a_gate_runs_at_once_and_a_forged_holder_does_not(home):
    inner = SM + " run -- sh -c 'echo inner $HZ_GATE_HELD'"
    done = gate("run", "--", "sh", "-c", f"echo outer $HZ_GATE_HELD; {inner}", home=home, timeout=15)
    outer, inner_line = done.stdout.split("\n")[:2]
    assert done.returncode == 0 and outer.split()[1] == inner_line.split()[1]
    first = holder(home)
    try:
        forged = gate("run", "--max-wait", "1", "--", "true", home=home, load="20", extra={"HZ_GATE_HELD": str(first.pid)})
        assert forged.returncode == 75
    finally:
        first.kill()


def test_two_gates_run_together_below_load_20_and_share_the_workers(home):
    first = holder(home, heavy_slots=2)
    try:
        done = gate("run", "--", "sh", "-c", "echo $VITEST_MAX_WORKERS", home=home, load="19", heavy_slots=2)
        assert done.stdout.split()[-1] == "4"
        assert gate("run", "--max-wait", "1", "--", "true", home=home, load="20", heavy_slots=2).returncode == 75
        assert "heavy slots in use 1/2, workers per gate 9" in gate("status", home=home, heavy_slots=2).stdout
    finally:
        first.kill()


def test_full_local_suites_are_refused_also_under_an_ancestor_and_allow_full_is_logged(home):
    done = gate("run", "--", "make", "pr", home=home)
    assert done.returncode == 2 and "refusing: `make pr` is never run on this machine" in done.stderr and "sm gate run --" in done.stderr
    no_pre = gate("run", "--", "make", "pr", home=home, extra={"NO_PREFLIGHT": "1"})
    assert no_pre.returncode == 2 and "`make pr` is never run on this machine" in no_pre.stderr and "`sm head pr BEAD TITLE`" in no_pre.stderr
    nested = gate("run", "--", "sh", "-c", SM + " run -- pytest", home=home)
    assert nested.returncode == 2 and "without a specific test path" in nested.stderr
    assert gate("run", "--allow-full", "--", "sh", "-c", "true", home=home).returncode == 0
    allowed = gate("run", "--allow-full", "--", "env", "X=1", "make", "-n", "--version", home=home)
    assert allowed.returncode == 0


def test_timed_gate_stops_a_running_command_and_usage_errors_exit_2(home):
    done = gate("run", "--timeout", "1", "--", "sleep", "20", home=home)
    assert done.returncode == 124 and "timed out after 1s" in done.stderr
    assert gate("run", home=home).returncode == 2


def test_a_stale_slot_record_and_an_ancestor_pid_do_not_admit_a_gate(home):
    import os

    (home / ".cache/gate.lock").write_text(f"{os.getpid()} stale\n")
    done = gate("run", "--", "echo", "ran", home=home, extra={"HZ_GATE_HELD": str(os.getpid())})
    assert done.returncode == 0 and "acquired after" in done.stderr  # it took the slot itself; the record alone admitted nothing


def runtime(tmp_path):
    from steuermaschine.app import Runtime
    from steuermaschine.config import Project
    from steuermaschine.plugins import stack_of
    from steuermaschine.testing import fake_providers
    from steuermaschine_heinzel.plugin import plugin

    stack = stack_of(plugin)
    table = {"lock_directory": str(tmp_path), "lock_history": str(tmp_path / "history.jsonl"), "lock_allow_full_log": str(tmp_path / "full.log")}
    settings, _ = stack.validate({"heinzel": table}, tmp_path)
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=True), stack, settings,
                   _providers=stack.providers(settings, fake_providers()))  # the plugin's policy reaches the core's gate as an extra


def test_the_command_in_text_and_json_with_its_options(tmp_path):
    from steuermaschine.commands import gate
    from steuermaschine.protocols import Result
    from steuermaschine.runlock import Status
    from steuermaschine.testing import invoke

    rt = runtime(tmp_path)
    rt.providers.commands.on(["ps"], Result(0, "1\n"))
    rt.providers.commands.on(["git"], Result(1, ""))
    rt.providers.commands.on(["true"], Result(0, ""))
    status = Status.model_validate(json.loads(invoke(rt, ["--format", "json", "gate", "status"]).stdout))
    assert status.slots_in_use == 0 and status.heavy_slots == 3 and status.max_load == 40
    assert "heavy slots in use 0/3, workers per gate 9" in invoke(rt, ["gate", "status"]).stdout
    refused = invoke(rt, ["gate", "run", "--", "make", "pr"])
    assert refused.exit_code == 2 and "sm gate: refusing: `make pr` is never run on this machine" in refused.stderr and refused.stdout.strip() == ""
    assert "Allowed route: `sm head pr BEAD TITLE`" in refused.stderr and "--live" in refused.stderr and "-- make pr`" not in refused.stderr  # the route names sm head pr, never make pr
    ran = gate.GateRun.model_validate(json.loads(invoke(rt, ["--format", "json", "gate", "run", "--light", "--timeout", "5", "--", "true"]).stdout))
    assert (ran.status, ran.lane, ran.code, ran.command) == ("ran", "light", 0, ["true"])
    assert rt.providers.commands.calls[-1] == ["true"]
    allowed = invoke(rt, ["gate", "run", "--allow-full", "--light", "--", "make", "preflight"])
    assert allowed.exit_code == 127 and "make preflight" in (tmp_path / "full.log").read_text()  # admitted and logged; the fake has no make
    plain = invoke(rt, ["gate", "run", "--allow-full", "--light", "--", "make", "pr"])
    assert plain.exit_code == 2 and "`make pr` is never run on this machine" in plain.stderr and "sm head pr" in plain.stderr
    never = invoke(rt, ["gate", "run", "--allow-full", "--light", "--", "env", "NO_PREFLIGHT=1", "make", "pr"])
    assert never.exit_code == 2 and "`make pr` is never run on this machine" in never.stderr and not any(line.endswith("make pr") for line in (tmp_path / "full.log").read_text().splitlines())
    assert invoke(rt, ["gate", "run", "--priority", "9", "--", "true"]).exit_code == 2


def test_exclusive_reaches_the_run_lock_as_an_exclusive_request(tmp_path, monkeypatch):
    from steuermaschine.commands import gate
    from steuermaschine.testing import invoke

    seen = []
    monkeypatch.setattr(gate, "execute", lambda the_lock, request, say: seen.append(request) or gate.GateRun(status="ran", command=list(request.argv), code=0))
    rt = runtime(tmp_path)
    assert invoke(rt, ["gate", "run", "--exclusive", "--priority", "2", "--", "git", "push"]).exit_code == 0
    assert invoke(rt, ["gate", "run", "--", "true"]).exit_code == 0
    assert [(r.exclusive, r.priority) for r in seen] == [(True, 2), (False, 0)]
    assert "--exclusive" in invoke(rt, ["gate", "run", "--help"]).stdout
