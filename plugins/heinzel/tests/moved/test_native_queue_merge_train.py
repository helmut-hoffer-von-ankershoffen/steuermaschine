import json
import sys

import pytest

CORE = "helmut-hoffer-von-ankershoffen/heinzel"


@pytest.fixture(autouse=True)
def isolated_release_holds(tmp_path, monkeypatch):
    """Merge tests must not wait on another coordinator's real release hold."""
    from steuermaschine_heinzel.native.queue_lib import hzlib

    monkeypatch.setattr(hzlib, "release_holds", lambda: tmp_path / "release-holds")


class FakeGh:
    """Answers `gh pr view` from a script of states and records the other calls."""

    def __init__(self, views, merge_ok=True):
        self.views, self.merge_ok, self.calls = list(views), merge_ok, []

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("pr", "view"):
            fields = args[args.index("--json") + 1]
            # only the call that reads mergeStateStatus advances the script; the state and poll calls peek
            view = self.views.pop(0) if "mergeStateStatus" in fields and len(self.views) > 1 else self.views[0]
            return 0, json.dumps({k: v for k, v in view.items() if k in fields.split(",")})
        if args[:2] == ("pr", "merge"):
            return (0 if self.merge_ok else 1), ""
        return 0, ""


def check(name, conclusion="SUCCESS", status="COMPLETED"):
    return {"name": name, "status": status, "conclusion": conclusion}


def train(tool, monkeypatch, fake, repo=CORE, queue_core=False):
    module = tool("hz-merge-train")
    monkeypatch.setattr(module, "gh", fake)
    monkeypatch.setattr(module.time, "sleep", lambda s: fake.calls.append(("sleep", s)))
    monkeypatch.setattr(module, "FREEZE", module.Path("/nonexistent/freeze"))
    module.train(repo, "7", queue_core=queue_core)
    return module


def merged_calls(fake):
    return [c for c in fake.calls if c[:2] == ("pr", "merge")]


def green(head="abcdef123456", status="CLEAN", extra=()):
    return {"state": "OPEN", "isDraft": False, "headRefOid": head, "mergeStateStatus": status, "statusCheckRollup": [check("CI OK"), *extra]}


def test_an_already_merged_pr_is_skipped(tool, monkeypatch, capsys):
    fake = FakeGh([{"state": "MERGED", "isDraft": False}])
    train(tool, monkeypatch, fake)
    assert capsys.readouterr().out == "#7 skip: MERGED\tfalse\n"
    assert not merged_calls(fake)


def test_a_draft_is_skipped(tool, monkeypatch, capsys):
    train(tool, monkeypatch, FakeGh([{"state": "OPEN", "isDraft": True}]))
    assert capsys.readouterr().out == "#7 skip: OPEN\ttrue\n"


def test_green_pr_is_squash_merged_at_that_exact_head(tool, monkeypatch, capsys):
    fake = FakeGh([green()])
    train(tool, monkeypatch, fake)
    assert capsys.readouterr().out == "#7 merged at abcdef12\n"
    assert merged_calls(fake) == [("pr", "merge", "7", "-R", CORE, "--squash", "--match-head-commit", "abcdef123456")]


@pytest.fixture
def merge_cli(tool, monkeypatch, tmp_path):
    """Use an executable fake gh with no fallback to the real CLI."""
    module = tool("hz-merge-train")
    monkeypatch.setattr(module, "FREEZE", tmp_path / "freeze")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("GH_DEBUG", "api")
    monkeypatch.setenv("GH_TOKEN", "synthetic-environment-credential")
    calls = tmp_path / "merge-call.json"
    head = "a" * 40

    def run(code, stdout, stderr, *, post_head=None):
        fake = tmp_path / "gh"
        fake.write_text(f"#!{sys.executable}\n" + f"""
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
calls = Path({str(calls)!r})
if args[:2] == ["pr", "view"]:
    fields = args[args.index("--json") + 1]
    view = {green(head=head)!r}
    if calls.exists() and {post_head is not None!r}:
        view["headRefOid"] = {post_head!r}
    print(json.dumps({{key: view[key] for key in fields.split(",") if key in view}}))
elif args[:2] == ["pr", "merge"]:
    assert os.environ["GH_DEBUG"] == ""
    assert not calls.exists(), "unexpected second merge attempt"
    calls.write_text(json.dumps(args))
    sys.stdout.write({stdout!r})
    sys.stderr.write({stderr!r})
    sys.exit({code!r})
else:
    raise AssertionError(args)
""")
        fake.chmod(0o700)
        result = module.train(CORE, "726", queue_core=True, expected_head=head)
        assert json.loads(calls.read_text()) == [
            "pr", "merge", "726", "-R", CORE, "--squash", "--match-head-commit", head,
        ]
        return result

    return run, module.MAX_MERGE_DIAGNOSTIC_BYTES


