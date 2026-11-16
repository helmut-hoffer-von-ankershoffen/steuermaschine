"""ci triage, head merge-ready and mini loadguard, native. The triage and merge-ready cases are the assertions of the
old tools' test files (the reads are a scripted `gh` instead of a patched subprocess). Compared live on 2026-11-12
23:40 with the old read-only tools for five open pull requests: `prs` and `deadline --due 784 --due 797` print the
same lines (the commands after the arrow are sm commands). No test here starts a process or reads the network."""

import json
from pathlib import Path

import policy_kit as kit
import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github.testing import FakeGh
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel.native import ci_triage, loadguard, merge_ready
from steuermaschine_heinzel.native.ci_lib import triage as t
from steuermaschine_heinzel.plugin import plugin

REPO = "helmut-hoffer-von-ankershoffen/heinzel"
HEAD = "a" * 40
KNOWN = [("e2e web-1", "9kk", "")]
REGISTRY = '[[known]]\nmatch = "Known flaky"\nbead = "9kk"\nuntil = 2099-11-11\nnote = "Private text"\n'


def chk(n, c):
    return {"name": n, "status": "COMPLETED", "conclusion": c}


def runtime_for(tmp_path, commands, registry=REGISTRY, actions: str | None = "", **settings):
    research = tmp_path / "research"
    research.mkdir(exist_ok=True)
    (research / "known-failures.toml").write_text(registry)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    table = {"legacy_state_dir": str(state), "ci_known_failures_file": str(research / "known-failures.toml"), "ci_research_dir": str(research),
             "loadguard_state_file": str(tmp_path / "stopped.json"), "status_repos": {"heinzel": "", "heinzel-plugins": "plugins "}, **settings}
    if actions is not None:
        (tmp_path / "pr-actions.toml").write_text(actions)
        table["go_pr_actions_file"] = str(tmp_path / "pr-actions.toml")
    stack = stack_of(plugin)
    validated, _ = stack.validate({"heinzel": table}, Path("."))
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, validated, _providers=fake_providers(commands=commands))


# ------------------------------------------------------------------ triage prs and review
def test_states():
    assert "DEFERRED" in t.triage_pr(1, [chk("CI OK", "FAILURE"), chk("Build", "CANCELLED")], KNOWN)
    assert "KNOWN(9kk)" in t.triage_pr(2, [chk("End-to-end e2e web-1 (x)", "FAILURE")], KNOWN)
    assert "FAIL" in t.triage_pr(3, [chk("Lint", "FAILURE")], KNOWN) and "sm ci why 3" in t.triage_pr(3, [chk("Lint", "FAILURE")], KNOWN)
    assert "READY" in t.triage_pr(4, [chk("Lint", "SUCCESS")], KNOWN)
    assert t.triage_pr(5, [], KNOWN) == "PR 5 PENDING -> wait; no push (sm head push)"
    assert t.triage_pr(6, [chk("Focused web", "FAILURE"), chk("Lint", "SUCCESS")], KNOWN).startswith("PR 6 READY -> sm head ship 6")


def test_review(tmp_path):
    (tmp_path / "review-7.md").write_text("# R\n## Verdict: APPROVE\n")
    (tmp_path / "review-8.md").write_text("## Verdict: REQUEST CHANGES\n")
    assert t.review_line("7", tmp_path).startswith("7 CLEARED") and "--reviewed" in t.review_line("7", tmp_path)
    assert "NEEDS_REVIEW verdict REQUEST-CHANGES" in t.review_line("8", tmp_path)
    assert "no research/review-9.md" in t.review_line("9", tmp_path)


