"""b5vp: the validation window drops waiters whose command is gone by itself, and `sm ci window drop HEAD` takes a
waiting head out of the line (anyone for a dead waiter, only its owner for a live one)."""

import json
import socket
from pathlib import Path

import pytest
from test_native_dispatch import Commands, lanes, remote, rt  # noqa: F401 - fixtures

from steuermaschine.testing import invoke
from steuermaschine_heinzel.native import window

HERE = socket.gethostname()


@pytest.fixture
def alive(monkeypatch):
    """Which pids count as running: the rest are gone."""
    running: set[int] = set()
    monkeypatch.setattr(window, "liveness", lambda turn: None if turn.host != HERE else turn.pid in running)
    return running


@pytest.fixture
def stopped(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(window, "stop_process", calls.append)
    return calls


@pytest.fixture
def starts(monkeypatch):
    """The start time of each pid, as `ps -o lstart=` reports it."""
    started: dict[int, str] = {}
    monkeypatch.setattr(window, "process_start", lambda _rt, pid: started.get(pid, ""))
    return started


def test_a_dead_waiter_leaves_the_line_by_itself(rt, alive):  # noqa: F811
    runtime = rt()
    w = window.the_window(runtime)
    alive.update({1, 3})
    assert w.enter("fix/holder", "1" * 40, 1, 0, owner="h", pid=1, host=HERE).state == "go"
    assert w.enter("fix/dead", "2" * 40, 1, 0, owner="d", pid=2, host=HERE).position == 1
    assert w.enter("fix/far", "4" * 40, 1, 0, owner="f", pid=4, host="elsewhere").state == "wait"
    assert [turn.branch for turn in w.status().waiting] == ["fix/far"]  # pid 2 is gone; elsewhere cannot be checked
    third = w.enter("fix/next", "3" * 40, 1, 0, owner="n", pid=3, host=HERE)
    assert third.position == 2 and "fix/dead" not in json.dumps(runtime.providers.store.load("heinzel.validation.window", "state"))


def test_drop_takes_a_dead_waiter_for_anyone_and_refuses_the_holder_and_unknown_heads(rt, alive, stopped):  # noqa: F811
    runtime = rt()
    w = window.the_window(runtime)
    alive.update({1})
    w.enter("fix/holder", "1" * 40, 1, 0, owner="h", pid=1, host=HERE)
    w.enter("fix/dead", "2" * 40, 1, 0, owner="d", pid=2, host=HERE)
    holder = invoke(runtime, ["ci", "window", "drop", "fix/holder"])
    assert holder.exit_code == 1 and "holds the window, it does not wait" in holder.stderr and "sm ci window release" in holder.stderr
    assert invoke(runtime, ["ci", "window", "drop", "fix/none"]).exit_code == 1
    assert invoke(runtime, ["ci", "window", "drop", "222"]).exit_code == 1  # a prefix needs 7 characters
    w.enter("fix/far", "4" * 40, 1, 0, owner="f", pid=4, host="elsewhere")
    far = invoke(runtime, ["ci", "window", "drop", "fix/far"])
    assert far.exit_code == 1 and "waits from another machine" in far.stderr


def test_a_live_waiter_is_dropped_only_by_its_owner_and_its_command_is_stopped(rt, alive, stopped, starts):  # noqa: F811
    runtime = rt()  # the fake worktree is at aaaa... on feat/x
    w = window.the_window(runtime)
    alive.update({1, 7})
    starts[7] = "Sat Nov 14 09:00:00 2026"
    w.enter("fix/holder", "1" * 40, 1, 0, owner="h", pid=1, host=HERE)
    w.enter("feat/x", "a" * 40, 2, 0, owner="mine", pid=7, host=HERE)
    window.remember_waiter(runtime, "mine", str(Path(".").resolve()), 7)
    stranger = invoke(runtime, ["ci", "window", "drop", "aaaaaaa"])
    assert stranger.exit_code == 1 and "only its owner drops it" in stranger.stderr and stopped == []
    runtime.providers.vcs.h = "b" * 40
    other = invoke(runtime, ["ci", "window", "drop", "feat/x", "--worktree", "."])
    assert other.exit_code == 1 and "that worktree is at bbbbbbbbb" in other.stderr
    runtime.providers.vcs.h = "a" * 40
    done = invoke(runtime, ["--format", "json", "ci", "window", "drop", "aaaaaaa", "--worktree", "."])
    doc = window.WindowDrop.model_validate(json.loads(done.stdout))
    assert done.exit_code == 0 and stopped == [7] and [turn.branch for turn in doc.dropped] == ["feat/x"]
    assert w.status().waiting == []
    text = window.WindowDrop(dropped=doc.dropped, stopped=[7]).render()
    assert text == ["dropped feat/x aaaaaaaaa from the line (stopped its command, pid 7)"]


def test_a_waiter_that_ended_before_the_stop_is_still_dropped(rt, alive, monkeypatch, starts):  # noqa: F811
    runtime = rt()
    w = window.the_window(runtime)
    alive.update({1, 7})
    starts[7] = "Sat Nov 14 09:00:00 2026"
    w.enter("fix/holder", "1" * 40, 1, 0, owner="h", pid=1, host=HERE)
    w.enter("feat/x", "a" * 40, 2, 0, owner="mine", pid=7, host=HERE)
    window.remember_waiter(runtime, "mine", str(Path(".").resolve()), 7)

    def gone(pid):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(window, "stop_process", gone)
    done = invoke(runtime, ["ci", "window", "drop", "feat/x", "--worktree", "."])
    assert done.exit_code == 0 and "pid 7 ended before the stop" in done.stdout and w.status().waiting == []


def test_a_worktree_at_the_same_head_or_a_reused_pid_is_not_ownership(rt, alive, stopped, starts, tmp_path):  # noqa: F811
    """S6 (the reviewer's probe): `git worktree add` at the same SHA proves nothing, and a reused pid is never stopped."""
    runtime = rt()
    w = window.the_window(runtime)
    alive.update({1, 7})
    starts[7] = "Sat Nov 14 09:00:00 2026"
    w.enter("fix/holder", "1" * 40, 1, 0, owner="h", pid=1, host=HERE)
    w.enter("feat/x", "a" * 40, 2, 0, owner="mine", pid=7, host=HERE)
    unrecorded = invoke(runtime, ["ci", "window", "drop", "feat/x", "--worktree", "."])
    assert unrecorded.exit_code == 1 and "an unrecorded worktree" in unrecorded.stderr and stopped == []
    window.remember_waiter(runtime, "mine", str(tmp_path / "the-owners-worktree"), 7)
    copy = invoke(runtime, ["ci", "window", "drop", "feat/x", "--worktree", "."])  # another worktree, same head
    assert copy.exit_code == 1 and "a worktree at the same head is not ownership" in copy.stderr and stopped == []
    window.remember_waiter(runtime, "mine", str(Path(".").resolve()), 7)
    starts[7] = "Sat Nov 14 11:30:00 2026"  # the waiter died and pid 7 is another process now
    reused = invoke(runtime, ["ci", "window", "drop", "feat/x", "--worktree", "."])
    assert reused.exit_code == 1 and "the pid may be reused" in reused.stderr and stopped == []
    assert [turn.branch for turn in w.status().waiting] == ["feat/x"]


def test_validate_records_the_worktree_and_start_time_of_its_turn(rt, starts):  # noqa: F811
    runtime = rt()
    starts[42] = "Sat Nov 14 09:00:00 2026"
    window.remember_waiter(runtime, "tok", "/w", 42)
    assert runtime.providers.store.load(window.WAITER_KIND, "tok") == {"worktree": "/w", "pid": 42, "started": "Sat Nov 14 09:00:00 2026"}


def test_process_start_asks_ps_through_the_runner():
    from types import SimpleNamespace

    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(rc=0 if argv[-1] == "7" else 1, out="Sat Nov 14 09:00:00 2026\n" if argv[-1] == "7" else "", err="")

    fake = SimpleNamespace(providers=SimpleNamespace(commands=SimpleNamespace(run=run)))
    assert window.process_start(fake, 7) == "Sat Nov 14 09:00:00 2026" and window.process_start(fake, 8) == ""
    assert calls[0] == ["ps", "-o", "lstart=", "-p", "7"]


def test_drop_is_a_write_command_that_reaches_the_files_of_head_validate():
    from steuermaschine.reviewgraph import reach

    cmd = window.ci_window_drop.__sm_native__
    drop, validate = reach([cmd, window.head_validate.__sm_native__])
    assert cmd.writes and drop.files == validate.files


def test_the_real_liveness_is_the_cores(monkeypatch):
    from steuermaschine.window import Turn

    assert window.liveness(Turn(branch="b", head="h", runs=1, requested=0, asked=0, pid=1, host="elsewhere")) is None


def test_stop_process_signals_the_pid():
    with pytest.raises(ProcessLookupError):
        window.stop_process(999_999_999)  # no such process: the caller notes it and still drops the turn
