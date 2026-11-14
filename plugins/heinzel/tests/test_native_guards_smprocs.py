"""The process shapes of sm, taken from the LIVE process table: this test starts a real `sm gate run -- <sleep> 5` (through
a stable-layout copy of the launcher that bin/promote writes, and through the console script) and a real
`sm daemon loadguard --loop 1` in a sandboxed HOME, reads `ps` and `lsof`, and asks the load guard and the governor what
they would do. Nothing is signalled by the guards; the test stops its own processes at the end.

The bug this pins: the never-touch patterns named `sm gate run|release|train|daemon`, but the launcher and the console
scripts exec the venv's python, so the real process reads `…/venv/bin/python …/venv/bin/steuermaschine gate run -- …`.
The command under the gate is a shell script named `pytest` inside a worktree below the sandbox's ~/Code that runs
`sleep`, called with a test path (the gate refuses a bare `pytest`), so that the load guard sees a test tree and the
governor a builder process.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import guards_runtime as g
import pytest

from steuermaschine import smprocs
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, invoke
from steuermaschine_heinzel.native import guards
from steuermaschine_heinzel.native.guards_lib import governor, loadguard

REPO = Path(__file__).resolve().parents[3]
VENV_BIN = Path(sys.executable).parent
OLD_GATED = re.compile(r"(^|[\s/])sm\s+(gate\s+run|release|train)(\s|$)")  # the former spelling, to show why it missed
OLD_NEVER = re.compile(r"(^|[\s/])sm\s+(daemon|queue|release|train|go|gate|mini)(\s|$)")


def launcher_text() -> str:
    """The launcher exactly as bin/promote writes it to <stable>/bin/sm (install_launcher in bin/promote_lib.sh)."""
    text = (REPO / "bin" / "promote_lib.sh").read_text()
    return text.split("<<'SH'\n", 1)[1].split("\nSH\n", 1)[0] + "\n"


def ps(fields: str) -> str:
    return subprocess.run(["ps", "-axo", fields], capture_output=True, text=True, check=True).stdout


LOAD_WAIT_CEILING = 900.0  # the run lock admits nothing at or above load 40: on a busy machine the gates start later


def wait_for(predicate, seconds: float = 20.0, held_by_load=lambda: False):
    """The predicate's first truthy answer within `seconds`. While `held_by_load()` says a gate of the fixture still
    waits for the machine's load to drop (the run lock's bar, real by design), the clock restarts, up to the ceiling:
    a gate that has not started yet is not a missing process (bead t84w: this fixture failed a promote gate at load 41)."""
    start = time.monotonic()
    end = start + seconds
    while time.monotonic() < end:
        found = predicate()
        if found:
            return found
        if held_by_load() and time.monotonic() - start < LOAD_WAIT_CEILING:
            end = time.monotonic() + seconds
        time.sleep(0.05)
    pytest.fail("the process did not appear in the process table")


def waiting_for_load(log: Path) -> bool:
    """Whether a gate's stderr says it waits for the load bar and has not been admitted since."""
    try:
        text = log.read_text()
    except OSError:
        return False
    return "acquired after" not in text and bool(re.search(r"waiting \d+s: load \d+ is at or above", text))


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("smprocs")
    home = tmp / "home"
    worktree = home / "Code" / "heinzel-wt-shapes"
    (worktree / "bin").mkdir(parents=True)
    sleeper = worktree / "bin" / "pytest"
    sleeper.write_text("#!/bin/sh\nsleep 30\n")
    sleeper.chmod(0o755)
    project = tmp / "project"
    project.mkdir()
    (project / "steuermaschine.toml").write_text('[project]\nname = "guards-shapes"\nplugin = "heinzel"\n\n[heinzel]\nlock_extra_slot_load = 1000.0\n')
    stable = tmp / "stable"
    (stable / "releases" / "r1").mkdir(parents=True)
    (stable / "releases" / "r1" / "venv").symlink_to(VENV_BIN.parent)
    (stable / "current").symlink_to("releases/r1")
    (stable / "bin").mkdir()
    (stable / "bin" / "sm").write_text(launcher_text())
    (stable / "bin" / "sm").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("STEUERMASCHINE_", "SM_", "XDG_", "HZ_"))}
    env.update(HOME=str(home), STEUERMASCHINE_PROJECT=str(project), XDG_CONFIG_HOME=str(tmp / "no-config"))
    started: list[subprocess.Popen] = []

    logs = [tmp / "gate-a.err", tmp / "gate-b.err"]

    def start(argv, stdout=subprocess.DEVNULL, stderr: Path | None = None):
        with open(stderr, "w") if stderr else contextlib.nullcontext(subprocess.DEVNULL) as err:
            proc = subprocess.Popen([str(a) for a in argv], cwd=worktree, env=env, stdin=subprocess.DEVNULL, stdout=stdout,
                                    stderr=err, start_new_session=True)
        started.append(proc)
        return proc

    try:
        via_launcher = start([stable / "bin" / "sm", "gate", "run", "--", sleeper, "tests/a_test.py"], stderr=logs[0])
        via_script = start([VENV_BIN / "steuermaschine", "gate", "run", "--", sleeper, "tests/b_test.py"], stderr=logs[1])
        # The daemons are held (SIGSTOP) right after exec: their review is open, so a tick would refuse and exit at once.
        # One runs the path the launcher execs (what launchd's `sm daemon ...` becomes), one the `sm` console script.
        daemon = start([stable / "current" / "venv" / "bin" / "steuermaschine", "daemon", "loadguard", "--loop", "1"])
        os.kill(daemon.pid, signal.SIGSTOP)
        daemon_sm = start([VENV_BIN / "sm", "daemon", "governor", "--loop", "1"])
        os.kill(daemon_sm.pid, signal.SIGSTOP)
        # The ungated tree is detached (its parent exits), so no test runner or gate is among its ancestors.
        spawner = start(["/bin/sh", "-c", f"'{sleeper}' tests/c_test.py >/dev/null 2>&1 & echo $!"], stdout=subprocess.PIPE)
        ungated = int(spawner.communicate(timeout=10)[0].strip())

        def children_running():
            table = loadguard.parse_ps(ps(loadguard.PS_FIELDS))
            kids = [p for p in table.values() if p["ppid"] in (via_launcher.pid, via_script.pid) and str(sleeper) in p["cmd"]]
            launcher_execd = "steuermaschine" in table.get(via_launcher.pid, {}).get("cmd", "")
            return table if len(kids) == 2 and launcher_execd and ungated in table else None

        procs = wait_for(children_running, held_by_load=lambda: any(waiting_for_load(log) for log in logs))
        rows = governor.ps_table(ps("pid=,ppid=,pcpu=,uid=,args="))
        yield {"procs": procs, "rows": rows, "home": str(home), "stable": stable, "worktree": worktree, "launcher": via_launcher.pid,
               "script": via_script.pid, "daemon": daemon.pid, "daemon_sm": daemon_sm.pid, "ungated": ungated}
    finally:
        for proc in started:
            stop(proc)


