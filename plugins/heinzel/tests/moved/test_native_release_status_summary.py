import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from steuermaschine_heinzel.native.release_read_lib import status as m  # noqa: E402

TRAIN = {"version": "0.9.29", "phase": "release", "start": 1000.0}
PR = {"number": 612, "mergeable": "MERGEABLE", "checks": {"ok": 59, "fail": 2, "run": 1, "skip": 12}}


def test_counts():
    c = m.counts([{"status": "COMPLETED", "conclusion": "SUCCESS"}, {"status": "COMPLETED", "conclusion": "FAILURE"},
                  {"status": "IN_PROGRESS", "conclusion": ""}, {"status": "COMPLETED", "conclusion": "SKIPPED"}])
    assert c == {"ok": 1, "fail": 1, "run": 1, "skip": 1}


def test_idle_is_not_active():
    assert m.render(m.summarize({"phase": "idle"}, None, [], [], False, 0)) == "no release in flight"


def test_flake_and_acceptance_in_progress():
    runs = [{"name": "CI", "status": "completed", "conclusion": "failure", "attempt": 1},
            {"name": "Acceptance", "status": "in_progress", "conclusion": "", "attempt": 2}]
    s = m.summarize(TRAIN, PR, runs, ["Test (macos-latest, Python 3.14, integration)"], True, 1000 + 3600)
    out = m.render(s)
    assert "age 60 min" in out and "freeze: ON" in out and "integration" in out and "attempt 2" in out
    assert "wait for Acceptance" in s["next"]


def test_failed_without_inflight_points_to_known_failures():
    runs = [{"name": "CI", "status": "completed", "conclusion": "failure", "attempt": 1}]
    assert "known-failures" in m.summarize(TRAIN, PR, runs, [], False, 2000)["next"]


def test_run_names_carry_sha_and_padding():
    runs = [{"name": "CI 4c0c87f", "status": "completed", "conclusion": "failure", "attempt": 1},
            {"name": "Acceptance  ", "status": "completed", "conclusion": "success", "attempt": 2}]
    s = m.summarize(TRAIN, PR, runs, ["x"], False, 2000)
    assert s["ci"] == "failure" and s["acc"] == "success" and s["acc_attempt"] == 2
