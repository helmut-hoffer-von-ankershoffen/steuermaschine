"""`sm queue auto`: every ready PR is queued or merged by itself; a gate-touching one only with a recorded review of its head.
`gh` is scripted; the queue state is a temporary file."""

import json
from types import SimpleNamespace

import pytest
from test_native_auto_review import BASE, HEAD, said, view
from test_native_ci_triage import REPO, runtime_for

from steuermaschine.github.testing import FakeGh
from steuermaschine.testing import invoke
from steuermaschine_heinzel.native import auto_merge as area
from steuermaschine_heinzel.native import auto_review, queue
from steuermaschine_heinzel.native.queue_lib import auto_arm, hzlib
from steuermaschine_heinzel.native.status_lib import prtable

OWNER = REPO.split("/")[0]


def row(**over):
    base = dict(repo="heinzel", number=7, title="t", state="CLEAN", ci="ok", sonar="ok", threads=0, author=OWNER, head=HEAD)
    return prtable.PrRow(**{**base, **over})


def test_the_cheap_rules_name_what_is_missing():
    assert area.eligible(row()) == "" and area.eligible(row(sonar="-")) == "no Sonar check"
    assert "unknown" in area.eligible(row(head=""))
    assert area.eligible(row(state="DRAFT")) == "draft" and "BLOCKED" in area.eligible(row(state="BLOCKED"))
    assert area.eligible(row(ci="FAIL 1")) == "CI FAIL 1" and area.eligible(row(ci="running 3")) == "CI running 3"
    assert area.eligible(row(sonar="FAIL")) == "Sonar FAIL" and "2 unresolved" in area.eligible(row(threads=2))
    assert area.eligible(row(state="BEHIND")) == ""


@pytest.mark.parametrize("name", ["heinzel", "heinzel-plugins", "steuermaschine"])
def test_review_policy_is_limited_to_the_exact_owner_and_repository(name):
    assert not hzlib.requires_independent_review(f"{OWNER}/{name}")
    assert hzlib.requires_independent_review(f"another/{name}")
    assert hzlib.requires_independent_review(f"{OWNER}/{name}-test")


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
        monkeypatch.setattr(auto_review, "needs_review", lambda *a, **k: [r for r in reasons if r.startswith("diff guard:")] if k.get("errors_only") else list(reasons))
        monkeypatch.setattr(prtable, "fetch", lambda g, o, name, now: ([r for r in rows if r.repo == name], ""))
        gh.on("pr view", {**view(comments=comments), "baseRefName": "main", "files": [], "headRefOid": moved or HEAD})
        for repo, names in {"heinzel-management": ["Gate review", "check"], "steuermaschine": ["Gate review", "check"],
                            "heinzel": ["CI OK", "SonarCloud Code Analysis"], "heinzel-plugins": ["CI OK", "SonarCloud Code Analysis"]}.items():
            gh.on(f"repos/{OWNER}/{repo}/rules/branches/main", [rules(*names)])
            checks = [run(n, 12526 if n.startswith("Sonar") else 15368) for n in names]
            if repo in ("heinzel-management", "steuermaschine"):
                checks.append(run("SonarCloud Code Analysis", 12526))
            gh.on(f"repo={repo} -F number=", snapshot(checks))
        table = {"status_pr_repos": ["heinzel", "steuermaschine"], "queue_state_file": str(tmp_path / "queue.json"), **settings}
        return runtime_for(tmp_path, gh, **table)

    return make


def merged_cmds(gh):
    return [c for c in gh.lines() if c.startswith("pr merge")]


def test_a_ready_pr_in_a_repo_without_a_queue_is_listed_then_merged_on_live(world_of_prs):
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="steuermaschine", number=9)], gh)
    assert invoke(rt, ["queue", "auto"]).stdout.strip() == "steuermaschine #9: would-merge" and not gh.writes()
    out = invoke(rt, ["queue", "auto", "--live"])
    assert "steuermaschine #9: merged" in out.stdout and merged_cmds(gh) == [f"pr merge 9 -R {OWNER}/steuermaschine --squash --match-head-commit {HEAD}"]


def test_live_selection_can_act_only_on_the_named_repository_and_pr(world_of_prs):
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(number=9), row(repo="steuermaschine", number=10), row(repo="steuermaschine", number=9)], gh)
    out = invoke(rt, ["queue", "auto", "--repo", f"{OWNER}/steuermaschine", "--pr", "9", "--live"])
    assert out.stdout.strip() == "steuermaschine #9: merged"
    assert merged_cmds(gh) == [f"pr merge 9 -R {OWNER}/steuermaschine --squash --match-head-commit {HEAD}"]
    assert all("pr view 10" not in call and "repo=heinzel " not in call for call in gh.lines())


