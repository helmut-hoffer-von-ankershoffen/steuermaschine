"""The bead stamp reads the merge and release facts through `gh` and `bd`: every answer it cannot read stamps nothing."""

import json

import pytest

from steuermaschine_heinzel.native.queue_lib import bead_stamp as bs


@pytest.fixture
def tools(monkeypatch):
    """`bs.run` answers by the first words of the command; each answer is (code, text); the calls are recorded."""
    answers, calls = {}, []

    def run(*argv, cwd=None):
        calls.append(argv)
        return answers.get(" ".join(argv[:3]), (1, ""))

    monkeypatch.setattr(bs, "run", run)
    return answers, calls


def test_pr_facts_need_a_merged_pr_and_a_readable_answer(tools):
    answers, _ = tools
    answers["gh pr view"] = (0, json.dumps({"state": "MERGED", "mergeCommit": {"oid": "abc"}, "mergedAt": "2026-11-22T03:00:00Z"}))
    assert bs.pr_facts(7, "o/r") == ("abc", "2026-11-22T03:00:00Z")
    answers["gh pr view"] = (0, json.dumps({"state": "OPEN"}))
    assert bs.pr_facts(7, "o/r") is None
    answers["gh pr view"] = (0, "not json")
    assert bs.pr_facts(7, "o/r") is None
    answers["gh pr view"] = (1, "")
    assert bs.pr_facts(7, "o/r") is None


def test_release_facts_are_the_publish_time_of_a_non_draft_release(tools):
    answers, _ = tools
    answers["gh release view"] = (0, json.dumps({"isDraft": False, "publishedAt": "2026-11-22T04:00:00Z"}))
    assert bs.release_facts("v1") == "2026-11-22T04:00:00Z"
    answers["gh release view"] = (0, json.dumps({"isDraft": True, "publishedAt": "x"}))
    assert bs.release_facts("v1") is None
    answers["gh release view"] = (0, "broken")
    assert bs.release_facts("v1") is None


def test_stamp_is_a_dry_run_or_a_note_and_a_label_and_reports_a_failed_write(tools, capsys):
    answers, calls = tools
    assert bs.stamp("abc", "merged_in: PR #7", "merged-7", dry=True) == 0 and calls == []
    answers["bd update heinzel-management-abc"] = (0, "")
    answers["bd label add"] = (0, "")
    assert bs.stamp("abc", "merged_in: PR #7", "merged-7") == 0
    answers["bd label add"] = (1, "")
    assert bs.stamp("abc", "merged_in: PR #7", "merged-7") == 1
    assert "abc: merged_in: PR #7" in capsys.readouterr().out


def test_has_label_reads_a_list_or_a_single_bead_and_an_unreadable_answer_has_none(tools):
    answers, _ = tools
    answers["bd show heinzel-management-abc"] = (0, json.dumps([{"labels": ["merged-7"]}]))
    assert bs.has_label("abc", "merged-7") and not bs.has_label("abc", "released-v1")
    answers["bd show heinzel-management-abc"] = (0, json.dumps({"labels": None}))
    assert not bs.has_label("abc", "merged-7")
    answers["bd show heinzel-management-abc"] = (0, "[]")
    assert not bs.has_label("abc", "merged-7")


def test_do_stamp_refuses_an_unmerged_pr_and_an_unpublished_release_and_skips_a_stamped_one(monkeypatch, capsys):
    monkeypatch.setattr(bs, "pr_facts", lambda pr, repo: None)
    assert bs.do_stamp("abc", pr=7) == 1 and "is not merged" in capsys.readouterr().err
    monkeypatch.setattr(bs, "release_facts", lambda tag: None)
    assert bs.do_stamp("abc", released="1.0") == 1 and "v1.0 is not published" in capsys.readouterr().err
    stamped = []
    monkeypatch.setattr(bs, "pr_facts", lambda pr, repo: ("abcdef123", "t"))
    monkeypatch.setattr(bs, "release_facts", lambda tag: "t2")
    monkeypatch.setattr(bs, "stamp", lambda bead, line, label, dry=False: stamped.append(label) or 0)
    monkeypatch.setattr(bs, "has_label", lambda bead, label: label == "merged-7")
    assert bs.do_stamp("abc", pr=7, released="v1.0") == 0 and stamped == ["released-v1.0"]
