"""`sm ci rerun`: the plan (moved tests of the former batch tool, assertions unchanged, only the import renamed), then
the command through the whole command tree: text, json, the review gate, the exact calls in order. Nothing real is re-run."""

import json

from ci_ops_kit import REPO, World, runtime
from steuermaschine_heinzel.native import ci_ops
from steuermaschine_heinzel.native.ci_ops_lib import checks, deferred, legacy
from steuermaschine_heinzel.native.ci_ops_lib import rerun_plan as r

from steuermaschine.testing import invoke

# ----------------------------------------------------------------------------- moved tests

def chk(n, c): return {"name": n, "status": "COMPLETED", "conclusion": c}
CANC = [chk("CI OK", "FAILURE"), chk("Build", "CANCELLED")]
def pr(n, created, roll=CANC, draft=False): return {"number": n, "headRefName": f"b{n}", "createdAt": created, "isDraft": draft, "statusCheckRollup": roll}
D = r.parse_deferred("1 b1 CI\n2 b1 Merge gate\n3 b2 CI\n4 b3 CI\n5 b4 CI\n6 b5 CI\n7 gone CI\n")

def test_parse_groups_by_branch():
    assert D["b1"] == [("1", "CI"), ("2", "Merge gate")]

def test_queued_first_then_oldest_cap_and_skips():
    prs = [pr(1, "2026-11-07T10:00"), pr(2, "2026-11-07T09:00"), pr(3, "2026-11-07T08:00", [chk("Build", "FAILURE")]), pr(4, "2026-11-07T07:00", draft=True), pr(5, "2026-11-07T11:00")]
    picked, skipped, ref = r.plan(D, prs, [{"pr": 5, "status": "queued"}], [], n=2)
    assert not ref and [p[0] for p in picked] == [5, 2]
    why = dict(skipped)
    assert "failing" in why["#3"] and why["#4"] == "draft" and why["gone"] == "no open PR" and "cap" in why["#1"]

def test_inflight_counts_against_cap():
    busy = [{"name": "x", "status": "IN_PROGRESS", "conclusion": ""}]
    prs = [pr(10 + i, "2026-11-07T0%d:00" % i, busy) for i in range(5)] + [pr(1, "2026-11-07T10:00")]  # noqa: UP031
    assert r.plan(D, prs, [], [], n=5)[0] == []

def test_refuses_for_release_or_queued_main():
    assert "release" in r.plan(D, [pr(1, "t")], [], [{"status": "queued", "headBranch": "release/0.9.29"}])[2]
    assert "main" in r.plan(D, [pr(1, "t")], [], [{"status": "queued", "headBranch": "main"}])[2]
    assert r.plan(D, [pr(1, "t")], [], [{"status": "in_progress", "headBranch": "main"}])[2] == ""


def test_adaptive_cap_from_free_jobs():
    prs = [pr(i, "2026-11-07T0%d:00" % i) for i in range(1, 6)]  # noqa: UP031
    d = r.parse_deferred("".join(f"{i} b{i} CI\n" for i in range(1, 6)))
    assert r.free_slots(0) == 3 and r.free_slots(10) == 1 and r.free_slots(14) == 0 and r.free_slots(31) == 0
    picked, skipped, ref = r.plan(d, prs, [], [], active_jobs=2)  # idle runners: 3 PRs fit, not 5
    assert not ref and len(picked) == 3 and "free capacity" in dict(skipped)["#4"]
    assert r.plan(d, prs, [], [], active_jobs=2, target=30)[0].__len__() == 5
    assert r.plan(d, prs, [], [], active_jobs=14)[0] == []
    assert len(r.plan(d, prs, [], [], n=1, active_jobs=0)[0]) == 1  # --n overrides


def test_adaptive_still_refuses_release_and_prefers_queued():
    prs = [pr(1, "2026-11-07T10:00"), pr(2, "2026-11-07T09:00")]
    d = r.parse_deferred("1 b1 CI\n2 b2 CI\n")
    assert "release" in r.plan(d, prs, [], [{"status": "in_progress", "headBranch": "release/0.9.34"}], active_jobs=0)[2]
    picked = r.plan(d, prs, [{"pr": 1, "status": "queued"}], [], active_jobs=10)[0]
    assert [p[0] for p in picked] == [1]


