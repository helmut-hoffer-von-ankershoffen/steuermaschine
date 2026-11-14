"""The core's CI log reading (`steuermaschine.github.logs`): pure functions on log text and on a check rollup."""

from steuermaschine.github import logs

LOG = "\n".join([
    "Lint\tRun ruff\t2026-11-13T10:00:00.0000000Z \x1b[31mok\x1b[0m",
    "Lint\tRun ruff\t2026-11-13T10:00:01.0000000Z error: E501 line too long",
    "Tests\tpytest\t2026-11-13T10:00:02.0000000Z FAILED tests/unit/x_test.py::test_y - assert 1 == 2",
    "Tests\tpytest\t2026-11-13T10:00:03.0000000Z All checks passed",
    "Web\tvitest\t2026-11-13T10:00:04.0000000Z  FAIL  src/a.test.ts > renders > the title",
    "E2E\tplaywright\t2026-11-13T10:00:05.0000000Z   1) [chromium] › journeys/login.spec.ts:3:1 › logs in (2.1s)",  # noqa: RUF001
])


def test_clean_splits_job_and_step_and_strips_stamps_and_colours():
    rows = logs.clean(LOG)
    assert rows[0] == ("Lint / Run ruff", "ok") and rows[2][0] == "Tests / pytest" and not rows[2][1].startswith("2026")
    assert logs.clean("plain line") == [("", "plain line")]


def test_condense_keeps_error_places_by_step_and_the_tail():
    out = logs.condense(logs.clean(LOG), k=6, tail=2)
    assert "[Lint / Run ruff]" in out and any("E501" in line for line in out) and any("x_test.py::test_y" in line for line in out)
    assert out[-3] == "--- last 2 lines ---" and len(out[-1]) <= 200
    assert logs.condense(["no problem here"], tail=1) == ["(no error-looking line found)", "--- last 1 lines ---", "no problem here"]


def test_failing_tests_of_three_runners():
    assert logs.failing_tests(LOG) == ["tests/unit/x_test.py::test_y", "src/a.test.ts > renders > the title", "[chromium] › journeys/login.spec.ts:3:1 › logs in"]  # noqa: RUF001


def test_failing_runs_and_checks_without_a_run():
    rollup = [{"name": "Lint", "conclusion": "FAILURE", "detailsUrl": "https://github.com/o/r/actions/runs/7/job/1"},
              {"name": "gate", "conclusion": "TIMED_OUT", "detailsUrl": "https://github.com/o/r/runs/99"},
              {"name": "Build", "conclusion": "SUCCESS", "detailsUrl": "https://github.com/o/r/actions/runs/7/job/2"}]
    assert logs.failing_runs(rollup) == {"7": ["Lint"]} and [c["name"] for c in logs.attached_checks(rollup)] == ["gate"]
    assert logs.failing_runs([]) == {} and logs.attached_checks([]) == []
