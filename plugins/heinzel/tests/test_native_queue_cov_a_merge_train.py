"""queue_lib.merge_train: the diagnostics, the release waiver and the train, with a scripted gh and no sleeping."""

import json
from types import SimpleNamespace

import pytest

from steuermaschine_heinzel.native.queue_lib import hzlib
from steuermaschine_heinzel.native.queue_lib import merge_train as mt

HEAD = "a" * 40
OK = {"name": "CI OK", "status": "COMPLETED", "conclusion": "SUCCESS"}


class Script:
    """A fake gh: `views` is a list of answers per view call (the last repeats), merges records the merge call."""

    def __init__(self, monkeypatch, views, merge=(0, "")):
        self.views, self.merge, self.calls, self.sleeps, self.merged = list(views), merge, [], [], []
        monkeypatch.setattr(mt, "gh", self.gh)
        monkeypatch.setattr(mt.time, "sleep", self.sleeps.append)
        monkeypatch.setattr(hzlib, "advisory_checks", lambda repo: {"review"})
        monkeypatch.setattr(hzlib, "tool_required_checks", lambda repo: set())
        monkeypatch.setattr(hzlib, "merge_frozen", lambda legacy=None: False)

    def gh(self, *args):
        self.calls.append(args)
        if args[:2] == ("pr", "view"):
            data = self.views.pop(0) if len(self.views) > 1 else self.views[0]
            return 0, json.dumps(data)
        if args[:2] == ("pr", "merge"):
            self.merged.append(args)
            return self.merge
        return 0, ""


def test_gh_wrapper_merge_prefers_stderr(monkeypatch):
    seen = []
    monkeypatch.setattr(mt.edge, "run", lambda argv, **kw: seen.append(kw) or SimpleNamespace(returncode=1, stdout="out", stderr="err\n"))
    assert mt.gh("pr", "merge", "1") == (1, "err\n")
    assert seen[0]["env"]["GH_DEBUG"] == ""
    monkeypatch.setattr(mt.edge, "run", lambda argv, **kw: SimpleNamespace(returncode=1, stdout="out", stderr=" "))
    assert mt.gh("pr", "merge", "1") == (1, "out")
    assert mt.gh("pr", "view", "1") == (1, "out")


def test_merge_diagnostic_redacts_and_bounds(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "sekrit-token-value")
    text = "\x1b[31mred\x1b[0m boom\x00 sekrit-token-value Authorization: Bearer abc123 ghp_ABC123xyz https://user:pw@host/x?token=zzz"
    out = mt.merge_diagnostic(text)
    assert "sekrit" not in out and "ghp_" not in out and "abc123" not in out and "pw@" not in out and "zzz" not in out
    assert out.startswith("red boom")
    long = mt.merge_diagnostic("word " * 400)
    assert long.endswith("...") and len(long.encode()) <= mt.MAX_MERGE_DIAGNOSTIC_BYTES


def test_view_handles_failures_and_bad_json(monkeypatch):
    monkeypatch.setattr(mt, "gh", lambda *a: (1, "{}"))
    assert mt.view("o/r", "1", "state") == {}
    monkeypatch.setattr(mt, "gh", lambda *a: (0, "nope"))
    assert mt.view("o/r", "1", "state") == {}
    monkeypatch.setattr(mt, "gh", lambda *a: (0, '{"a": 1}'))
    assert mt.view("o/r", "1", "state") == {"a": 1}


def test_settled():
    assert not mt.settled([]) and not mt.settled([{"status": "IN_PROGRESS"}]) and not mt.settled([{"state": "SUCCESS"}])
    assert mt.settled([{"status": "COMPLETED"}])


