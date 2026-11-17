"""The GitHub provider against a scripted `gh`: Forge, CIProvider, ForgeReader, CIRuns. No network, no real gh."""

import pytest

from steuermaschine.github import CIRuns, ForgeReader, GitHub, ProviderError
from steuermaschine.github.models import PROVIDER_MODELS
from steuermaschine.github.testing import FakeGh
from steuermaschine.protocols import CIProvider, Forge, Result

R = "o/r"
PR = {"number": 12, "title": "t", "state": "OPEN", "isDraft": False, "headRefName": "feat/x", "headRefOid": "a" * 40, "baseRefName": "main",
      "mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED", "reviewDecision": "", "url": "https://h/pull/12", "author": {"login": "bot"},
      "updatedAt": "2026-01-01T00:00:00Z", "autoMergeRequest": None}
RULES = [{"type": "deletion"}, {"type": "required_status_checks", "parameters": {"strict_required_status_checks_policy": False,
                                                                                  "required_status_checks": [{"context": "CI OK"}, {"context": "gate"}]}}]


def check(name, bucket, state=None):
    return {"name": name, "bucket": bucket, "state": state or bucket.upper(), "link": f"https://h/{name}", "workflow": "CI", "event": "pull_request",
            "startedAt": "2026-01-01T00:00:00Z", "completedAt": ""}


def world(pr=PR, checks=(), required=(), rules=RULES, behind=0, threads=0):
    fake = FakeGh()
    fake.on("pr view 12", pr)
    fake.on("pr checks 12", Result(8 if any(c["bucket"] == "pending" for c in checks) else 0, __import__("json").dumps(list(checks)), "") if checks
            else Result(1, "", "no checks reported on the 'feat/x' branch"))
    fake.on("pr checks 12 -R o/r --json name,state,bucket,link,workflow,event,startedAt,completedAt --required",
            [c for c in checks if c["name"] in required] or Result(1, "", "no required checks reported on the 'feat/x' branch"))
    fake.on("rules/branches/main", rules if rules is not None else 403)
    fake.on("protection/required_status_checks", Result(1, "", "gh: Branch not protected (HTTP 404)"))
    fake.on("compare/main...", {"behind_by": behind, "ahead_by": 1})
    fake.on("api graphql", {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [{"isResolved": False}] * threads + [{"isResolved": True}]}}}}})
    return fake, GitHub(fake, R)


def test_it_satisfies_the_core_protocols_and_the_reader_protocols():
    g = GitHub(FakeGh(), R)
    assert isinstance(g, Forge) and isinstance(g, CIProvider) and isinstance(g, ForgeReader) and isinstance(g, CIRuns)


def test_pr_by_number_by_branch_and_by_commit():
    fake, g = world()
    pr = g.pr("#12")
    assert (pr.number, pr.state, pr.branch, pr.head, pr.base, pr.mergeable, pr.merge_state, pr.author) == (12, "open", "feat/x", "a" * 40, "main", "mergeable", "blocked", "bot")
    fake.on("pr list -R o/r --head feat/x", [{**PR, "number": 3, "state": "CLOSED", "updatedAt": "2027"}, PR])
    assert g.pr("feat/x").number == 12  # the open one wins over a newer closed one
    fake.on("pr list -R o/r --head abcdef1", []).on("commits/abcdef1/pulls", [{"number": 12, "state": "open", "head": {"sha": "abcdef1234"}}, {"number": 9, "state": "open", "head": {"sha": "ffff"}}])
    assert g.pr("abcdef1").number == 12
    fake.on("pr list -R o/r --head nothing", [])
    assert g.pr("nothing").state == "none"


