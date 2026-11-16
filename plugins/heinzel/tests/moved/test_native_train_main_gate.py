"""Release train main gate (coordinator decision 2026-11-07): the candidate is the gate; main may be unfinished or red
by KNOWN failures, never by an UNKNOWN one. hz-release's default main-tip-green step is unchanged."""

import json
import subprocess
import sys

import pytest

from moved_support import TOOLS
from steuermaschine_heinzel import state as statedir

TIP = "e" * 40
OLD = "a" * 40
WEB1 = "End-to-end web-1 (ubuntu-24.04)"
MAC = "Test (macos-latest, Python 3.15, unit)"
KNOWN = (f'[[known]]\nmatch = "{MAC}"\nbead = "5ox"\nadded = 2026-11-07\nuntil = 2099-01-01\n'
         '[[known]]\nmatch = "test_an_installed_window_opened_while_hz_is_down"\nbead = "wzd"\nadded = 2026-11-07\nuntil = 2099-01-01\n')
WZD_TEST = "tests/e2e/web_ui_test.py::test_an_installed_window_opened_while_hz_is_down_opens_the_app[chromium]"


def jobs(failed=(), cancelled=(), incomplete=False):
    names = ["CI OK", "Lint", WEB1, MAC, "make setup (macos-latest)"]
    rows = []
    for n in names:
        conclusion = "failure" if n in failed or (n == "CI OK" and (failed or cancelled)) else "cancelled" if n in cancelled else "success"
        rows.append({"name": n, "status": "in_progress" if incomplete and n == "Lint" else "completed", "conclusion": conclusion})
    return {"total_count": len(rows), "jobs": rows}


def run(rid, sha=OLD, status="completed", conclusion="failure", attempt=1):
    return {"databaseId": rid, "headSha": sha, "status": status, "conclusion": conclusion if status == "completed" else None,
            "attempt": attempt, "displayTitle": f"CI {sha}"}


class Flakes:
    def __init__(self, tests):
        self.tests = tests

    def read_attempt(self, repo, rid, attempt):
        return {"tests": self.tests.get((rid, attempt), [])}


@pytest.fixture
def world(monkeypatch, tmp_path):
    sys.path.insert(0, str(TOOLS))
    from steuermaschine_heinzel.native.queue_lib import hzlib

    known = tmp_path / "known.toml"
    known.write_text(KNOWN)
    monkeypatch.setenv("HZ_KNOWN_FAILURES", str(known))
    state = {"rows": [], "jobs": {}, "tests": {}, "known": known}

    def ghj(*args, default=None):
        joined = " ".join(args)
        if args[:2] == ("run", "list"):
            return state["rows"]
        for (rid, attempt), payload in state["jobs"].items():
            if f"runs/{rid}/attempts/{attempt}/jobs" in joined:
                return payload
        return default

    monkeypatch.setattr(hzlib, "ghj", ghj)
    monkeypatch.setattr(hzlib, "resolve_main_runs", lambda rows: rows)
    monkeypatch.setattr(hzlib, "load", lambda name: Flakes(state["tests"]))
    state["gate"] = lambda: hzlib.main_release_gate(TIP)
    return state


def test_first_attempt_green_is_ok(world):
    world["rows"] = [run(1, TIP, conclusion="success")]
    gate = world["gate"]()
    assert gate["verdict"] == "ok" and gate["kind"] == "green" and gate["tip_finished"]


def test_unfinished_tip_is_not_waited_for(world):
    world["rows"] = [run(2, TIP, status="queued"), run(1, OLD, conclusion="success")]
    gate = world["gate"]()
    assert gate["verdict"] == "ok" and gate["sha"] == OLD and not gate["tip_finished"]


def test_red_by_known_job_name_is_ok(world):
    world["rows"] = [run(2, TIP, status="queued"), run(1)]
    world["jobs"][(1, 1)] = jobs(failed=(MAC,))
    gate = world["gate"]()
    assert gate["verdict"] == "ok" and gate["kind"] == "known" and f"{MAC}=KNOWN(5ox)" in gate["why"]


def test_single_job_red_by_known_tests_is_ok(world):
    world["rows"] = [run(1)]
    world["jobs"][(1, 1)] = jobs(failed=(WEB1,))
    world["tests"][(1, 1)] = [WZD_TEST]
    gate = world["gate"]()
    assert gate["verdict"] == "ok" and "KNOWN(wzd) via 1 failing test" in gate["why"]


