"""`sm ci defer` and `sm ci deferred`: keeps the kept PRs' runs, never cancels main or release/*, refuses unknown or
closed PRs. A scripted gh; nothing real is cancelled.

The first part is the former deferral script's test file, moved: the same canned runs, the same assertions. What
changed is said at each place: the command line (`sm ci defer --cancel ...`), and three tests whose subject changed.
The second part runs the commands through the whole command tree: text, json, the review gate, the exact calls.
"""

import json
import re
from datetime import UTC, datetime

import pytest
from ci_ops_kit import REPO, World, runtime

from steuermaschine.native import NativeRuntime, using
from steuermaschine.protocols import Result
from steuermaschine.testing import invoke
from steuermaschine_heinzel.native import ci_ops
from steuermaschine_heinzel.native.ci_ops_lib import defer as defer_lib
from steuermaschine_heinzel.native.ci_ops_lib import deferred, legacy

OLD = "2026-01-01T00:00:00Z"


def _r(i, branch, wf="CI", event="pull_request", created=OLD):
    return {"databaseId": i, "headBranch": branch, "workflowName": wf, "event": event, "createdAt": created}


def canned() -> World:
    """The former test's fake gh: two kept PRs, one closed, runs on a feature branch, main, release/* and bump/*."""
    world = World()
    world.prs = {"603": ("OPEN", "fix/603"), "598": ("OPEN", "fix/598"), "7": ("CLOSED", "old/7")}
    world.main_queued = [{"createdAt": OLD}]
    world.runs = {"queued": [_r(1, "fix/603"), _r(2, "feat/x"), _r(3, "main"), _r(4, "release/0.9.30"), _r(7, "bump/v0.9.33")],
                  "in_progress": [_r(5, "fix/598", "Merge gate"), _r(6, "feat/y", "CodeQL")]}
    # run 6 has a running macOS job, run 5 only a queued one
    world.jobs = {"6": {"jobs": [{"labels": ["macos-latest"], "status": "in_progress"}, {"labels": ["ubuntu-latest"], "status": "queued"}]},
                  "5": {"jobs": [{"labels": ["macos-latest"], "status": "queued"}]}, "9": None}
    return world


@pytest.fixture
def env(tmp_path, monkeypatch):
    """One world and one store per test, as the former tests had one throwaway HOME per test."""
    world = canned()
    rt = runtime(tmp_path, monkeypatch, world)
    return world, rt


def run(env, *args):
    world, rt = env
    p = invoke(rt, ["ci", "defer", *args])
    log = deferred.text(deferred.runs(rt.providers.store))
    return p, list(world.cancelled), log


def audit(env) -> str:
    return "\n".join(deferred.audit_lines(env[1].providers.store))


# ----------------------------------------------------------------------------- moved tests
def test_keep_spares_kept_prs_and_records_the_rest(env):
    p, cancelled, log = run(env, "--cancel", "--keep", "603,598")
    assert p.exit_code == 0, p.stderr
    assert cancelled == ["2"]
    assert log.splitlines() == ["2 feat/x CI"]
    assert "kept 1 fix/603 CI" in p.stdout and "kept 5 fix/598 Merge gate" in p.stdout


def test_never_cancels_main_or_release_even_without_keep(env):
    _, cancelled, _ = run(env, "--cancel")
    assert cancelled == ["1", "2", "5"]
    _, cancelled, _ = run(env, "--cancel", "--keep", "603")
    assert "3" not in cancelled and "4" not in cancelled


def test_unknown_or_closed_pr_refuses_before_cancelling(env):
    for bad in ("603,999", "603,7", "abc"):
        p, cancelled, log = run(env, "--cancel", "--keep", bad)
        assert p.exit_code == 2 and cancelled == [] and log == "", bad


def test_if_main_starved_passes_keep_through(env):
    # command line changed: the former `--if-main-starved 15 --keep 603` is `--cancel --if-main-starved --minutes 15 --keep 603`
    p, cancelled, _ = run(env, "--cancel", "--if-main-starved", "--minutes", "15", "--keep", "603")
    assert p.exit_code == 0, p.stderr
    assert cancelled == ["2", "5"]


