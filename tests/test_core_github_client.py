"""The one `gh` client: timeout, one retry on a transient error, bounded pages, reasons instead of tracebacks."""

import pytest

from steuermaschine.github import Gh, GhError
from steuermaschine.github.testing import FakeGh
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeClock

R = "o/r"


def test_run_passes_the_timeout_and_returns_the_result():
    seen = {}

    class C:
        def run(self, argv, cwd=None, timeout=None):
            seen.update(argv=argv, cwd=cwd, timeout=timeout)
            return Result(0, "ok", "")

    res = Gh(C(), R, timeout=7, cwd="/w").run(["pr", "view", 12])
    assert res.out == "ok" and seen == {"argv": ["gh", "pr", "view", "12"], "cwd": "/w", "timeout": 7}


@pytest.mark.parametrize("first", [Result(124, "", "timed out after 30s: gh"), Result(1, "", "gh: Bad Gateway (HTTP 502)"),
                                   Result(1, "", "Post \"https://api.github.com/graphql\": unexpected EOF"), Result(1, "", "dial tcp: i/o timeout")])
def test_a_transient_failure_is_tried_exactly_once_more(first):
    fake, clock = FakeGh().on("pr view", first, {"number": 1}), FakeClock()
    assert Gh(fake, R, clock=clock).json(["pr", "view", "1"]) == {"number": 1}
    assert len(fake.calls) == 2


def test_two_transient_failures_end_with_the_reason_and_no_third_call():
    fake = FakeGh().on("pr view", Result(1, "", "gh: Server Error (HTTP 500)"))
    data, why = Gh(fake, R).try_json(["pr", "view", "1"])
    assert data is None and why == "Server Error (HTTP 500)" and len(fake.calls) == 2


def test_a_plain_failure_is_not_retried_and_retry_can_be_switched_off():
    fake = FakeGh().on("pr view", 404).on("pr merge", Result(124, "", "timed out"))
    gh = Gh(fake, R)
    assert gh.try_json(["pr", "view", "1"]) == (None, "Not Found (HTTP 404)")
    gh.run(["pr", "merge", "1"], retry=False)
    assert len(fake.calls) == 2


def test_json_raises_gherror_with_one_short_line():
    with pytest.raises(GhError, match="Not Found"):
        Gh(FakeGh().on("x", 404), R).json(["api", "x"])
    with pytest.raises(GhError, match="no JSON"):
        Gh(FakeGh().on("x", "<html>"), R).json(["api", "x"])
    assert Gh(FakeGh().on("x", ""), R).json(["api", "x"]) is None


def test_why_names_a_missing_gh_and_a_timeout():
    assert "not installed" in Gh.why(Result(127, "", "No such file"))
    assert Gh.why(Result(124, "", "timed out after 30s: gh")) == "gh timed out after 30s: gh"
    assert Gh.why(Result(4, "", "")) == "gh exited 4"


def test_the_repository_is_asked_once_when_not_configured_and_missing_is_a_reason():
    fake = FakeGh().on("repo view", "o/r\n").on("repos/o/r/x", {"a": 1})
    gh = Gh(fake)
    assert gh.api("x") == ({"a": 1}, "") and gh.api("x") == ({"a": 1}, "")
    assert sum("repo view" in ln for ln in fake.lines()) == 1
    data, why = Gh(FakeGh().on("repo view", Result(1, "", "not a git repository"))).api("x")
    assert data is None and "[github] repo" in why


def test_api_puts_get_parameters_into_the_url_and_does_not_retry_a_write():
    fake = FakeGh().on("repos/o/r/actions/runs", {"workflow_runs": []}).on("-X POST", Result(1, "", "gh: Bad Gateway (HTTP 502)"))
    gh = Gh(fake, R)
    gh.api("actions/runs", {"status": "queued", "branch": "", "head_sha": None})
    assert fake.lines()[0] == "api repos/o/r/actions/runs?status=queued"
    _, why = gh.api("git/refs", method="POST", fields={"ref": "refs/tags/v1", "sha": "abc"})
    assert "502" in why and fake.lines()[1:] == ["api -X POST repos/o/r/git/refs -f ref=refs/tags/v1 -f sha=abc"]


def test_pages_stop_at_a_short_page_at_the_limit_and_at_max_pages():
    def page(argv):
        n = int(argv[2].split("page=")[-1])
        return {"total_count": 250, "workflow_runs": [{"id": i} for i in range((n - 1) * 100, min(n * 100, 250))]}

    fake = FakeGh().on("actions/runs", page)
    rows, why, truncated = Gh(fake, R, max_pages=5).pages("actions/runs", key="workflow_runs")
    assert (len(rows), why, truncated, len(fake.calls)) == (250, "", False, 3)
    rows, why, truncated = Gh(FakeGh().on("actions/runs", page), R, max_pages=2).pages("actions/runs", key="workflow_runs")
    assert (len(rows), truncated) == (200, True)
    fake = FakeGh().on("actions/runs", page)
    rows, why, truncated = Gh(fake, R).pages("actions/runs", key="workflow_runs", limit=20)
    assert (len(rows), truncated, len(fake.calls)) == (20, True, 1)
    rows, why, truncated = Gh(FakeGh().on("pulls", [{"n": 1}]), R).pages("commits/abc/pulls", limit=5)
    assert (rows, truncated) == ([{"n": 1}], False)
    rows, why, truncated = Gh(FakeGh().on("actions/runs", 403), R).pages("actions/runs", key="workflow_runs")
    assert rows == [] and "403" in why


def test_shown_quotes_for_a_shell():
    assert Gh(FakeGh(), R).shown(["pr", "create", "--title", "a b"]) == "gh pr create --title 'a b'"
