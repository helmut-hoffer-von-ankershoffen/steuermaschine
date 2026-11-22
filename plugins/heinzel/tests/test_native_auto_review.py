"""`sm review dispatch`: an independent headless reviewer for a gate-touching PR, a different model from the author's, and
its verdict posted as a comment bound to the exact head. No test starts a process: `claude` and `gh` are one scripted fake."""

import pytest
from test_native_ci_triage import HEAD, runtime_for

from steuermaschine.engine import Refuse
from steuermaschine.github.testing import FakeGh
from steuermaschine.protocols import Result
from steuermaschine.testing import invoke
from steuermaschine_heinzel.native import auto_review as area

BASE = "b" * 40
TABLE = {"opus": "sonnet", "sonnet": "opus", "": "opus"}
OWNER = "helmut-hoffer-von-ankershoffen"
CREATORS = [OWNER]


def said(body, login=OWNER):
    return {"body": body, "author": {"login": login}}


DIFF = "diff --git a/x b/x\n+timeout: 99\n"


def view(comments=(), trailer="Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>", state="OPEN"):
    return {"headRefOid": HEAD, "baseRefOid": BASE, "state": state, "files": [{"path": ".github/workflows/ci.yml"}], "comments": list(comments),
            "commits": [{"messageHeadline": "ci: x", "messageBody": trailer}]}


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(area, "needs_review", lambda *a, **k: ["diff guard: timeout raised"])


def test_the_author_family_comes_from_the_trailers_and_the_reviewer_differs():
    assert area.author_families(["x\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"]) == {"sonnet"}
    assert area.author_families(["x\nCo-Authored-By: Claude Opus 4 <a@b>"]) == {"opus"} and area.author_families(["nothing"]) == set()
    both = ["a\nCo-Authored-By: Claude Opus 4 <a@b>", "b\nCo-Authored-By: Claude Sonnet 5.5 <a@b>"]  # every commit counts
    assert area.author_families(both) == {"opus", "sonnet"}
    assert area.reviewer_model({"sonnet"}, TABLE) == "opus" and area.reviewer_model({"opus"}, TABLE) == "sonnet" and area.reviewer_model(set(), TABLE) == "opus"
    assert area.reviewer_model({"sonnet"}, TABLE, "claude-opus-4") == "claude-opus-4"
    assert area.reviewer_model({"opus", "sonnet"}, {**TABLE, "haiku": "haiku-x"}, "claude-haiku-4") == "claude-haiku-4"
    with pytest.raises(Refuse):
        area.reviewer_model({"sonnet"}, TABLE, "sonnet")
    with pytest.raises(Refuse):
        area.reviewer_model({"opus", "sonnet"}, TABLE)  # no other family left in the table


def test_review_models_never_map_a_family_to_itself():
    assert area.AutoReviewSettings(review_models=TABLE).review_models == TABLE
    with pytest.raises(ValueError, match="another family"):
        area.AutoReviewSettings(review_models={"opus": "claude-opus-4", "": "opus"})
    with pytest.raises(ValueError, match="empty key"):
        area.AutoReviewSettings(review_models={"opus": "sonnet"})


def test_a_verdict_counts_only_for_exactly_its_head_from_the_owner_and_another_family():
    ok = said(area.comment_body(HEAD, "opus", "sonnet", "fine\nVERDICT: APPROVE ok", "APPROVE"))
    assert area.recorded_verdict([ok], HEAD, CREATORS, {"sonnet"}) == "APPROVE" and area.recorded_verdict([ok], "c" * 40, CREATORS, {"sonnet"}) == ""
    no = said(area.comment_body(HEAD, "opus", "sonnet", "bad", "REJECT"))
    assert area.recorded_verdict([ok, no], HEAD, CREATORS, {"sonnet"}) == "REJECT" and area.recorded_verdict([said("VERDICT: APPROVE")], HEAD, CREATORS, set()) == ""
    stranger = said(area.comment_body(HEAD, "opus", "sonnet", "fine", "APPROVE"), login="someone-else")
    assert area.recorded_verdict([stranger], HEAD, CREATORS, {"sonnet"}) == "" and area.recorded_verdict([{"body": ok["body"]}], HEAD, CREATORS, {"sonnet"}) == ""
    same = said(area.comment_body(HEAD, "claude-sonnet-5", "sonnet", "fine", "APPROVE"))
    assert area.recorded_verdict([same], HEAD, CREATORS, {"sonnet"}) == "" and area.recorded_verdict([same], HEAD, CREATORS, {"opus", "sonnet"}) == ""
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
    gh = FakeGh().on("pr view", view()).on("pr diff", DIFF).on("--model opus", "Looks justified.\nVERDICT: APPROVE the timeout matches the job").on("pr comment", "")
    out = invoke(runtime_for(tmp_path, gh), ["--format", "json", "review", "dispatch", "5", "--live"])
    done = area.Dispatched.model_validate_json(out.stdout)
    assert out.exit_code == 0 and done.posted and done.verdict == "APPROVE" and done.reviewer_model == "opus" and done.author_model == "sonnet"
    run = next(c for c in gh.calls if c[0] == "claude")
    assert run[run.index("--permission-mode") + 1] == "dontAsk" and run[run.index("--tools") + 1] == "" and "--allowedTools" not in run
    denied = next(a for a in run if a.startswith("--disallowedTools=")).removeprefix("--disallowedTools=").split(",")  # one argument: a list flag must not swallow the prompt
    assert run[-2] == "--" and run[-1].startswith("Independent review of PR #5") and "--disallowedTools" not in run
    assert {"Edit", "Write", "Bash"} <= set(denied) and "+timeout: 99" in run[-1]  # no tool at all: the diff is in the prompt
    posted = next(c for c in gh.calls if c[:3] == ["gh", "pr", "comment"])
    assert posted[3] == "5" and "--body-file" in posted