def stop(proc: subprocess.Popen) -> None:
    """Kill the process group the test started (the gate's child and the detached tree included), then the process."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def shapes(live):
    return smprocs.establish([live["stable"]], sys.executable)


def test_the_real_processes_are_the_venv_python_running_the_entry_not_the_word_sm(live):
    procs = live["procs"]
    for pid in (live["launcher"], live["script"], live["daemon"], live["daemon_sm"]):
        first = procs[pid]["cmd"].split()[0]
        assert smprocs.base(first).startswith("python"), procs[pid]["cmd"]
    assert "/stable/releases/r1/venv/bin/steuermaschine gate run" in procs[live["launcher"]]["cmd"]  # the launcher exec'd the resolved release
    assert not OLD_GATED.search(procs[live["launcher"]]["cmd"]) and not OLD_GATED.search(procs[live["script"]]["cmd"])
    assert not OLD_NEVER.search(procs[live["daemon"]]["cmd"])  # so the former patterns missed every process the launcher starts


def test_the_shapes_read_the_command_path_of_every_live_sm_process(live, shapes):
    procs = live["procs"]
    assert shapes.verbs(procs[live["launcher"]]["cmd"])[:2] == ["gate", "run"]
    assert shapes.verbs(procs[live["script"]]["cmd"])[:2] == ["gate", "run"]
    assert shapes.verbs(procs[live["daemon"]]["cmd"]) == ["daemon", "loadguard"]
    assert shapes.verbs(procs[live["daemon_sm"]]["cmd"]) == ["daemon", "governor"]
    assert shapes.verbs(procs[live["ungated"]]["cmd"]) is None
    assert all(loadguard.gated(procs[live[k]]["cmd"], shapes) for k in ("launcher", "script"))


def test_the_install_root_is_derived_from_the_running_stable_interpreter(live):
    derived = smprocs.establish([], str(live["stable"] / "current" / "venv" / "bin" / "python"))
    assert any(r.startswith(str(live["stable"].absolute())) for r in derived.roots)
    released = smprocs.establish([], str(live["stable"] / "releases" / "r1" / "venv" / "bin" / "python"))
    assert set(released.roots) == set(derived.roots)
    procs = live["procs"]
    assert derived.under_root(procs[live["launcher"]]["cmd"].split()[1])


def test_the_load_guard_pauses_only_the_ungated_tree_of_the_live_table(live, shapes):
    procs, home, uid = live["procs"], live["home"], os.getuid()
    roots = [p["pid"] for p in loadguard.ungated_roots(procs, home, uid, shapes)]
    assert roots == [live["ungated"]]  # not the two trees under the run lock, not the gate processes themselves
    action, root, pids = loadguard.plan(procs, [], 1000.0, home, uid, shapes)
    assert action == "stop" and root["pid"] == live["ungated"] and pids[0] == live["ungated"]


def test_the_load_guard_pauses_nothing_without_established_shapes(live):
    assert loadguard.plan(live["procs"], [], 1000.0, live["home"], os.getuid(), None) == ("none", None, [])


def test_the_governor_never_stops_a_live_sm_process_but_does_stop_a_builder_process(live, shapes):
    rows = {r["pid"]: r for r in live["rows"]}

    def cwd(pid: int) -> str:
        return governor.cwd_of(subprocess.run(["lsof", "-a", "-d", "cwd", "-Fn", "-p", str(pid)], capture_output=True, text=True).stdout)

    assert re.search(r"/Code/heinzel-wt-shapes$", cwd(live["daemon"]))  # every sm process sits in a builder worktree
    for key in ("launcher", "script", "daemon", "daemon_sm"):
        assert not governor.eligible(rows[live[key]], me=0, cwd=cwd, shapes=shapes), key
    assert governor.eligible(rows[live["ungated"]], me=0, cwd=cwd, shapes=shapes)
    busy = [{**rows[live[k]], "cpu": 50.0} for k in ("launcher", "script", "daemon", "daemon_sm", "ungated")]
    assert [r["pid"] for r in governor.pick(busy, set(), cwd=cwd, shapes=shapes)] == [live["ungated"]]
    assert governor.pick(busy, set(), cwd=cwd, shapes=None) == []  # fail closed


# --- fail closed in the commands --------------------------------------------------------------------------------------------

def test_no_install_root_means_no_shapes(tmp_path):
    with pytest.raises(smprocs.Unestablished):
        smprocs.establish([], "/usr/bin/python3")
    with pytest.raises(smprocs.Unestablished):
        smprocs.establish([tmp_path / "missing"], sys.executable)
    assert smprocs.establish([tmp_path], "/usr/bin/python3").roots[0] == str(tmp_path.absolute())


PS = """\
 300     1  501 S       02:00 /Users/x/Code/wt-b/.venv/bin/python3 /Users/x/Code/wt-b/.venv/bin/pytest -n 4
 301   300  501 R       01:59 /Users/x/Code/wt-b/.venv/bin/python3 -c import sys;exec(eval(sys.stdin.readline()))