def test_triage_prs_and_review_through_the_command_tree(tmp_path):
    prs = [{"number": 7, "isDraft": False, "statusCheckRollup": [chk("Known flaky web", "FAILURE")]}, {"number": 8, "isDraft": True, "statusCheckRollup": []},
           {"number": 9, "isDraft": False, "statusCheckRollup": [chk("Lint", "FAILURE")]}]
    fake = FakeGh().on("pr list", prs)
    out = invoke(runtime_for(tmp_path, fake), ["ci", "triage", "prs"])
    assert out.stdout.splitlines() == ["PR 7 KNOWN(9kk) Known flaky web -> sm ci one rerun 7 --job NAME --wait (once), no code change",
                                       "PR 9 FAIL Lint -> sm ci why 9; open a flaky bead and add it to known-failures.toml, or fix"]
    report = ci_triage.TriagePrs.model_validate_json(invoke(runtime_for(tmp_path, fake), ["--format", "json", "ci", "triage", "prs"]).stdout)
    assert [(r.number, r.state) for r in report.rows] == [(7, "KNOWN"), (9, "FAIL")] and not fake.writes()
    assert invoke(runtime_for(tmp_path, FakeGh().on("pr list", [])), ["ci", "triage", "prs"]).stdout.strip() == "no open pull request that is not a draft"
    out = invoke(runtime_for(tmp_path, FakeGh().on("pr list", 502)), ["ci", "triage", "prs"])
    assert out.exit_code == 1 and "could not be read" in out.stderr
    (tmp_path / "research" / "review-7.md").write_text("## Verdict: APPROVE\n")
    out = invoke(runtime_for(tmp_path, FakeGh()), ["ci", "triage", "review", "7"])
    assert out.stdout.strip() == "7 CLEARED (APPROVE) -> sm head ship 7 --reviewed research/review-7.md"
    report = ci_triage.TriageReview.model_validate_json(invoke(runtime_for(tmp_path, FakeGh()), ["--format", "json", "ci", "triage", "review", "8"]).stdout)
    assert report.state == "NEEDS_REVIEW" and report.number == 8
    out = invoke(runtime_for(tmp_path, FakeGh(), ci_research_dir=None), ["ci", "triage", "review", "8"])
    assert out.exit_code == 1 and "ci_research_dir is not set" in out.stderr
    out = invoke(runtime_for(tmp_path, FakeGh(), ci_known_failures_file=None), ["ci", "triage", "deadline"])
    assert out.exit_code == 1 and "ci_known_failures_file" in out.stderr


# ------------------------------------------------------------------ triage deadline
def pr(number, *checks, draft=False):
    contexts = {"totalCount": len(checks), "nodes": list(checks)}
    rollup = {"contexts": contexts} if checks else None
    return {"number": number, "isDraft": draft, "headRefOid": HEAD,
            "commits": {"nodes": [{"commit": {"oid": HEAD, "statusCheckRollup": rollup}}]}}


def check(name, conclusion, *, status="COMPLETED", url=None):
    return {"__typename": "CheckRun", **chk(name, conclusion), "status": status, "detailsUrl": url}


class Source:
    """A scripted GitHub with one GraphQL answer the test fills in, and the command runner on it."""

    def __init__(self, tmp_path):
        self.tmp_path, self.connection = tmp_path, {"totalCount": 0, "nodes": []}
        self.payload = {"data": {"repository": {"pullRequests": self.connection}}}
        self.fake = FakeGh().on("api graphql", lambda argv: self.payload)
        self.registry = REGISTRY

    def run(self, *args):
        return invoke(runtime_for(self.tmp_path, self.fake, self.registry), ["ci", "triage", "deadline", *args])

    @property
    def calls(self):
        return self.fake.calls


@pytest.fixture
def source(tmp_path):
    return Source(tmp_path)


def test_deadline_counts_and_deduplicates_details(source):
    source.connection["nodes"] = [
        pr(7, check("CI OK", "FAILURE"), check("Build", "CANCELLED")),
        pr(9, check("Known flaky", "FAILURE")),
        pr(11, check("Known flaky", "FAILURE"), check("Lint\nerror", "FAILURE")),
        pr(13, check("Focused Linux", "FAILURE")),
        pr(15, check("Lint", "FAILURE"), draft=True),
        pr(17, {"__typename": "StatusContext", "context": "Sonar", "state": "ERROR"}),
    ]
    source.connection["totalCount"] = len(source.connection["nodes"])
    out = source.run("--due", "11", "--due", "11", "--due", "15", "--due", "19")
    assert out.exit_code == 0, out
    assert out.stdout.splitlines() == [
        "Core open non-draft PRs: genuine=2 deferred=0 known=1 cancellation-unverified=1 pending=1 ready=0",
        f"PR #11 {HEAD} GENUINE due 'Lint\\nerror'",
        f"PR #15 {HEAD} GENUINE due draft 'Lint'",
        "PR #19 NOT-OPEN due",
        f"PR #17 {HEAD} GENUINE 'Sonar'",
    ]
    assert len(source.calls) == 1