def test_count_jobs_with_fake_gh():
    fake = {1: ["queued", "in_progress", "completed"], 2: ["queued"]}
    assert r.count_jobs([{"databaseId": 1}, {"databaseId": 2}], fake.__getitem__) == 3


def test_rerun_failed_only_when_some_job_succeeded():
    assert r.rerun_argv(7, ["success", "cancelled"], "R")[-1] == "--failed"
    assert "--failed" not in r.rerun_argv(7, ["cancelled", None], "R")


def test_no_rerun_while_macos_busy():
    d = r.parse_deferred("1 b1 CI\n")
    prs = [pr(1, "2026-11-07T10:00")]
    assert "macOS" in r.plan(d, prs, [], [], active_jobs=0, macos_busy=3)[2]
    assert len(r.plan(d, prs, [], [], active_jobs=0, macos_busy=2)[0]) == 1
    fake = {1: [(["macos-latest"], "in_progress"), (["ubuntu"], "queued")], 2: [(["macos-14"], "completed")]}
    assert r.count_macos_busy([{"databaseId": 1}, {"databaseId": 2}], fake.__getitem__) == 1


def test_wait_while_running_main_run_has_queued_jobs():
    d = r.parse_deferred("1 b1 CI\n")
    assert "queued jobs" in r.plan(d, [pr(1, "2026-11-07T10:00")], [], [{"status": "in_progress", "headBranch": "main"}], active_jobs=0, main_jobs_queued=True)[2]


# ----------------------------------------------------------------------------- through the command tree
FIELDS = "number,headRefName,createdAt,isDraft,statusCheckRollup"


def world_with_deferred(tmp_path) -> World:
    """Four recorded runs (in the former list file), two open PRs with cancelled CI, PR 2 queued for merge, idle runners."""
    (tmp_path / "former-deferred.txt").write_text("1 b1 CI\n2 b1 Merge gate\n3 b2 CI\n7 gone CI\n")
    (tmp_path / "queue.json").write_text(json.dumps({"items": [{"pr": 2, "status": "queued"}, {"pr": 1, "status": "merged"}]}))
    world = World()
    world.open_prs = [pr(1, "2026-11-07T09:00"), pr(2, "2026-11-07T10:00")]
    world.default_jobs = {"jobs": [{"status": "completed", "conclusion": "cancelled", "labels": ["ubuntu-24.04"]}]}
    world.jobs = {"1": {"jobs": [{"status": "completed", "conclusion": "success", "labels": []}, {"status": "completed", "conclusion": "cancelled", "labels": []}]}}
    return world


def test_rerun_makes_exactly_these_calls_in_this_order(tmp_path, monkeypatch):
    world = world_with_deferred(tmp_path)
    rt = runtime(tmp_path, monkeypatch, world)
    done = invoke(rt, ["--format", "json", "ci", "rerun"])
    assert done.exit_code == 0, done
    result = ci_ops.Rerun.model_validate(json.loads(done.stdout))
    api = f"api repos/{REPO}/actions/runs"
    assert world.gh_lines() == [
        f"pr list -R {REPO} --state open --limit 100 --json {FIELDS}",
        f"run list -R {REPO} --limit 100 --json status,headBranch,databaseId",
        f"{api}/3/jobs?per_page=100", f"run rerun 3 -R {REPO}",  # PR 2 first: it is queued for merge
        f"{api}/1/jobs?per_page=100", f"run rerun 1 -R {REPO} --failed",  # a job of run 1 was green: only the rest is re-run
        f"{api}/2/jobs?per_page=100", f"run rerun 2 -R {REPO}"]
    assert result.state == "done" and result.live and result.capacity == 0 and result.rerun == ["1", "2", "3"] and result.failed == []
    assert [(p.pr, p.branch, p.why, [(x.id, x.state) for x in p.runs]) for p in result.picked] == [
        (2, "b2", "queued-for-merge", [("3", "rerun")]), (1, "b1", "oldest", [("1", "rerun"), ("2", "rerun")])]
    assert [(s.what, s.why) for s in result.skipped] == [("gone", "no open PR")]
    assert result.lines == ["capacity: 0 jobs queued/in progress", "rerun #2 b2 (queued-for-merge): 1 run(s)", "rerun #1 b1 (oldest): 2 run(s)", "skip gone: no open PR"]
    assert deferred.text(deferred.runs(rt.providers.store)) == "7 gone CI\n"  # what was re-run left the list; the rest stays
    journal = [line for day in rt.providers.store.keys("heinzel.journal") for line in rt.providers.store.load("heinzel.journal", day)["lines"]]
    assert len(journal) == 1 and journal[0].endswith("ci rerun: re-ran CI for #2, #1; 1 skipped or waiting")