def test_pr_unknown_is_a_result_not_an_exception():
    g = GitHub(FakeGh().on("pr view", Result(1, "", "GraphQL: Could not resolve to a PullRequest with the number of 99.")).on("pr list", Result(124, "", "timed out after 30s: gh")), R)
    assert g.pr("99").state.startswith("unknown: GraphQL: Could not resolve") and g.pr("99").number == 99
    assert g.pr("some-branch").state == "unknown: gh timed out after 30s: gh"
    assert g.open_prs().state.startswith("unknown: ")
    assert GitHub(FakeGh().on("repo view", Result(127, "", "")), "").pr("1").state.startswith("unknown: no repository")


def test_open_prs_are_bounded():
    fake = FakeGh().on("pr list", [{**PR, "number": n} for n in range(1, 5)])
    got = GitHub(fake, R).open_prs(limit=3)
    assert [p.number for p in got.pulls] == [1, 2, 3] and got.truncated and "-L 4" in fake.lines()[0]


def test_checks_rollup_required_apart_from_optional():
    _, g = world(checks=[check("CI OK", "pass"), check("lint", "fail"), check("docs", "pending"), check("skip", "skipping")], required=["CI OK"])
    r = g.pr_checks(12)
    assert (r.state, r.scope, r.required, r.required_source) == ("pending", "required", ["CI OK", "gate"], "rules")
    assert (r.missing_required, r.red_required, r.pending_required, r.red_optional, r.pending_optional) == (["gate"], [], [], ["lint"], ["docs"])
    assert [c.name for c in r.checks if c.required] == ["CI OK"] and {c.name: c.state for c in r.checks}["skip"] == "skipped"


@pytest.mark.parametrize("checks,state", [
    ([check("CI OK", "pass"), check("gate", "pass"), check("lint", "fail")], "green"),  # an optional red does not turn it red
    ([check("CI OK", "fail"), check("gate", "pending")], "red"),
    ([check("CI OK", "cancel"), check("gate", "pass")], "red"),
    ([check("CI OK", "pass"), check("gate", "pending")], "pending"),
    ([check("CI OK", "pass"), check("CI OK", "fail"), check("gate", "pass")], "red"),  # the same context twice: red wins
])
def test_rollup_state_is_over_the_required_contexts(checks, state):
    _, g = world(checks=checks, required=["CI OK", "gate"])
    assert g.pr_checks(12).state == state


def test_rollup_without_required_contexts_is_over_all_checks_and_none_when_nothing_reported():
    _, g = world(checks=[check("a", "pass"), check("b", "fail")], rules=[])
    r = g.pr_checks(12)
    assert (r.state, r.scope, r.red_optional) == ("red", "all", ["b"])
    _, g = world(rules=[])
    assert g.pr_checks(12).state == "none"
    _, g = world()  # nothing reported, two contexts required
    r = g.pr_checks(12)
    assert r.state == "pending" and r.missing_required == ["CI OK", "gate"]


def test_rollup_falls_back_to_what_gh_marks_required_when_rules_are_unreadable():
    _, g = world(checks=[check("CI OK", "pass"), check("x", "pass")], required=["CI OK"], rules=None)
    r = g.pr_checks(12)
    assert (r.state, r.required, r.required_source) == ("green", ["CI OK"], "checks")


def test_rollup_unknown_when_the_checks_cannot_be_read():
    fake, g = world()
    fake.on("pr checks 12", Result(1, "", "gh: Server Error (HTTP 500)"))
    assert g.pr_checks(12).state == "unknown: Server Error (HTTP 500)"
    assert GitHub(FakeGh().on("pr view", 404), R).pr_checks(5).state.startswith("unknown: ")


def test_why_lists_every_visible_cause():
    pr = {**PR, "isDraft": True, "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY", "reviewDecision": "CHANGES_REQUESTED"}
    _, g = world(pr=pr, checks=[check("CI OK", "fail")], required=["CI OK"], behind=4, threads=2,
                 rules=[{"type": "required_status_checks", "parameters": {"strict_required_status_checks_policy": True, "required_status_checks": [{"context": "CI OK"}, {"context": "gate"}]}}])
    w = g.why(12)
    assert w.mergeable == "no" and w.behind_by == 4 and w.checks == "red"
    assert [r.kind for r in w.reasons] == ["draft", "conflicts", "missing_required", "red_required", "behind", "review", "threads"]
    assert "gate" in w.reasons[2].text and "CI OK" in w.reasons[3].text and "4 commit" in w.reasons[4].text


