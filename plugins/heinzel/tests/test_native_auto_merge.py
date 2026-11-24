"""`sm queue auto`: every ready PR is queued or merged by itself; a gate-touching one only with a recorded review of its head.
`gh` is scripted; the queue state is a temporary file."""

from types import SimpleNamespace

import pytest
from test_native_auto_review import BASE, HEAD, said, view
from test_native_ci_triage import REPO, runtime_for

from steuermaschine.github.testing import FakeGh
from steuermaschine.testing import invoke
from steuermaschine_heinzel.native import auto_merge as area
from steuermaschine_heinzel.native import auto_review, queue
from steuermaschine_heinzel.native.status_lib import prtable

OWNER = REPO.split("/")[0]


def row(**over):
    base = dict(repo="heinzel", number=7, title="t", state="CLEAN", ci="ok", sonar="ok", threads=0, author=OWNER, head=HEAD)
    return prtable.PrRow(**{**base, **over})


def test_the_cheap_rules_name_what_is_missing():
    assert area.eligible(row()) == "" and area.eligible(row(sonar="-")) == ""  # the target branch decides whether Sonar is required
    assert "unknown" in area.eligible(row(head=""))
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


def rules(*names):
    return [{"type": "required_status_checks", "parameters": {"required_status_checks": [
        {"context": n, "integration_id": 12526 if n.startswith("Sonar") else 15368} for n in names]}}]


def run(name, app=15368, conclusion="SUCCESS"):
    return {"__typename": "CheckRun", "name": name, "status": "COMPLETED", "conclusion": conclusion, "checkSuite": {"app": {"databaseId": app}}}


def snapshot(checks, classic=None, branch="main", count=None):
    connection = {"nodes": checks, "totalCount": len(checks) if count is None else count}
    pr = {"state": "OPEN", "isDraft": False, "mergeStateStatus": "CLEAN", "headRefOid": HEAD, "baseRefOid": BASE, "baseRef": {"name": branch, "branchProtectionRule": classic},
          "commits": {"nodes": [{"commit": {"oid": HEAD, "statusCheckRollup": {"contexts": connection}}}]},
          "reviewThreads": {"nodes": [], "totalCount": 0}}
    return {"data": {"repository": {"pullRequest": pr}}}


@pytest.fixture
def world_of_prs(tmp_path, monkeypatch):
    def make(rows, gh, reasons=(), comments=(), moved="", **settings):
        monkeypatch.setattr(auto_review, "needs_review", lambda *a, **k: list(reasons))
        monkeypatch.setattr(prtable, "fetch", lambda g, o, name, now: ([r for r in rows if r.repo == name], ""))
        gh.on("pr view", {**view(comments=comments), "baseRefName": "main", "files": [], "headRefOid": moved or HEAD})
        for repo, names in {"steuermaschine": ["Gate review", "check"], "heinzel": ["CI OK", "SonarCloud Code Analysis"], "heinzel-plugins": ["CI OK"]}.items():
            gh.on(f"repos/{OWNER}/{repo}/rules/branches/main", [rules(*names)])
            gh.on(f"repo={repo} -F number=", snapshot([run(n, 12526 if n.startswith("Sonar") else 15368) for n in names]))
        table = {"status_pr_repos": ["heinzel", "steuermaschine"], "queue_state_file": str(tmp_path / "queue.json"), **settings}
        return runtime_for(tmp_path, gh, **table)

    return make


def merged_cmds(gh):
    return [c for c in gh.lines() if c.startswith("pr merge")]


def test_a_ready_pr_in_a_repo_without_a_queue_is_listed_then_merged_on_live(world_of_prs):
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="steuermaschine", number=9, sonar="-")], gh)
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
    old = said(auto_review.comment_body("c" * 40, "opus", "sonnet", "ok", "APPROVE"))
    rt = world_of_prs([row(repo="steuermaschine", number=9)], FakeGh().on("pr merge", ""), reasons=["x"], comments=[old])
    assert "waiting" in invoke(rt, ["queue", "auto", "--live"]).stdout
    bad = said(auto_review.comment_body(HEAD, "opus", "sonnet", "no", "REJECT"))
    rt = world_of_prs([row(repo="steuermaschine", number=9)], FakeGh().on("pr merge", ""), reasons=["x"], comments=[bad])
    assert "skipped (the independent review rejected" in invoke(rt, ["queue", "auto", "--live"]).stdout


def test_an_approved_head_is_merged(world_of_prs):
    ok = said(auto_review.comment_body(HEAD, "opus", "sonnet", "fine", "APPROVE"))
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="steuermaschine", number=9)], gh, reasons=["diff guard: timeout"], comments=[ok])
    assert "merged" in invoke(rt, ["queue", "auto", "--live"]).stdout and len(merged_cmds(gh)) == 1