@pytest.mark.parametrize("hold_kind", ["owned", "incident", "missing receipt", "wrong token", "wrong version", "extra hold", "symlink", "duplicate keys", "missing file", "missing directory"])
def test_release_and_incident_holds_block_direct_tooling_merges(world_of_prs, tmp_path, monkeypatch, hold_kind):
    gh = FakeGh().on("pr merge", "")
    comment = said(auto_review.comment_body(HEAD, "opus", "sonnet", "ok", "APPROVE"))
    rt = world_of_prs([row(repo="steuermaschine", number=9)], gh, reasons=["workflow"], comments=[comment])
    state = tmp_path / "merge-state"
    monkeypatch.setattr(hzlib.statedir, "path", lambda name: state / name)
    directory = state / "release-holds"
    directory.mkdir(parents=True)
    token, version = "a" * 32, "0.9.55"
    held = directory / token
    held.write_text(version)
    receipt = state / "release.json"
    data = {"version": version, "freeze_token": token, "merge_route": "reviewed-release-queue"}
    if hold_kind != "missing receipt":
        receipt.write_text(json.dumps(data))
    if hold_kind == "incident":
        (state / "merge-freeze").write_text("incident")
    elif hold_kind == "wrong token":
        receipt.write_text(json.dumps(data | {"freeze_token": "b" * 32}))
    elif hold_kind == "wrong version":
        held.write_text("0.9.56")
    elif hold_kind == "extra hold":
        (directory / ("b" * 32)).write_text(version)
    elif hold_kind == "symlink":
        held.unlink()
        held.symlink_to(receipt)
    elif hold_kind in ("missing file", "missing directory"):
        held.unlink()
        if hold_kind == "missing directory":
            directory.rmdir()
    elif hold_kind == "duplicate keys":
        receipt.write_text(f'{{"version":"0.9.56","version":"{version}","freeze_token":"{token}"}}')
    out = invoke(rt, ["queue", "auto", "--repo", f"{OWNER}/steuermaschine", "--pr", "9", "--live"])
    assert "waiting (merge freeze is on)" in out.stdout and not gh.writes()


def test_direct_merge_observes_an_incident_started_during_verification(world_of_prs, tmp_path, monkeypatch):
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="steuermaschine", number=9)], gh)
    original = area.required_gap
    monkeypatch.setattr(hzlib.statedir, "path", lambda name: tmp_path / name)

    def new_incident(*args):
        result = original(*args)
        (tmp_path / "merge-freeze").touch()
        return result

    monkeypatch.setattr(area, "required_gap", new_incident)
    assert "waiting (merge freeze is on)" in invoke(rt, ["queue", "auto", "--live"]).stdout
    assert not gh.writes()


def test_shared_arm_policy_holds_core_dependencies_and_tooling(tmp_path, monkeypatch):
    monkeypatch.setattr(hzlib.statedir, "path", lambda name: tmp_path / name)
    token, version = "a" * 32, "0.9.55"
    receipt = {"version": version, "freeze_token": token, "merge_route": "reviewed-release-queue"}
    (tmp_path / "release.json").write_text(json.dumps(receipt))
    directory = tmp_path / "release-holds"
    directory.mkdir()
    (directory / token).write_text(version)
    assert hzlib.freeze_scope(tmp_path) == "release"
    assert auto_arm.freeze_without_candidate(hzlib.CORE)
    assert auto_arm.freeze_without_candidate(hzlib.PLUGINS)
    assert auto_arm.freeze_without_candidate(f"{OWNER}/steuermaschine")
    assert auto_arm.freeze_without_candidate(f"{OWNER}/heinzel-management")

    (directory / token).unlink()
    assert hzlib.freeze_scope(tmp_path) == "uncertain"
    assert auto_arm.freeze_without_candidate(hzlib.CORE)
    assert auto_arm.freeze_without_candidate(hzlib.PLUGINS)
    assert auto_arm.freeze_without_candidate(f"{OWNER}/steuermaschine")
    assert auto_arm.freeze_without_candidate(f"{OWNER}/heinzel-management")