@pytest.mark.parametrize(("code", "stdout", "stderr", "reason"), [
    (1, "unrelated stdout", "GraphQL: base branch policy prohibits the merge", "GraphQL: base branch policy prohibits the merge"),
    (4, "authentication needed", " \n", "authentication needed"),
    (9, "", "", ""),
])
def test_failed_merge_retains_exit_code_and_preferred_text(merge_cli, capsys, code, stdout, stderr, reason):
    run, _ = merge_cli
    merged, detail = run(code, stdout, stderr)
    expected = f"state CLEAN; gh pr merge exited {code}" + (f": {reason}" if reason else "")
    assert not merged and detail == expected
    assert capsys.readouterr().out == f"#726 NOT merged, {expected}\n"


def test_failed_merge_diagnostic_is_redacted_single_line_and_byte_bounded(merge_cli, capsys):
    run, limit = merge_cli
    stderr = (
        "\x1b[31mGraphQL: merge rejected\x1b[0m\r\n"
        "Authorization: Bearer example-auth-header\n"
        "synthetic-environment-\x1b[32mcredential\x1b[0m github_pat_example_secret "
        "https://username:password@example.invalid/?access_token=example-query-secret "
        "\x1b]8;;https://internal-link.invalid\x07link\x1b]8;;\x07\x00\u202e "
        + "ä" * limit
    )
    merged, detail = run(1, "", stderr)
    diagnostic = detail.split("exited 1: ", 1)[1]
    assert not merged and diagnostic.startswith("GraphQL: merge rejected")
    assert diagnostic.isprintable() and diagnostic.endswith("...")
    assert len(diagnostic.encode("utf-8")) <= limit
    for secret in ("example-auth-header", "synthetic-environment-credential", "github_pat_example_secret",
                   "username:password", "example-query-secret", "internal-link.invalid"):
        assert secret not in detail
    assert capsys.readouterr().out.splitlines() == [f"#726 NOT merged, {detail}"]


def test_merge_failure_does_not_hide_changed_reviewed_head(merge_cli, capsys):
    run, _ = merge_cli
    merged, detail = run(1, "", "GraphQL: head changed", post_head="b" * 40)
    assert not merged
    assert detail == (
        "reviewed head changed; independent review and requeue required; "
        "gh pr merge exited 1: GraphQL: head changed"
    )
    assert capsys.readouterr().out == f"#726 NOT merged, {detail}\n"


def test_successful_merge_retains_existing_success_detail(merge_cli, capsys):
    run, _ = merge_cli
    assert run(0, "squashed", "non-failure notice") == (True, "aaaaaaaa")
    assert capsys.readouterr().out == "#726 merged at aaaaaaaa\n"


def test_the_advisory_review_check_is_ignored(tool, monkeypatch, capsys):
    fake = FakeGh([green(status="UNSTABLE", extra=[check("review", "FAILURE")])])
    train(tool, monkeypatch, fake)
    assert "merged" in capsys.readouterr().out


def test_a_red_check_is_reported_not_merged(tool, monkeypatch, capsys):
    fake = FakeGh([green(extra=[check("Lint", "FAILURE")])])
    train(tool, monkeypatch, fake)
    assert capsys.readouterr().out == "#7 NOT merged, state CLEAN: Lint\n"
    assert not merged_calls(fake)


def test_behind_is_updated_again_and_unknown_is_waited_out(tool, monkeypatch, capsys):
    fake = FakeGh([green(status="BEHIND"), green(status="UNKNOWN"), green()])
    train(tool, monkeypatch, fake)
    assert "merged" in capsys.readouterr().out
    assert len([c for c in fake.calls if c[:2] == ("pr", "update-branch")]) == 2
    assert ("sleep", 60) in fake.calls


