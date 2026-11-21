"""One journal per subject (subjects.py): events appended, the newest wins, repeats skipped; the pipeline runs kept in
them by JournalStore; today's run state files moved in by `sm self migrate-state`, and back for a rollback."""

import json
from pathlib import Path

import pytest

from steuermaschine import subjects
from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.defaults import FileStore, core_providers
from steuermaschine.plugins import Settings, stack_of
from steuermaschine.testing import fake_providers, invoke


def test_events_are_appended_the_newest_wins_and_a_repeat_is_skipped(tmp_path):
    j = subjects.Journals(tmp_path)
    assert j.append("pr", "890", "queue", {"status": "queued"}, 1.0)
    assert not j.append("pr", "890", "queue", {"status": "queued"}, 2.0)
    assert j.append("pr", "890", "queue", {"status": "merged"}, 3.0) and j.append("pr", "890", "queue", {"status": "merged"}, 4.0, repeat=True)
    assert j.latest("pr", "890", "queue") == {"status": "merged"} and j.latest("pr", "890", "other") is None
    assert [e["at"] for e in j.events("pr", "890")] == [1.0, 3.0, 4.0] and j.events("pr", "1") == []
    j.append("branch", "fix/topic", "x", {}, 1.0)
    assert j.keys("branch") == ["fix/topic"] and j.keys("bead") == [] and (tmp_path / "branch" / "fix--topic.jsonl").is_file()
    (tmp_path / "pr" / "890.jsonl").write_text((tmp_path / "pr" / "890.jsonl").read_text() + "broken\n[1]\n")
    assert len(j.events("pr", "890")) == 3
    with pytest.raises(ValueError):
        j.path("nothing", "x")


def test_runs_live_in_the_journal_of_their_subject(tmp_path):
    store = subjects.JournalStore(tmp_path)
    store.save("runs.release.cut", "0.9.55", {"status": "running", "updated": "2026-11-21T10:00:00+0000"})
    store.save("runs.head.admit", "abc123", {"status": "refused", "label": "fix/topic abc123"})
    store.save("runs.head.admit", "abc123", {"status": "finished", "label": "fix/topic abc123"})
    store.save("other", "k", {"plain": True})
    assert store.load("runs.release.cut", "0.9.55")["status"] == "running" and store.load("runs.head.admit", "abc123")["status"] == "finished"
    assert store.keys("runs.head.admit") == ["abc123"] and store.load("other", "k") == {"plain": True} and store.keys("other") == ["k"]
    assert store.kinds() == ["other", "runs.head.admit", "runs.release.cut"]
    assert [e["data"]["state"]["status"] for e in subjects.Journals(tmp_path / "journals").events("branch", "fix/topic")] == ["refused", "finished"]
    store.delete("runs.head.admit", "abc123")
    store.delete("runs.head.admit", "abc123")
    store.delete("other", "k")
    assert store.load("runs.head.admit", "abc123") is None and store.keys("runs.head.admit") == [] and store.load("other", "k") is None
    assert subjects.subject_of("head.admit", "k", None) == ("branch", "k") and store.load("runs.none", "x") is None


def test_migrate_and_back(tmp_path):
    old = FileStore(tmp_path)
    old.save("runs.release.cut", "0.9.42", {"status": "refused", "updated": "bad"})
    old.save("runs.head.admit", "sha1", {"status": "dry_run", "label": "fix/a sha1"})
    (tmp_path / "state" / "runs.head.admit" / "broken.json").write_text("{")
    lines = subjects.migrate_runs(tmp_path)
    assert lines == ["runs.head.admit/sha1.json -> journals/branch/fix--a.jsonl", "runs.release.cut/0.9.42.json -> journals/version/0.9.42.jsonl"]
    assert not (tmp_path / "state" / "runs.release.cut").exists() and subjects.migrate_runs(tmp_path) == []
    assert subjects.JournalStore(tmp_path).load("runs.release.cut", "0.9.42")["status"] == "refused"
    back = subjects.unmigrate_runs(tmp_path)
    assert sorted(back) == ["journals -> state/runs.head.admit/sha1.json", "journals -> state/runs.release.cut/0.9.42.json"]
    assert json.loads((tmp_path / "state" / "runs.head.admit" / "sha1.json").read_text())["status"] == "dry_run"


def test_the_real_providers_keep_runs_in_journals(tmp_path):
    assert isinstance(core_providers(tmp_path).store, subjects.JournalStore)


def test_migrate_state_moves_the_run_files(tmp_path, monkeypatch):
    from tests.test_core_surface import Boxes

    monkeypatch.setenv("STEUERMASCHINE_HOME", str(tmp_path))
    stack = stack_of(Boxes())
    rt = Runtime(Project(name="p", plugin="boxes", root=Path(tmp_path), plugins=["boxes"], allow_unreviewed=True), stack, Settings({}),
                 _providers=fake_providers())
    FileStore(rt.project.home).save("runs.box.run", "1.0.0", {"status": "finished"})
    out = invoke(rt, ["self", "migrate-state"])
    assert out.exit_code == 0 and "runs.box.run/1.0.0.json -> journals/branch/1.0.0.jsonl" in out.stdout
    back = invoke(rt, ["self", "migrate-state", "--back"])
    assert back.exit_code == 0 and "state/runs.box.run/1.0.0.json" in back.stdout


def test_the_focus_list_and_the_alarm_acks_live_in_the_journals(tmp_path):
    store = subjects.JournalStore(tmp_path)
    assert store.load("focus", "list") is None and store.load("alarms", "acks") is None
    store.save("focus", "list", {"/wt/a": {"for": "0.9.55", "branch": "fix/a"}, "0.9.55": {"note": "n"}})
    assert store.load("focus", "list") == {"/wt/a": {"for": "0.9.55", "branch": "fix/a"}, "0.9.55": {"note": "n"}}
    store.save("focus", "list", {"0.9.55": {"note": "n"}})
    assert store.load("focus", "list") == {"0.9.55": {"note": "n"}}
    assert [e["data"]["entry"] for e in store.journals.events("branch", "fix/a")] == [{"for": "0.9.55", "branch": "fix/a"}, None]
    store.save("alarms", "acks", {"stall:801": 1.0})
    assert store.load("alarms", "acks") == {"stall:801": 1.0} and store.journals.latest("branch", "main", "acks") == {"stall:801": 1.0}
    assert subjects.focus_subject("v0.9.55", None) == ("version", "0.9.55") and subjects.focus_subject("/wt/b", None) == ("branch", "/wt/b")


def test_the_focus_list_and_the_acks_are_migrated_and_back(tmp_path):
    old = FileStore(tmp_path)
    old.save("focus", "list", {"/wt/a": {"for": "1.0.0"}})
    old.save("alarms", "acks", {"k": 1})
    (tmp_path / "state" / "focus" / "broken.json").write_text("{")
    assert subjects.migrate_singletons(tmp_path) == ["focus/list.json -> journals", "alarms/acks.json -> journals"]
    assert not (tmp_path / "state" / "focus" / "list.json").exists() and subjects.migrate_singletons(tmp_path) == []
    back = subjects.unmigrate_runs(tmp_path)
    assert "journals -> state/focus/list.json" in back and json.loads((tmp_path / "state" / "alarms" / "acks.json").read_text()) == {"k": 1}
