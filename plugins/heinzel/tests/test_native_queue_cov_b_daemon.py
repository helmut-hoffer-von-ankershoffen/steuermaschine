"""queue_daemon bookkeeping: state file, lock, item transitions, assess, wait records, gate and freeze text, small
validators. GitHub is monkeypatched at hzlib; the state lives in tmp_path."""

import json

import pytest

from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native.queue_lib import hzlib, stall
from steuermaschine_heinzel.native.queue_lib import queue_daemon as qd


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HZ_QUEUE_STATE", str(tmp_path / "queue.json"))
    monkeypatch.setenv("HZ_ALARM_STATE", str(tmp_path / "alarm.json"))
    monkeypatch.setenv("HZ_ALARM_NOTIFY", "off")
    (tmp_path / ".cache").mkdir()
    notes = []
    monkeypatch.setattr(hzlib, "note", notes.append)
    return notes


def item(key="5", repo=hzlib.CORE, **kw):
    return {"key": key, "repo": repo, "pr": key, "status": "queued"} | kw


def test_load_save_update():
    assert qd.load()["items"] == [] and qd.load()["last_batch"] == 0
    qd.save({"items": [item("1"), item("2")], "last_batch": 7})
    assert qd.load()["last_batch"] == 7
    qd.update("2", status="merged")
    qd.update(None, blocked="x")
    data = qd.load()
    assert data["items"][1]["status"] == "merged" and data["blocked"] == "x"
    qd.state_path().write_text("{bad")
    with pytest.raises(qd.StateUnreadable):  # a corrupt state file is never read as an empty queue
        qd.load()


def test_log_and_lock(capsys, tmp_path):
    qd.log("hello")
    assert "hello" in capsys.readouterr().out
    assert "hello" in qd.lock_path().with_suffix(".log").read_text()
    first = qd.acquire_lock(block=False)
    assert first is not None
    assert qd.acquire_lock(block=False) is None
    first.close()
    again = qd.acquire_lock()
    assert again is not None
    again.close()


def test_stuck_or_merged(home):
    qd.save({"items": [item("5")]})
    qd.stuck_or_merged(item("5"), "merged", "")
    assert qd.load()["items"][0]["status"] == "merged" and "#5 merged" in home[0]
    qd.save({"items": [item("plugins #3", repo=hzlib.PLUGINS)]})
    qd.stuck_or_merged(item("plugins #3", repo=hzlib.PLUGINS), "needs work", "red")
    assert "plugins #3 needs work: red" in home[-1]
    n = len(home)
    qd.stuck_or_merged(item("plugins #3", repo=hzlib.PLUGINS, noted="needs work"), "needs work", "red")
    assert len(home) == n


def test_deps_met(monkeypatch):
    assert qd.deps_met(item(), [])
    items = [item("7", status="merged")]
    assert qd.deps_met(item(after=["7"]), items)
    assert not qd.deps_met(item(after=["bogus words"]), items)
    monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: {"state": "MERGED"})
    assert qd.deps_met(item(after=["8"]), items)
    monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: None)
    assert not qd.deps_met(item(after=["8"]), items)


class Merge:
    FAILED = {"FAILURE"}

    def __init__(self, settled=True):
        self._settled = settled

    def settled(self, rollup):
        return self._settled


def view(monkeypatch, **info):
    base = {"state": "OPEN", "isDraft": False, "headRefOid": "h", "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [{"name": "CI OK", "status": "COMPLETED", "conclusion": "SUCCESS"}]}
    monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: base | info)
    monkeypatch.setattr(hzlib, "advisory_checks", lambda repo: {"review"})


def test_assess_states(monkeypatch):
    qd.save({"items": [item("5")]})
    monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: {})
    assert qd.assess(item(), Merge()) == "wait"
    view(monkeypatch, state="MERGED")
    assert qd.assess(item(), Merge()) == "done"
    view(monkeypatch, state="CLOSED")
    assert qd.assess(item(), Merge()) == "done" and qd.load()["items"][0]["detail"] == "closed"
    view(monkeypatch)
    assert qd.assess(item(reviewed_head="other"), Merge()) == "done"
    assert qd.assess(item(fix_forward=True), Merge()) == "done"
    view(monkeypatch, isDraft=True)
    assert qd.assess(item(), Merge()) == "done"
    view(monkeypatch)
    assert qd.assess(item(), Merge()) == "ready"
    assert qd.assess(item(), Merge(settled=False)) == "wait"