def test_queued_behind_core_is_updated_and_never_merged(tool, monkeypatch, capsys):
    """The tree CI tested must be the tree the squash produces: a Core head behind main is updated, its CI runs again, it is requeued."""
    fake = FakeGh([green(status="BEHIND")])
    train(tool, monkeypatch, fake, queue_core=True)
    assert len([c for c in fake.calls if c[:2] == ("pr", "update-branch")]) == 1
    assert not merged_calls(fake)
    assert "behind main: the branch was updated" in capsys.readouterr().out


def test_queued_core_does_not_merge_red_or_conflicted_prs(tool, monkeypatch):
    for view in (green(status="BEHIND", extra=[check("Lint", "FAILURE")]), green(status="DIRTY")):
        fake = FakeGh([view])
        train(tool, monkeypatch, fake, queue_core=True)
        assert not merged_calls(fake)
        assert not [c for c in fake.calls if c[:2] == ("pr", "update-branch")]


def test_queued_core_does_not_retry_a_changed_head_after_merge_rejection(tool, monkeypatch):
    fake = FakeGh([green()], merge_ok=False)
    train(tool, monkeypatch, fake, queue_core=True)
    assert len(merged_calls(fake)) == 1
    assert "--match-head-commit" in merged_calls(fake)[0]


def test_queue_core_mode_does_not_change_non_core_updates(tool, monkeypatch):
    fake = FakeGh([green()])
    train(tool, monkeypatch, fake, repo="helmut-hoffer-von-ankershoffen/heinzel-sf", queue_core=True)
    assert len([c for c in fake.calls if c[:2] == ("pr", "update-branch")]) == 1


def test_a_repository_without_ci_ok_is_judged_on_all_its_checks(tool, monkeypatch, capsys):
    view = {**green(), "statusCheckRollup": [check("test"), check("lint")]}
    fake = FakeGh([view])
    train(tool, monkeypatch, fake, repo="helmut-hoffer-von-ankershoffen/heinzel-sf")
    assert "merged" in capsys.readouterr().out
    assert ("sleep", 45) not in fake.calls


def test_a_check_still_running_is_waited_for(tool):
    module = tool("hz-merge-train")
    assert not module.settled([check("a"), check("b", conclusion="", status="IN_PROGRESS")])
    assert not module.settled([])
    assert module.settled([check("a"), check("b")])


def test_the_freeze_file_holds_back_core_merges_only(tool, monkeypatch, tmp_path, capsys):
    freeze = tmp_path / "freeze"
    freeze.touch()
    fake = FakeGh([green()])
    module = tool("hz-merge-train")
    monkeypatch.setattr(module, "gh", fake)
    monkeypatch.setattr(module, "FREEZE", freeze)
    monkeypatch.setattr(module.time, "sleep", lambda s: freeze.unlink(missing_ok=True) if s == 60 else None)
    module.train(CORE, "7")
    assert ("pr", "merge") == merged_calls(fake)[0][:2] and not freeze.exists()


def test_the_tools_demand_a_private_repos_floor_once_main_has_the_job(monkeypatch):
    from steuermaschine_heinzel.native.queue_lib import hzlib

    sf = f"{hzlib.OWNER}/heinzel-sf"
    monkeypatch.setattr(hzlib, "gh", lambda *_: (0, "jobs:\n  coverage-floor:\n"))
    assert hzlib.tool_required_checks(sf) == {"coverage-floor"}
    assert hzlib.tool_required_checks(hzlib.CORE) == set()
    monkeypatch.setattr(hzlib, "gh", lambda *_: (0, "jobs:\n  check:\n"))
    assert hzlib.tool_required_checks(sf) == set()


@pytest.mark.parametrize("phase", ["initial", "checks"])
def test_diagnostic_head_pin_is_checked_before_and_after_waiting(tool, monkeypatch, phase):
    module = tool("hz-merge-train")
    fake = FakeGh([green()])
    original = fake.__call__

    def gh(*args):
        code, body = original(*args)
        if args[:2] == ("pr", "view"):
            fields = args[args.index("--json") + 1]
            if (phase == "initial" and "state" in fields) or (phase == "checks" and "mergeStateStatus" in fields):
                body = json.dumps({**json.loads(body), "headRefOid": "changed"})
        return code, body

    monkeypatch.setattr(module, "gh", gh)
    result, detail = module.train(CORE, "7", queue_core=True, expected_head="abcdef123456")
    assert not result and "independent review" in detail
    assert not merged_calls(fake)
    assert not any(c[:2] in (("pr", "update-branch"), ("run", "rerun")) for c in fake.calls)


