"""`sm queue auto`: every ready PR is queued or merged by itself; a gate-touching one only with a recorded review of its head.
`gh` is scripted; the queue state is a temporary file."""

from types import SimpleNamespace

import pytest
from test_native_auto_review import BASE, HEAD, view
from test_native_ci_triage import REPO, runtime_for

from steuermaschine.github.testing import FakeGh
from steuermaschine.testing import invoke
from steuermaschine_heinzel.native import auto_merge as area
from steuermaschine_heinzel.native import auto_review, queue
from steuermaschine_heinzel.native.status_lib import prtable

OWNER = REPO.split("/")[0]


def row(**over):
    base = dict(repo="heinzel", number=7, title="t", state="CLEAN", ci="ok", sonar="ok", threads=0, author="a")
    return prtable.PrRow(**{**base, **over})


def test_the_cheap_rules_name_what_is_missing():
    assert area.eligible(row()) == "" and area.eligible(row(sonar="-")) == ""
    assert area.eligible(row(state="DRAFT")) == "draft" and "BLOCKED" in area.eligible(row(state="BLOCKED"))
    assert area.eligible(row(ci="FAIL 1")) == "CI FAIL 1" and area.eligible(row(ci="running 3")) == "CI running 3"
    assert area.eligible(row(sonar="FAIL")) == "Sonar FAIL" and "2 unresolved" in area.eligible(row(threads=2))
    assert area.eligible(row(state="BEHIND")) == ""


def test_the_review_rule():
    assert area.review_gate("", []) == "" and area.review_gate("APPROVE", ["x"]) == ""
    assert "rejected" in area.review_gate("REJECT", ["x"]) and "sm review dispatch" in area.review_gate("", ["x"])


def test_queue_words_know_the_queue_repositories_only():
    assert area.queue_words("heinzel", 7, OWNER) == ["7"] and area.queue_words("heinzel-plugins", 3, OWNER) == ["plugins", "3"]
    assert area.queue_words("steuermaschine", 3, OWNER) is None


@pytest.fixture
def world_of_prs(tmp_path, monkeypatch):
    def make(rows, gh, reasons=(), comments=()):
        monkeypatch.setattr(auto_review, "needs_review", lambda *a, **k: list(reasons))
        monkeypatch.setattr(prtable, "fetch", lambda g, o, name, now: ([r for r in rows if r.repo == name], ""))
        gh.on("pr view", {**view(comments=comments), "files": []})
        return runtime_for(tmp_path, gh, status_pr_repos=["heinzel", "steuermaschine"], queue_state_file=str(tmp_path / "queue.json"))

    return make


def merged_cmds(gh):
    return [c for c in gh.lines() if c.startswith("pr merge")]


def test_a_ready_pr_in_a_repo_without_a_queue_is_listed_then_merged_on_live(world_of_prs):
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="steuermaschine", number=9)], gh)
    assert invoke(rt, ["queue", "auto"]).stdout.strip() == "steuermaschine #9: would-merge" and not gh.writes()
    out = invoke(rt, ["queue", "auto", "--live"])
    assert "steuermaschine #9: merged" in out.stdout and merged_cmds(gh) == [f"pr merge 9 -R {OWNER}/steuermaschine --squash --match-head-commit {HEAD}"]


def test_a_pr_that_is_not_ready_is_not_touched(world_of_prs):
    gh = FakeGh()
    rt = world_of_prs([row(repo="steuermaschine", number=9, ci="FAIL 1"), row(repo="steuermaschine", number=10, threads=1)], gh)
    out = invoke(rt, ["queue", "auto", "--live"])
    assert out.stdout.strip() == "no open pull request is ready" and not gh.writes()


def test_a_gate_touching_pr_waits_for_a_review_of_its_head(world_of_prs):
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="steuermaschine", number=9)], gh, reasons=["diff guard: timeout"])
    assert "waiting (needs an independent review of this head" in invoke(rt, ["queue", "auto", "--live"]).stdout and not gh.writes()
    old = {"body": auto_review.comment_body("c" * 40, "opus", "sonnet", "ok", "APPROVE")}
    rt = world_of_prs([row(repo="steuermaschine", number=9)], FakeGh().on("pr merge", ""), reasons=["x"], comments=[old])
    assert "waiting" in invoke(rt, ["queue", "auto", "--live"]).stdout
    bad = {"body": auto_review.comment_body(HEAD, "opus", "sonnet", "no", "REJECT")}
    rt = world_of_prs([row(repo="steuermaschine", number=9)], FakeGh().on("pr merge", ""), reasons=["x"], comments=[bad])
    assert "skipped (the independent review rejected" in invoke(rt, ["queue", "auto", "--live"]).stdout


def test_an_approved_head_is_merged(world_of_prs):
    ok = {"body": auto_review.comment_body(HEAD, "opus", "sonnet", "fine", "APPROVE")}
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="steuermaschine", number=9)], gh, reasons=["diff guard: timeout"], comments=[ok])
    assert "merged" in invoke(rt, ["queue", "auto", "--live"]).stdout and len(merged_cmds(gh)) == 1


def test_dispatch_starts_the_review_when_none_exists(world_of_prs):
    gh = FakeGh().on("pr merge", "").on("--model opus", "ok\nVERDICT: APPROVE fine").on("pr comment", "")
    rt = world_of_prs([row(repo="steuermaschine", number=9)], gh, reasons=["diff guard: timeout"])
    out = invoke(rt, ["queue", "auto", "--live", "--dispatch"])
    assert "steuermaschine #9: merged" in out.stdout and any(c[0] == "claude" for c in gh.calls)
    assert invoke(rt, ["queue", "auto", "--dispatch"]).exit_code == 2


def test_a_queue_repo_pr_is_queued_with_the_review_pinned_to_its_head(world_of_prs, monkeypatch):
    seen = []
    monkeypatch.setattr(queue, "_changed", lambda action, argv, prs, after=None: seen.append(argv) or SimpleNamespace(exit=0, errors=[], lines=[]))
    ok = {"body": auto_review.comment_body(HEAD, "opus", "sonnet", "fine", "APPROVE")}
    rt = world_of_prs([row(number=7)], FakeGh(), reasons=["ci change"], comments=[ok])
    assert "heinzel #7: queued" in invoke(rt, ["queue", "auto", "--live"]).stdout
    assert seen == [["add", "--reviewed", f"review: sm-review comment on head {HEAD[:12]}", "--reviewed-head", HEAD, "7"]]
    seen.clear()
    rt = world_of_prs([row(number=7)], FakeGh())
    assert "would-queue" in invoke(rt, ["queue", "auto"]).stdout and seen == []
    invoke(rt, ["queue", "auto", "--live"])
    assert seen == [["add", "7"]]


def test_the_queue_hold_stops_the_whole_pass(world_of_prs):
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="steuermaschine", number=9)], gh)
    invoke(rt, ["queue", "hold", "--reason", "release blocker"])
    out = invoke(rt, ["queue", "auto", "--live"])
    assert out.stdout.startswith("nothing done: the merge queue is held") and not gh.writes()
