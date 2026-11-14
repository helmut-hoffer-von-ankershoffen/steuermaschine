"""alarm_lib.watch.collect: every check's reading, blind-check warnings and the slot sizes, on a scripted world.
The pure rules are replaced by markers, so these tests pin what watch feeds them and how it survives a failing read."""

import json
from types import SimpleNamespace

import pytest

from steuermaschine_heinzel.native.alarm_lib import rules, watch

NOW = 1_800_000_000.0
REPO = "o/r"


class Gh:
    def __init__(self, answers):
        self.answers = answers  # (first words) -> data, or an Exception text
        self.runs = []

    def try_json(self, args, timeout=0):
        for prefix, value in self.answers.items():
            if " ".join(args).startswith(prefix):
                return (None, value) if isinstance(value, str) else (value, None)
        return None, "no answer"

    def run(self, args, timeout=0, retry=True):
        self.runs.append(args)
        return self.answers["__ref"]


def world(tmp_path, answers, ps=(), rehearsal=None):
    def processes():
        if isinstance(ps, Exception):
            raise ps
        return list(ps)

    return SimpleNamespace(repo=REPO, state_dir=tmp_path, gh=Gh(answers), processes=processes, rehearsal=rehearsal if rehearsal is not None else {})


@pytest.fixture(autouse=True)
def markers(monkeypatch):
    monkeypatch.setattr(rules, "required_contexts", lambda data: ["CI OK"])
    monkeypatch.setattr(rules, "stall_alarm", lambda key, state, rollup, required, since, now, block: rules.alarm("WARN", f"stall:{key}", f"{state} {[c['name'] for c in rollup]} {since} {block}"))
    monkeypatch.setattr(rules, "main_red_alarm", lambda run, jobs: rules.alarm("ALERT", "main-red", f"red {run['databaseId']} {jobs}"))
    monkeypatch.setattr(rules, "candidate_alarms", lambda runs, jobs_of, now, dropped: [])
    monkeypatch.setattr(rules, "admission_alarms", lambda runs, covered, now, waiting: [])
    monkeypatch.setattr(rules, "slot_alarm", lambda sizes: rules.alarm("WARN", "registry-slots", json.dumps(sizes, sort_keys=True)))
    monkeypatch.setattr(rules, "release_alarm", lambda data, changed, active, now: rules.alarm("INFO", "release", f"{data} {active}"))
    monkeypatch.setattr(rules, "rehearsal_alarms", lambda state, now: [])


QUIET = {
    "run list -R o/r --workflow": [],
    "run list -R o/r -L 100": [],
    "api repos/o/r/actions/variables": {"variables": [{"name": "FOCUSED_CI_PLANS", "value": "xx"}]},
}


def keys(found):
    return {a["key"]: a for a in found}


def test_a_quiet_world_yields_only_the_slot_line(tmp_path):
    found = watch.collect(world(tmp_path, dict(QUIET)), NOW)
    assert list(keys(found)) == ["registry-slots"] and keys(found)["registry-slots"]["text"] == '{"FOCUSED_CI_PLANS": 2}'


def test_queue_stalls_read_each_queued_pr(tmp_path):
    (tmp_path / "queue.json").write_text(json.dumps({"blocked": "freeze", "items": [
        {"pr": 5, "key": "5", "status": "queued", "repo": REPO, "added": 100.0},
        {"pr": 6, "key": "6", "status": "queued", "repo": REPO},
        {"pr": 7, "key": "7", "status": "merged", "repo": REPO},
        {"pr": 8, "key": "8", "status": "queued", "repo": "other/x"}]}))
    answers = {**QUIET, "api repos/o/r/rules/branches/main": [],
               "pr view 5": {"state": "OPEN", "mergeStateStatus": "BLOCKED", "statusCheckRollup": [{"name": "CI OK", "completedAt": "2026-11-13T10:00:00Z"}, {"name": "Focused x"}]},
               "pr view 6": {"state": "CLOSED"}}
    found = keys(watch.collect(world(tmp_path, answers), NOW))
    assert "stall:6" not in found
    text = found["stall:5"]["text"]
    assert text.startswith("BLOCKED ['CI OK']") and text.endswith("freeze")
    answers["pr view 6"] = {"state": "OPEN", "statusCheckRollup": None, "mergeStateStatus": "CLEAN"}
    answers["api repos/o/r/rules"] = None
    assert "stall:6" in keys(watch.collect(world(tmp_path, answers), NOW))
    del answers["pr view 6"]
    blind = keys(watch.collect(world(tmp_path, answers), NOW))
    assert blind["blind:queue-stall"]["text"] == "ALARM CHECK queue-stall unavailable: PR 6 unreadable" and blind["blind:queue-stall"]["cls"] == "WARN"


