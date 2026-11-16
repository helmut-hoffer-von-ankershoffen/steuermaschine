"""The load guard: the former tool's four tests moved onto the library (state, clock and signals through `Edge`), and
`sm daemon loadguard` through the command tree on fakes (the exact calls, in order; off without review; json).

Changed in the moved tests: the old ones persisted the paused list in a file under an environment variable and read the
log file for the SIGSTOP/SIGCONT counts; here the state and the log lines are kept in memory by `Ticker`. Every assertion
is the old one.
"""

import json
import signal
from pathlib import Path

import guards_runtime as g
import pytest

from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, invoke
from steuermaschine_heinzel.native import guards
from steuermaschine_heinzel.native.guards_lib import legacy, loadguard

HOME = "/Users/x"
PS = """\
   1     0    0 Ss   05:00:00 /sbin/launchd
 100     1  501 Ss   02:00:00 /Users/x/.local/share/uv/tools/heinzel/bin/python -I -u -m heinzel ui --port 8737
 110   100  501 S       30:00 /Users/x/.local/share/uv/tools/heinzel/bin/python -m pytest-looking-name
 200     1  501 S       10:00 uv run coverage run -m pytest -m unit -n 4
 201   200  501 R       10:00 /Users/x/Code/wt-a/.venv/bin/python3 /Users/x/Code/wt-a/.venv/bin/coverage run -m pytest -n 4
 202   201  501 R       09:59 /Users/x/Code/wt-a/.venv/bin/python3 -c import sys;exec(eval(sys.stdin.readline()))
 300     1  501 S       02:00 /Users/x/Code/wt-b/.venv/bin/python3 /Users/x/Code/wt-b/.venv/bin/pytest -n 4
 301   300  501 R       01:59 /Users/x/Code/wt-b/.venv/bin/python3 -c import sys;exec(eval(sys.stdin.readline()))
 400     1  501 S       01:00 /usr/bin/python3 /Users/x/.local/bin/hz-gate make pr
 401   400  501 R       01:00 /Users/x/Code/wt-c/.venv/bin/python3 /Users/x/Code/wt-c/.venv/bin/pytest -n 8
 500     1  502 R       00:30 /Users/x/Code/wt-d/.venv/bin/python3 /Users/x/Code/wt-d/.venv/bin/pytest
 600     1  501 S       00:20 /usr/bin/python3 /Users/x/Code/heinzel-management/tools/hz-release 0.1.0
 601   600  501 R       00:20 /Users/x/Code/wt-e/.venv/bin/python3 /Users/x/Code/wt-e/.venv/bin/pytest
"""


class Ticker:
    """The moved tests' guard: the paused list and the log kept in memory, as the old tests kept them in files."""

    def __init__(self):
        self.state, self.logs = [], []

    def tick(self, ps, load, kill, home, uid):
        def save(stopped):
            self.state = list(stopped)

        edge = loadguard.Edge(kill=kill, now=lambda: 1000.0, load_state=lambda: list(self.state), save_state=save, log=self.logs.append)
        return loadguard.tick(ps, load, edge, home, uid)


@pytest.fixture
def guard():
    return Ticker()


def test_only_ungated_worktree_test_trees_of_this_user_are_candidates_newest_first():
    roots = loadguard.ungated_roots(loadguard.parse_ps(PS), HOME, 501)
    assert [r["pid"] for r in roots] == [300, 201]  # not the live hz, a gated run, another user or a release run


def test_above_the_bar_it_pauses_the_newest_tree_and_below_resume_continues_it(guard):
    sent = []
    kill = lambda pid, sig: sent.append((pid, sig))  # noqa: E731
    assert guard.tick(PS, 150, kill, HOME, 501) == "stop 300"
    assert sent == [(300, signal.SIGSTOP), (301, signal.SIGSTOP)]
    paused = PS.replace(" 300     1  501 S ", " 300     1  501 T ").replace(" 301   300  501 R ", " 301   300  501 T ")
    assert guard.tick(paused, 150, kill, HOME, 501) == "stop 201"  # one more tree per tick, never the same one twice
    sent.clear()
    assert guard.tick(paused, 60, kill, HOME, 501) == "none"  # between the bars nothing changes
    assert guard.tick(paused, 30, kill, HOME, 501) == "resume 300"
    assert sent == [(301, signal.SIGCONT), (300, signal.SIGCONT)]
    assert guard.tick(paused, 30, kill, HOME, 501) == "resume 201"
    assert guard.tick(paused, 30, kill, HOME, 501) == "none"
    log = "\n".join(guard.logs)
    assert log.count("SIGSTOP") == 2 and log.count("SIGCONT") == 2


