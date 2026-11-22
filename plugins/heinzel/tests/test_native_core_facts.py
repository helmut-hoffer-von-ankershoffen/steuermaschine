"""core_facts: Core's release policy at a commit or at main's tip, read once per commit, one clear line when main has none."""

from __future__ import annotations

import policy_kit as kit
import pytest

from steuermaschine_heinzel.native.queue_lib import core_facts
from steuermaschine_heinzel.native.queue_lib.release_candidate import CandidateError

REAL_AT, REAL_MAIN_SHA = core_facts.at, core_facts.main_sha  # before the suite's fixture replaces them
MAIN = "d" * 40


@pytest.fixture
def github(monkeypatch):
    calls = []
    answers = {"commits/main": {"sha": MAIN}, **kit.contents_routes(MAIN, kit.files("new"))}

    def gh_json(path):
        calls.append(path)
        return answers.get(path)

    monkeypatch.setattr(core_facts, "_gh_json", gh_json)
    monkeypatch.setattr(core_facts, "at", REAL_AT)
    monkeypatch.setattr(core_facts, "main_sha", REAL_MAIN_SHA)
    core_facts.forget()
    return answers, calls


def test_the_policy_at_main_is_read_once_per_commit(github):
    _answers, calls = github
    policy = core_facts.at_main()
    assert policy.commit == MAIN and core_facts.aggregate() == "CI OK" and core_facts.ci_file() == kit.WORKFLOW.rsplit("/", 1)[-1]
    assert core_facts.judge_path() == kit.JUDGE and core_facts.judge_file() == kit.JUDGE.rsplit("/", 1)[-1]
    assert core_facts.ci_path() == kit.WORKFLOW and set(core_facts.api_checks()) | {"CI OK"} == set(core_facts.contexts())
    assert calls.count(f"contents/{kit.PATH}?ref={MAIN}") == 1 and calls.count("commits/main") == 1  # cached
    assert core_facts.safe() is policy and core_facts.safe(MAIN) is policy


def test_main_without_the_manifest_is_one_clear_line(github):
    answers, _calls = github
    del answers[f"contents/{kit.PATH}?ref={MAIN}"]
    with pytest.raises(CandidateError) as no:
        core_facts.at_main()
    assert str(no.value) == core_facts.NO_POLICY and "\n" not in str(no.value)
    assert core_facts.safe() is None
    answers["commits/main"] = {"sha": "short"}
    core_facts.forget()
    with pytest.raises(CandidateError, match="no exact commit"):
        core_facts.at_main()


def test_a_garbled_manifest_at_main_names_its_fault(github):
    answers, _calls = github
    answers[f"contents/{kit.PATH}?ref={MAIN}"] = kit.contents(b"{nope\n")
    with pytest.raises(CandidateError, match="not valid JSON"):
        core_facts.at_main()


def test_names_are_read_on_use_never_at_import(github):
    names = core_facts.Names(lambda: ("a", "b"))
    assert list(names) == ["a", "b"] and "a" in names and len(names) == 2 and names[1] == "b"
    assert names + ("c",) == ("a", "b", "c") and ("z",) + names == ("z", "a", "b") and names == ("a", "b")  # noqa: RUF005
    assert (names == 3) is False and "Names" in repr(names)
    assert "merge-gate" in core_facts.API_CHECKS and "CI OK" in core_facts.CONTEXTS


def test_the_github_read_fails_soft(monkeypatch):
    import subprocess

    monkeypatch.setattr(core_facts.shutil, "which", lambda name: None)
    assert core_facts._gh_json("commits/main") is None
    monkeypatch.setattr(core_facts.shutil, "which", lambda name: "/bin/gh")
    monkeypatch.setattr(core_facts.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, '{"sha": "x"}', ""))
    assert core_facts._gh_json("commits/main") == {"sha": "x"}
    monkeypatch.setattr(core_facts.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "not json", ""))
    assert core_facts._gh_json("commits/main") is None
    monkeypatch.setattr(core_facts.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "HTTP 404"))
    assert core_facts._gh_json("commits/main") is None

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("gh", 60)

    monkeypatch.setattr(core_facts.subprocess, "run", boom)
    assert core_facts._gh_json("commits/main") is None
