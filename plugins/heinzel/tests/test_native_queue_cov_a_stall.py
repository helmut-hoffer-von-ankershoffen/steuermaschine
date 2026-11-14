"""queue_lib.stall: the pure stall rules, the alarm state file and the owner push, all on tmp_path and fakes."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from steuermaschine.plugins import PluginError
from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native.queue_lib import stall

NOW = 1_800_000_000.0


def run(name, status="COMPLETED", conclusion="SUCCESS", **more):
    return {"name": name, "status": status, "conclusion": conclusion, **more}


@pytest.fixture
def state(tmp_path, monkeypatch):
    path = tmp_path / "sub" / "alarm.json"
    monkeypatch.setenv("HZ_ALARM_STATE", str(path))
    return path


def test_alarm_squeezes_whitespace_and_stamp_reads_iso():
    assert stall.alarm("WARN", "k", "a  b\n c") == {"cls": "WARN", "key": "k", "text": "a b c"}
    assert stall.stamp("2026-11-13T10:00:00Z") == 1794564000.0
    assert stall.stamp(None) is None and stall.stamp("garbage") is None
    assert stall.minutes(None, NOW) == 0 and stall.minutes(NOW - 600, NOW) == 10 and stall.minutes(NOW + 600, NOW) == 0


def test_check_names_and_results():
    assert stall.check_name({"context": "ctx"}) == "ctx" and stall.check_name({}) == "?"
    assert stall.check_result({"state": "SUCCESS"}) == "SUCCESS"
    assert stall.check_result({"state": "EXPECTED"}) == "PENDING"
    assert stall.check_result({"state": "FAILURE"}) == "FAILURE"
    assert stall.check_result({"state": ""}) == "PENDING"
    assert stall.check_result(run("a", status="IN_PROGRESS")) == "PENDING"
    assert stall.check_result(run("a", conclusion=None)) == "PENDING"
    assert stall.check_result(run("a", conclusion="FAILURE")) == "FAILURE"


def test_green_and_green_since():
    assert not stall.green([]) and stall.green([run("a"), run("b", conclusion="SKIPPED")])
    assert not stall.green([run("a"), run("b", conclusion="FAILURE")])
    assert stall.green_since([]) is None and stall.green_since([run("a")]) is None
    rows = [run("a", completedAt="2026-11-13T10:00:00Z"), run("b", completedAt="2026-11-13T10:05:00Z")]
    assert stall.green_since(rows) == stall.stamp("2026-11-13T10:05:00Z")


def test_required_missing_and_not_green():
    rows = [run("CI OK"), run("merge-gate", conclusion="FAILURE")]
    assert stall.required_missing(stall.REQUIRED, rows) == ["coverage-floor"]
    assert stall.required_not_green(stall.REQUIRED, rows) == ["merge-gate=FAILURE"]


def test_wait_reason_three_cases_and_not_blocked():
    since = NOW - 1200
    ok = [run("CI OK"), run("merge-gate"), run("coverage-floor")]
    assert stall.wait_reason("CLEAN", ok, stall.REQUIRED, since, NOW) == ""
    assert stall.wait_reason("BLOCKED", [run("x", conclusion="FAILURE")], stall.REQUIRED, since, NOW) == ""
    assert "missing from the PR rollup: merge-gate, coverage-floor" in stall.wait_reason("BLOCKED", [run("CI OK")], stall.REQUIRED, since, NOW)
    weak = stall.wait_reason("BLOCKED", [run("CI OK")], ("CI OK",), since, NOW, everything=[run("CI OK", conclusion="NEUTRAL")])
    assert "present but not SUCCESS: CI OK=NEUTRAL" in weak and "20 min" in weak
    assert "every required context is green" in stall.wait_reason("BLOCKED", ok, stall.REQUIRED, since, NOW)


def test_pending_reason():
    many = [run(f"c{i}", status="IN_PROGRESS") for i in range(7)]
    assert stall.pending_reason("BLOCKED", many) == "checks pending: c0, c1, c2, c3, c4 (+2)"
    assert stall.pending_reason("X", [run("b", status="QUEUED"), run("a", status="QUEUED")]) == "checks pending: a, b"
    assert stall.pending_reason("BLOCKED", []) == "no checks reported on the head yet"
    assert stall.pending_reason(None, [run("a")]) == "GitHub mergeStateStatus unknown"


def test_stall_alarm_branches():
    ok = [run("CI OK"), run("merge-gate"), run("coverage-floor")]
    old = NOW - 3600
    assert stall.stall_alarm("5", "BLOCKED", [run("a", conclusion="FAILURE")], stall.REQUIRED, old, NOW) is None
    assert stall.stall_alarm("5", "BLOCKED", ok, stall.REQUIRED, NOW - 60, NOW) is None
    blocked = stall.stall_alarm("5", "BLOCKED", ok, stall.REQUIRED, old, NOW)
    assert blocked["cls"] == "ALERT" and blocked["key"] == "stall:5" and blocked["text"].startswith("STALL #5 BLOCKED")
    warn = stall.stall_alarm("abc", "CLEAN", ok, stall.REQUIRED, old, NOW, "merge freeze on")
    assert warn["cls"] == "WARN" and warn["text"].startswith("STALL abc green") and "merge freeze on" in warn["text"]
    alert = stall.stall_alarm("7", None, ok, stall.REQUIRED, old, NOW, "something odd")
    assert alert["cls"] == "ALERT" and "state unknown" in alert["text"] and "`something odd`" in alert["text"]
    assert "no block" in stall.stall_alarm("7", "CLEAN", ok, stall.REQUIRED, old, NOW)["text"]


def test_state_load_save_roundtrip_and_bad_files(state):
    assert stall.load() == {}
    stall.save({"a": {"x": 1}})
    assert stall.load() == {"a": {"x": 1}}
    state.write_text("[1]")
    assert stall.load() == {}
    state.write_text("{nope")
    assert stall.load() == {}


def test_state_path_default(monkeypatch, tmp_path):
    monkeypatch.delenv("HZ_ALARM_STATE", raising=False)
    monkeypatch.setattr(stall.Path, "home", classmethod(lambda cls: tmp_path))
    assert stall.state_path() == statedir.root() / "alarm.json"


def test_locked_runs_body_and_survives_unwritable_directory(state, tmp_path):
    with stall.locked():
        pass
    assert state.with_suffix(".lock").exists()
    blocker = tmp_path / "file"
    blocker.write_text("x")
    import os
    os.environ["HZ_ALARM_STATE"] = str(blocker / "child" / "a.json")
    try:
        ran = []
        with stall.locked():
            ran.append(1)
        assert ran == [1]
    finally:
        os.environ["HZ_ALARM_STATE"] = str(state)


def test_quiet_hours(monkeypatch):
    assert stall.quiet_hours(1_800_000_000 - (1_800_000_000 % 86400) + 0) in (True, False)
    # 2026-11-13 22:30 UTC is 23:30 in Berlin; 12:00 UTC is 13:00 in Berlin; 05:30 UTC is 06:30 in Berlin
    base = stall.stamp("2026-11-13T00:00:00Z")
    assert stall.quiet_hours(base + 22.5 * 3600) is True
    assert stall.quiet_hours(base + 12 * 3600) is False
    assert stall.quiet_hours(base + 5.5 * 3600) is True


def test_quiet_hours_without_tz_database(monkeypatch):
    import zoneinfo

    def boom(name):
        raise zoneinfo.ZoneInfoNotFoundError(name)

    monkeypatch.setattr(zoneinfo, "ZoneInfo", boom)
    monkeypatch.setattr(stall.time, "localtime", lambda now: SimpleNamespace(tm_hour=3))
    assert stall.quiet_hours(NOW) is True
    monkeypatch.setattr(stall.time, "localtime", lambda now: SimpleNamespace(tm_hour=12))
    assert stall.quiet_hours(NOW) is False


def test_notifier_choices(monkeypatch, tmp_path):
    monkeypatch.setattr(stall.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("HZ_ALARM_NOTIFY", raising=False)
    monkeypatch.delenv("HZ_TRAIN_NOTIFY", raising=False)
    assert stall.notifier() == ""
    script = tmp_path / ".claude/plugins/synced/x/telegram-bridge/skills/telegram-notify/scripts/send.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")
    assert stall.notifier() == str(script)
    monkeypatch.setenv("HZ_TRAIN_NOTIFY", "/train")
    assert stall.notifier() == "/train"
    monkeypatch.setenv("HZ_ALARM_NOTIFY", "/alarm")
    assert stall.notifier() == "/alarm"
    monkeypatch.setenv("HZ_ALARM_NOTIFY", "off")
    assert stall.notifier() == ""


def test_push_paths(monkeypatch):
    monkeypatch.setenv("HZ_ALARM_NOTIFY", "off")
    assert stall.push("t") is False
    monkeypatch.setenv("HZ_ALARM_NOTIFY", "/bin/notify")
    calls = []
    monkeypatch.setattr(stall.edge, "run", lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0))
    assert stall.push("hello") is True and calls == [["/bin/notify", "hello"]]
    monkeypatch.setattr(stall.edge, "run", lambda argv, **kw: SimpleNamespace(returncode=1))
    assert stall.push("hello") is False

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 30)

    monkeypatch.setattr(stall.edge, "run", boom)
    assert stall.push("hello") is False
    monkeypatch.setattr(stall.edge, "run", lambda argv, **kw: (_ for _ in ()).throw(FileNotFoundError()))
    assert stall.push("hello") is False


def test_renotify_seconds(monkeypatch):
    monkeypatch.delenv("HZ_ALARM_RENOTIFY_MIN", raising=False)
    assert stall.renotify_seconds() == 7200
    monkeypatch.setenv("HZ_ALARM_RENOTIFY_MIN", "1")
    assert stall.renotify_seconds() == 60
    monkeypatch.setenv("SM_HEINZEL__ALARM_RENOTIFY_MINUTES", "2")  # the setting wins over its old variable
    assert stall.renotify_seconds() == 120
    monkeypatch.setenv("SM_HEINZEL__ALARM_RENOTIFY_MINUTES", "soon")  # typed: a value that is no number is refused, not guessed
    with pytest.raises(PluginError, match="alarm_renotify_minutes"):
        stall.renotify_seconds()


def test_process_records_pushes_once_and_drops_what_is_over(state, monkeypatch):
    monkeypatch.setattr(stall, "quiet_hours", lambda now: False)
    sent = []
    notify = lambda text: sent.append(text) or True  # noqa: E731
    alarms = [{"cls": "ALERT", "key": "a", "text": "boom"}, {"cls": "WARN", "key": "b", "text": "hmm"}]
    out = stall.process(alarms, "queue", NOW, notify)
    assert sent == ["ALERT boom"] and out[0]["pushed"] and out[0]["new"] and not out[1]["pushed"]
    again = stall.process(alarms, "queue", NOW + 60, notify)
    assert sent == ["ALERT boom"] and not again[0]["pushed"] and not again[0]["new"] and again[0]["first"] == NOW
    later = stall.process(alarms[:1], "queue", NOW + 3 * 3600, notify)
    assert len(sent) == 2 and later[0]["pushed"]
    assert "b" not in json.loads(state.read_text())
    stall.process([], "queue", NOW + 4 * 3600, notify)
    assert stall.load() == {}


def test_process_quiet_acked_and_incomplete_and_failed_push(state, monkeypatch):
    sent = []
    alarm_a = [{"cls": "ALERT", "key": "a", "text": "boom"}]
    monkeypatch.setattr(stall, "quiet_hours", lambda now: True)
    assert not stall.process(alarm_a, "queue", NOW, lambda t: sent.append(t))[0]["pushed"] and sent == []
    monkeypatch.setattr(stall, "quiet_hours", lambda now: False)
    assert stall.process(alarm_a, "queue", NOW + 1, lambda t: False)[0]["pushed"] is False
    data = stall.load()
    data["a"]["acked"] = True
    stall.save(data)
    out = stall.process(alarm_a, "queue", NOW + 10**6, lambda t: sent.append(t))
    assert out[0]["acked"] and sent == []
    stall.process([{"cls": "WARN", "key": "w", "text": "x"}], "watch", NOW)
    stall.process([], "queue", NOW + 5, complete=False)
    assert set(stall.load()) == {"a", "w"}
    stall.process([], "queue", NOW + 6)
    assert set(stall.load()) == {"w"}
    stall.process([{"cls": "WARN", "key": "w", "text": "x"}], "queue", NOW + 7)
    stall.process([], "watch", NOW + 8)
    assert stall.load()["w"]["sources"] == ["queue"]


def test_process_defaults_the_clock(state, monkeypatch):
    monkeypatch.setattr(stall.time, "time", lambda: NOW)
    out = stall.process([{"cls": "INFO", "key": "i", "text": "t"}], "queue")
    assert out[0]["first"] == NOW


def test_clear_variants(state):
    assert stall.clear("none") is False
    stall.save({"a": {"sources": ["queue", "watch"]}, "b": {"sources": ["queue"]}, "c": {"sources": ["queue"]}})
    assert stall.clear("a", "other") is False
    assert stall.clear("a", "queue") is True and stall.load()["a"]["sources"] == ["watch"]
    assert stall.clear("b", "queue") is True and "b" not in stall.load()
    assert stall.clear("c") is True and "c" not in stall.load()