def test_assess_failures_and_merge_states(monkeypatch):
    qd.save({"items": [item("5")]})
    monkeypatch.setattr(hzlib, "failed_runs", lambda rollup: [1])
    monkeypatch.setattr(hzlib, "failing_tests", lambda repo, r: ["tests/a.py::t"])
    view(monkeypatch, statusCheckRollup=[{"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"}])
    assert qd.assess(item(), Merge()) == "done" and "tests tests/a.py::t" in qd.load()["items"][0]["detail"]
    monkeypatch.setattr(hzlib, "failing_tests", lambda repo, r: [])
    assert qd.assess(item(), Merge()) == "done" and "no failing test" in qd.load()["items"][0]["detail"]
    updated = []
    monkeypatch.setattr(hzlib, "gh", lambda *a: updated.append(a) or (0, ""))
    view(monkeypatch, mergeStateStatus="BEHIND")
    assert qd.assess(item("3", repo=hzlib.PLUGINS), Merge()) == "wait" and updated
    assert qd.assess(item(), Merge()) == "ready"
    view(monkeypatch, mergeStateStatus="DIRTY")
    assert qd.assess(item(), Merge()) == "done"
    view(monkeypatch, mergeStateStatus="BLOCKED")
    assert qd.assess(item(), Merge()) == "wait"


def test_record_wait(monkeypatch):
    qd.save({"items": [item("5")]})
    rollup = [{"name": "CI OK", "status": "COMPLETED", "conclusion": "SUCCESS"}]
    info = {"mergeStateStatus": "BLOCKED", "statusCheckRollup": rollup}
    it = item("5")
    qd.record_wait(it, "wait", info, rollup, set(), now=10_000_000.0)
    assert it["waiting_since"]
    ready = item("5", waiting_since=5)
    qd.record_wait(ready, "ready", info, rollup, set(), now=10_000_000.0)
    assert ready["ready_since"] == 10_000_000 and ready["waiting_since"] is None
    monkeypatch.setattr(stall, "wait_reason", lambda *a: 1 / 0)
    qd.record_wait(item("5"), "wait", info, rollup, set())


def test_valid_main_run_and_supersede():
    ok = {"status": "completed", "conclusion": "success", "databaseId": 1, "headSha": "a" * 40}
    assert qd.valid_main_run(ok)
    assert not qd.valid_main_run(dict(ok, conclusion="odd")) and not qd.valid_main_run(dict(ok, headSha="x"))
    assert not qd.valid_main_run(dict(ok, databaseId=0)) and not qd.valid_main_run(None)
    assert qd.valid_main_run(dict(ok, status="queued", conclusion=None))
    done = [{"status": "completed"}, {"status": "completed"}]
    runs = [{"status": "queued"}, done[0], done[1]]
    assert qd.superseded_by_pending(runs, done[0], done) is True
    assert qd.superseded_by_pending(runs, {"x": 1}, done) is False
    assert qd.superseded_by_pending(runs, done[0], done[:1]) is False


def test_freeze_holder_and_block_detail(tmp_path, monkeypatch):
    assert "no freeze file found" in qd.freeze_holder()
    (statedir.root() / "merge-freeze").write_text("")
    holds = hzlib.release_holds()
    holds.mkdir()
    (holds / "h1").write_text("")
    receipt = statedir.root() / "release.json"
    receipt.write_text(json.dumps({"version": "1.0.0", "acceptance_run_id": 9}))
    text = qd.freeze_holder()
    assert "since" in text and "release holds h1" in text and "acceptance run 9" in text
    for body, stage in (({"compose": "x"}, "compose done"), ({"pypi": "x"}, "PyPI done"), ({}, "before PyPI")):
        receipt.write_text(json.dumps({"version": "1.0.0"} | body))
        assert stage in qd.freeze_holder()
    monkeypatch.setattr(hzlib, "merge_frozen", lambda: True)
    assert qd.block_detail("merge freeze is on").startswith(" [")
    monkeypatch.setattr(hzlib, "merge_frozen", lambda: False)
    assert "stale" in qd.block_detail("merge freeze is on") and qd.block_detail("other") == ""