def test_keep_alone_and_unknown_flags_refuse(env):
    # CHANGED on purpose: the former script refused `--keep 603` without a mode (exit 2). Here a command without
    # --cancel is the dry run, so --keep alone is a preview that cancels nothing. The other two refusals are unchanged.
    p, cancelled, log = run(env, "--keep", "603")
    assert p.exit_code == 0 and cancelled == [] and log == "" and "would cancel 2 feat/x CI" in p.stdout
    assert run(env, "--cancel", "--bogus")[0].exit_code == 2
    assert run(env, "--cancel", "--keep")[0].exit_code == 2


def test_help_documents_keep(env):
    p, _, _ = run(env, "--help")
    assert "--keep PR[,PR...]" in " ".join(p.stdout.split())


def test_bump_branches_are_never_cancelled(env):
    """Tag-on-green-main: the lean bump PR's run is the cut; deferring it would stall the release (review CHANGE 3)."""
    p, cancelled, log = run(env, "--cancel")
    assert p.exit_code == 0, p.stderr
    assert "7" not in cancelled and "bump/" not in log
    assert "4" not in cancelled and "3" not in cancelled


def _run_with(tmp_path, monkeypatch, runs):
    """Run --cancel against canned runs (list of dicts); return cancelled ids and the runtime."""
    world = World()
    world.open_branches = ["feat/normal", "feat/wr", "feat/review"]
    world.runs = {"queued": runs}
    world.default_jobs = {"jobs": []}
    rt = runtime(tmp_path, monkeypatch, world)
    p = invoke(rt, ["ci", "defer", "--cancel"])
    assert p.exit_code == 0, p.stderr
    return world.cancelled, rt


