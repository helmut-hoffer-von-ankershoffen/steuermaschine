"""`sm ci flakes`: the daily count, --group, the cache of finished attempts, the batch resolution of the core repository.

The expected texts are the former counter's output for the same rows (its format strings, kept in the models' render).
"""

import json
from types import SimpleNamespace

import pytest
from sm_runtime_kit import runtime_with

from steuermaschine.protocols import Result
from steuermaschine.testing import FakeClock, FakeCommands, invoke
from steuermaschine_heinzel.native import flakes
from steuermaschine_heinzel.native import queue as queue_commands
from steuermaschine_heinzel.native.flakes_lib import count

REPO = "o/r"
NOW = 1_763_000_000.0  # 2025-11-13T02:13:20Z


def run(rid, day, conclusion="success", attempt=1, branch="main", event="push", status="completed"):
    return {"databaseId": rid, "attempt": attempt, "conclusion": conclusion, "status": status, "headBranch": branch,
            "headSha": f"s{rid}", "event": event, "createdAt": f"2025-11-{day:02d}T10:00:00Z", "displayTitle": f"t{rid}"}


RUNS = [run(1, 12), run(2, 12, attempt=2), run(3, 11, "failure"), run(4, 11, "cancelled"),
        run(5, 12, "failure", branch="feat", event="pull_request"), run(6, 1)]  # 6 is older than --days
JOBS = {  # (run, attempt) -> gh run view --json jobs
    (2, 1): [{"name": "unit (macos)", "conclusion": "failure"}, {"name": "CI OK", "conclusion": "failure"}],
    (3, 1): [{"name": "web", "conclusion": "failure"}],
    (5, 1): [{"name": "web", "conclusion": "failure"}],
}
LOGS = {(2, 1): "2025-11-12T10:00:00.1Z FAILED tests/a_test.py::test_x - boom\n", (3, 1): " FAIL  web/x.test.ts > renders\n", (5, 1): ""}


def fake_gh(argv):
    args = argv[1:]
    if args[:2] == ["run", "list"]:
        listed = RUNS if "--branch" not in args else [r for r in RUNS if r["headBranch"] == "main"]
        return Result(0, json.dumps(listed), "")
    rid = int(args[2])
    attempt = int(args[args.index("--attempt") + 1]) if "--attempt" in args else None
    if "--log-failed" in args:
        return Result(0, LOGS[(rid, attempt)], "")
    if attempt is None:  # the latest attempt's jobs
        latest = {1: [{"name": "unit (macos)", "conclusion": "success"}], 2: [{"name": "unit (macos)", "conclusion": "success"}],
                  3: [{"name": "web", "conclusion": "failure"}], 6: [{"name": "lint", "conclusion": "success"}]}
        return Result(0, json.dumps({"jobs": latest.get(rid, [])}), "")
    return Result(0, json.dumps({"jobs": JOBS.get((rid, attempt), [])}), "")


@pytest.fixture
def cmd(tmp_path, monkeypatch):
    monkeypatch.setattr(flakes, "resolve", lambda repo, listed: listed)

    def build(conf=None):
        commands = FakeCommands()
        commands.on(["gh"], fake_gh)
        clock = FakeClock()
        clock.t = NOW
        conf = {"repo": REPO, "ci_flakes_cache_dir": str(tmp_path / "cache"), **(conf or {})}
        return runtime_with(tmp_path, conf, commands=commands, clock=clock), commands

    return build


def test_the_daily_count_in_the_former_format(cmd):
    runtime, _ = cmd()
    done = invoke(runtime, ["ci", "flakes", "--days", "7"])
    assert done.exit_code == 0, done
    assert done.stdout.splitlines() == [
        "day         scope  runs  clean  failed  flakes(rerun passed)  cancelled",
        "2025-11-12  pr        1      0       1                     0          0",
        "2025-11-12  main      2      1       0                     1          0",
        "2025-11-11  main      2      0       1                     0          1",
        "",
        "Failing jobs (failed attempts):",
        "    2  web",
        "    1  unit (macos)",
        "",
        "Failing tests (failed attempts):",
        "    1  tests/a_test.py::test_x",
        "    1  web/x.test.ts > renders",
        "",
        "main: 1 runs in a row without a failure or rerun, bar 3, 2 to go (0 cancelled skipped)",
    ]


def test_finished_attempts_are_read_once_then_from_the_cache(cmd, tmp_path):
    runtime, commands = cmd()
    invoke(runtime, ["ci", "flakes"])
    first = len(commands.calls)
    commands.calls.clear()
    assert invoke(runtime, ["ci", "flakes"]).exit_code == 0
    assert [c[1:3] for c in commands.calls] == [["run", "list"]] and first > 1
    assert (tmp_path / "cache" / "o_r-2-1.json").exists() and (tmp_path / "cache" / "o_r-2-1.log").exists()


