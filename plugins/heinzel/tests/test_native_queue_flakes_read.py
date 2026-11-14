"""Failing jobs and tests of one run attempt, read from its log once and cached (queue_lib/flakes_read.py)."""
# ruff: noqa: RUF001  (Playwright prints U+203A between the parts of a test name)

import json
from types import SimpleNamespace

from steuermaschine_heinzel.native.queue_lib import flakes_read as fr

LOG = "\n".join([
    "unit\tstep\t2026-11-13T10:00:00.123Z FAILED tests/unit/a_test.py::test_x - assert 1",
    "web\tstep\t2026-11-13T10:00:01Z  FAIL  src/a.test.ts > renders > the logo",
    "e2e\tstep\t  1) [chromium] › journeys/login.spec.ts:3:1 › logs in (12s)",
    "e2e\tstep\t  ✘  2 [webkit] › journeys/chat.spec.ts:9:1 › sends",
    "unit\tstep\tsomething else",
])


def test_test_names_from_pytest_vitest_and_playwright_lines():
    assert fr.test_names(LOG) == ["tests/unit/a_test.py::test_x", "src/a.test.ts > renders > the logo",
                                  "[chromium] › journeys/login.spec.ts:3:1 › logs in", "[webkit] › journeys/chat.spec.ts:9:1 › sends"]


def test_gh_returns_stdout_or_nothing(monkeypatch):
    monkeypatch.setattr(fr.edge, "run", lambda argv, **kw: SimpleNamespace(returncode=0, stdout="out"))
    assert fr.gh("api", "x") == "out"
    monkeypatch.setattr(fr.edge, "run", lambda argv, **kw: SimpleNamespace(returncode=1, stdout="err"))
    assert fr.gh("api", "x") == ""


def test_read_attempt_fetches_the_log_only_for_failed_jobs_and_caches(monkeypatch, tmp_path):
    monkeypatch.setattr(fr, "CACHE", tmp_path)
    calls = []

    def gh(*args):
        calls.append(args)
        if "--json" in args:
            return json.dumps({"jobs": [{"name": "unit", "conclusion": "failure"}, {"name": "lint", "conclusion": "success"}]})
        return LOG

    monkeypatch.setattr(fr, "gh", gh)
    first = fr.read_attempt("o/r", 7, 1)
    assert first["jobs"] == ["unit"] and len(first["tests"]) == 4 and len(calls) == 2
    assert fr.read_attempt("o/r", 7, 1) == first and len(calls) == 2  # cached
    assert (tmp_path / "o_r-7-1.log").read_text() == LOG


def test_read_attempt_does_not_cache_a_failed_gh(monkeypatch, tmp_path):
    monkeypatch.setattr(fr, "CACHE", tmp_path)
    monkeypatch.setattr(fr, "gh", lambda *args: "")
    assert fr.read_attempt("o/r", 8, 2) == {"jobs": [], "tests": []}
    assert not list(tmp_path.iterdir())