def test_queue_with_nothing_queued_is_quiet(tmp_path):
    (tmp_path / "queue.json").write_text(json.dumps({"items": [{"pr": 7, "status": "merged"}]}))
    assert "blind:queue-stall" not in keys(watch.collect(world(tmp_path, dict(QUIET)), NOW))


def test_main_red_reads_jobs_and_flags_unreadable_runs(tmp_path):
    ok = {**QUIET, "run list -R o/r --workflow": [{"databaseId": 3, "conclusion": "failure"}], "api repos/o/r/actions/runs/3/jobs": {"jobs": [{"name": "j", "conclusion": "failure", "status": "completed", "x": 1}]}}  # noqa: E501
    found = keys(watch.collect(world(tmp_path, ok), NOW))
    assert found["main-red"]["text"] == "red 3 [{'name': 'j', 'conclusion': 'failure', 'status': 'completed'}]"
    ok["api repos/o/r/actions/runs/3/jobs"] = None
    assert keys(watch.collect(world(tmp_path, ok), NOW))["main-red"]["text"] == "red 3 []"
    ok["run list -R o/r --workflow"] = [{"databaseId": 3, "conclusion": "SUCCESS"}]
    assert "main-red" not in keys(watch.collect(world(tmp_path, ok), NOW))
    ok["run list -R o/r --workflow"] = []
    assert "main-red" not in keys(watch.collect(world(tmp_path, ok), NOW))
    ok["run list -R o/r --workflow"] = "boom"
    assert "blind:main-red" in keys(watch.collect(world(tmp_path, ok), NOW))


def test_recent_runs_unreadable_blinds_candidate_and_admission(tmp_path, monkeypatch):
    bad = {**QUIET, "run list -R o/r -L 100": "boom"}
    found = keys(watch.collect(world(tmp_path, bad), NOW))
    assert "blind:candidate" in found and "blind:admission" in found


def test_candidate_dropped_branch_and_admission_waiting_and_covered(tmp_path, monkeypatch):
    seen = {}

    def candidate(runs, jobs_of, now, dropped):
        seen["dropped"] = [dropped("release/v1")]
        seen["jobs"] = jobs_of(8)
        return [rules.alarm("INFO", "cand", "c")]

    def admission(runs, covered, now, waiting):
        seen["covered"], seen["waiting"] = covered, waiting
        return [rules.alarm("INFO", "adm", "a")]

    monkeypatch.setattr(rules, "candidate_alarms", candidate)
    monkeypatch.setattr(rules, "admission_alarms", admission)
    (tmp_path / "focused-admission-receipts").mkdir()
    (tmp_path / "focused-admission-receipts" / f"head-{'a' * 40}-1.json").write_text("{}")
    answers = {**QUIET, "pr list -R o/r --head release/v1": [{"number": 1, "state": "MERGED"}, {"number": 2, "state": "CLOSED"}],
               "api repos/o/r/actions/runs/8/jobs": {"jobs": [{"name": "n", "conclusion": None, "status": "queued"}]},
               "__ref": SimpleNamespace(rc=1, out="gh: HTTP 404", err="")}
    ps = [(7, "00:05", "/usr/bin/python3 -I tool prepare /abs/packet.json")]
    keys(watch.collect(world(tmp_path, answers, ps=ps), NOW))
    assert seen["dropped"] == [True] and seen["jobs"] == [{"name": "n", "conclusion": None, "status": "queued"}] and seen["covered"] == {"a" * 40}
    # branch still there, an unreadable answer, open pulls and no pulls are never "dropped"
    for ref in (SimpleNamespace(rc=0, out="", err=""), SimpleNamespace(rc=1, out="gh: HTTP 500", err="")):
        answers["__ref"] = ref
        keys(watch.collect(world(tmp_path, answers), NOW))
        assert seen["dropped"] == [False]
    answers["__ref"] = SimpleNamespace(rc=1, out="", err="HTTP 404")
    for pulls in ([{"number": 1, "state": "OPEN"}], [], "boom"):
        answers["pr list -R o/r --head release/v1"] = pulls
        keys(watch.collect(world(tmp_path, answers), NOW))
        assert seen["dropped"] == [False]