def test_deadline_bounds_detail_and_refreshes_same_head(source):
    source.connection.update(totalCount=2, nodes=[pr(9, check("Lint", "FAILURE")), pr(11)])
    assert source.run("--due", "11", "--limit", "1").stdout.splitlines()[1:] == [f"PR #11 {HEAD} PENDING due", "Details omitted: 1"]
    source.connection["nodes"][1] = pr(11, check("Lint", "SUCCESS"))
    assert source.run("--due", "11", "--limit", "1").stdout.splitlines()[1] == f"PR #11 {HEAD} READY due"
    assert len(source.calls) == 2


def test_deadline_reports_completed_failure_alongside_active_check(source):
    active = check("Test", None, status="IN_PROGRESS")
    failure = check("Lint", "FAILURE")
    source.connection.update(totalCount=3, nodes=[
        pr(7, failure, active),
        pr(9, check("CI OK", "STARTUP_FAILURE"), check("Build", "CANCELLED"), active),
        pr(11, check("CI OK", "FAILURE"), check("Build", "CANCELLED"), active),
    ])
    assert source.run("--due", "11").stdout.splitlines() == [
        "Core open non-draft PRs: genuine=2 deferred=0 known=0 cancellation-unverified=0 pending=1 ready=0",
        f"PR #11 {HEAD} PENDING due",
        f"PR #7 {HEAD} GENUINE 'Lint'",
        f"PR #9 {HEAD} GENUINE 'CI OK'",
    ]
    assert " PENDING " in t.triage_pr(7, [failure, active], KNOWN)


@pytest.mark.parametrize(("status", "conclusion", "state"), [
    ("COMPLETED", "SUCCESS", "READY"),
    ("COMPLETED", "SKIPPED", "READY"),
    ("COMPLETED", "NEUTRAL", "READY"),
    ("COMPLETED", "ACTION_REQUIRED", "GENUINE"),
    ("COMPLETED", "TIMED_OUT", "GENUINE"),
    ("COMPLETED", "STALE", "GENUINE"),
    ("COMPLETED", "CANCELLED", "CANCELLATION-UNVERIFIED"),
    ("REQUESTED", None, "PENDING"),
    ("QUEUED", None, "PENDING"),
    ("WAITING", None, "PENDING"),
    ("PENDING", None, "PENDING"),
])
def test_deadline_supported_check_outcomes(source, status, conclusion, state):
    source.connection.update(totalCount=1, nodes=[pr(11, check("Lint", conclusion, status=status))])
    assert source.run("--due", "11").stdout.splitlines()[1].startswith(f"PR #11 {HEAD} {state} due")


@pytest.mark.parametrize(("status", "conclusion"), [
    ("UNRECOGNIZED_STATUS", None),
    ("COMPLETED", "UNRECOGNIZED_CONCLUSION"),
    ("COMPLETED", None),
    ("IN_PROGRESS", "SUCCESS"),
])
def test_deadline_unknown_or_inconsistent_outcome_fails_closed(source, status, conclusion):
    source.connection.update(totalCount=2, nodes=[
        pr(9, check("Lint", "SUCCESS")),
        pr(11, check("Lint", conclusion, status=status)),
    ])
    out = source.run("--due", "11")
    assert out.exit_code == 1 and out.stdout == "" and "incomplete" in out.stderr
    assert "UNRECOGNIZED_" not in out.stderr


