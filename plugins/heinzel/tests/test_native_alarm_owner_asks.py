"""qtz6 (b): an open bead labelled owner-ask or heinzel-issues (an in-app report) is an ALERT, so it is never left unseen."""

import json
from types import SimpleNamespace

from steuermaschine.protocols import Result
from steuermaschine_heinzel.native.alarm_lib import rules, watch


def test_only_open_beads_with_an_ask_label_raise_an_alert():
    beads = [{"id": "t-a1", "status": "open", "labels": ["owner-ask"], "title": "Decide X"},
             {"id": "t-b2", "status": "open", "labels": ["heinzel-issues", "x"], "title": "In-app report"},
             {"id": "t-c3", "status": "open", "labels": ["sm"], "title": "Not an ask"},
             {"id": "t-d4", "status": "closed", "labels": ["owner-ask"], "title": "Done"}]
    found = rules.ask_alarms(beads)
    assert [(a["cls"], a["key"]) for a in found] == [("ALERT", "bead-ask:t-a1"), ("ALERT", "bead-ask:t-b2")]
    assert "OWNER-ASK bead t-a1: Decide X" in found[0]["text"] and "HEINZEL-ISSUES" in found[1]["text"]
    assert rules.exit_code(found) == 3 and rules.exit_code([{**a, "acked": True} for a in found]) == 0  # sm alarm ack silences it


class Commands:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def run(self, argv, cwd=None, timeout=None, env=None):
        self.calls.append((argv, cwd))
        return self.reply


class Gh:
    def try_json(self, args, timeout=0):
        return None, "no answer"

    def run(self, args, timeout=0, retry=True):
        return Result(1, "", "no")


def world(tmp_path, reply):
    commands = Commands(reply)
    return SimpleNamespace(repo="o/r", state_dir=tmp_path, gh=Gh(), processes=lambda: [], rehearsal={}, commands=commands,
                           settings={"coord_tracker_dir": str(tmp_path / "beads")}), commands


def asks(found):
    return [a for a in found if a["key"].startswith(("bead-ask:", "blind:owner-asks"))]


def test_the_watch_reads_the_tracker_and_alerts_for_each_ask_bead(tmp_path):
    reply = Result(0, json.dumps([{"id": "t-b2", "status": "open", "labels": ["heinzel-issues"], "title": "Crash on start"}]), "")
    w, commands = world(tmp_path, reply)
    found = asks(watch.collect(w, 1_800_000_000.0))
    assert [(a["cls"], a["key"]) for a in found] == [("ALERT", "bead-ask:t-b2")]
    argv, cwd = commands.calls[0]
    assert argv[:2] == ["bd", "list"] and "owner-ask,heinzel-issues" in argv and cwd == str(tmp_path / "beads")


def test_a_failing_tracker_read_is_a_warning_never_silence(tmp_path):
    w, _ = world(tmp_path, Result(1, "", "boom"))
    found = asks(watch.collect(w, 1_800_000_000.0))
    assert [(a["cls"], a["key"]) for a in found] == [("WARN", "blind:owner-asks")]