@pytest.mark.parametrize("failure", ["CI OK", "merge-gate", "coverage-floor", "SonarCloud Code Analysis", "CodeQL"])
def test_diagnostic_train_keeps_required_check_failures(tool, monkeypatch, failure):
    from steuermaschine_heinzel.native.queue_lib import hzlib

    monkeypatch.setattr(hzlib, "advisory_checks", lambda repo: {"review"})
    module = tool("hz-merge-train")
    fake = FakeGh([green(extra=[check(failure, "FAILURE")])])
    monkeypatch.setattr(module, "gh", fake)
    result, _ = module.train(CORE, "7", queue_core=True, expected_head="abcdef123456")
    assert not result and not merged_calls(fake)


def test_diagnostic_train_pins_the_merge_and_honors_release_hold(tool, monkeypatch):
    from steuermaschine_heinzel.native.queue_lib import hzlib

    module = tool("hz-merge-train")
    fake = FakeGh([green()])
    held = [True]
    monkeypatch.setattr(module, "gh", fake)
    monkeypatch.setattr(hzlib, "merge_frozen", lambda *a: held[0])
    monkeypatch.setattr(module.time, "sleep", lambda s: (fake.calls.append(("sleep", s)), held.__setitem__(0, False)))
    result, _ = module.train(CORE, "7", queue_core=True, expected_head="abcdef123456")
    assert result
    assert ("sleep", 60) in fake.calls
    assert merged_calls(fake)[0][-2:] == ("--match-head-commit", "abcdef123456")


def test_reviewed_normal_merge_refuses_a_push_during_the_freeze_wait(tool, monkeypatch):
    from steuermaschine_heinzel.native.queue_lib import hzlib

    module = tool("hz-merge-train")
    expected_head, replacement_head = "1" * 40, "2" * 40
    fake = FakeGh([green(head=expected_head)])
    held = True
    actual_head = expected_head

    def release_hold_after_push(seconds):
        nonlocal held, actual_head
        assert seconds == 60
        actual_head = replacement_head
        held = False
        fake.views[:] = [green(head=actual_head)]

    def gh(*args):
        if args[:2] == ("pr", "merge"):
            fake.merge_ok = args[-2:] == ("--match-head-commit", actual_head)
        return fake(*args)

    monkeypatch.setattr(module, "gh", gh)
    monkeypatch.setattr(hzlib, "merge_frozen", lambda *_: held)
    monkeypatch.setattr(module.time, "sleep", release_hold_after_push)
    result, detail = module.train(CORE, "594", queue_core=True, expected_head=expected_head)
    assert not result and "reviewed head changed" in detail
    assert [c[-2:] for c in merged_calls(fake)] == [("--match-head-commit", expected_head)]


def test_the_rules_decide_what_is_advisory_and_unreadable_rules_hold_the_gate(monkeypatch):
    import json

    from steuermaschine_heinzel.native.queue_lib import hzlib

    rules = [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "merge-gate"}]}}]
    monkeypatch.setattr(hzlib, "_RULES", {})
    monkeypatch.setattr(hzlib, "gh", lambda *_: (1, ""))
    unread = hzlib.advisory_checks("o/r")
    assert {"review", "attempt", "Claude review"} == {n for n in ("review", "attempt", "Claude review", "coverage-floor", "merge-gate") if n in unread}
    monkeypatch.setattr(hzlib, "gh", lambda *_: (0, json.dumps(rules)))
    ruled = hzlib.advisory_checks("o/r")
    assert "coverage-floor" in ruled and "merge-gate" not in ruled and "review" in ruled


def test_flip_requires_proof_the_check_passed_on_a_pull_request(tool, monkeypatch):
    import json

    from steuermaschine_heinzel.native.queue_lib import hzlib

    gate = tool("hz-merge-gate")
    answers = {"pr": [{"headRefOid": "abc"}], "api": {"check_runs": [{"conclusion": "failure", "app": {"id": gate.ACTIONS_APP}}]}}
    monkeypatch.setattr(hzlib, "gh", lambda *a: (0, json.dumps(answers[a[0]])))
    assert not gate.reported_on_a_pull_request("r", "merge-gate")
    answers["api"]["check_runs"][0]["conclusion"] = "success"
    assert gate.reported_on_a_pull_request("r", "merge-gate")


