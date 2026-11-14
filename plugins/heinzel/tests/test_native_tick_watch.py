"""`sm ci watch`: the moved tests of the former watch tool and of the helper functions it took from the shared
library, the merge train and the flake counter; then the command through the whole command tree, on fakes.

Moved tests keep their assertions. Changed: the imports, and the fake of `read_attempt`'s GitHub call (it returns
exit code and text now); the assertion that a second read comes from a cache is gone with the cache (hub.py)."""

import json

import pytest
from steuermaschine_heinzel.native import tick
from steuermaschine_heinzel.native.tick_lib import checks, hub
from tick_support import REPO, build, contract_gaps

from steuermaschine.github.testing import FakeGh
from steuermaschine.testing import invoke
from steuermaschine.testing.fakes import FakeClock

CORE = "helmut-hoffer-von-ankershoffen/heinzel"
PLUGINS = "helmut-hoffer-von-ankershoffen/heinzel-plugins"


@pytest.fixture(autouse=True)
def fresh_library(monkeypatch):
    """Every test starts with an unbound library and the former poll interval."""
    monkeypatch.setattr(hub, "EDGE", hub.Edge())
    monkeypatch.setattr(hub, "CORE", CORE)
    monkeypatch.setattr(checks, "POLL", 30)


@pytest.fixture
def lib():
    return hub


@pytest.fixture
def tool():
    return lambda _name: checks


def check(name, conclusion="SUCCESS", status="COMPLETED", run=None):
    url = f"https://github.com/o/r/actions/runs/{run}/job/1" if run else ""
    return {"name": name, "status": status, "conclusion": conclusion, "detailsUrl": url}


class Fake:
    """gh stand-in: `views` maps (subcommand, number) to a list of replies (the last repeats); other calls are recorded."""

    def __init__(self, **replies):
        self.replies, self.calls = {k: list(v) for k, v in replies.items()}, []

    def __call__(self, *args):
        self.calls.append(args)
        key = " ".join(args[:2]) + (" " + args[2] if args[0] in ("pr", "run") and args[1] == "view" else "")
        for name, queue in self.replies.items():
            if key.replace(" ", "_") == name or key.startswith(name.replace("_", " ")):
                reply = queue.pop(0) if len(queue) > 1 else queue[0]
                return 0, json.dumps(reply) if not isinstance(reply, str) else reply
        return 0, "{}"


def patch(monkeypatch, module, lib, fake, **extra):
    monkeypatch.setattr(lib, "gh", fake)
    monkeypatch.setattr(lib, "sleep", lambda s: None)
    monkeypatch.setattr(lib, "ghj", lambda *a, default=None: json.loads(fake(*a)[1]) or default)
    for name, value in extra.items():
        monkeypatch.setattr(lib, name, value)


# ----------------------------------------------------------------------------- moved: the watch
def test_watch_parses_runs_prs_and_urls(tool, lib):
    w = tool("watch")
    assert w.parse(["41295168692"], lib.CORE) == ("run", lib.CORE, "41295168692")
    assert w.parse(["393"], lib.CORE) == ("pr", lib.CORE, "393")
    assert w.parse(["plugins", "38"], lib.CORE) == ("pr", lib.PLUGINS, "38")
    assert w.parse(["https://github.com/a/b/pull/7"], lib.CORE) == ("pr", "a/b", "7")
    assert w.parse(["https://github.com/a/b/actions/runs/99"], lib.CORE) == ("run", "a/b", "99")


def test_watch_prints_failing_tests_of_a_red_pr(tool, lib, monkeypatch, capsys):
    w = tool("watch")
    rollup = [check("Lint"), check("E2E", "FAILURE", run=555), check("review", "FAILURE", run=556)]
    fake = Fake(pr_view=[{"state": "OPEN", "statusCheckRollup": rollup}])
    patch(monkeypatch, w, lib, fake, failing_tests=lambda repo, run: ["a_test.py::test_x"] if run == 555 else ["WRONG"])
    code, text = w.watch_pr(lib.CORE, "7", 1e18)
    assert code == 1
    assert text == "PR #7: red\n  failed: E2E\n  tests:\n    a_test.py::test_x"