def test_an_incident_starting_during_owned_hold_verification_wins(monkeypatch):
    incident = iter((False, True))
    monkeypatch.setattr(hzlib, "incident_frozen", lambda *_: next(incident))
    monkeypatch.setattr(hzlib, "release_freeze_scope", lambda *_: "release")
    assert auto_arm.freeze_without_candidate(f"{OWNER}/steuermaschine") == "merge freeze is on"


@pytest.mark.parametrize("remove_directory", [False, True])
def test_hold_disappearing_while_read_is_uncertain(tmp_path, monkeypatch, remove_directory):
    token, version = "a" * 32, "0.9.55"
    (tmp_path / "release.json").write_text(json.dumps({"version": version, "freeze_token": token}))
    directory = tmp_path / "release-holds"
    directory.mkdir()
    hold = directory / token
    hold.write_text(version)
    original = hzlib.os.listdir

    def remove_after_listing(path):
        names = original(path)
        hold.unlink()
        if remove_directory:
            directory.rmdir()
        return names

    monkeypatch.setattr(hzlib.os, "listdir", remove_after_listing)
    assert hzlib.freeze_scope(tmp_path) == "uncertain"


@pytest.mark.parametrize("state, expected", [
    ("request", "uncertain"), ("pause", "open"), ("bad pause", "uncertain"),
    ("resume", "uncertain"), ("tag", "open"), ("drop pending archive", "uncertain"),
])
def test_missing_hold_needs_durable_transition(tmp_path, state, expected):
    token, version, at = "a" * 32, "0.9.55", "2026-11-25T12:00:00Z"
    base = {"version": version, "freeze_token": token, "merge_route": "reviewed-release-queue"}
    stamp = {"version": version, "token": token, "at": at}
    request = stamp | {"action": "pause-request", "pr": None, "head": None, "was_ready": False}
    pause = stamp | {"action": "pause", "pr": None, "head": None, "draft": True}
    events = {
        "request": [request], "pause": [request, pause], "bad pause": [request, pause | {"head": "a" * 40}],
        "resume": [request, pause, stamp | {"action": "resume"}],
    }
    if state in events:
        base["hold_events"] = events[state]
    elif state == "tag":
        base["freeze_lifted_at"] = at
    else:
        base["drop"] = stamp
    (tmp_path / "release.json").write_text(json.dumps(base))
    assert hzlib.freeze_scope(tmp_path) == expected
    if state == "drop pending archive":
        (tmp_path / "release.json").unlink()
        assert hzlib.freeze_scope(tmp_path) == "open"


@pytest.mark.parametrize("args", [["--repo", f"{OWNER}/steuermaschine"], ["--pr", "9"], ["--repo", "other/steuermaschine", "--pr", "9"]])
def test_partial_or_unconfigured_selection_refuses_without_writes(world_of_prs, args):
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="steuermaschine", number=9)], gh)
    assert invoke(rt, ["queue", "auto", "--live", *args]).exit_code == 2 and not gh.writes()


def test_a_pr_that_is_not_ready_is_not_touched(world_of_prs):
    gh = FakeGh()
    rt = world_of_prs([row(repo="steuermaschine", number=9, ci="FAIL 1"), row(repo="steuermaschine", number=10, threads=1)], gh)
    out = invoke(rt, ["queue", "auto", "--live"])
    assert out.stdout.strip() == "no open pull request is ready" and not gh.writes()


def test_a_gate_touching_pr_waits_for_a_review_of_its_head(world_of_prs):
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="heinzel-management", number=9)], gh, status_pr_repos=["heinzel-management"], reasons=["diff guard: timeout"])
    assert "waiting (needs an independent review of this head" in invoke(rt, ["queue", "auto", "--live"]).stdout and not gh.writes()
    old = said(auto_review.comment_body("c" * 40, "opus", "sonnet", "ok", "APPROVE"))
    rt = world_of_prs([row(repo="heinzel-management", number=9)], FakeGh().on("pr merge", ""), status_pr_repos=["heinzel-management"], reasons=["x"], comments=[old])
    assert "waiting" in invoke(rt, ["queue", "auto", "--live"]).stdout
    bad = said(auto_review.comment_body(HEAD, "opus", "sonnet", "no", "REJECT"))
    rt = world_of_prs([row(repo="heinzel-management", number=9)], FakeGh().on("pr merge", ""), status_pr_repos=["heinzel-management"], reasons=["x"], comments=[bad])
    assert "skipped (the independent review rejected" in invoke(rt, ["queue", "auto", "--live"]).stdout


