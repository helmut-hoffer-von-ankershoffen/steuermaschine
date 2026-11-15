"""qtz6 (b): open beads labelled owner-ask or heinzel-issues raise ONE recorded alarm, so bare `sm` shows it and `sm alarm ack`
works; the ack is a baseline and only newer beads raise it again."""

import json
from pathlib import Path

from test_native_go import scripted

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel.native import go as go_mod
from steuermaschine_heinzel.native.alarm_lib import asks
from steuermaschine_heinzel.plugin import plugin

NOW = 1_800_000_000.0


def iso(epoch):
    import datetime

    return datetime.datetime.fromtimestamp(epoch, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def bead(name, created, label="owner-ask"):
    return {"id": f"t-{name}", "status": "open", "labels": [label], "title": name, "created_at": iso(created)}


def runtime(tmp_path, beads, labels=None):
    state = tmp_path / "state"
    (state / "focused-admission-receipts").mkdir(parents=True, exist_ok=True)
    commands = scripted(tmp_path)
    commands.on(["bd", "list"], Result(0, json.dumps(beads)))
    table = {"legacy_state_dir": str(state), "core_repo_dir": "/core", "go_packet_globs": [str(tmp_path / "no/*.json")], "coord_tracker_dir": str(tmp_path), "alarm_state_file": str(state / "alarm.json"),
             **({"alarm_ask_labels": labels} if labels else {})}
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": table}, Path("."))
    providers = fake_providers(commands=commands)
    providers.clock.t = NOW
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=True), stack, settings, _providers=providers), commands


def alerts(rt):
    out = invoke(rt, ["--format", "json", "go"])  # bare `sm` is `go`: it lists the recorded, unacknowledged ALERTs
    assert out.exit_code == 0, out
    return [f"{a.text}  (silence: sm alarm ack {a.key})" for a in go_mod.Go.model_validate_json(out.stdout).alarms]


def test_one_combined_alarm_is_recorded_shown_by_bare_sm_and_acknowledged_as_a_baseline(tmp_path):
    beads = [bead(str(i), NOW - 5000 + i) for i in range(41)]
    rt, commands = runtime(tmp_path, beads)
    out = invoke(rt, ["alarm", "asks"])
    assert out.exit_code == 0 and "41 open owner-ask/heinzel-issues beads, 41 new since the last ack" in out.stdout, out
    argv = next(a for a in commands.calls if a[:2] == ["bd", "list"])
    assert "owner-ask,heinzel-issues" in argv  # the labels come from the setting's default, not the code
    shown = alerts(rt)  # bare `sm` is `go`: it shows the recorded alarm, once for all 41 beads
    assert len(shown) == 1 and "41 open" in shown[0] and "sm alarm ack asks:open" in shown[0]
    assert invoke(rt, ["alarm", "ack", asks.KEY]).exit_code == 0  # a recorded key: the ack is accepted
    assert alerts(rt) == []  # acknowledged: no ALERT, and it persists (the ack is in the store)
    assert rt.providers.store.load("alarms", "acks")[asks.KEY] == NOW
    invoke(rt, ["alarm", "asks"])  # the loop runs again with nothing new: still acknowledged
    assert alerts(rt) == []


def test_only_beads_created_after_the_ack_raise_it_again(tmp_path):
    rt, commands = runtime(tmp_path, [bead("old", NOW - 100)])
    invoke(rt, ["alarm", "asks"])
    invoke(rt, ["alarm", "ack", asks.KEY])
    commands.on(["bd", "list"], Result(0, json.dumps([bead("old", NOW - 100), bead("report", NOW + 60, "heinzel-issues")])))
    out = invoke(rt, ["alarm", "asks"])
    assert "2 open owner-ask/heinzel-issues beads, 1 new since the last ack" in out.stdout
    assert asks.KEY not in (rt.providers.store.load("alarms", "acks") or {})  # the ack was dropped
    assert len(alerts(rt)) == 1


def test_the_alarm_goes_when_no_ask_bead_is_open_and_the_labels_are_a_setting(tmp_path):
    rt, commands = runtime(tmp_path, [bead("a", NOW - 10)], labels=["needs-human"])
    assert "needs-human" in invoke(rt, ["alarm", "asks"]).stdout
    assert "needs-human" in " ".join(next(a for a in commands.calls if a[:2] == ["bd", "list"]))
    commands.on(["bd", "list"], Result(0, "[]"))
    assert invoke(rt, ["alarm", "asks"]).stdout.strip() == "no open ask bead" and alerts(rt) == []


def test_an_unreadable_tracker_refuses_and_records_nothing(tmp_path):
    rt, commands = runtime(tmp_path, [])
    commands.on(["bd", "list"], Result(1, "", "boom"))
    out = invoke(rt, ["alarm", "asks"])
    assert out.exit_code != 0 and "tracker cannot be read" in out.stderr and alerts(rt) == []