def test_watch_waits_then_reports_green_and_ignores_review(tool, lib, monkeypatch):
    w = tool("watch")
    pending = {"state": "OPEN", "statusCheckRollup": [check("CI OK", "", "IN_PROGRESS")]}
    done = {"state": "OPEN", "statusCheckRollup": [check("CI OK"), check("review", "FAILURE")]}
    patch(monkeypatch, w, lib, Fake(pr_view=[pending, done]))
    assert w.watch_pr(lib.CORE, "7", 1e18) == (0, "PR #7: green")


def test_watch_run_outcomes_and_timeout(tool, lib, monkeypatch):
    w = tool("watch")
    patch(monkeypatch, w, lib, Fake(run_view=[{"status": "completed", "conclusion": "success", "jobs": []}]))
    assert w.watch_run(lib.CORE, "123", 1e18) == (0, "run 123: success")
    red = {"status": "completed", "conclusion": "failure", "jobs": [{"name": "Unit", "conclusion": "failure"}, {"name": "Lint", "conclusion": "success"}]}
    patch(monkeypatch, w, lib, Fake(run_view=[red]), failing_tests=lambda r, i: ["t::a"])
    assert w.watch_run(lib.CORE, "123", 1e18) == (1, "run 123: failure\n  failed: Unit\n  tests:\n    t::a")
    patch(monkeypatch, w, lib, Fake(run_view=[{"status": "in_progress"}]))
    assert w.watch_run(lib.CORE, "123", 0)[0] == 2


def test_keys(lib):
    assert lib.split_key(["plugins", "#38"]) == (lib.PLUGINS, "38", "plugins #38")
    assert lib.split_key("393") == (lib.CORE, "393", "393")
    with pytest.raises(ValueError):
        lib.split_key(["nope", "1"])


# ----------------------------------------------------------------------------- moved: the duplicated helpers
def test_a_check_still_running_is_waited_for():
    module = hub
    assert not module.settled([check("a"), check("b", conclusion="", status="IN_PROGRESS")])
    assert not module.settled([])
    assert module.settled([check("a"), check("b")])


def test_test_names_come_from_pytest_vitest_and_playwright_lines():
    flakes = hub
    log = "\n".join(
        [
            "Test\tstep\t2026-11-01T10:00:00.1Z FAILED tests/unit/a_test.py::test_one - assert 1 == 2",
            "E2E\tstep\t2026-11-01T10:00:00.2Z   1) [chromium] › web/e2e/nav.spec.ts:12:3 › opens the doctor report (1.2s)",  # noqa: RUF001
            "Web\tstep\t2026-11-01T10:00:00.3Z  FAIL  src/chat.test.ts > chat > sends",
            "nothing here",
        ]
    )
    assert flakes.test_names(log) == ["tests/unit/a_test.py::test_one", "[chromium] › web/e2e/nav.spec.ts:12:3 › opens the doctor report", "src/chat.test.ts > chat > sends"]  # noqa: RUF001


def test_logs_are_fetched_only_for_failed_jobs(monkeypatch):
    flakes = hub
    calls = []

    def fake_gh(*args, **_kw):
        calls.append(args)
        if "--log-failed" in args:
            return 0, "FAILED tests/a.py::test_x"
        return 0, json.dumps({"jobs": [{"name": "a", "conclusion": "success"}]})

    monkeypatch.setattr(flakes, "gh", fake_gh)
    assert flakes.read_attempt("o/r", 5, 1) == {"jobs": [], "tests": []}
    assert not any("--log-failed" in c for c in calls)
    monkeypatch.setattr(flakes, "gh", lambda *a, **_kw: (0, json.dumps({"jobs": [{"name": "a", "conclusion": "failure"}]}) if "--log-failed" not in a else "FAILED tests/a.py::test_x"))
    assert flakes.read_attempt("o/r", 6, 1) == {"jobs": ["a"], "tests": ["tests/a.py::test_x"]}


