"""The agent load governor: the former tool's tests moved onto the library, and `sm mini governor` (read) and
`sm daemon governor` (write) through the command tree on fakes.

Changed in the moved tests: `digest_line` takes the state dict instead of a path (the state lives in the Store now), the
two tests that sent a real signal to a vanished pid are not here (signals go through `kill` on the Commands provider; the
command test below asserts a failing `kill` is ignored), and `NEVER` also names the new commands (extra cases).
"""

import json
import os
import signal

import guards_runtime as g
import pytest

from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, invoke
from steuermaschine_heinzel.native import guards
from steuermaschine_heinzel.native.guards_lib import governor as gv

U = os.getuid()


def row(pid, cpu, args, uid=None):
    return {"pid": pid, "ppid": 1, "cpu": cpu, "uid": U if uid is None else uid, "args": args}


TABLE = [
    row(10, 90, "/usr/bin/python -m pytest /Users/<user>/Code/hz-abc/tests"),
    row(11, 50, "node /Users/<user>/Code/heinzel-x/node_modules/.bin/vitest"),
    row(12, 99, "/Users/<user>/.venv/bin/python -m heinzel.web io.github.helmut-hoffer-von-ankershoffen.heinzel.web /Users/<user>/Code/hz-abc"),
    row(13, 80, "/Applications/Claude.app/Contents/MacOS/Claude /Code/hz-q"),
    row(14, 70, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    row(15, 60, "python /Users/<user>/Code/heinzel-management/tools/hz-governor run"),
    row(16, 55, "/usr/bin/python -m pytest /Users/<user>/Code/hz-abc", uid=U + 1),
    row(17, 40, "node server.js"),
    row(18, 0.1, "pytest /Users/<user>/Code/hz-idle"),
]


@pytest.fixture(autouse=True)
def thresholds():
    gv.bind(45.0, 25.0)


def run(load, st, now=1000, dry=False, off=False, cwd=lambda p: "/Users/<user>/Code/hz-w" if p == 17 else ""):
    sent = []
    acts = gv.step(load, st, TABLE, lambda p, s: sent.append((p, s)), now, dry=dry, cwd=cwd, off=off, logf=lambda m: None)
    return acts, sent


def fresh():
    return {"stopped": {}, "last_resume": 0}


def test_stops_descending_cpu_and_skips_protected():
    st = fresh()
    acts, sent = run(60, st, me=None) if False else run(60, st)
    assert [p for _, p in acts] == [10, 11]  # 17 (node server.js in a builder worktree) is no test runner: never stopped
    assert all(s == signal.SIGSTOP for _, s in sent)
    assert set(st["stopped"]) == {"10", "11"}


def test_below_high_no_action_and_dry_run_signals_nothing():
    st = fresh()
    assert run(30, st)[0] == []
    acts, sent = run(60, st, dry=True)
    assert acts and sent == [] and st["stopped"] == {}


def test_resume_six_at_a_time_every_20s():
    st = {"stopped": {str(i): {"t": i, "args": "x"} for i in range(100, 110)}, "last_resume": 0}
    rows = [row(i, 1, "x") for i in range(100, 110)]
    sent = []
    k = lambda p, s: sent.append((p, s))  # noqa: E731
    a = gv.step(10, st, rows, k, 1000, logf=lambda m: None)
    assert [p for _, p in a] == list(range(100, 106)) and len(st["stopped"]) == 4
    assert gv.step(10, st, rows, k, 1010, logf=lambda m: None) == []  # too soon
    assert len(gv.step(10, st, rows, k, 1021, logf=lambda m: None)) == 4


def test_mid_load_holds():
    st = {"stopped": {"10": {"t": 1, "args": TABLE[0]["args"]}}, "last_resume": 0}  # the same process (bead ltui: "x" would now be continued)
    assert gv.step(35, st, TABLE, lambda p, s: None, 1000, logf=lambda m: None) == [] and "10" in st["stopped"]


def test_kill_switch_resumes_all_and_dead_pids_dropped():
    st = {"stopped": {"10": {"t": 1, "args": TABLE[0]["args"]}, "999": {"t": 2, "args": "dead"}}, "last_resume": 0}
    acts, _sent = run(90, st, off=True)
    assert acts == [("CONT", 10)] and st["stopped"] == {}


def test_digest_line():
    assert gv.digest_line({"stopped": {"1": {"t": 1, "args": "x"}}, "last_resume": 0}, load=50) == "GOV: stopped 1 pids, load 50"
    assert gv.digest_line({"stopped": {}, "last_resume": 0}, load=5) == ""


@pytest.mark.parametrize("args", [
    "python /Users/<user>/Code/heinzel-management/tools/hz-queue run",
    "python /Users/<user>/Code/hz-x/tools/hz-train 0.9.32",
    "/bin/sh /Users/<user>/Code/hz-x/tools/hz-release run",
    "/Users/<user>/.local/share/uv/tools/heinzel/bin/python -m heinzel",
    "uv tool install heinzel /Users/<user>/Code/hz-x/uv/tools/heinzel/x",
    "node /Users/<user>/Code/hz-x/server --port 8737",
    "/Users/<user>/.local/bin/sm daemon queue --loop 30",
    "python /Users/<user>/.local/bin/sm release cut /Users/<user>/Code/hz-x",
    "python /Users/<user>/.local/bin/sm gate run -- pytest /Users/<user>/Code/hz-x",
])
def test_never_pause_list(args):
    assert not gv.eligible(row(500, 99, args), me=0, cwd=lambda p: "/Users/<user>/Code/hz-w")
    assert gv.NEVER.search(args)


def test_the_parsers_of_the_process_table_and_cwd():
    assert gv.ps_table("  10  1  3.5  501 /bin/x --y\nnoise\n 11 x 1 2 3\n") == [{"pid": 10, "ppid": 1, "cpu": 3.5, "uid": 501, "args": "/bin/x --y"}]
    assert gv.cwd_of("p123\nfcwd\nn/Users/x/Code/hz-w\n") == "/Users/x/Code/hz-w" and gv.cwd_of("") == ""


# --- the commands --------------------------------------------------------------------------------------------------------

def ps_text(rows):
    return "".join(f"{r['pid']} {r['ppid']} {r['cpu']} {r['uid']} {r['args']}\n" for r in rows)


@pytest.fixture
def cmd(monkeypatch, tmp_path):
    conf = g.register(monkeypatch, {"guards_governor_off_file": str(tmp_path / "governor.off")})
    load = {"value": 60.0}
    monkeypatch.setattr(guards, "load_now", lambda: load["value"])

    def build(reviewed=True, ps=None):
        commands = FakeCommands()
        commands.on(["ps"], ps or Result(0, ps_text(TABLE), ""))
        commands.on(["lsof"], Result(0, "p17\nfcwd\nn/Users/<user>/Code/hz-w\n", ""))
        commands.on(["kill"], Result(1, "", "kill: no such process"))  # a vanished pid: ignored
        return g.runtime_of(tmp_path, reviewed, commands=commands), commands

    return build, load, conf


def test_the_governor_tick_is_off_until_its_review_is_approved(cmd, monkeypatch):
    from steuermaschine.native import NativeCommand

    monkeypatch.setattr(NativeCommand, "review_state", lambda self: ("open", f"reviews/{self.review_file} has no verdict (test)"))  # whatever the shipped review files say
    runtime, commands = cmd[0](reviewed=False)
    off = invoke(runtime, ["daemon", "governor"])
    assert off.exit_code == 70 and "daemon-governor.md" in off.stderr and commands.calls == []


def test_one_governor_tick_makes_exactly_these_calls_in_this_order(cmd):
    build, _load, _ = cmd
    runtime, commands = build()
    done = invoke(runtime, ["daemon", "governor"])
    assert done.exit_code == 0 and done.stdout.splitlines()[-1] == "STOP:10 STOP:11"
    assert commands.calls == [["ps", "-axo", "pid=,ppid=,pcpu=,uid=,args="], ["kill", "-STOP", "10"], ["kill", "-STOP", "11"]]
    assert set(runtime.providers.store.load("heinzel.guards.governor", "state")["stopped"]) == {"10", "11"}
    status = invoke(runtime, ["--format", "json", "mini", "governor"])
    doc = guards.GovernorStatus.model_validate(json.loads(status.stdout))
    assert [p.pid for p in doc.stopped] == [10, 11] and doc.high == 45.0 and doc.low == 25.0 and doc.off is False
    text = invoke(runtime, ["mini", "governor"]).stdout.splitlines()
    assert text[0] == "load 60.0 high=45 low=25 off=False stopped=2" and text[1].startswith("  10 /usr/bin/python")
    assert guards.governor_digest(runtime.providers.store, 60.0) == "GOV: stopped 2 pids, load 60"
    assert "ALARM 2 held > 30 min" in guards.governor_digest(runtime.providers.store, 60.0, now=runtime.providers.clock.now() + 3600)


def test_the_kill_switch_file_continues_everything_and_dry_run_records_nothing(cmd, tmp_path):
    build, _load, _ = cmd
    runtime, commands = build()
    runtime.providers.store.save("heinzel.guards.governor", "state", {"stopped": {"10": {"t": 1, "args": TABLE[0]["args"]}}, "last_resume": 0})
    (tmp_path / "governor.off").write_text("")
    dry = invoke(runtime, ["--format", "json", "daemon", "governor", "--dry-run"])
    tick = guards.GovernorTick.model_validate(json.loads(dry.stdout))
    assert tick.dry_run and tick.actions == ["CONT:10"] and not [c for c in commands.calls if c[0] == "kill"]
    assert runtime.providers.store.load("heinzel.guards.governor", "state")["stopped"] == {"10": {"t": 1, "args": TABLE[0]["args"]}}
    done = invoke(runtime, ["daemon", "governor"])
    assert done.stdout.splitlines()[-1] == "CONT:10" and ["kill", "-CONT", "10"] in commands.calls
    assert runtime.providers.store.load("heinzel.guards.governor", "state")["stopped"] == {}
    assert invoke(runtime, ["mini", "governor"]).stdout.startswith("load 60.0 high=45 low=25 off=True stopped=0")


def test_an_unreadable_process_table_signals_nothing(cmd):
    runtime, commands = cmd[0](ps=Result(1, "", "ps: boom"))
    done = invoke(runtime, ["daemon", "governor"])
    assert done.exit_code == 1 and "could not be read" in done.stdout and [c[0] for c in commands.calls] == ["ps"]


def test_a_state_the_former_governor_wrote_is_read(cmd, tmp_path, monkeypatch):
    runtime, _ = cmd[0]()
    old = tmp_path / "legacy"
    old.mkdir()
    (old / "governor.json").write_text(json.dumps({"stopped": {"77": {"t": 1, "args": "old tool"}}, "last_resume": 5}))
    monkeypatch.setitem(runtime.settings, "legacy_state_dir", str(old)) if hasattr(runtime.settings, "__setitem__") else None
    shown = invoke(runtime, ["mini", "governor"]).stdout
    assert "stopped=1" in shown and "77 old tool" in shown
    (old / "governor.json").write_text("broken")
    assert "stopped=0" in invoke(runtime, ["mini", "governor"]).stdout


# --- the allowlist: only test processes agents started (review of daemon-governor.md, finding 1) ----------------------

@pytest.mark.parametrize("args, cwd", [
    ("/bin/sh /Users/<user>/Code/heinzel-management/tools/hz-popup-guard", ""),  # the coordinator's checkout, by args
    ("git -C /Users/<user>/Code/heinzel-management status", "/Users/<user>/Code/heinzel-management"),
    ("/usr/bin/python -m pytest tools/tests/test_x.py", "/Users/<user>/Code/heinzel-management"),  # a test, but the coordinator's
    ("uv run --frozen ruff check src", "/Users/<user>/Code/hz-w"),  # a builder's, but no test runner
    ("git push origin HEAD", "/Users/<user>/Code/heinzel-w"),
    ("make check-fast", "/Users/<user>/Code/heinzel-w"),
    ("node server.js", "/Users/<user>/Code/hz-w"),
])
def test_the_coordinator_and_non_test_processes_are_never_eligible(args, cwd):
    assert not gv.eligible(row(500, 99, args), me=0, cwd=lambda p: cwd)


@pytest.mark.parametrize("args, cwd", [
    ("/usr/bin/python -m pytest tests/unit/x_test.py -n 2", "/Users/<user>/Code/heinzel-w"),
    ("/Users/<user>/Code/hz-w/.venv/bin/python /Users/<user>/Code/hz-w/.venv/bin/pytest tests", ""),
    ("node /Users/<user>/Code/heinzel-w/node_modules/vitest/vitest.mjs run src/a.test.ts", ""),
    ("node /x/node_modules/.bin/playwright test", "/Users/<user>/Code/heinzel-management/.worktrees/b-1"),
    ("python -m coverage run -m pytest t.py", "/Users/<user>/Code/hz-w"),
])
def test_test_runners_of_builder_worktrees_are_eligible(args, cwd):
    assert gv.eligible(row(500, 99, args), me=0, cwd=lambda p: cwd)


def test_the_children_of_a_test_runner_are_stopped_with_it_but_nothing_else_below_a_builder():
    table = [{"pid": 40, "ppid": 1, "cpu": 5.0, "uid": U, "args": "/usr/bin/python -m pytest tests/x_test.py -n 2"},
             {"pid": 41, "ppid": 40, "cpu": 90.0, "uid": U, "args": "/usr/bin/python -c import sys;exec(eval(sys.stdin.readline()))"},
             {"pid": 42, "ppid": 41, "cpu": 30.0, "uid": U, "args": "/bin/sleep 5"},
             {"pid": 50, "ppid": 1, "cpu": 95.0, "uid": U, "args": "/usr/bin/python -c import sys;exec(eval(sys.stdin.readline()))"},
             {"pid": 51, "ppid": 40, "cpu": 20.0, "uid": U, "args": "/Users/<user>/.local/bin/sm daemon queue --loop 30"}]
    picked = gv.pick(table, set(), cwd=lambda p: "/Users/<user>/Code/heinzel-w")
    assert [r["pid"] for r in picked] == [41, 42, 40]  # 50 is no runner's child; 51 is sm (NEVER), even below a runner


def test_a_second_governor_tick_waits_while_one_holds_the_store_lock(cmd):
    runtime, commands = cmd[0]()
    with runtime.providers.store.lock("heinzel.guards.governor"):
        done = invoke(runtime, ["daemon", "governor"])
    assert done.exit_code == 75 and "another governor tick is running" in done.stderr and commands.calls == []


# --- review of daemon-governor.md: findings 3 and 4 --------------------------------------------------------------------

def test_a_recorded_pid_that_now_runs_something_else_is_continued_once_and_dropped():
    st = {"stopped": {"10": {"t": 1, "args": "/usr/bin/python -m pytest /Users/<user>/Code/hz-OTHER/tests", "uid": U}}, "last_resume": 0}
    acts, sent = run(10, st)
    assert acts == [("CONT", 10)] and sent == [(10, signal.SIGCONT)] and st["stopped"] == {}  # never left stopped
    assert run(10, st) == ([], [])  # dropped: not continued again
    st = {"stopped": {"10": {"t": 1, "args": "older entry without a uid"}}, "last_resume": 0}
    assert run(10, st, dry=True) == ([("CONT", 10)], []) and st["stopped"] == {}  # an old entry: the governor's own uid; dry signals nothing
    st = {"stopped": {"10": {"t": 1, "args": TABLE[0]["args"]}}, "last_resume": 0}
    assert run(10, st)[0] == [("CONT", 10)]  # the same process: continued


def test_a_pid_held_stopped_for_more_than_half_an_hour_raises_an_alarm():
    st = {"stopped": {"10": {"t": 1000 - 1900, "args": TABLE[0]["args"]}, "11": {"t": 990, "args": TABLE[1]["args"]}}, "last_resume": 0}
    logs = []
    gv.step(35, st, TABLE, lambda p, s: None, 1000, logf=logs.append)
    assert logs == ["ALARM: pid 10 held stopped for 32 min (load never fell below 25); continue it with the off file or kill -CONT 10"]
    assert gv.digest_line(st, 35, now=1000) == "GOV: stopped 2 pids, load 35; ALARM 1 held > 30 min (longest pid 10, 32 min)"
    assert gv.digest_line(st, 35) == "GOV: stopped 2 pids, load 35"


def test_a_changed_pid_of_another_uid_is_not_signalled_but_raises_an_alarm():
    st = {"stopped": {"16": {"t": 1, "args": "something else", "uid": U}, "999": {"t": 1, "args": "gone", "uid": U}}, "last_resume": 0}
    logs, sent = [], []
    acts = gv.step(10, st, TABLE, lambda p, s: sent.append((p, s)), 1000, logf=logs.append)
    assert acts == [] and sent == [] and st["stopped"] == {}  # 16 now runs as uid U+1; 999 is gone
    assert logs == ["ALARM: pid 16 was stopped by the governor and now runs another uid's process; dropped, not continued"]


def test_a_changed_pid_that_cannot_be_continued_raises_an_alarm_and_a_new_stop_records_the_uid():
    st = {"stopped": {"10": {"t": 1, "args": "other", "uid": U}}, "last_resume": 0}
    logs = []

    def refuse(pid, sig):
        raise PermissionError(pid)

    assert gv.step(10, st, TABLE, refuse, 1000, logf=logs.append) == [("CONT", 10)] and st["stopped"] == {}
    assert logs[-1] == "ALARM: pid 10 could not be continued; check it with ps and kill -CONT 10"
    st = fresh()
    run(50, st)
    assert st["stopped"]["10"] == {"t": 1000, "args": TABLE[0]["args"], "uid": U}