def test_previous_release(tmp_path, monkeypatch):
    monkeypatch.setattr(mt, "RELEASE_DIR", tmp_path)
    assert mt.previous_release((0, 9, 41)) is None
    (tmp_path / "v0.9.39.json").write_text(json.dumps({"finished": True}))
    (tmp_path / "v0.9.40.json").write_text(json.dumps({"absorbed_by": "0.9.41"}))
    (tmp_path / "v0.9.99.json").write_text("{}")
    (tmp_path / "vX.json").write_text("{}")
    assert mt.previous_release((0, 9, 41)) == ("0.9.40", {"absorbed_by": "0.9.41"})
    assert mt.previous_release((0, 9, 40)) == ("0.9.39", {"finished": True})
    (tmp_path / "v0.9.40.json").write_text("[1]")
    assert mt.previous_release((0, 9, 41)) == ("0.9.40", {})
    (tmp_path / "v0.9.40.json").write_text("{bad")
    assert mt.previous_release((0, 9, 41)) is None


def test_published_dry_run(monkeypatch):
    good = {"name": "Acceptance (linux)", "workflowName": "Acceptance", "detailsUrl": "https://x/actions/runs/55/job/1"}
    answers = []
    monkeypatch.setattr(mt, "gh", lambda *a: answers.append(a) or (0, "pull_request\t" + HEAD + "\n"))
    assert mt.published_dry_run("o/r", good, HEAD) is True
    assert mt.published_dry_run("o/r", good, "b" * 40) is False
    assert mt.published_dry_run("o/r", {**good, "name": "CI OK"}, HEAD) is False
    assert mt.published_dry_run("o/r", {**good, "name": "Other"}, HEAD) is False
    assert mt.published_dry_run("o/r", {**good, "workflowName": "CI"}, HEAD) is False
    assert mt.published_dry_run("o/r", {**good, "detailsUrl": ""}, HEAD) is False
    monkeypatch.setattr(mt, "gh", lambda *a: (1, ""))
    assert mt.published_dry_run("o/r", good, HEAD) is False


def waiver_world(tmp_path, monkeypatch, receipt):
    monkeypatch.setattr(mt, "RELEASE_DIR", tmp_path)
    (tmp_path / "v0.9.40.json").write_text(json.dumps(receipt))
    monkeypatch.setattr(mt, "published_dry_run", lambda repo, c, head: c["name"].startswith("Acceptance"))
    monkeypatch.setattr(mt, "LOGGED", set())
    monkeypatch.setenv("HZ_INTERVENTIONS_LOG", str(tmp_path / "log" / "i.log"))
    (tmp_path / "log").mkdir(exist_ok=True)


def test_waiver_refusals(tmp_path, monkeypatch):
    waiver_world(tmp_path, monkeypatch, {"finished": True, "absorbed_by": "0.9.41"})
    rollup = [{"name": "Acceptance (linux)", "conclusion": "FAILURE"}]
    assert mt.release_acceptance_waiver("o/r", "9", "release/v0.9.41", HEAD, rollup) == set()
    assert mt.release_acceptance_waiver(mt.CORE, "9", "feat/x", HEAD, rollup) == set()
    assert mt.release_acceptance_waiver(mt.CORE, "9", "release/v0.9.39", HEAD, rollup) == set()
    for receipt in ({"absorbed_by": "0.9.41"}, {"finished": True, "accepted": True, "absorbed_by": "0.9.41"}, {"finished": True, "absorbed_by": "0.9.50"}):
        waiver_world(tmp_path, monkeypatch, receipt)
        assert mt.release_acceptance_waiver(mt.CORE, "9", "release/v0.9.41", HEAD, rollup) == set()