def test_failed_runs_are_the_run_ids_behind_failed_checks_without_the_review():
    rollup = [check("Lint"), check("E2E", "FAILURE", run=555), check("Unit", "TIMED_OUT", run=555), check("review", "FAILURE", run=556),
              check("Docs", "CANCELLED", run=557), check("No url", "FAILURE")]
    assert hub.failed_runs(rollup) == [555, 557]


# ----------------------------------------------------------------------------- the command, through the tree
def pr_view(*rollup, state="OPEN"):
    return {"state": state, "statusCheckRollup": list(rollup)}


def test_a_green_pr_in_text_and_json_after_one_poll(tmp_path, monkeypatch):
    gh, clock = FakeGh(), FakeClock()
    gh.on("pr view 795", pr_view(check("CI OK", "", "IN_PROGRESS")), pr_view(check("CI OK"), check("review", "FAILURE")))
    done = invoke(build(tmp_path, monkeypatch, gh, clock), ["ci", "watch", "795", "--timeout", "40"])
    assert (done.exit_code, done.stdout) == (0, "PR #795: green\n")
    assert clock.slept == [30]  # one poll through the clock provider, no real sleep
    assert gh.lines() == [f"pr view 795 -R {REPO} --json state,statusCheckRollup"] * 2 and not gh.writes()
    gh = FakeGh().on("pr view 795", pr_view(check("CI OK")))
    doc = json.loads(invoke(build(tmp_path, monkeypatch, gh), ["--format", "json", "ci", "watch", "795"]).stdout)
    result = tick.Watched.model_validate(doc)
    assert (result.kind, result.repository, result.number, result.outcome, result.code, result.failed, result.tests) == ("pr", REPO, 795, "green", 0, [], [])


def test_a_red_pr_names_the_failing_checks_and_the_tests_from_the_log(tmp_path, monkeypatch):
    gh = FakeGh()
    gh.on("pr view 7", pr_view(check("Lint"), check("E2E", "FAILURE", run=555), check("review", "FAILURE", run=556)))
    gh.on("run view 555", {"attempt": 2})
    gh.on("--attempt 2 --json jobs", {"jobs": [{"name": "E2E", "conclusion": "failure"}, {"name": "Lint", "conclusion": "success"}]})
    gh.on("--attempt 2 --log-failed", "E2E\tstep\t2026-11-01T10:00:00.1Z FAILED a_test.py::test_x - assert 1 == 2\n")
    done = invoke(build(tmp_path, monkeypatch, gh), ["ci", "watch", "7"])
    assert done.exit_code == 1
    assert done.stdout == "PR #7: red\n  failed: E2E\n  tests:\n    a_test.py::test_x\n"
    assert not any("556" in line for line in gh.lines())  # the advisory review check's run is never read
    doc = json.loads(invoke(build(tmp_path, monkeypatch, gh), ["--format", "json", "ci", "watch", "7"]).stdout)
    result = tick.Watched.model_validate(doc)
    assert (result.outcome, result.code, result.failed, result.tests) == ("red", 1, ["E2E"], ["a_test.py::test_x"])


def test_merged_closed_and_another_repository(tmp_path, monkeypatch):
    gh = FakeGh().on("pr view 38", pr_view(state="MERGED"))
    done = invoke(build(tmp_path, monkeypatch, gh), ["ci", "watch", "plugins", "38"])
    assert (done.exit_code, done.stdout) == (0, "PR plugins #38: merged\n") and gh.lines()[0].startswith(f"pr view 38 -R {PLUGINS} ")
    gh = FakeGh().on("pr view 7", pr_view(state="CLOSED"))
    done = invoke(build(tmp_path, monkeypatch, gh), ["ci", "watch", "https://github.com/a/b/pull/7"])
    assert (done.exit_code, done.stdout) == (1, "PR b #7: closed\n") and gh.lines()[0].startswith("pr view 7 -R a/b ")
    gh = FakeGh().on("pr view 9", pr_view(check("CI OK")))
    assert invoke(build(tmp_path, monkeypatch, gh), ["ci", "watch", "9", "-R", "a/b"]).stdout == "PR b #9: green\n"