def test_a_ruleset_write_that_drops_a_required_check_is_refused(tool, capsys):
    gate = tool("hz-merge-gate")
    data = {"id": 1, "rules": [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "x"}]}}]}
    assert gate.write_ruleset("r", data, {"CI OK"}) is False
    assert "refusing" in capsys.readouterr().err


def test_only_the_newest_run_of_each_check_counts():
    from steuermaschine_heinzel.native.queue_lib import hzlib

    rollup = [
        {"name": "merge-gate", "conclusion": "CANCELLED", "completedAt": "2026-11-02T06:22:15Z"},
        {"name": "merge-gate", "conclusion": "SUCCESS", "completedAt": "2026-11-02T06:22:45Z"},
        {"name": "CI OK", "conclusion": "SUCCESS", "completedAt": "2026-11-02T06:31:28Z"},
        {"name": "coverage-floor", "conclusion": "SUCCESS", "completedAt": "2026-11-02T06:26:20Z"},
        {"name": "coverage-floor", "conclusion": "CANCELLED", "completedAt": "2026-11-02T06:22:30Z"},
    ]
    latest = {c["name"]: c["conclusion"] for c in hzlib.latest_checks(rollup)}
    assert latest == {"merge-gate": "SUCCESS", "CI OK": "SUCCESS", "coverage-floor": "SUCCESS"}
    failed = [{"name": "CI OK", "conclusion": "FAILURE", "completedAt": "2026-11-02T07:00:00Z"}, {"name": "CI OK", "conclusion": "SUCCESS", "completedAt": "2026-11-02T06:00:00Z"}]
    assert [c["conclusion"] for c in hzlib.latest_checks(failed)] == ["FAILURE"]


def test_a_red_run_whose_only_failures_are_never_started_macos_jobs_counts_as_not_red(monkeypatch):
    import json

    from steuermaschine_heinzel.native.queue_lib import hzlib

    def jobs(rows):
        monkeypatch.setattr(hzlib, "gh", lambda *a: (0, json.dumps(rows)))

    cancelled = {"name": "Test (macos-latest, Python 3.12, unit)", "conclusion": "cancelled", "runner": "", "steps": 0}
    ok = {"name": "CI OK", "conclusion": "failure", "runner": "GitHub Actions 1", "steps": 3}
    jobs([cancelled, ok, {"name": "Lint", "conclusion": "success", "runner": "x", "steps": 9}])
    assert hzlib.only_never_started_macos_jobs_failed("o/r", 1)
    jobs([dict(cancelled, steps=4), ok])  # it started: that is a real result
    assert not hzlib.only_never_started_macos_jobs_failed("o/r", 1)
    jobs([dict(cancelled, name="Test (ubuntu-24.04, Python 3.12, unit)"), ok])
    assert not hzlib.only_never_started_macos_jobs_failed("o/r", 1)
    jobs([cancelled, dict(cancelled, name="Test (ubuntu-24.04)", conclusion="failure", runner="r", steps=5), ok])
    assert not hzlib.only_never_started_macos_jobs_failed("o/r", 1)
    jobs([])
    assert not hzlib.only_never_started_macos_jobs_failed("o/r", 1)


class ReadBack(FakeGh):
    """The merge call fails; the read-back of `state,mergeCommit` answers `after` (None: the read fails)."""

    def __init__(self, views, after):
        super().__init__(views, merge_ok=False)
        self.after = after

    def __call__(self, *args):
        if args[:2] == ("pr", "view") and args[args.index("--json") + 1] == "state,mergeCommit":
            self.calls.append(args)
            return (1, "") if self.after is None else (0, json.dumps(self.after))
        return super().__call__(*args)