def test_waiver_grants_once_logs_and_withdraws_when_log_unwritable(tmp_path, monkeypatch, capsys):
    waiver_world(tmp_path, monkeypatch, {"finished": True, "absorbed_by": "0.9.41", "absorb_reason": "why"})
    rollup = [{"name": "Acceptance (linux)", "conclusion": "FAILURE"}, {"name": "CI OK", "conclusion": "SUCCESS"}]
    assert mt.release_acceptance_waiver(mt.CORE, "9", "release/v0.9.41", HEAD, rollup) == {"Acceptance (linux)"}
    assert mt.release_acceptance_waiver(mt.CORE, "9", "release/v0.9.41", HEAD, rollup) == {"Acceptance (linux)"}
    assert capsys.readouterr().out.count("ADVISORY on release PR") == 1
    assert "absorbed_by 0.9.41 (why)" in (tmp_path / "log" / "i.log").read_text()
    monkeypatch.setattr(mt, "LOGGED", set())
    monkeypatch.setenv("HZ_INTERVENTIONS_LOG", str(tmp_path / "missing" / "i.log"))
    assert mt.release_acceptance_waiver(mt.CORE, "9", "release/v0.9.41", HEAD, rollup) == set()
    assert "waiver withdrawn" in capsys.readouterr().out


def test_update_sleeps(monkeypatch):
    s = Script(monkeypatch, [{}])
    mt.update("o/r", "3")
    assert s.calls == [("pr", "update-branch", "3", "-R", "o/r")] and s.sleeps == [30]


def open_pr(**more):
    return {"state": "OPEN", "isDraft": False, "headRefOid": HEAD, "headRefName": "feat/x", "mergeStateStatus": "CLEAN", "statusCheckRollup": [OK], **more}


def test_train_skips_closed_and_changed_head(monkeypatch, capsys):
    Script(monkeypatch, [{"state": "MERGED", "isDraft": False}])
    assert mt.train("o/r", "1") == (False, "skip: MERGED\tfalse")
    Script(monkeypatch, [open_pr()])
    assert mt.train("o/r", "1", expected_head="b" * 40) == (False, "reviewed head changed; independent review and requeue required")


def test_train_merges_a_clean_pr_after_waiting_and_updating(monkeypatch):
    s = Script(monkeypatch, [open_pr(), {"statusCheckRollup": [{"status": "IN_PROGRESS"}]}, {"statusCheckRollup": [OK]}, open_pr(mergeStateStatus="BEHIND"),
                             {"statusCheckRollup": [OK]}, open_pr(mergeStateStatus="UNKNOWN"), {"statusCheckRollup": [OK]}, open_pr()])
    assert mt.train("o/r", "1") == (True, HEAD[:8])
    assert s.merged == [("pr", "merge", "1", "-R", "o/r", "--squash", "--match-head-commit", HEAD)]
    assert 45 in s.sleeps and 60 in s.sleeps


def test_train_queue_core_updates_a_behind_head_and_never_merges_it(monkeypatch):
    s = Script(monkeypatch, [open_pr(), {"statusCheckRollup": [OK]}, open_pr(mergeStateStatus="BEHIND")])
    merged, detail = mt.train(mt.CORE, "1", queue_core=True)
    assert merged is False and detail.startswith("behind main: the branch was updated") and not s.merged
    assert any(c[:2] == ("pr", "update-branch") for c in s.calls)


def test_train_queue_core_retries_when_checks_unsettled_then_gives_up(monkeypatch):
    unsettled = open_pr(statusCheckRollup=[{"status": "IN_PROGRESS", "name": "x"}])
    views = [open_pr(), {"statusCheckRollup": [OK]}, unsettled, {"statusCheckRollup": [OK]}, unsettled, {"statusCheckRollup": [OK]}, unsettled, {"statusCheckRollup": []}]
    s = Script(monkeypatch, views)
    merged, detail = mt.train(mt.CORE, "1", queue_core=True)
    assert merged is False and detail.startswith("state CLEAN") and not s.merged


def test_train_expected_head_changes_mid_way(monkeypatch):
    Script(monkeypatch, [open_pr(), {"statusCheckRollup": [OK]}, open_pr(headRefOid="c" * 40)])
    assert mt.train("o/r", "1", expected_head=HEAD) == (False, "reviewed head changed; independent review and requeue required")


