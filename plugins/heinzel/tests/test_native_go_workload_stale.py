"""go: main CI is labelled by what it actually ran (focused CI is never shown as the full candidate), and the next
view drops suggestions for PRs that are closed or merged now (heinzel-management-iy04)."""

import policy_kit as kit
import pytest

from steuermaschine_heinzel.native.go_lib import decide, probes, views

FULL = frozenset({"Coverage", "Build", "CI OK"})
FOCUSED_MAIN = [{"name": "Focused tests (python)", "conclusion": "success"}, {"name": "CI OK", "conclusion": "success"},
                {"name": "Coverage", "conclusion": "skipped"}, {"name": "Build", "conclusion": "skipped"}]


@pytest.mark.parametrize("jobs, expected", [
    (FOCUSED_MAIN, "focused"),
    ([{"name": n, "conclusion": "success"} for n in FULL], "full"),
    ([{"name": "Coverage", "conclusion": "success"}, {"name": "Build", "conclusion": "failure"}, {"name": "CI OK", "conclusion": "success"}], "unknown"),
    ([{"name": "Focused tests (python)", "conclusion": None}], "unknown"),  # still running: nothing ran yet
    ([], "unknown"), (None, "unknown"), ([1], "unknown"),
])
def test_the_workload_comes_from_the_jobs_that_ran(jobs, expected):
    assert decide.workload(jobs, FULL) == expected


class _World:
    repo = "o/heinzel"

    def __init__(self, answers):
        self.answers = answers

    def gh_json(self, args, timeout=12):
        return self.answers.get(args[1].removeprefix(f"repos/{self.repo}/"))


def test_the_full_candidate_set_is_the_success_inventory_of_the_commits_release_policy():
    sha = "a" * 40
    world = _World(kit.contents_routes(sha, kit.files("new")))
    full = probes.full_candidate_jobs(world, sha)
    assert full == {n for n, c in kit.expected_jobs("workflow_dispatch", "new").items() if c == "success"}
    assert "CI OK" in full and not any(name.startswith("Focused tests") for name in full)
    assert probes.full_candidate_jobs(_World({}), sha) is None  # no policy at that commit: the workload is unknown


def test_main_workload_is_unknown_without_a_policy_at_main(monkeypatch):
    sha = "a" * 40
    runs = [{"databaseId": 7, "workflowName": "CI", "event": "push", "headBranch": "main", "headSha": sha,
             "status": "completed", "conclusion": "success"}]
    jobs = {"actions/runs/7/jobs?per_page=100": {"jobs": [{"name": n, "conclusion": "success"}
                                                          for n in kit.expected_jobs("workflow_dispatch", "new")]}}
    world = _World(jobs)
    main_ci = {**decide.main_ci(runs, sha), "sha": sha}
    assert probes.probe_main_workload(world, {"main": {"sha": sha}, "main_ci": main_ci}) == {"run": 7, "workload": "unknown"}
    world.answers.update(kit.contents_routes(sha, kit.files("new")))
    assert probes.probe_main_workload(world, {"main": {"sha": sha}, "main_ci": main_ci})["run"] == 7


def test_exact_main_ci_is_independent_of_general_run_inventory_and_rejects_stale_results():
    sha = "a" * 40
    good = {"databaseId": 7, "workflowName": "CI", "event": "push", "headBranch": "main", "headSha": sha,
            "status": "completed", "conclusion": "success"}

    class MainWorld:
        repo = "o/heinzel"

        def __init__(self, rows):
            self.rows = rows

        def gh_json(self, args, timeout=12):
            assert "--commit" in args and args[args.index("--commit") + 1] == sha
            return self.rows

    state = {"main": {"sha": sha}, "runs": [{"databaseId": 99, "headSha": "b" * 40}],
             **{name: {"unknown": "not probed"} for name in ("releases", "release", "queue", "train", "holds", "rehearse", "slots", "mini", "focus")}}
    assert probes.probe_exact_main_ci(MainWorld([good]), state) == {"sha": sha, "state": "green", "run": 7, "conclusion": "success"}
    newer = {**good, "databaseId": 8, "status": "in_progress", "conclusion": None}
    assert probes.probe_exact_main_ci(MainWorld([good, newer]), state)["state"] == "pending"
    assert probes.probe_exact_main_ci(MainWorld([good, {**newer, "status": "completed", "conclusion": "failure"}]), state)["state"] == "red"
    assert probes.probe_exact_main_ci(MainWorld([{**good, "headSha": "a" * 8 + "b" * 32}]), state)["state"] == "none"
    assert probes.probe_exact_main_ci(MainWorld([{**good, "event": "workflow_dispatch"}]), state)["state"] == "none"
    authoritative = probes.probe_exact_main_ci(MainWorld([good]), state)
    assert "CI green (run 7)" in decide.decide(state | {"main_ci": authoritative}, 0)["facts"][0]
    assert "CI unknown" in decide.decide(state | {"main_ci": {"unknown": "lookup failed"}}, 0)["facts"][0]
    with pytest.raises(RuntimeError):
        probes.probe_exact_main_ci(MainWorld(None), state)
    with pytest.raises(RuntimeError):
        probes.probe_exact_main_ci(MainWorld([good] * 100), state)
    assert probes.probe_main_workload(MainWorld([]), state | {"main_ci": {"sha": "b" * 40, "state": "green", "run": 7}}) == {"run": None, "workload": "unknown"}


