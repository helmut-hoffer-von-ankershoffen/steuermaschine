"""The invocation log and `sm self stats`: no values logged, never raises, rotates; the report from fixtures."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_core_cli import Demo

from steuermaschine import statsreport, usagelog
from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import Settings, stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import fake_providers, invoke
from steuermaschine.testing.fakes import FakeCommands


@pytest.fixture
def r(tmp_path, monkeypatch):
    monkeypatch.setenv("STEUERMASCHINE_HOME", str(tmp_path))
    stack = stack_of(Demo())
    return Runtime(Project(name="proj", plugin=stack.name, root=Path("."), plugins=stack.names, allow_unreviewed=True), stack,
                   Settings({"demo": {"colour": "blue"}}), _providers=fake_providers())


def lines(tmp_path):
    return [json.loads(x) for x in (tmp_path / "proj" / usagelog.FILE).read_text().splitlines()]


def test_one_line_per_command_with_flag_names_only(r, tmp_path, monkeypatch):
    monkeypatch.setenv("SM_CALLER", "agent-7")
    invoke(r, ["--format", "json", "alarm", "ack", "secret-key-123", "--json"])
    (row,) = lines(tmp_path)
    assert row["command"] == "alarm ack" and row["caller"] == "agent-7"
    assert "--json" in row["flags"] and "--format" in row["flags"]
    assert set(row) == {"tree", "failure", "command", "flags", "started_at", "duration_ms", "exit", "reason", "caller"}
    assert "secret-key-123" not in (tmp_path / "proj" / usagelog.FILE).read_text()


def test_caller_unknown_and_refusal_reason_without_values(r, tmp_path):
    invoke(r, ["alarm", "ack", "stall:head.ship:abc"])
    invoke(r, ["go"])
    first, second = lines(tmp_path)
    assert first["caller"] == "unknown" and second["command"] == "go" and second["reason"] == ""
    if first["exit"]:
        assert first["reason"] and ":" not in first["reason"] and "abc" not in first["reason"]


def test_flag_names_never_values():
    assert usagelog.flag_names(["head", "--as=bob", "-n12", "-5", "--live", "--", "--after", "x"]) == ["--as", "-n", "--live"]
    assert usagelog.reason.__doc__
    usagelog.reason("pr 1234 not green: /Users/x/secret")
    assert usagelog.NOTE["reason"] == "pr-not-green"
    usagelog.NOTE.clear()


def test_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    usagelog.record(blocker / "below", None, object(), ["go"], 0.0, 0.0, 0)  # the directory cannot be made
    usagelog.record(None, None, object(), ["go"], 0.0, 0.0, 0)
    usagelog.record(tmp_path, None, object(), ["go"], "bad", "bad", 0)  # type: ignore[arg-type]


def test_rotates_by_size(tmp_path, monkeypatch):
    monkeypatch.setattr(usagelog, "MAX_BYTES", 200)
    for _ in range(8):
        usagelog.record(tmp_path, None, object(), ["go"], 1.0, 0.0, 0)
    assert (tmp_path / "invocations.jsonl.1").exists()
    assert (tmp_path / usagelog.FILE).stat().st_size < 400


NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)


def at(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime(statsreport.STAMP)


def row(command, ms, code=0, reason="", hours=1.0, tree=""):
    return {"tree": tree, "command": command, "flags": [], "started_at": at(hours), "duration_ms": ms, "exit": code, "reason": reason, "caller": "x"}


def test_stats_from_fixtures():
    calls = [row("head admit", ms, hours=2) for ms in (100, 200, 300, 400, 5000)]
    calls += [row("queue add", 10, 1, "not-green") for _ in range(3)] + [row("queue add", 10, 75, "busy")]
    calls += [row("go", 5, hours=30), row("old", 1, hours=24 * 9)]
    gate = [{"at": at(1), "waited_s": 3600, "ran_s": 1800, "exit": 0}, {"at": at(2), "waited_s": 0, "ran_s": 1800, "exit": 1},
            {"at": at(24 * 9), "waited_s": 99999, "ran_s": 1, "exit": 0}]
    rep = statsreport.build(calls, gate, "7d", NOW)
    by = {c.command: c for c in rep.commands}
    assert rep.calls == 10 and "old" not in by and by["head admit"].p50_ms == 300 and by["head admit"].p95_ms == 5000
    assert [(x.command, x.reason, x.count) for x in rep.refusals] == [("queue add", "not-green", 3), ("queue add", "busy", 1)]
    assert [x.reason for x in rep.repeated] == ["not-green"]
    assert rep.gate.runs == 2 and rep.gate.wait_hours == 1.0 and rep.gate.run_hours == 1.0 and rep.gate.failure_rate == 0.5
    assert "not available" in rep.merges_per_day
    assert len(rep.friction) == 3 and rep.friction[0].bead.startswith("sm: ")
    assert statsreport.build(calls, gate, "1d", NOW).calls == 9
    text = "\n".join(rep.render())
    assert "Top friction" in text and "Repeated refusals" in text


def test_stats_empty_and_read_lines(tmp_path):
    rep = statsreport.build([], [], "1d", NOW)
    assert rep.calls == 0 and rep.friction == [] and "none" in "\n".join(rep.render())
    path = tmp_path / "x.jsonl"
    path.with_name("x.jsonl.1").write_text('{"a": 1}\nnot json\n[1]\n')
    path.write_text('{"a": 2}\n')
    assert statsreport.read_lines(path) == [{"a": 1}, {"a": 2}] and statsreport.read_lines(tmp_path / "none") == []
    assert statsreport.number(True) == 0.0 and statsreport.stamp({}, "k") is None


def test_stats_command(r, tmp_path):
    invoke(r, ["go"])
    out = invoke(r, ["--format", "json", "self", "stats", "--since", "7d"])
    data = json.loads(out.stdout)
    assert out.exit_code == 0 and data["since"] == "7d" and data["calls"] >= 1
    assert "Top friction" in invoke(r, ["self", "stats"]).stdout
    assert invoke(r, ["self", "stats", "--since", "3d"]).exit_code == 2
    assert invoke(Runtime(None, None), ["self", "stats"]).exit_code == 2


def test_failure_class_and_tree():
    assert [usagelog.failure_class(c) for c in (0, 1, 75, 124, 130, 2, 70)] == ["", "refused", "refused", "timeout", "killed", "failed", "failed"]
    commands = FakeCommands()
    assert usagelog.tree(None) == "" and usagelog.tree(commands) == ""  # no provider; git says no
    commands.on(["git", "rev-parse"], Result(0, "b" * 40 + "\n"))
    assert usagelog.tree(commands) == "b" * 40
    commands.on(["git", "rev-parse"], Result(0, "not a sha"))
    assert usagelog.tree(commands) == ""
    commands.on(["git", "rev-parse"], lambda argv: 1 / 0)
    assert usagelog.tree(commands) == ""


def test_logged_line_has_tree_and_failure(r, tmp_path):
    invoke(r, ["go"])  # under pytest no process is started: the tree stays empty
    (entry,) = lines(tmp_path)
    assert entry["tree"] == "" and entry["failure"] == ""
    commands = FakeCommands()
    commands.on(["git", "rev-parse"], Result(0, "a" * 40))
    usagelog.record(tmp_path / "proj", commands, object(), ["go"], 1.0, 0.0, 1)
    assert lines(tmp_path)[-1]["tree"] == "a" * 40 and lines(tmp_path)[-1]["failure"] == "refused"


def test_unchanged_reruns_and_flaky():
    t1, t2 = "1" * 40, "2" * 40
    calls = [row("gate run", 1, 1, "busy", hours=5, tree=t1), row("gate run", 1, 1, "busy", hours=4, tree=t1), row("gate run", 1, 0, hours=3, tree=t1),
             row("gate run", 1, 1, hours=2, tree=t2), row("gate run", 1, 1, hours=1, tree="new"), row("go", 1, hours=1)]
    rep = statsreport.build(calls, [], "1d", NOW)
    assert [(x.command, x.reruns) for x in rep.unchanged_reruns] == [("gate run", 2)]
    assert [(f.command, f.tree, f.exits) for f in rep.flaky] == [("gate run", t1, [0, 1])]
    assert any("flaky" in f.bead for f in rep.friction)
    assert "Flaky candidates" in "\n".join(rep.render())