@pytest.mark.parametrize("incomplete", ["prs", "checks", "head", "graphql", "gh", "registry", "unknown-context"])
def test_deadline_fails_closed(source, incomplete):
    node = pr(11, check("Lint", "FAILURE"))
    source.connection.update(totalCount=1, nodes=[node])
    if incomplete == "prs":
        source.connection["totalCount"] = 2
    elif incomplete == "checks":
        node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["totalCount"] = 2
    elif incomplete == "head":
        node["headRefOid"] = "b" * 40
    elif incomplete == "graphql":
        source.payload["errors"] = [{"message": "Private text"}]
    elif incomplete == "gh":
        source.fake.on("api graphql", 502)
    elif incomplete == "unknown-context":
        node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"] = [{"__typename": "Other", "name": "x"}]
    else:
        source.registry = "[known]"
    out = source.run("--due", "11")
    assert out.exit_code == 1 and out.stdout == "" and "Private text" not in out.stderr
    if incomplete == "registry":
        assert "registry" in out.stderr and not source.calls
    else:
        assert "incomplete" in out.stderr


def test_deadline_recorded_and_unverified_cancellations(source, tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "ci-deferred.txt").write_text("42 branch CI\nnot-a-run x y\n7 short\n")
    base = "https://github.com/helmut-hoffer-von-ankershoffen/heinzel/actions/runs/"
    recorded = check("Build", "CANCELLED", url=base + "42/job/33")
    secret = check("Secret scan", "CANCELLED", url=base + "42877101519/job/130043301952")
    source.connection.update(totalCount=4, nodes=[
        pr(704, check("CI OK", "FAILURE"), secret),
        pr(2, recorded),
        pr(3, recorded, secret),
        pr(4, secret, check("Lint", "FAILURE")),
    ])
    out = source.run("--due", "704", "--due", "2", "--due", "3").stdout
    assert f"PR #704 {HEAD} CANCELLATION-UNVERIFIED due" in out
    assert f"PR #2 {HEAD} DEFERRED due" in out
    assert f"PR #3 {HEAD} CANCELLATION-UNVERIFIED due" in out
    assert f"PR #4 {HEAD} GENUINE 'Lint'" in out


@pytest.mark.parametrize("url", [None, "https://example.com/helmut-hoffer-von-ankershoffen/heinzel/actions/runs/42/job/33",
    "https://github.com/other/heinzel/actions/runs/42/job/33",
    "https://github.com/helmut-hoffer-von-ankershoffen/heinzel/actions/runs/420/job/33"])
def test_deadline_deferral_requires_exact_run_url(url):
    assert not t.deferred_check({"detailsUrl": url}, {"42"})


def test_deadline_canonical_focused_tests_are_not_diagnostic(source):
    source.connection.update(totalCount=1, nodes=[pr(11, check("CI OK", "FAILURE"), check("Focused tests (web)", "CANCELLED"))])
    assert f"PR #11 {HEAD} CANCELLATION-UNVERIFIED due" in source.run("--due", "11").stdout


def test_deadline_json_and_bad_options(source):
    source.connection.update(totalCount=1, nodes=[pr(11, check("Lint", "FAILURE"))])
    report = ci_triage.TriageDeadline.model_validate_json(invoke(runtime_for(source.tmp_path, source.fake), ["--format", "json", "ci", "triage", "deadline"]).stdout)
    assert report.counts == {"genuine": 1} and report.rows[0].failures == ["Lint"] and report.rows[0].due is False and report.read_only
    for args in (["--limit", "0"], ["--limit", "26"], ["--due", "0"]):
        out = source.run(*args)
        assert out.exit_code == 1 and "--limit must be 1 to 25" in out.stderr


# ------------------------------------------------------------------ head merge-ready
BASE = "b" * 40


def view(**kw):
    d = dict(state="OPEN", mergeStateStatus="CLEAN", files=[{"path": "a.py"}], comments=[{"body": "Verdict: ship"}], headRefOid=HEAD,
             baseRefOid=BASE, statusCheckRollup=[{"name": n, "conclusion": "SUCCESS"} for n in ("CI OK", "merge-gate", "coverage-floor")])
    d.update(kw)
    return d


