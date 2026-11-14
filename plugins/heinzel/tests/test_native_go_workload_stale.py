"""go: main CI is labelled by what it actually ran (focused CI is never shown as the full candidate), and the next
view drops suggestions for PRs that are closed or merged now (heinzel-management-iy04)."""

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


def test_the_full_candidate_set_is_the_pinned_readers_success_inventory():
    full = probes.full_candidate_jobs()
    assert "Coverage" in full and "CI OK" in full and not any(name.startswith("Focused tests") for name in full)


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