def test_a_failed_gh_answer_is_not_cached(tmp_path):
    commands = FakeCommands()
    commands.on(["gh"], Result(1, "", "boom"))
    reads = flakes.Reads(commands, REPO, tmp_path / "c")
    assert reads.attempt(9, 1) == {"jobs": [], "tests": []} and reads.jobs(9) == [] and not (tmp_path / "c").exists()
    assert reads.run_list(5, main_only=True) == []
    commands.on(["gh"], Result(0, "not json", ""))
    assert reads.run_list(5, main_only=False) == []


def test_group_in_the_former_format(cmd):
    runtime, _ = cmd()
    done = invoke(runtime, ["ci", "flakes", "--group", "--runs", "10"])
    assert done.exit_code == 0, done
    assert done.stdout.splitlines() == [
        "Last 4 finished main runs (every job's own conclusion; a macOS job that continue-on-error hides still counts):",
        "  Failing tests:",
        "    tests/a_test.py::test_x: 1 of 4 runs",
        "    web/x.test.ts > renders: 1 of 4 runs",
        "  Failing jobs:",
        "    unit (macos): 1 of 4 runs",
        "    web: 1 of 4 runs",
        "  full-suite green runs in a row, macOS included: 1 of the bar 3, 2 to go",
    ]


def test_group_json_and_short(cmd):
    runtime, _ = cmd()
    doc = flakes.FlakeGroups.model_validate(json.loads(invoke(runtime, ["--format", "json", "ci", "flakes", "--group", "--short"]).stdout))
    assert doc.runs == 4 and doc.short and doc.streak == 1 and len(doc.tests) == 2


def test_daily_json(cmd):
    runtime, _ = cmd()
    doc = flakes.FlakeDays.model_validate(json.loads(invoke(runtime, ["--format", "json", "ci", "flakes"]).stdout))
    assert doc.streak == 1 and doc.bar == 3 and doc.jobs[0].name == "web"


def test_group_with_no_runs_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(flakes, "resolve", lambda repo, listed: listed)
    commands = FakeCommands()
    commands.on(["gh"], Result(0, "[]", ""))
    runtime = runtime_with(tmp_path, {"repo": REPO}, commands=commands)
    done = invoke(runtime, ["ci", "flakes", "--group"])
    assert done.exit_code == 1 and "no finished main runs" in done.stderr


def test_an_unproven_batch_withholds_the_streak(cmd, monkeypatch):
    monkeypatch.setattr(flakes, "resolve", lambda repo, listed: None)
    runtime, _ = cmd()
    daily = invoke(runtime, ["ci", "flakes"])
    assert "streak qualification withheld: 1 unknown batch admission(s)" in daily.stdout and "main: 0 runs" in daily.stdout
    grouped = invoke(runtime, ["ci", "flakes", "--group"])
    assert "  streak qualification withheld: 1 unknown batch admission(s)" in grouped.stdout and "Last 0 finished" in grouped.stdout


def test_only_the_core_repository_goes_through_the_batch_resolution(monkeypatch):
    seen = []
    hzlib = SimpleNamespace(CORE="o/core", resolve_main_runs=lambda rows: seen.append(rows) or rows[:1])
    monkeypatch.setattr(queue_commands, "_queue", lambda: SimpleNamespace(hzlib=hzlib))
    rows = [{"a": 1}, {"b": 2}]
    assert flakes.resolve("o/other", rows) == rows and seen == []
    assert flakes.resolve("o/core", rows) == [{"a": 1}] and seen == [rows]


def test_the_group_streak_rules():
    green = [{"name": "suite (macos)", "conclusion": "success"}]
    no_mac = [{"name": "lint", "conclusion": "success"}]
    hidden = [{"name": "suite (macos)", "conclusion": "failure"}]
    runs = [run(1, 5), run(2, 4), run(3, 3), run(4, 2)]
    jobs = {1: no_mac, 2: green, 3: hidden, 4: green}
    data = count.group(runs, jobs_of=jobs.__getitem__, attempt_of=lambda r, a: {"tests": ["t"]}, bar=2)
    assert (data["streak"], data["not_full"], data["hidden"]["suite (macos)"], data["tests"]["t"]) == (1, 1, 1, 1)
    assert count.macos_suite("Tests (macOS)") and not count.macos_suite("macos-specific") and not count.macos_suite("notifier macos")


def test_a_met_bar_says_so():
    assert flakes.bar_text(3, 3) == ", bar met" and flakes.FlakeGroups(runs=0, tests=[], jobs=[], streak=3, not_full=0, bar=3).render()[-1].endswith("bar met")


def test_attempts_to_read_and_streak_rules():
    assert count.attempts_to_read(run(1, 1, attempt=3)) == [1, 2]
    assert count.attempts_to_read(run(1, 1, "failure", attempt=2)) == [1, 2]
    assert count.attempts_to_read(run(1, 1, "cancelled")) == []
    assert count.streak([{"batch_unknown": True}]) == (0, 0)
    assert count.tally([{"batch_unknown": True}], {}) == (count.Counter(), count.Counter())