def test_train_reports_failing_checks_and_tool_required(monkeypatch, capsys):
    bad = {"name": "tests", "status": "COMPLETED", "conclusion": "FAILURE"}
    Script(monkeypatch, [open_pr(), {"statusCheckRollup": [bad]}, open_pr(statusCheckRollup=[bad], mergeStateStatus="UNSTABLE"), {"statusCheckRollup": [bad]}])
    assert mt.train("o/r", "1") == (False, "state UNSTABLE: tests")
    s = Script(monkeypatch, [open_pr(), {"statusCheckRollup": [OK]}, open_pr()])
    monkeypatch.setattr(hzlib, "tool_required_checks", lambda repo: {"coverage-floor"})
    merged, _detail = mt.train("o/r", "1")
    assert not merged and "required by the tools, not passed: coverage-floor" in capsys.readouterr().out and not s.merged


def test_train_merge_failure_detail_and_head_drift(monkeypatch):
    Script(monkeypatch, [open_pr(), {"statusCheckRollup": [OK]}, open_pr(), {"state": "OPEN"}, {"statusCheckRollup": [OK]}], merge=(1, "not mergeable"))
    assert mt.train("o/r", "1") == (False, "state CLEAN; gh pr merge exited 1: not mergeable")
    Script(monkeypatch, [open_pr(), {"statusCheckRollup": [OK]}, open_pr(), {"state": "OPEN"}, {"headRefOid": "d" * 40, "statusCheckRollup": [OK]}], merge=(1, ""))
    assert mt.train("o/r", "1", expected_head=HEAD) == (False, "reviewed head changed; independent review and requeue required; gh pr merge exited 1")
    Script(monkeypatch, [open_pr(), {"statusCheckRollup": [OK]}, open_pr(), {"state": "OPEN"}, {"headRefOid": HEAD, "statusCheckRollup": [OK]}], merge=(1, ""))
    assert mt.train("o/r", "1", expected_head=HEAD) == (False, "state CLEAN; gh pr merge exited 1")


def test_train_pre_merge_guard(monkeypatch):
    def views():
        return [open_pr(), {"statusCheckRollup": [OK]}, open_pr()]

    def boom():
        raise RuntimeError("x")

    Script(monkeypatch, views())
    assert mt.train("o/r", "1", pre_merge_guard=lambda: False) == (False, "main evidence changed or unavailable after waits; reassessment required")
    s = Script(monkeypatch, views())
    assert mt.train("o/r", "1", pre_merge_guard=boom)[0] is False and not s.merged
    Script(monkeypatch, views())
    assert mt.train("o/r", "1", pre_merge_guard=lambda: True) == (True, HEAD[:8])


def test_train_waits_out_the_freeze_and_refuses_a_new_one(monkeypatch):
    s = Script(monkeypatch, [open_pr(), {"statusCheckRollup": [OK]}, open_pr()])
    freezes = iter([True, True, False])
    monkeypatch.setattr(hzlib, "merge_frozen", lambda legacy=None: next(freezes))
    assert mt.train(mt.CORE, "1") == (True, HEAD[:8]) and s.sleeps.count(60) == 2
    s = Script(monkeypatch, [open_pr(), {"statusCheckRollup": [OK]}, open_pr()])
    freezes = iter([False, True])
    monkeypatch.setattr(hzlib, "merge_frozen", lambda legacy=None: next(freezes))
    assert mt.train(mt.CORE, "1", pre_merge_guard=lambda: True) == (False, "merge freeze appeared during final evidence validation")


def test_main(monkeypatch):
    seen = []
    monkeypatch.setattr(mt, "train", lambda repo, pr: seen.append((repo, pr)))
    monkeypatch.setattr(mt.sys, "argv", ["x", "o/r", "1", "2"])
    mt.main()
    assert seen == [("o/r", "1"), ("o/r", "2")]
    monkeypatch.setattr(mt.sys, "argv", ["x"])
    with pytest.raises(SystemExit):
        mt.main()