def policy_gh(fake, repo=REPO):
    """Core's release policy at the base, through the contents API; a plugin repository has none, but branch rules."""
    for path, answer in kit.contents_routes(BASE, kit.files("new")).items():
        fake.on(f"api repos/{repo}/{path}", answer)
    fake.on("rules/branches/main", [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "CI OK"}]}}])
    return fake


@pytest.fixture(autouse=True)
def no_ci_change(monkeypatch):
    """ci_changes needs a Core clone; its own tests are in test_native_core_policy. Here: no CI change unless a test says so."""
    monkeypatch.setattr(merge_ready, "ci_reasons", lambda w, repo, base, head, files: tuple(
        f"{f['path']}: CI path" for f in files if f["path"].startswith(".github/")))


def ready(tmp_path, data, *args, actions: str | None = ""):
    return invoke(runtime_for(tmp_path, policy_gh(FakeGh().on("pr view", data)), actions=actions), ["head", "merge-ready", *args])


def test_go_and_refusals(tmp_path):
    for kw, word in [({}, None), ({"mergeStateStatus": "BEHIND"}, "mergeState"),
                     ({"files": [{"path": ".github/workflows/ci.yml"}]}, "ci.yml"),
                     ({"comments": []}, "verdict"),
                     ({"headRefOid": ""}, "checked head"),
                     ({"state": "MERGED"}, "state MERGED"),
                     ({"statusCheckRollup": [{"name": "CI OK", "conclusion": None}]}, "CI OK"),
                     ({"baseRefOid": ""}, "no exact base"),
                     ({"statusCheckRollup": [{"name": n, "conclusion": "SUCCESS"} for n in ("CI OK", "merge-gate")]}, "coverage-floor")]:
        out = ready(tmp_path, view(**kw), "1")
        assert (out.exit_code == 0 and out.stdout.strip() == "GO #1") if word is None else (out.exit_code == 1 and out.stdout.startswith("NO-GO #1: ") and word in out.stdout), (kw, out)


def test_a_review_file_counts_as_the_verdict_and_json_carries_the_checked_head(tmp_path):
    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "review-1-independent.md").write_text("ok")
    fake = policy_gh(FakeGh().on("pr view", view(comments=[])))
    out = invoke(runtime_for(tmp_path, fake), ["--format", "json", "head", "merge-ready", "1"])
    report = merge_ready.MergeReady.model_validate_json(out.stdout)
    assert out.exit_code == 0 and report.go and report.head == HEAD and report.repo == REPO and not fake.writes()
    assert [argv[1] for argv in fake.calls] == ["pr", "api"] and not fake.writes()  # it never merges


@pytest.mark.parametrize(("key", "repo"), [("669", REPO), ("plugins #669", "helmut-hoffer-von-ankershoffen/heinzel-plugins")])
def test_hold_refuses_green_pr(tmp_path, key, repo):
    actions = f'"{key}" = {{ hold = true }}\n'
    out = ready(tmp_path, view(), "669", "--repo", f"github.com/{repo.upper()}", actions=actions)
    assert out.exit_code == 1 and out.stdout.splitlines() == [f"NO-GO #669: held in pr-actions.toml: {key}"]
    other = REPO if "plugins" in repo else "helmut-hoffer-von-ankershoffen/heinzel-plugins"
    assert ready(tmp_path, view(), "669", "--repo", other, actions=actions).exit_code == 0


@pytest.mark.parametrize(("contents", "word"), [(None, "cannot read holds"), ("broken TOML", "cannot read holds"), ('"669" = { hold = "false" }', "invalid hold entry"),
                                                ('"nowhere #669" = { hold = true }', "unrecognized hold key")])
def test_hold_file_errors_are_a_no_go(tmp_path, contents, word):
    out = ready(tmp_path, view(), "669", actions=contents)
    assert out.exit_code == 1 and "NO-GO #669:" in out.stdout and word in out.stdout