def test_waiting_ignores_unreadable_process_list_and_missing_receipts(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(rules, "admission_alarms", lambda runs, covered, now, waiting: seen.append((covered, waiting)) or [])
    watch.collect(world(tmp_path, dict(QUIET), ps=RuntimeError("ps failed")), NOW)
    assert seen == [(set(), False)]
    watch.collect(world(tmp_path, dict(QUIET), ps=[(1, "01:00", "/sbin/launchd")]), NOW)
    assert seen[-1] == (set(), False)


def test_slots_unreadable_and_slot_sizes(tmp_path):
    found = keys(watch.collect(world(tmp_path, {**QUIET, "api repos/o/r/actions/variables": "boom"}), NOW))
    assert found["blind:registry-slots"]["text"].endswith("registry variables unreadable")
    w = world(tmp_path, {"api repos/o/r/actions/variables": {"variables": [{"name": "FOCUSED_CI_PLANS_V2_0", "value": "é"}, {"name": "OTHER", "value": "x"}, {"name": "FOCUSED_CI_PLANS_V2_1"}]}})
    assert watch.slot_sizes(w) == {"FOCUSED_CI_PLANS_V2_0": 2, "FOCUSED_CI_PLANS_V2_1": 0}
    assert watch.slot_sizes(world(tmp_path, {"api repos/o/r/actions/variables": {"variables": "x"}})) is None
    assert watch.slot_sizes(world(tmp_path, {"api repos/o/r/actions/variables": None, })) is None


def test_release_receipt_variants(tmp_path):
    answers = {**QUIET, "run list -R o/r -L 100": [{"headBranch": "release/v1", "status": "queued"}, {"headBranch": "main", "status": "queued"}, {"headBranch": "release/v2", "status": "completed"}]}
    assert "release" not in keys(watch.collect(world(tmp_path, answers), NOW))
    (tmp_path / "release.json").write_text("{bad")
    assert "release" not in keys(watch.collect(world(tmp_path, answers), NOW))
    (tmp_path / "release.json").write_text(json.dumps({"version": "1"}))
    assert keys(watch.collect(world(tmp_path, answers), NOW))["release"]["text"] == "{'version': '1'} 1"


def test_rehearsal_errors_become_blind_warnings(tmp_path):
    w = world(tmp_path, dict(QUIET))
    w.rehearsal = "not an object"
    assert "blind:rehearsal" in keys(watch.collect(w, NOW))


def test_recorded_rows():
    rows = watch.recorded_rows({"b": {"cls": "ALERT", "text": "t", "first": 1, "acked": 1}, "a": {}, "c": "x"})
    assert rows == [{"cls": "WARN", "key": "a", "text": "", "first": None, "acked": False, "new": False},
                    {"cls": "ALERT", "key": "b", "text": "t", "first": 1, "acked": True, "new": False}]