def test_policy_disables_dispatch_but_preserves_unreadable_guard_refusal(world_of_prs, monkeypatch):
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="steuermaschine")], gh, reasons=["diff guard: timeout"])
    assert "waiting (diff guard: timeout)" in invoke(rt, ["queue", "auto", "--live", "--dispatch"]).stdout
    assert not any(call[0] == "claude" for call in gh.calls)
    monkeypatch.setattr(auto_review, "needs_review", lambda *_, **kwargs: ["the diff guard could not read the diff"])
    out = invoke(rt, ["queue", "auto", "--live", "--dispatch"])
    assert "waiting (the diff guard could not read the diff)" in out.stdout
    assert not merged_cmds(gh)


def test_an_approved_head_is_merged(world_of_prs):
    ok = said(auto_review.comment_body(HEAD, "opus", "sonnet", "fine", "APPROVE"))
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="heinzel-management", number=9)], gh, status_pr_repos=["heinzel-management"], reasons=["diff guard: timeout"], comments=[ok])
    assert "merged" in invoke(rt, ["queue", "auto", "--live"]).stdout and len(merged_cmds(gh)) == 1


def test_dispatch_starts_the_review_when_none_exists(world_of_prs):
    gh = FakeGh().on("pr merge", "").on("pr diff", "diff --git a/x b/x\n+x\n").on("--model opus", "ok\nVERDICT: APPROVE fine").on("pr comment", "")
    rt = world_of_prs([row(repo="heinzel-management", number=9)], gh, status_pr_repos=["heinzel-management"], reasons=["diff guard: timeout"])
    out = invoke(rt, ["queue", "auto", "--live", "--dispatch"])
    assert "heinzel-management #9: merged" in out.stdout and any(c[0] == "claude" for c in gh.calls)
    assert invoke(rt, ["queue", "auto", "--dispatch"]).exit_code == 2


def test_a_queue_repo_pr_is_queued_with_the_review_pinned_to_its_head(world_of_prs, monkeypatch):
    seen = []
    monkeypatch.setattr(queue, "_changed", lambda action, argv, prs, after=None: seen.append(argv) or SimpleNamespace(exit=0, errors=[], lines=[]))
    ok = said(auto_review.comment_body(HEAD, "opus", "sonnet", "fine", "APPROVE"))
    rt = world_of_prs([row(number=7)], FakeGh(), reasons=["ci change"], comments=[ok])
    assert "heinzel #7: queued" in invoke(rt, ["queue", "auto", "--live"]).stdout
    assert seen == [["add", "--expected-head", HEAD, "7"]]
    seen.clear()
    rt = world_of_prs([row(number=7)], FakeGh())
    assert "would-queue" in invoke(rt, ["queue", "auto"]).stdout and seen == []
    invoke(rt, ["queue", "auto", "--live"])
    assert seen == [["add", "--expected-head", HEAD, "7"]]  # every entry is pinned


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
    assert seen == [["add", "--expected-head", HEAD, "plugins", "3"]]


def test_plugins_uses_both_enforced_integrations_and_holds_observed_optional_checks(world_of_prs):
    repo = "heinzel-plugins"
    gh = FakeGh()
    rt = world_of_prs([row(repo=repo, number=126)], gh, status_pr_repos=[repo])
    notes = [run("CI OK"), run("SonarCloud Code Analysis", 12526)]
    for checks, expected in [
        (notes, "would-queue"),
        (notes[:1], "SonarCloud Code Analysis (app 12526)"),
        ([*notes, run("Focused optional", conclusion="FAILURE")], "Focused optional: FAILURE"),
        ([*notes, run("Optional build") | {"status": "IN_PROGRESS"}], "Optional build: RUNNING"),
    ]:
        gh.on(f"repo={repo} -F number=", snapshot(checks))
        assert expected in invoke(rt, ["queue", "auto", "--repo", f"{OWNER}/{repo}", "--pr", "126"]).stdout
    assert not gh.writes()


def test_checks_read_for_another_head_are_not_trusted(world_of_prs):
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="steuermaschine", number=9)], gh, moved="d" * 40)
    assert invoke(rt, ["queue", "auto", "--live"]).stdout.strip() == "no open pull request is ready" and not gh.writes()