def test_why_yes_merged_blocked_computing_and_unknown():
    green = [check("CI OK", "pass"), check("gate", "pass")]
    _, g = world(pr={**PR, "mergeStateStatus": "CLEAN"}, checks=green, required=["CI OK", "gate"], behind=3)
    w = g.why(12)
    assert (w.mergeable, w.reasons, w.behind_by) == ("yes", [], 3)  # behind, but the rule is not strict
    _, g = world(pr={**PR, "state": "MERGED"})
    assert [r.kind for r in g.why(12).reasons] == ["merged"] and g.why(12).mergeable == "no"
    _, g = world(checks=green, required=["CI OK", "gate"], threads=1)
    assert [r.kind for r in g.why(12).reasons] == ["threads"]
    _, g = world(checks=green, required=["CI OK", "gate"])
    assert [r.kind for r in g.why(12).reasons] == ["blocked"]
    _, g = world(pr={**PR, "mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}, checks=green, required=["CI OK", "gate"])
    w = g.why(12)
    assert w.mergeable.startswith("unknown: GitHub has not computed") and [r.kind for r in w.reasons] == ["computing"]
    _, g = world(pr={**PR, "mergeStateStatus": "BEHIND"}, checks=green, required=["CI OK", "gate"], behind=2)
    assert [r.kind for r in g.why(12).reasons] == ["behind"]
    assert GitHub(FakeGh().on("pr view", 404), R).why(7).mergeable.startswith("unknown: ")
    fake, g = world(checks=green)
    fake.on("pr checks 12", Result(1, "", "gh: Server Error (HTTP 500)"))
    assert g.why(12).mergeable.startswith("unknown: the checks could not be read")


def test_branch_tip_found_missing_unknown():
    fake = FakeGh().on("git/ref/heads/main", {"object": {"sha": "c" * 40}}).on("git/ref/heads/gone", 404).on("git/ref/heads/feat/x", 500)
    g = GitHub(fake, R)
    assert (g.branch_tip("main").state, g.branch_tip("main").sha) == ("found", "c" * 40)
    assert g.branch_tip("gone").state == "missing" and g.branch_tip("feat/x").state.startswith("unknown: ")
    assert "git/ref/heads/feat/x" in fake.lines()[-1]


RUN = {"id": 7, "status": "in_progress", "conclusion": None, "name": "CI", "display_title": "fix", "head_branch": "feat/x", "head_sha": "a" * 40, "event": "pull_request",
       "run_attempt": 2, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:01:00Z", "html_url": "https://h/runs/7"}


def test_runs_by_commit_branch_and_status():
    fake = FakeGh().on("actions/runs?", {"total_count": 1, "workflow_runs": [RUN]})
    got = GitHub(fake, R).runs(commit="abc", branch="feat/x", status="in_progress", limit=5)
    assert fake.lines() == ["api repos/o/r/actions/runs?head_sha=abc&branch=feat%2Fx&status=in_progress&per_page=100&page=1"]
    r = got.runs[0]
    assert (got.state, r.id, r.status, r.conclusion, r.workflow, r.branch, r.head, r.event, r.attempt, r.url) == ("ok", "7", "in_progress", "", "CI", "feat/x", "a" * 40, "pull_request", 2, "https://h/runs/7")
    assert GitHub(FakeGh().on("actions/runs", 403), R).runs().state.startswith("unknown: ")
    assert GitHub(FakeGh().on("actions/runs", {"total_count": 9, "workflow_runs": [RUN] * 3}), R).runs(limit=2).truncated


def test_active_asks_every_unfinished_status_once_and_dedupes():
    fake = FakeGh().on("actions/runs", {"workflow_runs": []}).on("status=queued", {"workflow_runs": [{**RUN, "id": 8, "status": "queued", "created_at": "2025"}]})
    fake.on("status=in_progress", {"workflow_runs": [RUN]}).on("status=pending", {"workflow_runs": [RUN]})
    g = GitHub(fake, R)
    got = g.active()
    assert [r.id for r in got.runs] == ["7", "8"] and len(fake.calls) == 5 and [r.id for r in g.active_runs()] == ["7", "8"]
    fake.on("status=waiting", 500)
    assert g.active().state.startswith("unknown: ")
    with pytest.raises(ProviderError):
        g.active_runs()  # an empty list would read as "nothing runs"


JOBS = {"total_count": 3, "jobs": [
    {"id": 1, "name": "unit", "status": "completed", "conclusion": "failure", "html_url": "https://h/job/1", "started_at": "s", "completed_at": "c",
     "steps": [{"name": "Set up", "conclusion": "success"}, {"name": "Run tests", "conclusion": "failure"}]},
    {"id": 2, "name": "e2e", "status": "completed", "conclusion": "cancelled", "steps": []},
    {"id": 3, "name": "lint", "status": "completed", "conclusion": "success"}]}


def test_jobs_name_failed_and_cancelled_and_the_failed_steps():
    fake = FakeGh().on("actions/runs/7/jobs", JOBS)
    j = GitHub(fake, R).jobs("7")
    assert (j.state, j.total, j.failed, j.cancelled) == ("ok", 3, ["unit"], ["e2e"]) and j.jobs[0].failed_steps == ["Run tests"]
    assert "filter=latest" in fake.lines()[0]
    assert GitHub(FakeGh().on("jobs", 404), R).jobs("7").state == "unknown: Not Found (HTTP 404)"


def test_log_tail_through_the_jobs_api_strips_stamps_and_colour():
    log = "﻿2026-01-01T00:00:00.1234567Z first\n" + "".join(f"2026-01-01T00:00:01.0000000Z line {i}\n" for i in range(50)) + "2026-01-01T00:00:02.0000000Z \x1b[31mFAILED\x1b[0m test_x\n"
    fake = FakeGh().on("actions/jobs/1/logs", log)
    t = GitHub(fake, R).log_tail("1", lines=2)
    assert (t.state, t.total_lines, t.lines) == ("ok", 52, ["line 49", "FAILED test_x"])
    assert fake.lines() == ["api repos/o/r/actions/jobs/1/logs --allow-escape-sequences"]
    old = FakeGh().on("actions/jobs/1/logs", "a\nb\n").on("--allow-escape-sequences", Result(1, "", "unknown flag: --allow-escape-sequences"))
    assert GitHub(old, R).log_tail("1").lines == ["a", "b"]
    assert GitHub(FakeGh().on("logs", 404), R).log_tail("1").state == "unknown: Not Found (HTTP 404)"


def test_cancel_is_dry_by_default_and_live_posts_once():
    fake = FakeGh().on("actions/runs/7", RUN).on("actions/runs/7/cancel", {})
    g = GitHub(fake, R)
    dry = g.cancel_run("7")
    assert (dry.state, dry.live, dry.would_run) == ("dry_run", False, "gh api -X POST repos/o/r/actions/runs/7/cancel") and fake.writes() == []
    live = g.cancel_run("7", live=True)
    assert live.state == "cancelled" and fake.writes() == ["api -X POST repos/o/r/actions/runs/7/cancel"]


def test_cancel_refuses_a_finished_run_and_reports_a_failed_or_unreadable_one():
    fake = FakeGh().on("actions/runs/7", {**RUN, "status": "completed", "conclusion": "success"})
    assert GitHub(fake, R).cancel_run("7", live=True).state == "not_cancellable" and fake.writes() == []
    fake = FakeGh().on("actions/runs/7", RUN).on("actions/runs/7/cancel", Result(1, "", "gh: Bad Gateway (HTTP 502)"))
    assert GitHub(fake, R).cancel_run("7", live=True).state.startswith("unknown: Bad Gateway") and len(fake.writes()) == 1  # a write is not repeated
    fake = FakeGh().on("actions/runs/9", 404)
    assert GitHub(fake, R).cancel_run("9", live=True).state.startswith("unknown: ") and fake.writes() == []


@pytest.mark.parametrize(("reply", "expected"), [
    ("Cannot cancel a workflow run that has not been queued yet. (HTTP 409)", "not_queued"),
    ("Conflict (HTTP 409)", "unknown: Conflict (HTTP 409)"),
])
def test_cancel_distinguishes_github_not_queued_conflict(reply, expected):
    fake = FakeGh().on("actions/runs/7", RUN).on("actions/runs/7/cancel", Result(1, "", f"gh: {reply}"))
    result = GitHub(fake, R).cancel_run("7", live=True)
    assert result.state == expected and result.exit_code == 1
    assert len(fake.writes()) == 1
    if expected == "not_queued":
        assert "GitHub refused cancellation" in result.render()[0]


def test_variables_read_and_write_only_behind_the_flag():
    fake = FakeGh().on("actions/variables/A", {"name": "A", "value": "1", "updated_at": "u"}).on("actions/variables/B", 404).on("actions/variables/C", 500)
    fake.on("actions/variables?", {"total_count": 1, "variables": [{"name": "A", "value": "1"}]}).on("variable set", "")
    g = GitHub(fake, R)
    assert (g.variable("A").value, g.variable("B").state, g.get_variable("A"), g.get_variable("B")) == ("1", "missing", "1", None)
    assert g.variable("C").state.startswith("unknown: ") and [v.name for v in g.variables().variables] == ["A"]
    with pytest.raises(ProviderError):
        g.get_variable("C")
    assert g.write_variable("A", "secret-ish", live=True).state.startswith("refused: ")
    with pytest.raises(ProviderError, match="refused"):
        g.set_variable("A", "2")
    assert fake.writes() == []
    on = GitHub(fake, R, variable_writes=True)
    dry = on.write_variable("A", "secret-ish")
    assert dry.state == "dry_run" and "secret-ish" not in dry.would_run and fake.writes() == []
    on.set_variable("A", "2")
    assert fake.writes() == ["variable set A -R o/r --body 2"]


def test_create_pr_from_a_body_file_is_dry_adopts_an_open_pr_and_creates_live(tmp_path):
    body = tmp_path / "body.md"
    body.write_text("why")
    fake = FakeGh().on("pr list -R o/r --head feat/x", []).on("pr create", "https://h/pull/12\n")
    g = GitHub(fake, R)
    dry = g.create_pr("feat/x", "a title", str(body))
    assert dry.state == "dry_run" and dry.would_run == f"gh pr create -R o/r --base main --head feat/x --title 'a title' --body-file {body}" and fake.writes() == []
    assert g.create_pr("feat/x", "t", str(tmp_path / "missing.md"), live=True).state.startswith("unknown: the body file")
    fake.on("pr list -R o/r --head feat/x", [], [PR])
    made = g.create_pr("feat/x", "a title", str(body), base="dev", live=True)
    assert made.state == "created" and made.pr.number == 12 and len(fake.writes()) == 1 and "--base dev" in fake.writes()[0]
    again = g.create_pr("feat/x", "a title", str(body), live=True)
    assert again.state == "exists" and len(fake.writes()) == 1


def test_core_forge_methods():
    fake = FakeGh().on("pr list -R o/r --head feat/x", [PR]).on("pr list -R o/r --head none", []).on("pr list -R o/r --head bad", 500)
    g = GitHub(fake, R)
    assert g.pr_for_branch("feat/x").number == 12 and g.pr_for_branch("none") is None
    with pytest.raises(ProviderError):
        g.pr_for_branch("bad")
    fake.on("pr merge", "").on("git/refs", {})
    g.merge(12, "a" * 40)
    g.tag("v1", "b" * 40)
    assert fake.writes() == [f"pr merge 12 -R o/r --squash --match-head-commit {'a' * 40}", f"api -X POST repos/o/r/git/refs -f ref=refs/tags/v1 -f sha={'b' * 40}"]
    fake.on("pr merge", Result(1, "", "Head branch was modified"))
    with pytest.raises(ProviderError, match="not merged: Head branch was modified"):
        g.merge(12, "c" * 40)


def test_open_pr_writes_the_body_to_a_file_and_uses_the_default_branch():
    fake = FakeGh().on("pr list -R o/r --head feat/x", [], [PR]).on("repo view", "trunk\n").on("pr create", "")
    pr = GitHub(fake, R).open_pr("feat/x", "title", "the body")
    assert pr.number == 12 and "--base trunk" in fake.writes()[0] and "--body-file" in fake.writes()[0]


def test_checks_and_status_of_a_commit():
    runs = {"total_count": 4, "check_runs": [{"name": "unit", "status": "completed", "conclusion": "success"}, {"name": "e2e", "status": "completed", "conclusion": "cancelled", "html_url": "https://h/e2e"},
                                             {"name": "opt", "status": "completed", "conclusion": "skipped"}, {"name": "slow", "status": "in_progress", "conclusion": None}]}
    fake = FakeGh().on("commits/abc/check-runs", runs).on("commits/abc/status", {"statuses": [{"context": "sonar", "state": "success"}, {"context": "old", "state": "error"}]})
    g = GitHub(fake, R)
    assert {c.name: c.state for c in g.checks("abc")} == {"unit": "green", "e2e": "red", "opt": "skipped", "slow": "pending", "sonar": "green", "old": "red"}
    s = g.status("abc")
    assert (s.state, s.failing) == ("red", ("e2e", "old")) and g.failures("abc") == ["e2e: cancelled https://h/e2e", "old: error"]
    fake.on("commits/abc/check-runs", {"check_runs": [runs["check_runs"][0], runs["check_runs"][3]]}).on("commits/abc/status", {"statuses": []})
    assert g.status("abc").state == "pending"
    fake.on("commits/abc/check-runs", {"check_runs": [runs["check_runs"][0], runs["check_runs"][2]]})
    assert g.status("abc").state == "green"
    fake.on("commits/abc/check-runs", {"check_runs": []})
    assert g.status("abc").state == "pending" and "nothing has reported" in g.status("abc").detail
    fake.on("commits/abc/check-runs", 500)
    assert g.status("abc").state == "unknown" and "500" in g.status("abc").detail and g.failures("abc")[0].startswith("unknown: ")


def test_from_settings_and_every_model_has_a_schema():
    from steuermaschine.github.settings import GitHubSettings

    g = GitHub.from_settings(FakeGh(), GitHubSettings(repo="a/b", timeout_seconds=5, max_pages=1, variable_writes=True))
    assert (g.gh._repo, g.gh.timeout, g.gh.max_pages, g.variable_writes) == ("a/b", 5, 1, True)
    assert GitHub.from_settings(FakeGh(), {"repo": "a/b"}).merge_method == "squash"
    for name, model in PROVIDER_MODELS.items():
        assert model.model_json_schema()["title"] == name


def test_active_inventory_keeps_current_activity_before_old_backlog():
    old = [{**RUN, "id": i, "status": "queued", "created_at": "2025"} for i in range(55)]
    fake = FakeGh().on("actions/runs", {"workflow_runs": []})
    fake.on("status=queued", {"total_count": 55, "workflow_runs": old})
    fake.on("status=in_progress", {"workflow_runs": [{**RUN, "id": 999, "created_at": "2026"}]})
    result = GitHub(fake, R).active(limit=20)
    assert result.runs[0].id == "999" and result.truncated
    complete = GitHub(fake, R).active(limit=300)
    assert complete.runs[0].id == "999" and len(complete.runs) == 56 and not complete.truncated