@pytest.mark.parametrize(("after", "merged", "printed"), [
    ({"state": "MERGED", "mergeCommit": {"oid": "1" * 40}}, True, "#7 merged at abcdef12 (read back after gh pr merge exited 1)\n"),
    ({"state": "OPEN", "mergeCommit": None}, False, "#7 NOT merged"),
    (None, False, "#7 merge state unknown: gh pr merge exited 1; the read-back of its state failed\n"),
    ({}, False, "#7 merge state unknown"),
])
def test_a_failed_merge_reads_the_state_back_and_never_assumes(tool, monkeypatch, capsys, after, merged, printed):
    """Review F4: a dropped reply after a merge that landed is merged; an unreadable state is unknown, not needs work."""
    fake = ReadBack([green()], after)
    module = train(tool, monkeypatch, fake, queue_core=True)  # wires the fakes; prints the outcome
    out = capsys.readouterr().out
    assert out.startswith(printed), out
    assert ("pr", "view", "7", "-R", CORE, "--json", "state,mergeCommit") in fake.calls and len(merged_calls(fake)) == 1
    fake.calls.clear()
    merged_now, detail = module.train(CORE, "7", queue_core=True)
    assert merged_now is merged
    assert detail.startswith(module.MERGE_UNKNOWN) == (after in (None, {}))


# --- review F5: every gh call of queue, train and release is bounded --------------------------------------------------

def timing_out(seen):
    import subprocess

    def run(argv, *args, **kwargs):
        seen.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    return run


def test_the_queue_gh_call_is_bounded_and_a_timeout_is_a_refusal(monkeypatch, capsys):
    from steuermaschine_heinzel.native.queue_lib import edge

    hzlib = edge.fresh(f"{edge.PACKAGE}.queue_lib.hzlib")  # a copy without the conftest's failing gh stand-in
    seen = []
    monkeypatch.setattr(hzlib.edge, "run", timing_out(seen))
    assert hzlib.gh("pr", "view", "7", "-R", CORE) == (124, "")
    assert hzlib.gh("pr", "merge", "7", "-R", CORE) == (124, "")
    assert hzlib.ghj("run", "list", default="fallback") == "fallback"
    assert seen == [120, 300, 120]
    err = capsys.readouterr().err
    assert "refused: gh pr view 7 timed out after 120s" in err and "gh pr merge 7 timed out after 300s" in err
    assert hzlib.gh_timeout(("api", "-X", "POST", "repos/x")) == 300 and hzlib.gh_timeout(("api", "repos/x")) == 120


def test_a_merge_call_that_times_out_is_read_back_not_assumed(tool, monkeypatch, capsys):
    module = tool("hz-merge-train")
    seen = []
    fake = ReadBack([green()], {"state": "MERGED", "mergeCommit": {"oid": "1" * 40}})
    real_gh = module.gh

    def gh(*args):
        if args[:2] == ("pr", "merge"):
            monkeypatch.setattr(module.edge, "run", timing_out(seen))
            fake.calls.append(args)
            return real_gh(*args)
        return fake(*args)

    monkeypatch.setattr(module, "gh", gh)
    monkeypatch.setattr(module.time, "sleep", lambda s: None)
    monkeypatch.setattr(module, "FREEZE", module.Path("/nonexistent/freeze"))
    assert module.train(CORE, "7", queue_core=True) == (True, "abcdef12")
    assert seen == [300] and "read back after gh pr merge exited 124: gh pr merge timed out after 300s" in capsys.readouterr().out


def test_checks_that_never_settle_end_the_wait_with_a_clear_refusal(tool, monkeypatch, capsys):
    module = tool("hz-merge-train")
    pending = {**green(), "statusCheckRollup": [check("CI OK", conclusion="", status="IN_PROGRESS")]}
    fake = FakeGh([pending])
    train(tool, monkeypatch, fake, queue_core=True)
    sleeps = [c for c in fake.calls if c[0] == "sleep"]
    assert len(sleeps) == module.SETTLE_POLLS and not merged_calls(fake)
    assert capsys.readouterr().out == "#7 NOT merged, checks did not settle within 60 min; requeue once they complete\n"


def test_the_train_and_release_gh_calls_are_bounded(monkeypatch):
    from steuermaschine_heinzel.native.train_lib import train as release_train

    seen = []
    monkeypatch.setattr(release_train.edge, "run", timing_out(seen))
    with pytest.raises(RuntimeError, match="timed out after 120s; refused"):
        release_train.Real()._gh("api", "repos/x")
    with pytest.raises(RuntimeError, match="whether it exists is unknown"):
        release_train.Real().has_tag("v1.2.3")
    assert seen == [120, 120]