@pytest.mark.parametrize("tests", [[WZD_TEST, "tests/unit/x_test.py::test_new_bug"], []])
def test_single_job_with_an_unknown_or_no_failing_test_refuses(world, tests):
    world["rows"] = [run(1)]
    world["jobs"][(1, 1)] = jobs(failed=(WEB1,))
    world["tests"][(1, 1)] = tests
    gate = world["gate"]()
    assert gate["verdict"] == "refuse" and f"UNKNOWN failing job(s) {WEB1}" in gate["why"] and "regression candidate" in gate["why"]


def test_two_jobs_not_known_by_name_refuse_even_with_known_tests(world):
    world["rows"] = [run(1)]
    world["jobs"][(1, 1)] = jobs(failed=(WEB1, "Lint"))
    world["tests"][(1, 1)] = [WZD_TEST]
    assert world["gate"]()["verdict"] == "refuse"


def test_an_unknown_job_next_to_a_known_one_refuses(world):
    world["rows"] = [run(1)]
    world["jobs"][(1, 1)] = jobs(failed=(MAC, "Lint"))
    gate = world["gate"]()
    assert gate["verdict"] == "refuse" and "Lint" in gate["why"] and MAC not in gate["why"].split("UNKNOWN")[1]


def test_overdue_known_entry_counts_as_unknown(world):
    world["known"].write_text(KNOWN.replace("until = 2099-01-01", "until = 2026-11-01", 1))
    world["rows"] = [run(1)]
    world["jobs"][(1, 1)] = jobs(failed=(MAC,))
    assert world["gate"]()["verdict"] == "refuse"


def test_runs_where_nothing_failed_are_skipped_not_trusted(world):
    world["rows"] = [run(3, TIP, conclusion="cancelled"), run(2, conclusion="failure"), run(1)]
    world["jobs"][(3, 1)] = jobs(cancelled=(MAC, "make setup (macos-latest)"))
    world["jobs"][(2, 1)] = jobs(cancelled=(MAC,))  # red only through the aggregate CI OK
    world["jobs"][(1, 1)] = jobs(failed=("Lint",))
    gate = world["gate"]()
    assert gate["verdict"] == "refuse" and gate["run"] == 1 and "skipped 2 run(s)" in gate["why"]


def test_no_evidence_at_all_refuses(world):
    world["rows"] = [run(2, TIP, status="in_progress"), run(1, conclusion="cancelled")]
    world["jobs"][(1, 1)] = jobs(cancelled=(MAC,))
    gate = world["gate"]()
    assert gate["verdict"] == "refuse" and "no completed main run with evidence" in gate["why"]


@pytest.mark.parametrize("rows", [None, [], "garbage"])
def test_unreadable_run_list_refuses(world, rows):
    world["rows"] = rows
    assert world["gate"]()["verdict"] == "refuse"


def test_incomplete_job_list_refuses(world):
    world["rows"] = [run(1)]
    world["jobs"][(1, 1)] = jobs(failed=(MAC,), incomplete=True)
    gate = world["gate"]()
    assert gate["verdict"] == "refuse" and "unreadable or incomplete" in gate["why"]


def test_restored_by_rerun_of_known_is_ok(world):
    world["rows"] = [run(1, conclusion="success", attempt=2)]
    world["jobs"][(1, 1)] = jobs(failed=(MAC,))
    gate = world["gate"]()
    assert gate["verdict"] == "ok" and gate["kind"] == "restored" and "restored-by-rerun" in gate["why"]


def test_green_rerun_of_an_unknown_failure_refuses(world):
    world["rows"] = [run(1, conclusion="success", attempt=2)]
    world["jobs"][(1, 1)] = jobs(failed=("Lint",))
    gate = world["gate"]()
    assert gate["verdict"] == "refuse" and "green only on rerun of an UNKNOWN failure" in gate["why"]


# ---- hz-release: --candidate-gate only on the train path; the default step is unchanged

@pytest.fixture
def rel(tool, home, monkeypatch):
    rel = tool("hz-release")
    monkeypatch.setattr(rel.hzlib, "gh", lambda *a: (0, TIP))
    return rel