def test_an_unreadable_pull_request_is_a_no_go(tmp_path):
    out = invoke(runtime_for(tmp_path, FakeGh().on("pr view", 502)), ["head", "merge-ready", "5"])
    assert out.exit_code == 1 and out.stdout.startswith("NO-GO #5: gh failed: ")


def test_split_key():
    short = {"": "o/core", "core": "o/core", "plugins": "o/plugins"}
    assert merge_ready.split_key("393", short) == ("o/core", "393", "393") and merge_ready.split_key(["plugins", "#38"], short) == ("o/plugins", "38", "plugins #38")
    with pytest.raises(ValueError):
        merge_ready.split_key("plugins x", short)


# ------------------------------------------------------------------ mini loadguard
def test_loadguard_lists_what_the_guard_paused(tmp_path):
    rt = runtime_for(tmp_path, FakeGh())
    assert invoke(rt, ["mini", "loadguard"]).stdout.strip() == "nothing paused"
    now = rt.providers.clock.now()
    (tmp_path / "stopped.json").write_text(json.dumps([{"pid": 4242, "cmd": "pytest tests -n 8", "pids": [4242, 4243, 4244], "at": now - 95}, "junk"]))
    out = invoke(rt, ["mini", "loadguard"])
    assert out.exit_code == 0 and out.stdout.strip() == "paused 4242 for 95s: pytest tests -n 8"
    report = loadguard.Loadguard.model_validate_json(invoke(rt, ["--format", "json", "mini", "loadguard"]).stdout)
    assert report.paused[0].processes == 3 and report.stop_above == 100.0 and report.resume_below == 40.0 and report.load_1m >= 0 and report.read_only
    (tmp_path / "stopped.json").write_text("{not json")
    assert invoke(rt, ["mini", "loadguard"]).stdout.strip() == "nothing paused"
    (tmp_path / "stopped.json").write_text('{"pid": 1}')
    assert loadguard.read_state(tmp_path / "stopped.json") == []
    out = invoke(runtime_for(tmp_path, FakeGh(), loadguard_state_file=None), ["mini", "loadguard"])
    assert out.exit_code == 1 and "loadguard_state_file is not set" in out.stderr


def test_merge_ready_without_a_research_directory_accepts_only_a_comment_verdict(tmp_path):
    rt = runtime_for(tmp_path, policy_gh(FakeGh().on("pr view", view(comments=[]))), ci_research_dir=None)
    assert "no review verdict on file" in invoke(rt, ["head", "merge-ready", "1"]).stdout
    assert invoke(runtime_for(tmp_path, policy_gh(FakeGh().on("pr view", view())), ci_research_dir=None), ["head", "merge-ready", "1"]).exit_code == 0


def test_without_a_policy_at_core_base_the_required_checks_are_unknown(tmp_path):
    out = invoke(runtime_for(tmp_path, FakeGh().on("pr view", view())), ["head", "merge-ready", "1"])
    assert out.exit_code == 1 and "required checks unknown: no reviewed release policy at base" in out.stdout


def test_the_ci_change_check_reads_core_and_another_repo_by_its_workflows(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.undo()
    world = SimpleNamespace(repo=REPO, core="")
    assert merge_ready.ci_reasons(world, REPO, BASE, HEAD, []) == ("no local Core clone (setting core_repo_dir) to judge the CI paths",)
    files = [{"path": ".github/workflows/ci.yml"}, {"path": "src/x.py"}]
    assert merge_ready.ci_reasons(world, "o/plugins", BASE, HEAD, files) == (".github/workflows/ci.yml: a workflow file",)
    seen = []
    monkeypatch.setattr(merge_ready.probes, "World", object, raising=False)
    from steuermaschine_heinzel.native.queue_lib import core_policy

    monkeypatch.setattr(core_policy, "ci_changes", lambda repo, base, head: seen.append((repo, base, head)) or ("x: CI path",))
    assert merge_ready.ci_reasons(SimpleNamespace(repo=REPO, core="/core"), REPO, BASE, HEAD, []) == ("x: CI path",)
    assert seen == [("/core", BASE, HEAD)]