def test_dispatch_starts_the_review_when_none_exists(world_of_prs):
    gh = FakeGh().on("pr merge", "").on("pr diff", "diff --git a/x b/x\n+x\n").on("--model opus", "ok\nVERDICT: APPROVE fine").on("pr comment", "")
    rt = world_of_prs([row(repo="steuermaschine", number=9)], gh, reasons=["diff guard: timeout"])
    out = invoke(rt, ["queue", "auto", "--live", "--dispatch"])
    assert "steuermaschine #9: merged" in out.stdout and any(c[0] == "claude" for c in gh.calls)
    assert invoke(rt, ["queue", "auto", "--dispatch"]).exit_code == 2


def test_a_queue_repo_pr_is_queued_with_the_review_pinned_to_its_head(world_of_prs, monkeypatch):
    seen = []
    monkeypatch.setattr(queue, "_changed", lambda action, argv, prs, after=None: seen.append(argv) or SimpleNamespace(exit=0, errors=[], lines=[]))
    ok = said(auto_review.comment_body(HEAD, "opus", "sonnet", "fine", "APPROVE"))
    rt = world_of_prs([row(number=7)], FakeGh(), reasons=["ci change"], comments=[ok])
    assert "heinzel #7: queued" in invoke(rt, ["queue", "auto", "--live"]).stdout
    assert seen == [["add", "--reviewed", f"review: sm-review comment on head {HEAD[:12]}", "--reviewed-head", HEAD, "7"]]
    seen.clear()
    rt = world_of_prs([row(number=7)], FakeGh())
    assert "would-queue" in invoke(rt, ["queue", "auto"]).stdout and seen == []
    invoke(rt, ["queue", "auto", "--live"])
    assert seen == [["add", "--reviewed", f"sm queue auto: no gate touched, ready at head {HEAD[:12]}", "--reviewed-head", HEAD, "7"]]  # every entry is pinned


def test_the_queue_hold_stops_the_whole_pass(world_of_prs):
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="steuermaschine", number=9)], gh)
    invoke(rt, ["queue", "hold", "--reason", "release blocker"])
    out = invoke(rt, ["queue", "auto", "--live"])
    assert out.stdout.startswith("nothing done: the merge queue is held") and not gh.writes()


def test_a_plugins_pr_is_pinned_to_the_head_too(world_of_prs, monkeypatch):
    seen = []
    monkeypatch.setattr(queue, "_changed", lambda action, argv, prs, after=None: seen.append(argv) or SimpleNamespace(exit=0, errors=[], lines=[]))
    rt = world_of_prs([row(repo="heinzel-plugins", number=3)], FakeGh(), status_pr_repos=["heinzel-plugins"])
    assert "heinzel-plugins #3: queued" in invoke(rt, ["queue", "auto", "--live"]).stdout
    assert seen == [["add", "--reviewed", f"sm queue auto: no gate touched, ready at head {HEAD[:12]}", "--reviewed-head", HEAD, "plugins", "3"]]


def test_checks_read_for_another_head_are_not_trusted(world_of_prs):
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="steuermaschine", number=9)], gh, moved="d" * 40)
    assert invoke(rt, ["queue", "auto", "--live"]).stdout.strip() == "no open pull request is ready" and not gh.writes()


def test_a_pass_starts_at_most_review_max_per_pass_reviews(world_of_prs):
    gh = FakeGh().on("pr merge", "").on("pr diff", "diff --git a/x b/x\n+x\n").on("--model opus", "no idea")
    rows = [row(repo="steuermaschine", number=n) for n in (9, 10, 11)]
    rt = world_of_prs(rows, gh, reasons=["diff guard: timeout"], review_max_per_pass=2)
    out = invoke(rt, ["queue", "auto", "--live", "--dispatch"]).stdout
    assert len([c for c in gh.calls if c[0] == "claude"]) == 2 and "2 reviews were started in this pass" in out


def test_a_second_pass_is_refused_while_one_runs(world_of_prs):
    rt = world_of_prs([row(repo="steuermaschine", number=9)], FakeGh())
    with rt.providers.store.lock(area.PASS_LOCK):
        out = invoke(rt, ["queue", "auto", "--live"])
    assert out.exit_code == 1 and "another `sm queue auto` pass is running" in out.stderr


def test_a_pr_of_anyone_else_is_never_acted_on(world_of_prs):
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="steuermaschine", number=9, author="a-stranger"), row(repo="steuermaschine", number=10, author=OWNER.upper())], gh)
    out = invoke(rt, ["queue", "auto", "--live"]).stdout
    assert "#9" not in out and "steuermaschine #10: merged" in out and [c for c in gh.lines() if c.startswith("pr merge")] == [
        f"pr merge 10 -R {OWNER}/steuermaschine --squash --match-head-commit {HEAD}"]


