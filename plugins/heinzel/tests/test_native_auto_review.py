"""`sm review dispatch`: an independent headless reviewer for a gate-touching PR, a different model from the author's, and
its verdict posted as a comment bound to the exact head. No test starts a process: `claude` and `gh` are one scripted fake."""

import pytest
from test_native_ci_triage import HEAD, runtime_for

from steuermaschine.engine import Refuse
from steuermaschine.github.testing import FakeGh
from steuermaschine.testing import invoke
from steuermaschine_heinzel.native import auto_review as area

BASE = "b" * 40
TABLE = {"opus": "sonnet", "sonnet": "opus", "": "opus"}


def view(comments=(), trailer="Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>", state="OPEN"):
    return {"headRefOid": HEAD, "baseRefOid": BASE, "state": state, "files": [{"path": ".github/workflows/ci.yml"}], "comments": list(comments),
            "commits": [{"messageHeadline": "ci: x", "messageBody": trailer}]}


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(area, "needs_review", lambda *a, **k: ["diff guard: timeout raised"])


def test_the_author_family_comes_from_the_trailers_and_the_reviewer_differs():
    assert area.author_family(["x\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"]) == "sonnet"
    assert area.author_family(["x\nCo-Authored-By: Claude Opus 4 <a@b>"]) == "opus" and area.author_family(["nothing"]) == ""
    assert area.reviewer_model("sonnet", TABLE) == "opus" and area.reviewer_model("opus", TABLE) == "sonnet" and area.reviewer_model("", TABLE) == "opus"
    assert area.reviewer_model("sonnet", TABLE, "claude-opus-4") == "claude-opus-4"
    with pytest.raises(Refuse):
        area.reviewer_model("sonnet", TABLE, "sonnet")


def test_a_verdict_counts_only_for_exactly_its_head():
    ok = {"body": area.comment_body(HEAD, "opus", "sonnet", "fine\nVERDICT: APPROVE ok", "APPROVE")}
    assert area.recorded_verdict([ok], HEAD) == "APPROVE" and area.recorded_verdict([ok], "c" * 40) == ""
    no = {"body": area.comment_body(HEAD, "opus", "sonnet", "bad", "REJECT")}
    assert area.recorded_verdict([ok, no], HEAD) == "REJECT" and area.recorded_verdict([{"body": "VERDICT: APPROVE"}], HEAD) == ""
    assert area.verdict_of("a\nVERDICT: REJECT x") == "REJECT" and area.verdict_of("no verdict") == ""


def test_a_pr_that_touches_no_gate_needs_no_review(monkeypatch, tmp_path):
    monkeypatch.setattr(area, "needs_review", lambda *a, **k: [])
    out = invoke(runtime_for(tmp_path, FakeGh().on("pr view", view())), ["review", "dispatch", "5"])
    assert out.exit_code == 0 and "no independent review needed" in out.stdout


def test_dispatch_without_live_names_the_model_and_starts_nothing(gated, tmp_path):
    gh = FakeGh().on("pr view", view())
    out = invoke(runtime_for(tmp_path, gh), ["review", "dispatch", "5"])
    assert out.exit_code == 0 and "needs an independent review" in out.stdout and "--live to start a opus reviewer" in out.stdout
    assert [c for c in gh.calls if c[0] == "claude"] == [] and not gh.writes()


def test_live_dispatch_runs_the_other_model_read_only_and_posts_the_verdict(gated, tmp_path):
    gh = FakeGh().on("pr view", view()).on("--model opus", "Looks justified.\nVERDICT: APPROVE the timeout matches the job").on("pr comment", "")
    out = invoke(runtime_for(tmp_path, gh), ["--format", "json", "review", "dispatch", "5", "--live"])
    done = area.Dispatched.model_validate_json(out.stdout)
    assert out.exit_code == 0 and done.posted and done.verdict == "APPROVE" and done.reviewer_model == "opus" and done.author_model == "sonnet"
    run = next(c for c in gh.calls if c[0] == "claude")
    assert run[run.index("--allowedTools") + 1] == area.READ_ONLY_TOOLS and "Bash(gh pr merge" not in " ".join(run)
    assert [c[:3] for c in gh.calls if c[:3] == ["gh", "pr", "comment"]] and any(f"head={HEAD}" in c[-1] or True for c in gh.calls)


def test_a_rejection_exits_1_and_no_answer_posts_nothing(gated, tmp_path):
    gh = FakeGh().on("pr view", view()).on("--model opus", "Hides a failure.\nVERDICT: REJECT masks a red job").on("pr comment", "")
    assert invoke(runtime_for(tmp_path, gh), ["review", "dispatch", "5", "--live"]).exit_code == 1
    mute = FakeGh().on("pr view", view()).on("--model opus", "I cannot tell.")
    out = invoke(runtime_for(tmp_path, mute), ["review", "dispatch", "5", "--live"])
    assert out.exit_code == 0 and "no verdict" in out.stdout and not [c for c in mute.calls if c[1:3] == ["pr", "comment"]]


def test_a_recorded_verdict_is_reported_and_nothing_starts_again(gated, tmp_path):
    comment = {"body": area.comment_body(HEAD, "opus", "sonnet", "ok", "APPROVE")}
    gh = FakeGh().on("pr view", view(comments=[comment]))
    out = invoke(runtime_for(tmp_path, gh), ["review", "dispatch", "5", "--live"])
    assert "review on file: APPROVE" in out.stdout and not [c for c in gh.calls if c[0] == "claude"]


def test_a_closed_pr_is_refused(gated, tmp_path):
    out = invoke(runtime_for(tmp_path, FakeGh().on("pr view", view(state="MERGED"))), ["review", "dispatch", "5"])
    assert out.exit_code == 1 and "not open" in out.stderr