def test_gate_and_red_main_fixes(monkeypatch):
    core = [item("1")]
    monkeypatch.setattr(hzlib, "merge_frozen", lambda: True)
    assert qd.gate(core, 1000.0, {"last_batch": 0}) == "merge freeze is on"
    monkeypatch.setattr(hzlib, "merge_frozen", lambda: False)
    assert "one per 5 minutes" in qd.gate(core, 100.0, {"last_batch": 90})
    monkeypatch.setattr(qd, "main_state", lambda: "unknown")
    assert qd.gate(core, 1000.0, {"last_batch": 0}) == "main CI evidence is unknown"
    monkeypatch.setattr(qd, "main_state", lambda: "green")
    assert qd.gate(core, 1000.0, {"last_batch": 0}) == ""
    monkeypatch.setattr(qd, "main_state", lambda: "red")
    monkeypatch.setattr(qd.main_checkpoint, "permits", lambda *a: False)
    monkeypatch.setattr(qd, "later_main_passes", lambda: False)
    monkeypatch.setattr(qd, "restored_by_rerun", lambda: False)
    assert "not green" in qd.gate(core, 1000.0, {"last_batch": 0, "main_failure": {}})
    monkeypatch.setattr(qd, "restored_by_rerun", lambda: True)
    assert qd.gate(core, 1000.0, {"last_batch": 0}) == ""
    assert qd.gate([item("2", repo=hzlib.PLUGINS)], 1000.0, {"last_batch": 0}) == ""
    fixes = [item("1", fixes_main=True), item("2"), item("3", repo=hzlib.PLUGINS, fixes_main=True)]
    assert qd.red_main_fixes(fixes) == [fixes[0]]


def test_restored_by_rerun_records_once(monkeypatch, home):
    qd.save({"items": []})
    found = {"run": 5, "sha": "abcdef123456", "beads": ["b1"], "jobs": ["j"]}
    monkeypatch.setattr(qd, "rerun_restoration", lambda: found)
    assert qd.restored_by_rerun() is True
    assert qd.load()["main_restored"]["run"] == 5 and any("restored-by-rerun" in n for n in home)
    count = len(home)
    assert qd.restored_by_rerun() is True and len(home) == count
    monkeypatch.setattr(qd, "rerun_restoration", lambda: {})
    assert qd.restored_by_rerun() is False


def test_a_pr_red_on_a_check_holds_whether_or_not_the_ruleset_requires_it(monkeypatch):
    """CI OK and Sonar green, coverage-floor red: the PR needs work with a ruleset that requires CI OK and Sonar only (a red
    check holds, we fix the check not the queue) and with one that requires coverage-floor too. (no check is waived by name.)"""
    from steuermaschine_heinzel.native.queue_lib.ruleset import Advisory

    qd.save({"items": [item("5")]})
    rollup = [{"name": "CI OK", "status": "COMPLETED", "conclusion": "SUCCESS"},
              {"name": "SonarCloud Code Analysis", "status": "COMPLETED", "conclusion": "SUCCESS"},
              {"name": "coverage-floor", "status": "COMPLETED", "conclusion": "FAILURE"}]
    view(monkeypatch, statusCheckRollup=rollup)
    monkeypatch.setattr(hzlib, "advisory_checks", lambda repo: Advisory(frozenset({"CI OK", "SonarCloud Code Analysis"})))
    monkeypatch.setattr(hzlib, "failed_runs", lambda rollup: [])
    assert qd.assess(item(), Merge()) == "done" and "checks coverage-floor" in qd.load()["items"][0]["detail"]
    monkeypatch.setattr(hzlib, "advisory_checks", lambda repo: Advisory(frozenset({"CI OK", "coverage-floor"})))
    monkeypatch.setattr(hzlib, "failed_runs", lambda rollup: [])
    assert qd.assess(item(), Merge()) == "done" and "checks coverage-floor" in qd.load()["items"][0]["detail"]


def test_a_required_check_that_has_not_reported_holds_the_pr(monkeypatch):
    from steuermaschine_heinzel.native.queue_lib.ruleset import Advisory

    qd.save({"items": [item("5")]})
    view(monkeypatch)  # only CI OK reported
    monkeypatch.setattr(hzlib, "advisory_checks", lambda repo: Advisory(frozenset({"CI OK", "Gate review"})))
    assert qd.assess(item(), Merge()) == "wait"