def test_no_login_ends_the_reviews_of_the_pass(world_of_prs):
    from steuermaschine.protocols import Result

    gh = FakeGh().on("pr diff", "diff --git a/x b/x\n+x\n").on("--model opus", Result(1, "", "Please run /login"))
    rows = [row(repo="steuermaschine", number=n) for n in (9, 10, 11)]
    rt = world_of_prs(rows, gh, reasons=["diff guard: timeout"])
    out = invoke(rt, ["queue", "auto", "--live", "--dispatch"]).stdout
    assert len([c for c in gh.calls if c[0] == "claude"]) == 1 and out.count("cannot authenticate") == 1 and "reviews were started in this pass" in out

@pytest.mark.parametrize(("checks","why"), [
    ([run("check")], "Gate review (app 15368)"),
    ([run("check", conclusion="FAILURE"), run("Gate review")], "check: FAILURE"),
    ([run("check"), run("Gate review", 987)], "Gate review (app 15368)"),
    ([run("check"), {"__typename": "StatusContext", "context": "Gate review", "state": "SUCCESS"}], "Gate review (app 15368)"),
    ([run("check"), run("Gate review", conclusion="SKIPPED")], "Gate review (app 15368)"),
    ([run("check"), run("Gate review"), run("Focused optional", conclusion="FAILURE")], "Focused optional: FAILURE"),
])
def test_actual_required_integrations_and_every_reported_failure_block_the_merge(world_of_prs, checks, why):
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="steuermaschine", sonar="-")], gh)
    gh.on("repo=steuermaschine -F number=", snapshot(checks))
    out = invoke(rt, ["queue", "auto", "--live"])
    assert why in out.stdout and not merged_cmds(gh)


def test_core_still_requires_its_sonar_integration(world_of_prs):
    gh = FakeGh()
    rt = world_of_prs([row(sonar="-")], gh)
    gh.on("repo=heinzel -F number=", snapshot([run("CI OK"), run("SonarCloud Code Analysis", 15368)]))
    assert "SonarCloud Code Analysis (app 12526)" in invoke(rt, ["queue", "auto"]).stdout


@pytest.mark.parametrize("answer", [403, [], [{"type": "required_status_checks", "parameters": {}}]])
def test_unreadable_or_empty_rules_refuse(world_of_prs, answer):
    gh = FakeGh()
    rt = world_of_prs([row(repo="steuermaschine", sonar="-")], gh)
    gh.on(f"repos/{OWNER}/steuermaschine/rules/branches/main", answer)
    assert "waiting" in invoke(rt, ["queue", "auto", "--live"]).stdout and not gh.writes()


def test_unreported_classic_required_check_or_incomplete_head_refuses(world_of_prs):
    gh = FakeGh()
    rt = world_of_prs([row(repo="steuermaschine", sonar="-")], gh)
    checks = [run("Gate review"), run("check")]
    classic = {"requiresStatusChecks": True, "requiredStatusChecks": [{"context": "classic", "app": {"databaseId": 154}}]}
    gh.on("repo=steuermaschine -F number=", snapshot(checks, classic=classic))
    assert "classic (app 154)" in invoke(rt, ["queue", "auto"]).stdout
    gh.on("repo=steuermaschine -F number=", snapshot(checks, count=3))
    assert "incomplete GitHub inventory" in invoke(rt, ["queue", "auto"]).stdout


def test_rules_follow_the_pr_target_and_all_pages(world_of_prs):
    gh = FakeGh()
    rt = world_of_prs([row(repo="steuermaschine", sonar="-")], gh)
    gh.on("pr view", {**view(), "baseRefName": "release/next"})
    gh.on("repo=steuermaschine -F number=", snapshot([run("check"), run("Gate review")], branch="release/next"))
    gh.on(f"repos/{OWNER}/steuermaschine/rules/branches/release%2Fnext", [rules("check"), rules("release acceptance")])
    assert "release acceptance (app 15368)" in invoke(rt, ["queue", "auto"]).stdout
    assert not any("rules/branches/main" in command for command in gh.lines())


@pytest.mark.parametrize("changed", ["headRefOid", "baseRefOid", "baseRef"])
def test_a_retargeted_or_updated_pr_is_refused_after_review(world_of_prs, changed):
    gh = FakeGh()
    rt = world_of_prs([row(repo="steuermaschine", sonar="-")], gh)
    late = snapshot([run("check"), run("Gate review")])
    late["data"]["repository"]["pullRequest"][changed] = {"name": "other"} if changed == "baseRef" else "e" * 40
    gh.on("repo=steuermaschine -F number=", late)
    assert "changed while reading checks" in invoke(rt, ["queue", "auto", "--live"]).stdout and not gh.writes()