def test_candidate_gate_refuses_unknown_and_remembers_a_pass_outside_the_receipt(rel, home, monkeypatch):
    calls = []
    monkeypatch.setattr(rel, "release_pr", lambda tag: None)
    verdict = {"verdict": "refuse", "kind": "unknown", "why": "run 1 at aaa failure: UNKNOWN failing job(s) Lint", "run": 1}
    monkeypatch.setattr(rel.hzlib, "main_release_gate", lambda tip: calls.append(tip) or verdict)
    with pytest.raises(rel.Stop, match="main gate \\(train path\\) refused: .*UNKNOWN failing job"):
        rel.main_candidate_gate("0.9.29")
    verdict.update(verdict="ok", kind="known", why="red-by-KNOWN: x=KNOWN(wzd)")
    assert rel.main_candidate_gate("0.9.29") is True
    memo = json.loads((statedir.root() / "release-main-gate.json").read_text())
    assert memo["version"] == "0.9.29" and memo["kind"] == "known"
    assert not (statedir.root() / "release.json").exists()  # never a receipt field: pause/prepare-only key sets stay exact
    verdict.update(verdict="refuse")  # e.g. the train cancelled the unstarted tip run after the candidate started
    assert rel.main_candidate_gate("0.9.29") is True and calls == [TIP, TIP]
    with pytest.raises(rel.Stop):
        rel.main_candidate_gate("0.9.30")


def test_candidate_gate_rechecks_exact_pr_when_main_is_unknown(rel, home, monkeypatch):
    monkeypatch.setattr(rel.hzlib, "main_release_gate", lambda tip: {"verdict": "refuse", "why": "UNKNOWN"})
    monkeypatch.setattr(rel, "release_pr", lambda tag: {"number": 17, "state": "OPEN"} if tag == "v0.9.29" else None)
    calls = []
    monkeypatch.setattr(rel.release_candidate_policy, "verify_pr", lambda number: calls.append(number) or {"run_id": 42})
    assert rel.main_candidate_gate("0.9.29") is True
    assert rel.main_candidate_gate("0.9.29") is True
    assert calls == [17, 17]
    assert not (statedir.root() / "release-main-gate.json").exists()


def test_candidate_gate_refuses_when_exact_candidate_is_unverifiable(rel, monkeypatch):
    monkeypatch.setattr(rel.hzlib, "main_release_gate", lambda tip: {"verdict": "refuse", "why": "UNKNOWN"})
    monkeypatch.setattr(rel, "release_pr", lambda tag: {"number": 17, "state": "OPEN"})
    def fail(number):
        raise ValueError("invalid candidate")
    monkeypatch.setattr(rel.release_candidate_policy, "verify_pr", fail)
    with pytest.raises(rel.Stop, match="UNKNOWN"):
        rel.main_candidate_gate("0.9.29")


def test_default_release_path_still_requires_first_attempt_green_tip(rel, monkeypatch):
    monkeypatch.setattr(rel.hzlib, "resolve_main_runs", lambda rows: rows)
    monkeypatch.setattr(rel, "runs", lambda *a, **kw: [run(1, TIP, status="queued")])
    assert rel.main_tip_green() is False
    monkeypatch.setattr(rel, "runs", lambda *a, **kw: [run(1, TIP, conclusion="success", attempt=2)])
    with pytest.raises(rel.Stop, match="first-attempt"):
        rel.main_tip_green()


@pytest.mark.parametrize("argv", [["patch", "--candidate-gate"], ["--plugin", "clock", "--candidate-gate"],
                                  ["0.9.29", "--pause", "--candidate-gate"]])
def test_candidate_gate_is_refused_outside_an_exact_core_release(rel, argv):
    with pytest.raises(SystemExit) as exc:
        rel.main(argv)
    assert exc.value.code == 2


def test_help_texts_name_the_candidate_gate(tmp_path):
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    train = subprocess.run([sys.executable, "-c", "import sys; from steuermaschine_heinzel.native.train_lib import train; sys.exit(train.main(sys.argv[1:]))", "--help"], capture_output=True, text=True, timeout=30, env=env)
    assert train.returncode == 0 and "the CANDIDATE is the gate" in train.stdout and "never cancelled" in train.stdout
    assert '"--candidate-gate"' in (TOOLS.parent / "train_lib" / "train.py").read_text()