def test_a_pass_starts_at_most_review_max_per_pass_reviews(world_of_prs):
    gh = FakeGh().on("pr merge", "").on("pr diff", "diff --git a/x b/x\n+x\n").on("--model opus", "no idea")
    rows = [row(repo="heinzel-management", number=n) for n in (9, 10, 11)]
    rt = world_of_prs(rows, gh, status_pr_repos=["heinzel-management"], reasons=["diff guard: timeout"], review_max_per_pass=2)
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
    rows = [row(repo="heinzel-management", number=n) for n in (9, 10, 11)]
    rt = world_of_prs(rows, gh, status_pr_repos=["heinzel-management"], reasons=["diff guard: timeout"])
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
    rt = world_of_prs([row(repo="steuermaschine")], gh)
    gh.on("repo=steuermaschine -F number=", snapshot(checks))
    out = invoke(rt, ["queue", "auto", "--live"])
    assert why in out.stdout and not merged_cmds(gh)


def test_core_still_requires_its_sonar_integration(world_of_prs):
    gh = FakeGh()
    rt = world_of_prs([row()], gh)
    gh.on("repo=heinzel -F number=", snapshot([run("CI OK"), run("SonarCloud Code Analysis", 15368)]))
    assert "SonarCloud Code Analysis (app 12526)" in invoke(rt, ["queue", "auto"]).stdout


@pytest.mark.parametrize("answer", [403, [], [{"type": "required_status_checks", "parameters": {}}]])
def test_unreadable_or_empty_rules_refuse(world_of_prs, answer):
    gh = FakeGh()
    rt = world_of_prs([row(repo="steuermaschine")], gh)
    gh.on(f"repos/{OWNER}/steuermaschine/rules/branches/main", answer)
    assert "waiting" in invoke(rt, ["queue", "auto", "--live"]).stdout and not gh.writes()


def test_unreported_classic_required_check_or_incomplete_head_refuses(world_of_prs):
    gh = FakeGh()
    rt = world_of_prs([row(repo="steuermaschine")], gh)
    checks = [run("Gate review"), run("check")]
    classic = {"requiresStatusChecks": True, "requiredStatusChecks": [{"context": "classic", "app": {"databaseId": 154}}]}
    gh.on("repo=steuermaschine -F number=", snapshot(checks, classic=classic))
    assert "classic (app 154)" in invoke(rt, ["queue", "auto"]).stdout
    gh.on("repo=steuermaschine -F number=", snapshot(checks, classic=classic | {"requiredStatusChecks": []}))
    assert "unreadable classic required checks" in invoke(rt, ["queue", "auto"]).stdout
    gh.on("repo=steuermaschine -F number=", snapshot(checks, count=3))
    assert "incomplete GitHub inventory" in invoke(rt, ["queue", "auto"]).stdout


def test_rules_follow_the_pr_target_and_all_pages(world_of_prs):
    gh = FakeGh()
    rt = world_of_prs([row(repo="steuermaschine")], gh)
    gh.on("pr view", {**view(), "baseRefName": "release/next"})
    gh.on("repo=steuermaschine -F number=", snapshot([run("check"), run("Gate review")], branch="release/next"))
    gh.on(f"repos/{OWNER}/steuermaschine/rules/branches/release%2Fnext", [rules("check"), rules("release acceptance")])
    assert "release acceptance (app 15368)" in invoke(rt, ["queue", "auto"]).stdout
    assert not any("rules/branches/main" in command for command in gh.lines())


@pytest.mark.parametrize("changed", ["headRefOid", "baseRefOid", "baseRef"])
def test_a_retargeted_or_updated_pr_is_refused_after_review(world_of_prs, changed):
    gh = FakeGh()
    rt = world_of_prs([row(repo="steuermaschine")], gh)
    late = snapshot([run("check"), run("Gate review")])
    late["data"]["repository"]["pullRequest"][changed] = {"name": "other"} if changed == "baseRef" else "e" * 40
    gh.on("repo=steuermaschine -F number=", late)
    assert "changed while reading checks" in invoke(rt, ["queue", "auto", "--live"]).stdout and not gh.writes()


def test_steuermaschine_requires_local_sonar_in_addition_to_forge_rules(world_of_prs):
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="steuermaschine", number=84)], gh)
    gh.on("repo=steuermaschine -F number=", snapshot([run("check"), run("Gate review")]))
    out = invoke(rt, ["queue", "auto", "--repo", f"{OWNER}/steuermaschine", "--pr", "84", "--live"])
    assert "no Sonar check on the current head" in out.stdout and not gh.writes()