def test_a_paused_pid_that_now_runs_something_else_is_never_continued(guard):
    sent = []
    guard.tick(PS, 150, lambda pid, sig: sent.append((pid, sig)), HOME, 501)
    reused = PS.replace("/Users/x/Code/wt-b/.venv/bin/python3 /Users/x/Code/wt-b/.venv/bin/pytest -n 4", "/usr/bin/other")
    sent.clear()
    assert guard.tick(reused, 30, lambda pid, sig: sent.append((pid, sig)), HOME, 501) == "none"
    assert sent == []


def test_below_the_bar_nothing_is_paused(guard):
    assert guard.tick(PS, 99, lambda *_: pytest.fail("signalled"), HOME, 501) == "none"


def test_a_test_run_under_the_new_run_lock_or_release_commands_is_never_a_candidate():
    gated = PS + (" 700     1  501 S       00:40 /usr/bin/python3 /Users/x/.local/bin/sm gate run -- pytest tests/x\n"
                  " 701   700  501 R       00:40 /Users/x/Code/wt-f/.venv/bin/python3 /Users/x/Code/wt-f/.venv/bin/pytest -n 4\n"
                  " 800     1  501 S       00:30 /Users/x/.local/bin/sm release cut\n"
                  " 801   800  501 R       00:30 /Users/x/Code/wt-g/.venv/bin/python3 /Users/x/Code/wt-g/.venv/bin/pytest\n"
                  " 900     1  501 S       00:20 /usr/bin/python3 /Users/x/.local/bin/sm train run\n"
                  " 901   900  501 R       00:20 /Users/x/Code/wt-h/.venv/bin/python3 /Users/x/Code/wt-h/.venv/bin/pytest\n"
                  "1000     1  501 S       00:10 /Users/x/.local/bin/sm gates run -- pytest\n"
                  "1001  1000  501 R       00:10 /Users/x/Code/wt-i/.venv/bin/python3 /Users/x/Code/wt-i/.venv/bin/pytest\n")
    roots = loadguard.ungated_roots(loadguard.parse_ps(gated), HOME, 501)
    assert [r["pid"] for r in roots] == [1001, 300, 201]  # only the one under a look-alike command stays a candidate


# --- the command ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def cmd(monkeypatch, tmp_path):
    conf = g.register(monkeypatch, {"guards_loadguard_legacy_dir": str(tmp_path / "old")})
    monkeypatch.setenv("HOME", HOME)
    monkeypatch.setattr(guards, "user_id", lambda: 501)
    load = {"value": 150.0}
    monkeypatch.setattr(guards, "load_now", lambda: load["value"])

    def build(reviewed=True, ps=Result(0, PS, "")):
        commands = FakeCommands()
        commands.on(["ps"], ps)
        commands.on(["kill"], Result(0, "", ""))
        return g.runtime_of(tmp_path, reviewed, commands=commands), commands

    return build, load, conf


def test_one_tick_makes_exactly_these_calls_in_this_order_and_records_what_it_paused(cmd):
    build, load, _ = cmd
    runtime, commands = build()
    done = invoke(runtime, ["daemon", "loadguard"])
    assert done.exit_code == 0 and done.stdout.splitlines()[-1] == "stop 300" and "SIGSTOP ungated test tree 300 (2 processes)" in done.stdout
    assert commands.calls == [["ps", "-axo", "pid=,ppid=,uid=,stat=,etime=,command="], ["kill", "-STOP", "300"], ["kill", "-STOP", "301"]]
    record = runtime.providers.store.load("heinzel.guards.loadguard", "stopped")["stopped"]
    assert [e["pid"] for e in record] == [300] and record[0]["pids"] == [300, 301]
    load["value"] = 30.0
    commands.calls.clear()
    again = invoke(runtime, ["daemon", "loadguard"])
    assert again.stdout.splitlines()[-1] == "resume 300" and commands.calls[1:] == [["kill", "-CONT", "301"], ["kill", "-CONT", "300"]]
    assert runtime.providers.store.load("heinzel.guards.loadguard", "stopped") == {"stopped": []}


def test_the_tick_in_json(cmd):
    build, _, _ = cmd
    done = invoke(build()[0], ["--format", "json", "daemon", "loadguard"])
    doc = guards.LoadguardTick.model_validate(json.loads(done.stdout))
    assert doc.action == "stop" and doc.pid == 300 and doc.load == 150.0 and doc.readable