def test_allowlist_protects_release_runs(tmp_path, monkeypatch):
    from steuermaschine.testing import FakeClock

    fresh = datetime.fromtimestamp(FakeClock().now(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = [
        _r(1, "v0.9.34", "Acceptance", "push"),
        _r(2, "main", "Acceptance", "workflow_dispatch"),
        _r(3, "main", "Standard app composition", "schedule"),
        _r(4, "main", "Release one plugin", "workflow_dispatch"),
        _r(5, "main", "CI", "push"),
        _r(6, "release/v0.9.35", "CI"),
        _r(7, "bump/v0.9.35", "CI"),
        _r(8, "feat/normal", "CI"),
        _r(9, "v0.9.40", "CI"),  # PR branch named like a tag: spared
        _r(10, "refs/tags/v0.9.34", "CI"),
        _r(11, "feat/acc", "Acceptance"),
        _r(12, "feat/fresh", "CI", created=fresh),
        _r(13, "feat/disp", "CI", "workflow_dispatch"),
        _r(14, "feat/pub", "Publish"),
        _r(15, "feat/x", "Release bumped plugins"),
        _r(16, "feat/review", "Claude review"),
        _r(17, "feat/wr", "Merge gate", "workflow_run"),
        _r(18, "main", "Merge gate", "workflow_run"),
        _r(19, "v0.9.34", "Merge gate", "workflow_run"),
        _r(20, "feat/closed", "Merge gate", "workflow_run"),
        _r(21, "feat/wr", "CI", "workflow_run"),
    ]
    cancelled, rt = _run_with(tmp_path, monkeypatch, runs)
    assert cancelled == ["8", "16", "17"]
    assert "attempt id=8 workflow=CI event=pull_request branch=feat/normal caller=" in "\n".join(deferred.audit_lines(rt.providers.store))


def test_run_with_running_macos_job_is_spared_and_logged(env):
    p, cancelled, log = run(env, "--cancel")  # noqa: RUF059
    assert "6" not in cancelled and "5" in cancelled  # 6: macOS in_progress; 5: macOS only queued
    assert "spared 6 feat/y CodeQL (macOS started" in p.stdout
    assert "spared id=6" in audit(env) and "reason=macos-started" in audit(env)


def test_force_release_cancels_started_macos_runs_but_not_main_or_release(env):
    p, cancelled, _ = run(env, "--cancel", "--force-release")  # noqa: RUF059
    assert cancelled == ["1", "2", "5", "6"] and "3" not in cancelled and "4" not in cancelled and "7" not in cancelled
    assert "reason=release-candidate" in audit(env)
    assert run(env, "--if-main-starved", "--force-release")[0].exit_code == 2


def test_the_release_train_can_call_the_deferral_with_force_release(env):
    # REPLACED: the former test read the release train's source for `"--cancel", "--force-release"`. The train is
    # another area; what it will call is this function, with these arguments.
    world, rt = env
    with using(NativeRuntime(rt.providers, rt.settings, rt.project)):
        done = ci_ops.defer(cancel=True, force_release=True)
    assert done.live and done.force_release and done.cancelled == 4 and world.cancelled == ["1", "2", "5", "6"]
    with using(NativeRuntime(rt.providers, rt.settings, rt.project)):
        assert ci_ops.deferred_ids() == {"1", "2", "5", "6"} and [row["branch"] for row in ci_ops.deferred_runs()] == ["fix/603", "feat/x", "fix/598", "feat/y"]


# ----------------------------------------------------------------------------- through the command tree
def test_without_cancel_it_is_a_dry_run_in_text_and_json(env):
    world, rt = env
    text = invoke(rt, ["ci", "defer"])
    assert text.exit_code == 0 and text.stdout.splitlines() == [
        "would cancel 1 fix/603 CI", "would cancel 2 feat/x CI", "would cancel 5 fix/598 Merge gate",
        "spared 6 feat/y CodeQL (macOS started: 1 job(s), let it finish)",
        "dry run: nothing was cancelled or recorded; `sm ci defer --cancel` cancels the 3 run(s) marked `would cancel`"]
    doc = json.loads(invoke(rt, ["--format", "json", "ci", "defer", "--keep", "603"]).stdout)
    result = ci_ops.Deferral.model_validate(doc)
    assert result.state == "dry_run" and not result.live and result.keep == ["fix/603"] and result.would_cancel == 2 and result.cancelled == 0
    assert [(r.run, r.action) for r in result.runs] == [("1", "kept"), ("2", "would_cancel"), ("5", "would_cancel"), ("6", "spared")]
    assert result.runs[1].would_run == f"gh api -X POST repos/{REPO}/actions/runs/2/cancel"
    # a dry run writes nothing: no cancel, no record, no audit line, and it does not ask who called
    assert world.cancelled == [] and rt.providers.store.writes == 0 and not any(call[0] == "ps" for call in world.calls)
    assert not any("-X" in call for call in world.calls)


def test_a_live_deferral_makes_exactly_these_calls_in_this_order(env):
    world, rt = env
    done = invoke(rt, ["--format", "json", "ci", "defer", "--cancel", "--if-main-starved", "--keep", "603"])
    assert done.exit_code == 0, done
    result = ci_ops.Deferral.model_validate(json.loads(done.stdout))
    assert result.state == "cancelled" and result.live and result.main_queued_minutes >= 15 and result.starved_minutes == 15
    api = f"api repos/{REPO}/actions/runs"
    assert [" ".join(call[1:]) if call[0] == "gh" else call[0] for call in world.calls] == [
        f"pr view 603 -R {REPO} --json state,headRefName",  # the PRs to keep are checked first
        f"run list -R {REPO} --status queued --branch main --limit 50 --json createdAt",  # is main starved?
        f"pr list -R {REPO} --state open --limit 200 --json headRefName",
        f"run list -R {REPO} --status queued --limit 100 --json databaseId,headBranch,workflowName,event,createdAt",
        f"run list -R {REPO} --status in_progress --limit 100 --json databaseId,headBranch,workflowName,event,createdAt",
        f"{api}/2/jobs?per_page=100", "ps", f"{api}/2", f"api -X POST repos/{REPO}/actions/runs/2/cancel",
        f"{api}/5/jobs?per_page=100", f"{api}/5", f"api -X POST repos/{REPO}/actions/runs/5/cancel",
        f"{api}/6/jobs?per_page=100"]  # run 1 is kept without a call; run 6 is spared after its jobs were read; `ps` (who called) once
    assert [line.split(" ", 1)[1].split(" caller=")[0] for line in deferred.audit_lines(rt.providers.store)] == [
        "attempt id=2 workflow=CI event=pull_request branch=feat/x", "cancelled id=2",
        "attempt id=5 workflow=Merge gate event=pull_request branch=fix/598", "cancelled id=5",
        "spared id=6 branch=feat/y workflow=CodeQL reason=macos-started n=1"]
    listed = invoke(rt, ["ci", "deferred"])
    assert listed.stdout == "2 feat/x CI\n5 fix/598 Merge gate\n"
    doc = ci_ops.Deferred.model_validate(json.loads(invoke(rt, ["--format", "json", "ci", "deferred"]).stdout))
    assert [(r.id, r.branch, r.workflow) for r in doc.runs] == [("2", "feat/x", "CI"), ("5", "fix/598", "Merge gate")]


def test_no_call_follows_a_refusal(env):
    world, rt = env
    refused = invoke(rt, ["ci", "defer", "--cancel", "--keep", "603,7"])
    assert refused.exit_code == 2 and "PR #7 is CLOSED, not open" in refused.stderr
    assert world.gh_lines() == [f"pr view 603 -R {REPO} --json state,headRefName", f"pr view 7 -R {REPO} --json state,headRefName"]
    world.calls.clear()
    for argv, why in ((["--cancel", "--force-release", "--if-main-starved"], "--force-release cannot be combined"), (["--cancel", "--minutes", "5"], "--minutes only with"),
                      (["--cancel", "--keep", ","], "--keep needs PR")):
        bad = invoke(rt, ["ci", "defer", *argv])
        assert bad.exit_code == 2 and why in bad.stderr, argv
    assert world.calls == [] and rt.providers.store.writes == 0


def test_without_a_repository_setting_nothing_is_asked(env):
    world, rt = env
    rt.settings["repo"] = ""
    for argv in (["ci", "defer", "--cancel"], ["ci", "rerun"], ["ci", "quarantine", "list"]):
        refused = invoke(rt, argv)
        assert refused.exit_code == 1 and "setting `repo`" in refused.stderr, argv
    assert world.calls == []


def test_main_not_starved_leaves_pr_ci_alone(env):
    world, rt = env
    world.main_queued = []
    quiet = invoke(rt, ["ci", "defer", "--cancel", "--if-main-starved"])
    assert quiet.exit_code == 0 and quiet.stdout == "no queued main CI\n" and world.cancelled == []
    recent = datetime.fromtimestamp(rt.providers.clock.now() - 5 * 60, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    world.main_queued = [{"createdAt": recent}, {"createdAt": "not a date"}]
    young = invoke(rt, ["--format", "json", "ci", "defer", "--cancel", "--if-main-starved", "--minutes", "15"])
    result = ci_ops.Deferral.model_validate(json.loads(young.stdout))
    assert result.state == "not_starved" and result.main_queued_minutes == 5 and result.notes == ["main CI queued 5 min < 15: leave PR CI alone"]
    assert world.cancelled == [] and not any(" ".join(call[1:3]) == "pr list" for call in world.calls)
    shown = invoke(rt, ["ci", "defer", "--if-main-starved", "--minutes", "3"])  # starved, but a dry run
    assert shown.stdout.splitlines()[0] == "main CI queued 5 min >= 3: would cancel PR CI (never main, release/* or bump/*)" and world.cancelled == []


def test_unreadable_listings_refuse_before_anything_is_cancelled(env):
    world, rt = env
    world.fail = {"--status in_progress"}
    refused = invoke(rt, ["ci", "defer", "--cancel"])
    assert refused.exit_code == 1 and "cannot read the in_progress runs" in refused.stderr and world.cancelled == []
    world.fail = {"--branch main"}
    refused = invoke(rt, ["ci", "defer", "--cancel", "--if-main-starved"])
    assert refused.exit_code == 1 and "cannot read the queued main runs" in refused.stderr and world.cancelled == []


def test_jobs_unknown_spares_the_run_and_a_failed_cancel_is_said_and_not_recorded(env):
    world, rt = env
    world.runs = {"queued": [_r(9, "feat/z"), _r(2, "feat/x"), _r(8, "feat/w")]}
    world.fail = {"actions/runs/2/cancel"}
    world.jobs["8"] = {"no": "jobs"}
    done = invoke(rt, ["ci", "defer", "--cancel"])
    assert done.exit_code == 1 and done.stdout.splitlines() == ["spared 9 feat/z CI (jobs unknown)", "FAILED cancel 2 feat/x CI (Server Error (HTTP 500))",
                                                              "spared 8 feat/w CI (jobs unknown)", "Cancelled 0. Failed 1 (0 GitHub reported as not queued)."]
    assert deferred.runs(rt.providers.store) == [] and "FAILED id=2" in audit(env) and "spared id=9 branch=feat/z workflow=CI reason=jobs-unknown" in audit(env)


@pytest.mark.parametrize("mixed", [False, True])
def test_not_queued_refusal_is_reported_and_only_successes_are_recorded(env, mixed):
    world, rt = env
    world.runs = {"queued": [_r(2, "feat/x"), _r(4, "release/v0.9.50"), _r(7, "bump/v0.9.50")]}
    if mixed:
        world.runs["queued"].append(_r(1, "feat/y"))
    world.on("/2/cancel", Result(1, "", "gh: Cannot cancel a workflow run that has not been queued yet. (HTTP 409)"))
    done = invoke(rt, ["--format", "json", "ci", "defer", "--cancel"])
    result = ci_ops.Deferral.model_validate_json(done.stdout)
    assert done.exit_code == 1
    assert (result.state, result.cancelled, result.failed, result.not_queued) == (
        "partial" if mixed else "failed", int(mixed), 1, 1,
    )
    assert result.runs[0].action == "not_queued" and "GitHub says" in result.runs[0].render()[0]
    assert world.cancelled == (["1"] if mixed else [])
    assert [row["id"] for row in deferred.runs(rt.providers.store)] == world.cancelled
    assert not any(f"/{run}/cancel" in call for run in ("4", "7") for call in world.gh_lines())


def test_a_second_deferral_waits_while_one_holds_the_list(env):
    world, rt = env
    with rt.providers.store.lock(deferred.LOCK):
        busy = invoke(rt, ["ci", "defer", "--cancel"])
    assert busy.exit_code == 75 and "another deferral or re-run" in busy.stderr and world.calls == []


def test_the_former_list_is_read_until_the_store_has_one(env, tmp_path):
    world, rt = env
    (tmp_path / "former-deferred.txt").write_text("41 feat/old CI\n42 feat/older Merge gate\nnot a line\n43\n")
    assert invoke(rt, ["ci", "deferred"]).stdout == "41 feat/old CI\n42 feat/older Merge gate\n"
    world.runs = {"queued": [_r(2, "feat/x")]}
    assert invoke(rt, ["ci", "defer", "--cancel"]).exit_code == 0
    assert invoke(rt, ["ci", "deferred"]).stdout == "41 feat/old CI\n42 feat/older Merge gate\n2 feat/x CI\n"  # adopted with the first write
    (tmp_path / "former-deferred.txt").write_text("99 feat/late CI\n")
    assert "99" not in invoke(rt, ["ci", "deferred"]).stdout  # and never read again
    assert legacy.deferred_runs(tmp_path / "missing.txt") == [] and legacy.deferred_runs(None) == []


def test_the_allowlist_one_rule_at_a_time():
    now = datetime(2026, 6, 1, tzinfo=UTC).timestamp()
    assert defer_lib.eligible(_r(1, "feat/x"), now)
    assert defer_lib.eligible(_r(1, "feat/x", "Merge gate", "workflow_run"), now) and not defer_lib.eligible(_r(1, "feat/x", "Docs", "workflow_run"), now)
    for run in (_r(1, None), _r(1, ""), _r(1, "feat/x", created="yesterday"), _r(1, "feat/x", created=None), _r(1, "feat/x", "Deploy"),
                _r(1, "feat/x", event="push"), _r(1, "refs/pull/7/merge"), _r(1, "v1"), _r(1, "main"), _r(1, "release/x"), _r(1, "bump/x")):
        assert not defer_lib.eligible(run, now), run
    assert defer_lib.eligible(_r(1, "vendor/x"), now) and defer_lib.eligible(_r(1, "feat/main"), now)
    assert defer_lib.macos_started([{"labels": ["macos-14"], "status": "completed"}, {"labels": None, "status": "in_progress"},
                                    {"labels": ["macos-latest"], "status": "waiting"}, {"labels": ["macos-latest"], "status": "pending"}]) == 1


def test_every_command_of_the_area_has_contract_help_a_model_and_no_former_name(env):
    from steuermaschine.helpdoc import missing
    from steuermaschine.models import Model
    from steuermaschine.native import NativeCommand, collect

    found = collect(ci_ops)
    assert sorted(c.path for c in found) == ["ci defer", "ci deferred", "ci quarantine", "ci quarantine flag", "ci quarantine list", "ci rerun", "ci sonar-close"]
    commands = [c for c in found if isinstance(c, NativeCommand)]
    assert {c.path for c in commands if c.writes} == {"ci defer", "ci rerun", "ci quarantine flag", "ci sonar-close"}
    for c in found:
        assert missing(c.help) == "" and "hz-" not in c.help and "hz_" not in c.help, c.path
    for c in commands:
        assert issubclass(c.model, Model) and c.replaces, c.path
    _, rt = env
    for argv in (["ci", "defer", "--help"], ["ci", "rerun", "--help"], ["ci", "quarantine", "--help"], ["ci", "sonar-close", "--help"], ["ci", "deferred", "--help"]):
        shown = invoke(rt, argv)
        assert shown.exit_code == 0 and "hz-" not in shown.stdout, argv
    for name, info in ci_ops.CiOpsSettings.model_fields.items():
        assert name.startswith("ci_ops_") and info.description, name


# ----------------------------------------------------------------------------- the keep file (review C2)
def test_the_keep_file_is_kept_on_every_call_and_a_closed_pr_in_it_is_skipped(tmp_path, monkeypatch):
    keep = tmp_path / "keep.txt"
    keep.write_text("# fix PRs\n603\n7  # merged since\n")
    world = canned()
    rt = runtime(tmp_path, monkeypatch, world, ci_ops_keep_file=str(keep))
    done = invoke(rt, ["ci", "defer", "--cancel", "--keep", "598"])
    assert done.exit_code == 0, done.stderr
    assert world.cancelled == ["2"] and "kept 1 fix/603 CI" in done.stdout and "keep file: PR #7 is CLOSED, not open; not kept" in done.stdout


def test_explicit_keep_does_not_resolve_the_same_pr_again_from_the_keep_file(tmp_path, monkeypatch):
    keep = tmp_path / "keep.txt"
    keep.write_text("603\n")
    world = canned()
    rt = runtime(tmp_path, monkeypatch, world, ci_ops_keep_file=str(keep))
    done = invoke(rt, ["ci", "defer", "--cancel", "--keep", "603,598"])
    assert done.exit_code == 0 and "kept 1 fix/603 CI" in done.stdout
    assert world.cancelled == ["2"]
    assert sum("pr view 603 " in call for call in world.gh_lines()) == 1


def test_an_unreadable_or_malformed_keep_file_refuses_before_any_call(tmp_path, monkeypatch):
    for setting, text in (("missing.txt", None), ("bad.txt", "603 fix-later\n")):
        path = tmp_path / setting
        if text is not None:
            path.write_text(text)
        world = canned()
        done = invoke(runtime(tmp_path, monkeypatch, world, ci_ops_keep_file=str(path)), ["ci", "defer", "--cancel"])
        assert done.exit_code == 2 and "ci_ops_keep_file" in done.stderr and world.cancelled == [] and world.calls == [], setting


def test_help_says_keep_is_per_call_and_names_the_keep_file(env):
    p, _, _ = run(env, "--help")
    assert "this call only" in " ".join(p.stdout.split()) and "ci_ops_keep_file" in p.stdout


# ----------------------------------------------------------------------------- --supersede-main
def _main(i, minute, wf="CI", event="push", branch="main"):
    return {"databaseId": i, "headBranch": branch, "workflowName": wf, "event": event, "createdAt": f"2026-11-13T10:{minute:02d}:00Z"}


def superseding(tmp_path, monkeypatch, queued, started=()):
    from ci_ops_kit import ok

    from steuermaschine.protocols import Result

    world = World()
    world.main_queued = queued

    def view(argv):  # the read right before a cancel; a POST is the cancel itself
        rid = re.search(r"actions/runs/(\d+)", " ".join(argv)).group(1)
        if "POST" in argv:
            world.cancelled.append(rid)
            return ok({})
        return ok({"id": int(rid), "status": "in_progress" if rid in started else "queued", "name": "CI", "head_branch": "main", "event": "push"})

    world.on("actions/runs/", view)
    world.other = lambda argv, cwd, timeout: Result(0, "", "")
    return world, runtime(tmp_path, monkeypatch, world)


def test_supersede_main_cancels_older_queued_main_runs_and_keeps_the_newest_two(tmp_path, monkeypatch):
    queued = [_main(11, 1), _main(12, 2), _main(13, 3), _main(14, 4), _main(15, 0, wf="CodeQL"), _main(16, 0, branch="release/v0.9.41")]
    world, rt = superseding(tmp_path, monkeypatch, queued, started={"12"})
    dry = invoke(rt, ["ci", "defer", "--supersede-main"])
    assert dry.exit_code == 0 and world.cancelled == [] and "would cancel 11 main CI" in dry.stdout and "kept 14 main CI" in dry.stdout
    done = invoke(rt, ["--format", "json", "ci", "defer", "--supersede-main", "--cancel"])
    result = ci_ops.Deferral.model_validate(json.loads(done.stdout))
    assert done.exit_code == 0 and world.cancelled == ["11"] and result.cancelled == 1  # 12 started meanwhile: spared
    assert [(r.run, r.action) for r in result.runs] == [("14", "kept"), ("13", "kept"), ("12", "spared"), ("11", "cancelled")]
    assert deferred.runs(rt.providers.store) == []  # superseded, not deferred: nothing to re-run
    lines = [line.split(" ", 1)[1].split(" caller=")[0] for line in deferred.audit_lines(rt.providers.store)]
    assert lines == ["spared id=12 branch=main workflow=CI reason=started status=in_progress",
                     "attempt id=11 workflow=CI event=push branch=main", "superseded id=11"]


def test_supersede_main_leaves_two_or_fewer_queued_runs_alone_and_stands_alone(tmp_path, monkeypatch):
    world, rt = superseding(tmp_path, monkeypatch, [_main(21, 1), _main(22, 2), _main(23, 3, event="workflow_dispatch")])
    done = invoke(rt, ["ci", "defer", "--supersede-main", "--cancel"])
    assert done.exit_code == 0 and world.cancelled == [] and "nothing is superseded" in done.stdout
    for extra in (["--if-main-starved"], ["--keep", "603"], ["--force-release"]):
        assert invoke(rt, ["ci", "defer", "--supersede-main", *extra]).exit_code == 2
    assert "--supersede-main" in invoke(rt, ["ci", "defer", "--help"]).stdout
