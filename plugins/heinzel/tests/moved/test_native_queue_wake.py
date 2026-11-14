"""Idle wake tests use throwaway HOME and a virtual clock; no live queue, hold or network calls."""

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from steuermaschine_heinzel import state as statedir


@pytest.fixture
def idle(tool, home, monkeypatch):
    queue = tool("hz-queue")
    monkeypatch.setenv("HZ_QUEUE_STATE", str(home / ".cache/queue.json"))
    clock = {"now": 0.0, "sleeps": [], "event": lambda: None}
    monkeypatch.setattr(queue.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(queue, "tools_changed_at", lambda: 42.0)

    def sleep(seconds):
        assert 0 < seconds <= 1.0
        clock["sleeps"].append(seconds)
        clock["now"] += seconds
        clock["event"]()

    monkeypatch.setattr(queue.hzlib, "sleep", sleep)
    return queue, clock


def test_unchanged_idle_wait_keeps_remote_interval_without_spinning(idle, monkeypatch):
    queue, clock = idle
    monkeypatch.setattr(queue.hzlib, "gh", lambda *_: pytest.fail("idle wait called GitHub"))
    assert queue.wait_for_tick(300, 42) == ""
    assert clock["now"] == 300
    assert clock["sleeps"] == [1.0] * 300


@pytest.mark.parametrize("interval", [0, 0.25, 1.25])
def test_wait_respects_short_or_expired_interval(idle, interval):
    queue, clock = idle
    assert queue.wait_for_tick(interval, 42) == ""
    assert clock["now"] == max(interval, 0)
    assert len(clock["sleeps"]) <= 2


@pytest.mark.parametrize("event", ["create", "replace", "delete"])
def test_queue_file_events_wake_idle_runner_within_one_local_poll(idle, event):
    queue, clock = idle
    path = queue.state_path()
    if event != "create":
        path.write_text("{}")
    original = path.read_bytes() if path.exists() else None

    def change():
        if event == "delete":
            path.unlink()
        else:
            replacement = path.with_suffix(".tmp")
            replacement.write_text('{"items":[]}')
            replacement.replace(path)

    clock["event"] = change
    assert queue.wait_for_tick(300, 42) == "queue changed"
    assert clock["now"] == 1
    if path.exists():
        assert path.read_bytes() != original
    else:
        assert event == "delete"


def test_release_of_last_hold_wakes_without_removing_other_holds(idle):
    queue, clock = idle
    legacy = statedir.root() / "merge-freeze"
    legacy.touch()
    holds = queue.hzlib.release_holds()
    holds.mkdir()
    release = holds / "owned-release"
    release.write_text("hold")

    def change():
        if clock["now"] == 1:
            release.unlink()
        elif clock["now"] == 3:
            legacy.unlink()

    clock["event"] = change
    assert queue.wait_for_tick(300, 42) == "merge hold released"
    assert clock["now"] == 3  # releasing one hold cannot clear an independent incident hold
    assert holds.is_dir()


def test_hold_created_and_released_during_idle_is_detected(idle):
    queue, clock = idle
    hold = statedir.root() / "merge-freeze"

    def change():
        if clock["now"] == 1:
            hold.touch()
        elif clock["now"] == 2:
            hold.unlink()

    clock["event"] = change
    assert queue.wait_for_tick(300, 42) == "merge hold released"
    assert clock["now"] == 2


def test_malformed_hold_directory_stays_frozen_and_is_not_changed(idle):
    queue, clock = idle
    holds = queue.hzlib.release_holds()
    holds.write_text("malformed")
    assert queue.wait_for_tick(2, 42) == ""
    assert queue.hzlib.merge_frozen()
    assert holds.read_text() == "malformed"


def test_tool_edit_wakes_and_main_reexecs_before_a_second_tick(idle, monkeypatch):
    queue, clock = idle
    edited = {"value": False}
    clock["event"] = lambda: edited.update(value=True)
    monkeypatch.setattr(queue, "tools_changed_at", lambda: 43.0 if edited["value"] else 42.0)
    ticks, logs, execs = [], [], []
    monkeypatch.setattr(queue, "tick", lambda: ticks.append(True))
    monkeypatch.setattr(queue, "log", logs.append)

    class Reexec(Exception):
        pass

    def execv(*args):
        execs.append(args)
        raise Reexec

    monkeypatch.setattr(queue.os, "execv", execv)
    with pytest.raises(Reexec):
        queue.main(["run"])
    assert ticks == [True]
    assert "idle wake: tools changed" in logs
    assert execs == [(sys.executable, [sys.executable, *sys.argv])]
    assert "tick started" in logs and "tick finished in 0.0s" in logs


def test_once_logs_failed_tick_duration_without_waiting(idle, monkeypatch):
    queue, clock = idle
    logs = []
    monkeypatch.setattr(queue, "log", logs.append)

    def fail():
        clock["now"] += 7.5
        raise RuntimeError("test failure")

    monkeypatch.setattr(queue, "tick", fail)
    assert queue.main(["run", "--once"]) == 0
    assert "tick failed: RuntimeError('test failure')" in logs
    assert "tick finished in 7.5s" in logs
    assert clock["sleeps"] == []


def test_idle_wait_remains_terminable_by_launchd_signal(tool, home):
    tools = tool.__globals__["TOOLS"]
    script = (
        "import sys; "
        f"sys.path[:0] = {[p for p in sys.path if p]!r}; "
        "from steuermaschine_heinzel.native.queue_lib import edge; "
        f"edge.configure({str(home / 'Code' / 'heinzel-management')!r}); "  # queue_records_dir has no default
        "from steuermaschine_heinzel.native.queue_lib import queue_daemon as q; "
        "print('ready', flush=True); q.wait_for_tick(300, q.tools_changed_at())"
    )
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True,
                             env={**os.environ, "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        assert child.stdout.readline().strip() == "ready"
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=2) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_wake_rechecks_batch_cadence_and_keeps_runner_lock(idle, monkeypatch):
    queue, clock = idle
    hold = statedir.root() / "merge-freeze"
    hold.touch()
    reasons = []

    class EndTest(Exception):
        pass

    def tick():
        assert queue.acquire_lock(block=False) is None
        reasons.append(queue.gate([{"repo": queue.hzlib.CORE}], 10_000 + clock["now"], {"last_batch": 10_000}))

    def change():
        if clock["now"] == 1:
            hold.unlink()
        else:
            raise EndTest

    clock["event"] = change
    monkeypatch.setattr(queue, "tick", tick)
    monkeypatch.setattr(queue, "log", lambda _: None)
    monkeypatch.setattr(queue, "main_state", lambda: pytest.fail("cadence was bypassed"))
    with pytest.raises(EndTest):
        queue.main(["run"])
    assert reasons == ["merge freeze is on", "last batch 0 min ago, one per 5 minutes"]


def test_negative_interval_is_rejected_before_lock_or_remote_work(idle, monkeypatch, capsys):
    queue, _clock = idle
    monkeypatch.setattr(queue, "tick", lambda: pytest.fail("invalid interval ran a tick"))
    monkeypatch.setattr(queue, "acquire_lock", lambda **_: pytest.fail("invalid interval acquired a lock"))
    with pytest.raises(SystemExit) as error:
        queue.main(["run", "--interval", "-1"])
    assert error.value.code == 2
    assert "--interval must be non-negative" in capsys.readouterr().err