def test_a_rejection_exits_1_and_no_answer_posts_nothing(gated, tmp_path):
    gh = FakeGh().on("pr view", view()).on("pr diff", DIFF).on("--model opus", "Hides a failure.\nVERDICT: REJECT masks a red job").on("pr comment", "")
    assert invoke(runtime_for(tmp_path, gh), ["review", "dispatch", "5", "--live"]).exit_code == 1
    mute = FakeGh().on("pr view", view()).on("pr diff", DIFF).on("--model opus", "I cannot tell.")
    out = invoke(runtime_for(tmp_path, mute), ["review", "dispatch", "5", "--live"])
    assert out.exit_code == 0 and "no verdict" in out.stdout and not [c for c in mute.calls if c[1:3] == ["pr", "comment"]]


def test_a_recorded_verdict_is_reported_and_nothing_starts_again(gated, tmp_path):
    comment = said(area.comment_body(HEAD, "opus", "sonnet", "ok", "APPROVE"))
    gh = FakeGh().on("pr view", view(comments=[comment]))
    out = invoke(runtime_for(tmp_path, gh), ["review", "dispatch", "5", "--live"])
    assert "review on file: APPROVE" in out.stdout and not [c for c in gh.calls if c[0] == "claude"]


def test_a_closed_pr_is_refused(gated, tmp_path):
    out = invoke(runtime_for(tmp_path, FakeGh().on("pr view", view(state="MERGED"))), ["review", "dispatch", "5"])
    assert out.exit_code == 1 and "not open" in out.stderr


def runs_of(gh):
    return [c for c in gh.calls if c[0] == "claude"]


def test_a_review_without_a_verdict_is_retried_once_then_given_up_with_an_alarm(gated, tmp_path):
    gh = FakeGh().on("pr view", view()).on("pr diff", DIFF).on("--model opus", "no idea")
    rt = runtime_for(tmp_path, gh)
    for _ in range(2):
        assert "no verdict" in invoke(rt, ["review", "dispatch", "5", "--live"]).stdout
    third = invoke(rt, ["review", "dispatch", "5", "--live"])
    assert "given up: 2 reviews of this head ended without a verdict" in third.stdout and len(runs_of(gh)) == 2
    assert any(level == "alert" and "review:" in key for level, key, _ in rt.providers.notifier.sent)


def test_the_day_budget_stops_further_reviews(gated, tmp_path):
    gh = FakeGh().on("pr view", view()).on("pr diff", DIFF).on("--model opus", "no idea")
    rt = runtime_for(tmp_path, gh, review_max_per_day=1, review_max_attempts=5)
    invoke(rt, ["review", "dispatch", "5", "--live"])
    assert "budget of 1 reviews" in invoke(rt, ["review", "dispatch", "5", "--live"]).stdout and len(runs_of(gh)) == 1


def test_a_second_review_pass_is_refused_while_one_runs(gated, tmp_path):
    gh = FakeGh().on("pr view", view()).on("pr diff", DIFF).on("--model opus", "x")
    rt = runtime_for(tmp_path, gh)
    with rt.providers.store.lock(area.PASS_LOCK):
        out = invoke(rt, ["review", "dispatch", "5", "--live"])
    assert out.exit_code == 1 and "another review is running" in out.stderr and not runs_of(gh)


def test_an_unreadable_or_huge_diff_is_not_given_to_an_agent(gated, tmp_path):
    gh = FakeGh().on("pr view", view()).on("pr diff", "x" * (area.DIFF_LIMIT + 1))
    out = invoke(runtime_for(tmp_path, gh), ["review", "dispatch", "5", "--live"])
    assert "larger than the reviewer is given" in out.stdout and not runs_of(gh)


class Spy(FakeGh):
    """FakeGh that remembers the environment each call got."""

    def __init__(self):
        super().__init__()
        self.envs = []

    def run(self, argv, cwd=None, timeout=None, env=None):
        self.envs.append((list(argv), env))
        return super().run(argv, cwd, timeout, env)


def test_the_reviewer_gets_the_oauth_token_of_this_process(gated, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-abc")
    gh = Spy().on("pr view", view()).on("pr diff", DIFF).on("--model opus", "ok\nVERDICT: APPROVE fine").on("pr comment", "")
    out = invoke(runtime_for(tmp_path, gh), ["review", "dispatch", "5", "--live"])
    env = next(e for argv, e in gh.envs if argv[0] == "claude")
    assert out.exit_code == 0 and env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-abc" and "tok-abc" not in out.stdout + out.stderr


def test_an_expired_login_is_an_alarm_not_a_burned_attempt(gated, tmp_path):
    login = Result(1, "", "Invalid API key. Please run /login")
    gh = FakeGh().on("pr view", view()).on("pr diff", DIFF).on("--model opus", login)
    rt = runtime_for(tmp_path, gh, review_max_attempts=1)
    for _ in range(3):  # the single attempt is never used up
        out = invoke(rt, ["review", "dispatch", "5", "--live"])
        assert "headless review cannot authenticate" in out.stdout and "given up" not in out.stdout
    assert len(runs_of(gh)) == 3
    assert [k for level, k, _ in rt.providers.notifier.sent if level == "alert"] == ["review:auth"] * 3