def state(workload, run=7):
    return {"main": {"sha": "a" * 40}, "runs": [{"databaseId": 7, "workflowName": "CI", "event": "push", "headBranch": "main", "headSha": "a" * 40,
                                                   "status": "completed", "conclusion": "success"}], "main_workload": {"run": run, "workload": workload},
            **{name: {"unknown": "not probed"} for name in ("releases", "release", "queue", "train", "holds", "rehearse", "slots")}}


@pytest.mark.parametrize("workload, run, label", [
    ("focused", 7, "focused CI, not the full candidate"),
    ("full", 7, "full candidate workload"),
    ("full", 8, "workload unknown"),  # a label for another run never carries over
    ("unknown", 7, "workload unknown"),
])
def test_the_main_fact_names_the_actual_workload(workload, run, label):
    s = state(workload, run)
    ci = decide.main_ci(s["runs"], s["main"]["sha"])
    assert decide.facts(s, ci, None, [])[0] == f"main aaaaaaaa CI green (run 7, {label})"


def test_a_state_without_the_workload_probe_keeps_the_old_line():
    s = {k: v for k, v in state("focused").items() if k != "main_workload"}
    assert decide.facts(s, decide.main_ci(s["runs"], "a" * 40), None, [])[0] == "main aaaaaaaa CI green (run 7)"


def test_an_unread_workload_is_unknown():
    s = state("focused") | {"main_workload": {"unknown": "gh: timeout"}}
    assert decide.facts(s, decide.main_ci(s["runs"], "a" * 40), None, [])[0].endswith("(run 7, workload unknown)")


class World:
    repo = "o/r"

    def __init__(self, states):
        self.states, self.asked = states, []

    def gh_json(self, args, timeout=12):
        number = int(args[2])
        self.asked.append(number)
        if self.states.get(number) is None:
            raise RuntimeError("gh: unreachable")
        return {"state": self.states[number]}


def row(key, what, do=""):
    return {"prio": 3, "key": key, "what": what, "do": do, "rank": 3}


def test_rows_naming_closed_or_merged_prs_are_not_offered_and_open_or_unknown_ones_stay():
    w = World({568: "CLOSED", 575: "MERGED", 590: None})
    rows = [row("queue-568", "queue: 568 needs work"), row("queue-575", "queue: 575 needs work"), row("queue-804", "queue: 804 needs work"),
            row("queue-590", "queue: 590 needs work"), row("paused-x", "paused: revive PR #568 after the fix"),
            row("queue-plugins#71", "queue: plugins #71 needs work"), row("paused-y", "paused: no PR named")]
    kept, stale = views.current_pr_rows(w, {"prs": [{"number": 804}]}, rows)
    assert [r["key"] for r in kept] == ["queue-804", "queue-590", "queue-plugins#71", "paused-y"]
    assert kept[1]["what"].endswith("(PR #590 state unknown)")
    assert stale == ["queue: 568 needs work (#568 CLOSED)", "queue: 575 needs work (#575 MERGED)", "paused: revive PR #568 after the fix (#568 CLOSED)"]
    assert sorted(w.asked) == [568, 575, 590]  # open PRs and other repositories are never read; each number once