def test_dry_run_in_text_changes_nothing(tmp_path, monkeypatch):
    world = world_with_deferred(tmp_path)
    rt = runtime(tmp_path, monkeypatch, world)
    shown = invoke(rt, ["ci", "rerun", "--dry-run", "--n", "1"])
    assert shown.exit_code == 0 and shown.stdout.splitlines() == ["rerun #2 b2 (queued-for-merge): 1 run(s)", "skip gone: no open PR", "skip #1: over the cap of 1 in flight (waits)"]
    assert world.rerun == [] and rt.providers.store.writes == 0 and len(world.calls) == 2
    doc = ci_ops.Rerun.model_validate(json.loads(invoke(rt, ["--format", "json", "ci", "rerun", "--dry-run"]).stdout))
    assert doc.state == "planned" and not doc.live and doc.picked[0].runs[0].state == "would_rerun" and doc.rerun == []


def test_it_waits_with_exit_75_and_re_runs_nothing(tmp_path, monkeypatch):
    world = world_with_deferred(tmp_path)
    world.all_runs = [{"status": "in_progress", "headBranch": "release/v1.2.3", "databaseId": 50}]
    rt = runtime(tmp_path, monkeypatch, world)
    waiting = invoke(rt, ["--format", "json", "ci", "rerun"])
    result = ci_ops.Rerun.model_validate(json.loads(waiting.stdout))
    assert waiting.exit_code == 75 and result.state == "waiting" and result.waiting_on.startswith("a release/* run is active") and result.capacity == 0
    assert world.rerun == [] and rt.providers.store.writes == 0
    world.all_runs = [{"status": "in_progress", "headBranch": "main", "databaseId": 60}]
    world.jobs["60"] = {"jobs": [{"status": "queued", "labels": ["macos-latest"]}, {"status": "in_progress", "labels": ["ubuntu-24.04"]}]}
    world.calls.clear()
    text = invoke(rt, ["ci", "rerun"])
    assert text.exit_code == 75 and text.stdout.splitlines() == ["capacity: 2 jobs queued/in progress", "refusing: a main run still has queued jobs: wait until all its jobs run"]
    assert world.gh_lines().count(f"api repos/{REPO}/actions/runs/60/jobs?per_page=100") == 1  # three questions, one read
    world.jobs["60"] = None  # the jobs cannot be read: capacity unknown, and it waits
    unknown = invoke(rt, ["ci", "rerun"])
    assert unknown.exit_code == 75 and unknown.stdout.splitlines() == ["refusing: a main run still has queued jobs: wait until all its jobs run"]
    assert world.rerun == []


def test_a_refused_rerun_is_said_and_stays_on_the_list(tmp_path, monkeypatch):
    world = world_with_deferred(tmp_path)
    world.fail = {"run rerun 3"}
    world.jobs["2"] = None  # conclusions unknown: a full re-run
    rt = runtime(tmp_path, monkeypatch, world)
    done = invoke(rt, ["--format", "json", "ci", "rerun"])
    result = ci_ops.Rerun.model_validate(json.loads(done.stdout))
    assert done.exit_code == 0 and result.failed == ["3"] and result.rerun == ["1", "2"] and "FAILED rerun 3 b2" in result.lines
    assert result.picked[0].runs[0].state == "failed"
    assert world.gh_lines().count(f"run rerun 3 -R {REPO}") == 1  # a write is never tried twice
    assert deferred.text(deferred.runs(rt.providers.store)) == "3 b2 CI\n7 gone CI\n"


def test_nothing_deferred_and_unreadable_github(tmp_path, monkeypatch):
    world = World()
    rt = runtime(tmp_path, monkeypatch, world)
    empty = invoke(rt, ["ci", "rerun"])
    assert empty.exit_code == 0 and empty.stdout == "nothing deferred\n" and world.calls == []
    assert invoke(rt, ["ci", "rerun", "--all"]).stdout == "nothing deferred\n"
    world = world_with_deferred(tmp_path)
    world.fail = {"pr list"}
    refused = invoke(runtime(tmp_path, monkeypatch, world), ["ci", "rerun"])
    assert refused.exit_code == 1 and "cannot read GitHub" in refused.stderr and world.rerun == []
    for argv, why in ((["--force"], "--force only with --all"), (["--all", "--n", "2"], "--n does not apply")):
        bad = invoke(rt, ["ci", "rerun", *argv])
        assert bad.exit_code == 2 and why in bad.stderr


