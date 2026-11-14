"""The queue's state file: one lock around every load-change-save, and a broken file is never an empty queue
(review F1, 2026-11-13). Unit tests on the moved module with a state file in a throwaway HOME."""

import json
import threading

import pytest

from steuermaschine_heinzel.native.queue_lib import queue_daemon as qd

RAISED: list[tuple[str, str, str]] = []


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".cache").mkdir()
    path = tmp_path / ".cache" / "queue.json"
    monkeypatch.setenv("HZ_QUEUE_STATE", str(path))
    monkeypatch.setattr(qd.edge, "raise_alarm", lambda key, level, text, source="queue": RAISED.append((key, level, text)))
    RAISED.clear()
    return path


def row(key):
    return {"key": key, "repo": qd.hzlib.CORE, "pr": key, "after": [], "status": "queued", "detail": ""}


def test_a_missing_state_file_is_an_empty_queue(state):
    assert qd.load()["items"] == []
    assert RAISED == []


@pytest.mark.parametrize("text", ["{not json", "", "[1, 2]", '{"items": "x"}'])
def test_a_corrupt_state_file_refuses_and_raises_the_alarm_and_is_never_an_empty_queue(state, text):
    state.write_text(text)
    with pytest.raises(qd.StateUnreadable, match="unreadable"):
        qd.load()
    assert [(k, level) for k, level, _ in RAISED] == [("queue-state", "ALERT")]


def test_an_unreadable_state_file_refuses(state):
    state.mkdir()  # reading a directory is an OSError other than FileNotFoundError
    with pytest.raises(qd.StateUnreadable):
        qd.load()


def test_an_update_never_overwrites_a_corrupt_file_with_an_empty_queue(state):
    state.write_text("{broken")
    with pytest.raises(qd.StateUnreadable):
        qd.update(blocked="x")
    assert state.read_text() == "{broken"


def test_every_command_refuses_with_exit_1_on_a_corrupt_file_and_changes_nothing(state, capsys):
    state.write_text("{broken")
    for argv in (["list"], ["drop", "5"], ["why"]):
        assert qd.main(argv) == 1
        assert "REFUSED" in capsys.readouterr().err
    assert state.read_text() == "{broken"


def test_save_uses_a_temp_name_of_its_own_process(state, monkeypatch):
    seen = []
    real = qd.Path.replace
    monkeypatch.setattr(qd.Path, "replace", lambda self, target: (seen.append(self.name), real(self, target))[1])
    qd.save({"items": []})
    assert seen == [f"{state.name}.{qd.os.getpid()}.tmp"]


def test_two_writers_never_lose_an_update(state, monkeypatch):
    """Writer A loads, then B tries to change the file while A holds it: B waits, so both changes survive."""
    qd.save({"items": [row("1"), row("2")]})
    a_loaded, b_done = threading.Event(), threading.Event()
    real_load = qd.load

    def slow_load():
        data = real_load()
        if threading.current_thread().name == "A":
            a_loaded.set()
            assert not b_done.wait(0.3)  # B is held by the lock while A is between load and save
        return data

    monkeypatch.setattr(qd, "load", slow_load)
    a = threading.Thread(target=lambda: qd.update("1", status="merged"), name="A")
    a.start()
    a_loaded.wait(2)
    b = threading.Thread(target=lambda: (qd.update("2", status="needs work"), b_done.set()), name="B")
    b.start()
    a.join(5)
    b.join(5)
    items = {i["key"]: i["status"] for i in json.loads(state.read_text())["items"]}
    assert items == {"1": "merged", "2": "needs work"}


def test_the_state_lock_is_reentrant_within_one_process(state):
    qd.save({"items": [row("1")]})
    with qd.state_lock():
        qd.update("1", status="merged")  # update takes the same lock again: no deadlock
    assert json.loads(state.read_text())["items"][0]["status"] == "merged"