"""


def test_the_load_guard_tick_pauses_nothing_when_the_shapes_cannot_be_established(monkeypatch, tmp_path):
    g.register(monkeypatch, {"guards_sm_install_roots": [str(tmp_path / "missing")]})
    monkeypatch.setenv("HOME", "/Users/x")
    monkeypatch.setattr(guards, "user_id", lambda: 501)
    monkeypatch.setattr(guards, "load_now", lambda: 150.0)
    commands = FakeCommands()
    commands.on(["ps"], Result(0, PS, ""))
    done = invoke(g.runtime_of(tmp_path, commands=commands), ["daemon", "loadguard"])
    assert done.exit_code == 1 and "could not be established" in done.stdout and [c[0] for c in commands.calls] == ["ps"]


def test_the_governor_tick_stops_nothing_when_the_shapes_cannot_be_established(monkeypatch, tmp_path):
    g.register(monkeypatch, {"guards_sm_install_roots": [str(tmp_path / "missing")], "guards_governor_off_file": str(tmp_path / "off")})
    monkeypatch.setattr(guards, "load_now", lambda: 99.0)
    commands = FakeCommands()
    me = os.getuid()
    commands.on(["ps"], Result(0, f"900 1 80.0 {me} /Users/x/Code/hz-w/.venv/bin/python -m pytest\n", ""))
    commands.on(["lsof"], Result(0, "n/Users/x/Code/hz-w\n", ""))
    done = invoke(g.runtime_of(tmp_path, commands=commands), ["daemon", "governor"])
    assert done.exit_code == 1 and "could not be established" in done.stdout and not any(c[0] == "kill" for c in commands.calls)


def test_a_gate_waiting_for_the_load_bar_is_not_a_missing_process(tmp_path, monkeypatch):
    log = tmp_path / "gate.err"
    assert not waiting_for_load(log)
    log.write_text("sm gate: waiting 0s: load 41 is at or above 40. One heavy run at a time\n")
    assert waiting_for_load(log)
    log.write_text(log.read_text() + "sm gate: acquired after 55s\n")
    assert not waiting_for_load(log)
    answers = iter([None, None, None, "found"])
    now = iter(range(0, 1000, 10))
    monkeypatch.setattr(time, "monotonic", lambda: float(next(now)))
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    assert wait_for(lambda: next(answers), seconds=15, held_by_load=lambda: True) == "found"  # 30 s passed, the clock restarted
    with pytest.raises(pytest.fail.Exception):
        wait_for(lambda: None, seconds=15)