def test_all_re_runs_every_recorded_run_and_empties_the_list(tmp_path, monkeypatch):
    world = world_with_deferred(tmp_path)
    world.fail = {"run rerun 2"}
    rt = runtime(tmp_path, monkeypatch, world)
    shown = invoke(rt, ["ci", "rerun", "--all", "--dry-run"])
    assert shown.stdout.splitlines() == ["would rerun 1 b1 CI", "would rerun 2 b1 Merge gate", "would rerun 3 b2 CI", "would rerun 7 gone CI"]
    assert world.rerun == [] and rt.providers.store.writes == 0
    world.calls.clear()
    done = invoke(rt, ["--format", "json", "ci", "rerun", "--all"])
    result = ci_ops.Rerun.model_validate(json.loads(done.stdout))
    assert world.gh_lines() == [
        f"run list -R {REPO} --status in_progress --limit 100 --json headBranch", f"run list -R {REPO} --status queued --limit 100 --json headBranch",
        f"run rerun 1 -R {REPO}", f"run rerun 2 -R {REPO}", f"run rerun 3 -R {REPO}", f"run rerun 7 -R {REPO}"]
    assert done.exit_code == 0 and result.all_runs and result.rerun == ["1", "3", "7"] and result.failed == ["2"]
    assert result.lines == ["rerun 1 b1 CI", "FAILED rerun 2 b1 Merge gate", "rerun 3 b2 CI", "rerun 7 gone CI"]
    assert deferred.runs(rt.providers.store) == []  # as before: the list is emptied, also of the run GitHub refused


def test_all_refuses_while_a_release_run_is_active_unless_forced(tmp_path, monkeypatch):
    world = world_with_deferred(tmp_path)
    world.runs = {"queued": [{"headBranch": "release/v1.2.3"}], "in_progress": [{"headBranch": "feat/x"}, {"headBranch": None}]}
    rt = runtime(tmp_path, monkeypatch, world)
    refused = invoke(rt, ["ci", "rerun", "--all"])
    assert refused.exit_code == 1 and "a release/* run is active" in refused.stderr and "--all --force" in refused.stderr
    assert world.rerun == [] and rt.providers.store.writes == 0 and len(world.calls) == 2
    forced = invoke(rt, ["ci", "rerun", "--all", "--force"])
    assert forced.exit_code == 0 and len(world.rerun) == 4


def test_a_rerun_waits_while_another_command_holds_the_list(tmp_path, monkeypatch):
    world = world_with_deferred(tmp_path)
    rt = runtime(tmp_path, monkeypatch, world)
    with rt.providers.store.lock(deferred.LOCK):
        busy = invoke(rt, ["ci", "rerun"])
    assert busy.exit_code == 75 and "another deferral or re-run" in busy.stderr and world.calls == []


def test_the_queue_state_file_is_read_only_and_tolerant(tmp_path):
    assert legacy.queue_items(tmp_path / "missing.json") == [] and legacy.queue_items(None) == []
    for text in ("not json", "[1, 2]", '{"items": "no list"}'):
        (tmp_path / "queue.json").write_text(text)
        assert legacy.queue_items(tmp_path / "queue.json") == []


def test_focused_checks_never_count_and_real_failures_are_named():
    assert checks.classify_pr([chk("Focused e2e web-2", "FAILURE"), chk("Build", "SUCCESS")]) == ("ready", [])
    assert checks.classify_pr([]) == ("pending", []) and checks.classify_pr([{"name": "x", "status": "QUEUED", "conclusion": None}]) == ("pending", [])
    assert checks.classify_pr(CANC) == ("deferred", [])
    assert checks.classify_pr([chk("CI OK", "FAILURE"), chk("Build", "TIMED_OUT"), chk("Lint", "CANCELLED")]) == ("failing", ["Build"])
    assert checks.classify_pr([chk("CI OK", "FAILURE")]) == ("failing", ["CI OK"])