def test_an_unreadable_process_table_signals_nothing_and_keeps_the_paused_list(cmd):
    build, _, _ = cmd
    runtime, commands = build(ps=Result(1, "", "ps: boom"))
    runtime.providers.store.save("heinzel.guards.loadguard", "stopped", {"stopped": [{"pid": 300, "cmd": "x", "pids": [300], "at": 1.0}]})
    done = invoke(runtime, ["daemon", "loadguard"])
    assert done.exit_code == 1 and "could not be read" in done.stdout and [c[0] for c in commands.calls] == ["ps"]
    assert runtime.providers.store.load("heinzel.guards.loadguard", "stopped")["stopped"][0]["pid"] == 300


def test_a_tree_the_former_guard_paused_is_still_continued(cmd, tmp_path):
    build, load, _ = cmd
    old = tmp_path / "old"
    old.mkdir()
    cmdline = "/Users/x/Code/wt-b/.venv/bin/python3 /Users/x/Code/wt-b/.venv/bin/pytest -n 4"
    (old / "stopped.json").write_text(json.dumps([{"pid": 300, "cmd": cmdline, "pids": [300, 301], "at": 5.0}]))
    load["value"] = 30.0
    runtime, commands = build()
    assert legacy.loadguard_stopped(old)[0]["pid"] == 300
    done = invoke(runtime, ["daemon", "loadguard"])
    assert done.stdout.splitlines()[-1] == "resume 300" and ["kill", "-CONT", "300"] in commands.calls
    assert guards.loadguard_paused(runtime.providers.store, old) == []


def test_the_legacy_reader_survives_a_missing_or_broken_file(tmp_path):
    assert legacy.loadguard_stopped(tmp_path) == []
    (tmp_path / "stopped.json").write_text("{not json")
    assert legacy.loadguard_stopped(tmp_path) == []
    (tmp_path / "stopped.json").write_text(json.dumps([{"pid": 1}, "x"]))
    assert legacy.loadguard_stopped(tmp_path) == []
    assert Path(tmp_path).exists()


def test_a_second_tick_waits_while_one_holds_the_store_lock_and_signals_nothing(cmd):
    build, _, _ = cmd
    runtime, commands = build()
    runtime.providers.store.save("heinzel.guards.loadguard", "stopped", {"stopped": [{"pid": 300, "cmd": "x", "pids": [300], "at": 1.0}]})
    with runtime.providers.store.lock("heinzel.guards.loadguard"):
        done = invoke(runtime, ["daemon", "loadguard"])
    assert done.exit_code == 75 and "another load guard tick is running" in done.stderr and commands.calls == []
    assert runtime.providers.store.load("heinzel.guards.loadguard", "stopped")["stopped"][0]["pid"] == 300
    assert invoke(runtime, ["daemon", "loadguard"]).exit_code == 0  # the lock is free again


# --- review of daemon-loadguard.md: findings 2 and 3 ---------------------------------------------------------------------

def test_the_stopped_children_of_a_paused_tree_whose_root_exited_are_continued(guard):
    sent = []
    guard.tick(PS, 150, lambda pid, sig: sent.append((pid, sig)), HOME, 501)
    sent.clear()
    gone = "".join(line + "\n" for line in PS.splitlines() if not line.startswith(" 300 "))
    orphaned = gone.replace(" 301   300  501 R ", " 301     1  501 T ")  # reparented, still SIGSTOPped
    guard.tick(orphaned, 60, lambda pid, sig: sent.append((pid, sig)), HOME, 501)
    assert sent == [(301, signal.SIGCONT)] and guard.state == [] and "whose root is gone: [301]" in guard.logs[-1]
    sent.clear()
    guard.state = [{"pid": 300, "cmd": "gone", "pids": [300, 301], "at": 1.0}]
    guard.tick(gone, 60, lambda pid, sig: sent.append((pid, sig)), HOME, 501)
    assert sent == []  # a child that runs (a reused pid, or continued already) is left alone


def test_a_tree_paused_longer_than_half_an_hour_raises_an_alarm_on_every_tick(guard):
    paused = PS.replace(" 300     1  501 S ", " 300     1  501 T ")
    guard.state = [{"pid": 300, "cmd": "/Users/x/Code/wt-b/.venv/bin/python3 /Users/x/Code/wt-b/.venv/bin/pytest -n 4", "pids": [300, 301], "at": 1000.0 - 1801}]
    guard.tick(paused, 60, lambda *_: None, HOME, 501)
    assert any(line.startswith("ALARM: test tree 300 paused for 30 min") and "kill -CONT 300 301" in line for line in guard.logs)
    guard.logs.clear()
    guard.state[0]["at"] = 1000.0 - 60
    guard.tick(paused, 60, lambda *_: None, HOME, 501)
    assert not any("ALARM" in line for line in guard.logs)
