"""The alarm view's two tolerant readers: a timestamp it cannot render and an alarm state it cannot read are shown as
empty, never as a crash of `sm alarm list`."""

from __future__ import annotations

from types import SimpleNamespace

from steuermaschine_heinzel.native import alarm


def test_an_unreadable_timestamp_renders_empty():
    assert alarm.iso(None) == "" and alarm.iso("not a time") == "" and alarm.iso(1e30) == ""
    assert alarm.iso(1_700_000_000).startswith("2023-11-1")


def test_an_unreadable_alarm_state_counts_as_no_alarms(tmp_path, monkeypatch):
    def broken(_state_dir):
        raise ValueError("not json")

    monkeypatch.setattr(alarm.legacy, "alarm_state", broken)
    assert alarm.recorded(SimpleNamespace(state_dir=tmp_path, acks=set())) == {}


def test_an_acknowledged_alarm_is_marked_acknowledged(tmp_path, monkeypatch):
    monkeypatch.setattr(alarm.legacy, "alarm_state", lambda _d: {"a": {"cls": "ALERT"}, "b": {"cls": "WARN"}, "c": "odd"})
    got = alarm.recorded(SimpleNamespace(state_dir=tmp_path, acks={"a", "c"}))
    assert got["a"]["acked"] is True and "acked" not in got["b"] and got["c"] == "odd"
