"""Small pure helpers of the moved libraries: the overdue-bead triage, the merge-freeze holds, the push-gate rules and the
child-process command lines. Real temporary files, no network, no process."""

import sys
from pathlib import Path

import pytest

from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native.release_admit_lib import release_identity as ri
from steuermaschine_heinzel.native.release_lib import edge as release_edge
from steuermaschine_heinzel.native.release_read_lib import chores
from steuermaschine_heinzel.native.ship_lib import push_gate_rules as rules
from steuermaschine_heinzel.native.train_lib import edge as train_edge


def test_pr_refs_find_every_form_of_a_pull_request_reference_once():
    assert chores.pr_refs("fixed in #801, PR 802 and https://x/pull/803; again #801; v12") == [801, 802, 803]
    assert chores.pr_refs(None) == []


def test_triage_lists_only_the_overdue_beads_that_reference_a_merged_pr():
    beads = [{"id": "a", "title": "t" * 100}, {"id": "b", "title": "open"}]
    rows = chores.triage(beads, {"a": "see #801 and #900", "b": "see #900"}, {801})
    assert rows == [("a", "t" * 70, [801])]


def test_the_triage_and_post_publish_commands_print_what_they_found(monkeypatch, capsys):
    answers = {("bd", "list"): '[{"id": "a", "title": "old"}]', ("bd", "show"): "merged in #801", ("gh", "pr"): "MERGED\n"}
    monkeypatch.setattr(chores, "sh", lambda *a: answers[a[:2]])
    assert chores.main(["overdue-triage"]) == 0
    out = capsys.readouterr().out
    assert "1 overdue, 1 reference a merged PR" in out and "bd close a --reason 'merged #801'" in out
    assert chores.main(["post-publish", "1.2.3"]) == 0
    assert "Post-publish checklist for v1.2.3" in capsys.readouterr().out


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(statedir, "path", lambda name: tmp_path / name)
    return tmp_path


def test_a_release_hold_blocks_merging_whether_it_is_absent_a_file_an_empty_or_a_filled_directory(state_dir):
    assert ri.release_holds_present() is False  # no directory: no hold
    (state_dir / "release-holds").write_text("a file where a directory belongs")
    assert ri.release_holds_present() is True  # malformed: blocks like a hold
    (state_dir / "release-holds").unlink()
    (state_dir / "release-holds").mkdir()
    assert ri.release_holds_present() is False
    (state_dir / "release-holds" / "0.9.1").write_text("")
    assert ri.release_holds_present() is True


def test_the_incident_sentinel_freezes_merging_even_as_a_dangling_link(state_dir):
    assert ri.incident_frozen() is False and ri.merge_frozen() is False
    (state_dir / "merge-freeze").symlink_to(state_dir / "nowhere")
    assert ri.incident_frozen() is True and ri.merge_frozen() is True
    assert ri.incident_frozen(state_dir / "other") is False


def test_auto_defer_acts_only_on_a_starved_main_outside_the_cooldown_and_keeps_the_named_prs():
    plan = rules.auto_defer_plan
    assert plan(None, 30, [], None, 0) is None and plan(10, 30, [], None, 0) is None
    assert plan(40, 30, [], None, 0, special_active=True) is None
    assert plan(40, 30, [], 1000, 1100) is None  # the last auto-defer ran a minute ago
    assert plan(40, 30, [], 1000, 9000) == ["sm", "ci", "defer", "--if-main-starved", "--minutes", "30"]
    assert plan(40, 30, {9, 3}, None, 0)[-2:] == ["--keep", "3,9"]


def test_special_workflows_and_pushing_gate_commands_are_recognised_by_name():
    assert rules.special_run_active([{"status": "in_progress", "workflowName": " Acceptance "}]) is True
    assert rules.special_run_active([{"status": "completed", "workflowName": "acceptance"}, {"status": "queued", "workflowName": "CI"}]) is False
    assert rules.gate_command_pushes(["git", "push", "--force"]) is True and rules.gate_command_pushes(["make", "pr"]) is True
    assert rules.gate_command_pushes(["git", "status"]) is False


def test_child_command_lines_start_this_program_and_name_the_gate_and_ship_verbs(tmp_path, monkeypatch):
    fake = tmp_path / "sm"
    fake.write_text("")
    monkeypatch.setattr(sys, "argv", [str(fake)])
    assert release_edge.sm() == [str(fake)]
    assert release_edge.gate()[-5:] == ["gate", "run", "--full", "--exclusive", "--"]
    assert release_edge.ship(7)[-3:] == ["head", "ship", "7"]
    monkeypatch.setattr(sys, "argv", ["pytest"])
    monkeypatch.setattr(release_edge.shutil, "which", lambda name: None)
    assert release_edge.sm() == ["sm"]
    assert train_edge.gate()[-5:] == ["run", "--priority", "2", "--hold-exempt", "--"]
    assert train_edge.train_run("1.2.3")[-3:] == ["train", "--run", "1.2.3"]


def test_the_owner_notification_is_false_outside_a_native_command_and_when_the_notifier_fails(monkeypatch):
    assert train_edge.notify_owner("x") is False  # no runtime here
    import steuermaschine.native as native

    class Notifier:
        def __init__(self, fail):
            self.fail, self.sent = fail, []

        def notify(self, level, key, text):
            if self.fail:
                raise RuntimeError("down")
            self.sent.append((level, key, text))

    for fail, expected in ((False, True), (True, False)):
        notifier = Notifier(fail)
        monkeypatch.setattr(native, "runtime", lambda n=notifier: type("R", (), {"providers": type("P", (), {"notifier": n})()})())
        assert train_edge.notify_owner("hello") is expected
        assert notifier.sent == ([("info", "release-train", "hello")] if expected else [])


def test_the_assets_command_prints_the_digest_or_probes_once_and_exits_by_the_health(monkeypatch, capsys, tmp_path):
    from steuermaschine_heinzel.native.release_read_lib import assets

    cache = str(tmp_path / "assets-health.json")
    monkeypatch.setattr(assets, "CACHE", cache)
    monkeypatch.setattr(assets, "save", lambda res: None)
    monkeypatch.setattr(assets, "digest_line", lambda path=cache: "ASSETS: stub")
    assert assets.main(["--digest"]) == 0 and capsys.readouterr().out.strip() == "ASSETS: stub"
    monkeypatch.setattr(assets, "assets", lambda core: [("a", "https://x/a"), ("b", "https://x/b")])
    monkeypatch.setattr(assets, "check", lambda items: [(n, u, n == "a", 200 if n == "a" else 503) for n, u in items])
    assert assets.main([]) == 1
    out = capsys.readouterr().out
    assert "ok  200      a" in out and "BAD 503      b" in out
    monkeypatch.setattr(assets, "check", lambda items: [(n, u, True, 200) for n, u in items])
    monkeypatch.setattr(assets, "wait_stable", lambda n, interval, rounds, report=False: (True, rounds()))
    assert assets.main(["--stable", "2", "--interval", "1"]) == 0
    monkeypatch.setattr(assets, "wait_stable", lambda n, interval, rounds, report=False: (False, rounds()))
    assert assets.main(["--stable", "2"]) == 1