@pytest.mark.parametrize(("change", "reason"), [
    ({"errors": [{"message": "forbidden"}]}, "partial check data"),
    ({"state": "CLOSED"}, "no longer ready"),
    ({"isDraft": True}, "no longer ready"),
    ({"reviewThreads": {"nodes": [{"isResolved": False}], "totalCount": 1}}, "unresolved review threads"),
    ({"reviewThreads": {"nodes": [], "totalCount": 1}}, "incomplete GitHub inventory"),
    ({"checks": [{"__typename": "Unknown"}]}, "unknown check context type"),
    ({"transport": 403}, "cannot read head checks"),
])
def test_final_snapshot_must_be_complete_open_and_resolved(world_of_prs, change, reason):
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="steuermaschine")], gh)
    good = [run("check"), run("Gate review"), run("SonarCloud Code Analysis", 12526)]
    data = snapshot(change.get("checks", good))
    if "errors" in change:
        data.update(change)
    elif "checks" not in change:
        data["data"]["repository"]["pullRequest"].update(change)
    gh.on("repo=steuermaschine -F number=", change.get("transport", data))
    assert reason in invoke(rt, ["queue", "auto", "--live"]).stdout
    assert not gh.writes()


@pytest.mark.parametrize(("ruleset", "classic", "reason"), [
    (["invalid page"], None, "unreadable target branch rules"),
    ([[{"type": "deletion"}]], None, "no required checks found"),
    ([rules("check")], {"requiresStatusChecks": "true"}, "unreadable classic branch protection"),
    ([rules("check")], {"requiresStatusChecks": False}, "would-merge"),
    ([rules("check")], {"requiresStatusChecks": True, "requiredStatusChecks": [{"context": "check", "app": {"databaseId": "15368"}}]},
     "unreadable classic required check integration"),
    ([[{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "", "integration_id": 0}]}}]], None,
     "invalid required check or integration"),
])
def test_effective_rule_inventory_requires_valid_shapes(world_of_prs, ruleset, classic, reason):
    gh = FakeGh()
    rt = world_of_prs([row(repo="steuermaschine")], gh)
    gh.on(f"repos/{OWNER}/steuermaschine/rules/branches/main", ruleset)
    gh.on("repo=steuermaschine -F number=", snapshot([run("check"), run("SonarCloud Code Analysis", 12526)], classic=classic))
    assert reason in invoke(rt, ["queue", "auto"]).stdout and not gh.writes()


@pytest.mark.parametrize(("listed", "reason"), [(False, "could not list PRs"), (True, "selected open PR could not be identified")])
def test_targeted_pass_refuses_unavailable_or_absent_pr(world_of_prs, monkeypatch, listed, reason):
    gh = FakeGh()
    rt = world_of_prs([], gh)
    monkeypatch.setattr(prtable, "fetch", lambda *_: ([], "" if listed else "could not list PRs"))
    out = invoke(rt, ["queue", "auto", "--repo", f"{OWNER}/steuermaschine", "--pr", "84", "--live"])
    assert reason in out.stdout and not gh.writes()


@pytest.mark.parametrize("gate", ["freeze", "action hold", "unreadable PR"])
def test_direct_merge_stops_on_local_hold_or_unknown_head(world_of_prs, monkeypatch, gate):
    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="steuermaschine")], gh)
    reason = {"freeze": "merge freeze is on", "action hold": "action table holds this PR", "unreadable PR": "head unreadable"}[gate]
    if gate == "freeze":
        monkeypatch.setattr(area.hzlib, "incident_frozen", lambda *_: True)
    elif gate == "action hold":
        monkeypatch.setattr(area, "hold_reason", lambda *_: reason)
    else:
        gh.on("pr view", 403)
    assert reason in invoke(rt, ["queue", "auto", "--live"]).stdout
    assert not gh.writes()


def test_review_dispatch_refusal_never_advances_to_merge(world_of_prs, monkeypatch):
    from steuermaschine.engine import Refuse

    def refuse(**_):
        raise Refuse("review lane is held")

    gh = FakeGh().on("pr merge", "")
    rt = world_of_prs([row(repo="heinzel-management")], gh, status_pr_repos=["heinzel-management"], reasons=["protected source"])
    monkeypatch.setattr(auto_review, "review_dispatch", refuse)
    assert "review lane is held" in invoke(rt, ["queue", "auto", "--live", "--dispatch"]).stdout
    assert not gh.writes()
