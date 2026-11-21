"""Core's split main CI (bead il3nn): Linux push runs per SHA beside the dispatched, coalesced macOS lane run.

Every reader splits the two kinds, ignores a cancelled (replaced, pending) lane run, and keeps its old answer for today's
single-run main."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import policy_kit as kit  # noqa: E402
from steuermaschine_heinzel.native.queue_lib import main_batch, main_lanes  # noqa: E402

X, Y, Z = "a" * 40, "b" * 40, "c" * 40
LINUX = ["CI OK", "Test (ubuntu-24.04, Python 3.15)"]
MAC = ["Test (macos-latest, Python 3.15, unit)"]
DISPATCH = main_lanes.DISPATCH_JOB


def run(rid, sha, status="completed", conclusion="success", lane=False, attempt=1):
    return {"databaseId": rid, "headSha": sha, "status": status, "conclusion": conclusion if status == "completed" else None,
            "attempt": attempt, "displayTitle": ("CI macOS main " if lane else "CI ") + sha}


def job_rows(names, conclusion="success"):
    rows = [{"id": 1 + i, "name": n, "status": "completed", "conclusion": conclusion} for i, n in enumerate(names)]
    return {"total_count": len(rows), "jobs": rows}


class World:
    def __init__(self, runs, jobs=None, compare=None):
        self.runs, self.jobs, self.compare = runs, jobs or {}, compare or {}

    def ghj(self, *args, default=None):
        joined = " ".join(args)
        if args[:2] == ("run", "list"):
            return self.runs
        if "/compare/" in joined:
            return {"status": self.compare.get(joined.split("/compare/")[1], "diverged")}
        for rid, payload in self.jobs.items():
            if f"runs/{rid}/attempts/1/jobs" in joined or f"runs/{rid}/jobs" in joined:
                return payload
        for r in self.runs:
            if joined.endswith(f"actions/runs/{r['databaseId']}"):
                return {"id": r["databaseId"], "run_attempt": 1, "status": r["status"], "conclusion": r["conclusion"], "head_sha": r["headSha"]}
        return default


@pytest.fixture
def q(tool, home, monkeypatch):
    q = tool("hz-queue")
    q.world = World([])
    monkeypatch.setattr(q.hzlib, "ghj", lambda *a, default=None: q.world.ghj(*a, default=default))
    monkeypatch.setattr(q.hzlib, "gh", lambda *a: (0, ""))
    monkeypatch.setattr(q.hzlib, "merge_tier", lambda: False)
    monkeypatch.setattr(q.hzlib, "_aggregate", lambda: "CI OK")
    monkeypatch.setattr(q, "aggregate", lambda: "CI OK")
    monkeypatch.setattr(q, "ci_workflow", lambda: "ci.yml")
    monkeypatch.setattr(q.hzlib, "_ci_file", lambda: "ci.yml")
    monkeypatch.setattr(q, "reviewed_policy", lambda sha: kit.policy(commit=sha, test_jobs=frozenset(LINUX + MAC) - {"CI OK"}))
    return q


# --- the helper module
def test_split_runs_separates_lanes_and_drops_cancelled_lane_runs():
    rows = [run(1, X, lane=True, conclusion="cancelled"), run(2, X, lane=True), run(3, Y)]
    push, lane = main_lanes.split_runs(rows)
    assert [r["databaseId"] for r in push] == [3] and [r["databaseId"] for r in lane] == [2]


def test_a_push_run_is_never_a_lane_run():
    assert not main_lanes.is_macos_lane(run(1, X))
    assert not main_lanes.is_macos_lane({"displayTitle": "CI macOS main", "event": "push"})


def test_covers_is_x_or_a_descendant():
    status = {(X, Y): "ahead", (X, Z): "diverged"}

    def compare(a, b):
        return status.get((a, b), "behind")

    assert main_lanes.covers(X, X, compare) and main_lanes.covers(X, Y, compare)
    assert not main_lanes.covers(X, Z, compare) and not main_lanes.covers(Y, X, compare) and not main_lanes.covers(X, "nope", compare)


def test_macos_proof_takes_the_green_lane_run_at_a_descendant_only():
    def compare(a, b):
        return "ahead" if (a, b) == (X, Y) else "diverged"

    green = main_lanes.macos_proof(X, [run(5, Z, lane=True), run(4, Y, lane=True, conclusion="failure"), run(3, Y, lane=True)], compare)
    assert green["databaseId"] == 3
    assert main_lanes.macos_proof(X, [run(5, Z, lane=True)], compare) is None
    assert main_lanes.macos_proof(X, [run(5, Y, status="in_progress", lane=True)], compare) is None


# --- full_pass
def test_full_pass_needs_macos_cells_on_todays_single_run_but_not_on_a_split_push_run(q):
    assert not q.full_pass(job_rows(LINUX)["jobs"], X)
    assert q.full_pass(job_rows(LINUX + MAC)["jobs"], X)
    assert q.full_pass(job_rows(LINUX + [DISPATCH])["jobs"], X)
    assert not q.full_pass(job_rows(["CI OK", DISPATCH])["jobs"], X)  # the Linux cells are still required


# --- main_state
def test_main_state_ignores_a_cancelled_pending_lane_run_and_judges_push_runs(q):
    q.world = World([run(9, Z, lane=True, conclusion="cancelled"), run(8, Y, status="in_progress"), run(7, X)])
    assert q.main_state() == "green"


def test_main_state_is_red_on_a_failed_lane_run_next_to_a_green_push_run(q):
    q.world = World([run(9, Y, lane=True, conclusion="failure"), run(8, Y)], jobs={9: job_rows(["CI OK", *MAC], "failure")})
    assert q.main_state() == "red"
    assert q.load()["main_failure"]["databaseId"] == 9


def test_main_state_is_green_when_the_lane_failed_only_by_never_started_macos_cells(q):
    projection = [{"name": "CI OK", "conclusion": "cancelled", "runner": "", "steps": 0},
                  {"name": MAC[0], "conclusion": "cancelled", "runner": "", "steps": 0}]
    world = World([run(9, Y, lane=True, conclusion="failure"), run(8, Y)])
    plain = world.ghj
    world.ghj = lambda *a, default=None: projection if "runs/9/jobs?per_page=100" in " ".join(a) else plain(*a, default=default)
    q.world = world
    assert q.hzlib.only_never_started_macos_jobs_failed(q.hzlib.CORE, 9)
    assert q.main_state() == "green"


def test_a_lane_failure_pin_is_cleared_only_by_a_newer_green_lane_run(q):
    failed = job_rows(["CI OK", *MAC], "failure")
    q.world = World([run(9, Y, lane=True, conclusion="failure"), run(8, Y)], jobs={9: failed})
    assert q.main_state() == "red"
    q.world = World([run(10, Z), run(9, Y, lane=True, conclusion="failure"), run(8, Y)], jobs={9: failed})
    assert q.main_state() == "red"  # a green push run does not repair a macOS incident
    q.world = World([run(11, Z, lane=True), run(10, Z), run(9, Y, lane=True, conclusion="failure"), run(8, Y)], jobs={9: failed})
    assert q.main_state() == "green"
    assert not q.load().get("main_failure")


# --- later_main_passes
def test_a_later_split_push_pass_needs_a_green_lane_run_at_x_or_a_descendant(q):
    q.update(main_failure=run(7, X, conclusion="failure"))
    jobs = {10: job_rows(LINUX + [DISPATCH])}
    ahead = {f"{X}...{Y}": "ahead", f"{Y}...{Z}": "ahead"}
    q.world = World([run(10, Y), run(7, X, conclusion="failure")], jobs, ahead)
    assert not q.later_main_passes()  # Linux green at Y, no macOS proof yet
    q.world = World([run(12, Z, lane=True), run(10, Y), run(7, X, conclusion="failure")], jobs, ahead)
    assert q.later_main_passes()  # Z descends from Y


def test_a_later_single_run_pass_is_unchanged_by_lanes(q):
    q.update(main_failure=run(7, X, conclusion="failure"))
    q.world = World([run(10, Y), run(7, X, conclusion="failure")], {10: job_rows(LINUX + MAC)}, {f"{X}...{Y}": "ahead"})
    assert q.later_main_passes()


def test_a_lane_failure_is_repaired_by_a_later_first_attempt_lane_pass_only(q):
    q.update(main_failure=run(7, X, lane=True, conclusion="failure"))
    ahead = {f"{X}...{Y}": "ahead"}
    q.world = World([run(12, Y, lane=True), run(7, X, lane=True, conclusion="failure")], {12: job_rows(["CI OK", *MAC])}, ahead)
    assert q.later_main_passes()
    q.world = World([run(12, Y), run(7, X, lane=True, conclusion="failure")], {12: job_rows(LINUX + MAC)}, ahead)
    assert not q.later_main_passes()


# --- main_rerun_plan, rerun_restoration, main_red_jobs
def test_main_rerun_plan_looks_at_push_runs_only(q):
    q.world = World([run(9, Y, lane=True, conclusion="failure")])
    assert q.main_rerun_plan()[0] == "none"
    q.world = World([run(9, Y, lane=True, conclusion="failure"), run(8, Y, conclusion="success")])
    kind, why, _ = q.main_rerun_plan()
    assert kind == "none" and "run 8 is success" in why


def test_rerun_restoration_is_not_hidden_by_a_newer_lane_run(q):
    q.world = World([run(9, Y, lane=True, conclusion="failure"), run(8, Y, attempt=1)])
    assert q.rerun_restoration() == {}  # the push run is attempt 1: nothing to restore, and the lane row does not stop the read


def test_main_red_jobs_names_the_failed_jobs_of_a_lane_run(q):
    q.world = World([run(9, Y, lane=True, conclusion="failure"), run(8, Y)], jobs={9: job_rows(["CI OK", *MAC], "failure")})
    assert q.main_red_jobs() == {"run": 9, "sha": Y, "jobs": MAC}


def test_main_red_jobs_ignores_a_cancelled_lane_run(q):
    q.world = World([run(9, Y, lane=True, conclusion="cancelled"), run(8, Y)])
    assert q.main_red_jobs() == {}


# --- batch proof binding and the train gate
def test_a_lane_run_is_never_the_final_run_of_a_main_batch():
    lane = {"id": 9, "run_attempt": 1, "event": "workflow_dispatch", "head_branch": "main", "head_sha": Y, "status": "completed",
            "conclusion": "success", "path": ".github/workflows/ci.yml", "repository": {"full_name": main_batch.REPOSITORY}}
    assert not main_batch.valid_run(lane, {"run": 9, "prs": [[1, Y]]})


def test_the_train_gate_skips_a_green_or_cancelled_lane_run(q):
    q.world = World([run(9, Z, lane=True), run(8, Y, lane=True, conclusion="cancelled"), run(7, X)])
    verdict = q.hzlib.main_release_gate()
    assert verdict["run"] == 7 and verdict["kind"] == "green"


def test_real_push_run_names_and_a_lane_run_read_side_by_side(q):
    push = {**run(8, Y), "displayTitle": main_batch.PREFIX + Y}  # the push run's name on a split main
    q.world = World([run(9, Y, lane=True, status="in_progress"), push], jobs={8: job_rows(LINUX + [DISPATCH])})
    assert q.main_state() == "green"