def test_repo_option_reads_the_named_repository_for_a_pr_and_a_run(tmp_path, monkeypatch):
    # bead vyeh: a plugins PR given as a bare number is read from the plugins repository, never from Core
    for repo in ("plugins", PLUGINS):
        gh = FakeGh().on("pr view 38", pr_view(check("CI OK")))
        done = invoke(build(tmp_path, monkeypatch, gh), ["ci", "watch", "38", "--repo", repo])
        assert (done.exit_code, done.stdout) == (0, "PR plugins #38: green\n") and gh.lines()[0].startswith(f"pr view 38 -R {PLUGINS} ")
        assert not any(f"-R {CORE} " in line for line in gh.lines())
    gh = FakeGh().on("run view 41295168692", {"status": "completed", "conclusion": "success", "jobs": []})
    doc = invoke(build(tmp_path, monkeypatch, gh), ["--format", "json", "ci", "watch", "41295168692", "-R", "plugins"])
    result = tick.Watched.model_validate(json.loads(doc.stdout))
    assert (result.kind, result.repository, result.outcome) == ("run", PLUGINS, "success")
    assert gh.lines()[0].startswith(f"run view 41295168692 -R {PLUGINS} ")


def test_an_unknown_repository_word_is_refused_as_usage_before_any_read(tmp_path, monkeypatch):
    gh = FakeGh()
    bad = invoke(build(tmp_path, monkeypatch, gh), ["ci", "watch", "38", "--repo", "nope"])
    assert bad.exit_code == 2 and "--repo nope: neither OWNER/NAME nor a repository word" in bad.stderr and not gh.calls


def test_a_run_green_red_and_the_timeout(tmp_path, monkeypatch):
    gh = FakeGh().on("run view 41295168692", {"status": "completed", "conclusion": "success", "jobs": []})
    done = invoke(build(tmp_path, monkeypatch, gh), ["ci", "watch", "41295168692"])
    assert (done.exit_code, done.stdout) == (0, "run 41295168692: success\n")
    gh = FakeGh()
    gh.on("run view 41295168692", {"status": "completed", "conclusion": "failure", "jobs": [{"name": "Unit", "conclusion": "failure"}, {"name": "Lint", "conclusion": "success"}]})
    gh.on("--json attempt", {"attempt": 1})
    gh.on("--attempt 1 --json jobs", {"jobs": [{"name": "Unit", "conclusion": "failure"}]})
    gh.on("--attempt 1 --log-failed", "FAILED t::a\n")
    doc = invoke(build(tmp_path, monkeypatch, gh), ["--format", "json", "ci", "watch", "41295168692"])
    result = tick.Watched.model_validate(json.loads(doc.stdout))
    assert doc.exit_code == 1 and (result.kind, result.outcome, result.failed, result.tests) == ("run", "failure", ["Unit"], ["t::a"])
    assert result.text == "run 41295168692: failure\n  failed: Unit\n  tests:\n    t::a"
    gh, clock = FakeGh().on("run view 41295168692", {"status": "in_progress"}), FakeClock()
    late = invoke(build(tmp_path, monkeypatch, gh, clock), ["ci", "watch", "41295168692", "--timeout", "1"])
    assert (late.exit_code, late.stdout) == (2, "run 41295168692: timeout\n")
    assert clock.slept == [30, 30, 30] and not gh.writes()  # it polled for the minute and cancelled nothing


def test_what_is_not_a_target_is_refused_as_usage_before_any_read(tmp_path, monkeypatch):
    gh = FakeGh()
    bad = invoke(build(tmp_path, monkeypatch, gh), ["ci", "watch", "nope", "1"])
    assert bad.exit_code == 2 and "not a PR: nope 1" in bad.stderr and not gh.calls
    assert invoke(build(tmp_path, monkeypatch, gh), ["ci", "watch"]).exit_code == 2


def test_the_command_is_read_only_and_its_help_meets_the_contract():
    command = tick.ci_watch.__sm_native__
    assert command.writes is False and command.replaces == ("hz-watch",) and command.path == "ci watch"
    assert contract_gaps(tick.ci_watch) == {}
    assert "hz-" not in command.help
