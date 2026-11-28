import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

import pytest


@pytest.fixture
def lib(tool):
    sys.path.insert(0, str(tool.__globals__["TOOLS"]))
    from steuermaschine_heinzel.native.queue_lib import hzlib

    return hzlib


def diff(path, *hunks):
    """A unified diff of one file; each hunk is (old_start, new_start, [lines with their sign])."""
    out = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}"]
    for old, new, lines in hunks:
        out.append(f"@@ -{old},{len(lines)} +{new},{len(lines)} @@")
        out += lines
    return "\n".join(out) + "\n"


def rules(found):
    return sorted((f[2], f[1]) for f in found)


# --- hz-diff-guard ---------------------------------------------------------------------------------------------------

def test_guard_passes_a_plain_change(tool):
    guard = tool("hz-diff-guard")
    assert guard.scan(diff("src/a.py", (5, 5, [" x = 1", "-y = 2", "+y = 3", " z = 4"]))) == []
    assert guard.render("393", []) == "PASS 393"


def test_guard_flags_a_raised_timeout_deadline_and_step_timeout_but_not_a_lowered_one(tool):
    guard = tool("hz-diff-guard")
    found = guard.scan(diff(".github/workflows/ci.yml", (10, 10, [
        "-    timeout-minutes: 20", "+    timeout-minutes: 45",
        "-  STEP_TIMEOUT: 60", "+  STEP_TIMEOUT: 120",
        "-      run: pytest --session-timeout 300", "+      run: pytest --session-timeout 900",
        "-  deadline = 10", "+  deadline = 30",
        "-    timeout-minutes: 30", "+    timeout-minutes: 15",
    ])))
    assert rules(found) == [("timeout", 10), ("timeout", 11), ("timeout", 12), ("timeout", 13)]
    assert all(f[0] == ".github/workflows/ci.yml" for f in found)


def test_guard_flags_a_lowered_coverage_threshold_not_a_raised_one(tool):
    guard = tool("hz-diff-guard")
    low = guard.scan(diff("pyproject.toml", (3, 3, ["-fail_under = 85", "+fail_under = 70"])))
    assert rules(low) == [("threshold", 3)] and "85 -> 70" in low[0][3]
    assert guard.scan(diff("pyproject.toml", (3, 3, ["-fail_under = 70", "+fail_under = 85"]))) == []
    flag = guard.scan(diff("Makefile", (1, 1, ["-\tpytest --cov-fail-under=90", "+\tpytest --cov-fail-under=80"])))
    assert rules(flag) == [("threshold", 1)]


def test_guard_flags_new_skips_xfails_and_only_with_the_new_line_number(tool):
    guard = tool("hz-diff-guard")
    found = guard.scan(diff("tests/test_a.py", (20, 20, [
        " def helper(): ...",
        "+@pytest.mark.skip(reason='flaky')",
        "+@pytest.mark.xfail",
        "+    pytest.skip('later')",
        " def test_a(): ...",
    ])) + diff("web/a.spec.ts", (1, 1, ["+it.only('x', () => {})", "+describe.skip('y', () => {})"])))
    assert rules(found) == [("skip", 1), ("skip", 2), ("skip", 21), ("skip", 22), ("skip", 23)]


def test_guard_flags_a_deleted_test_but_not_one_that_moved(tool):
    guard = tool("hz-diff-guard")
    gone = guard.scan(diff("tests/test_a.py", (4, 4, ["-def test_gone():", "-    assert 1", " def test_kept(): ..."])))
    assert rules(gone) == [("deleted", 4)] and "test_gone" in gone[0][3]
    moved = diff("tests/test_a.py", (4, 4, ["-def test_moved():", "-    assert 1"])) + diff("tests/test_b.py", (1, 1, ["+def test_moved():", "+    assert 1"]))
    assert guard.scan(moved) == []
    js = guard.scan(diff("web/a.test.ts", (7, 7, ["-  it('opens the doctor', async () => {"])))
    assert rules(js) == [("deleted", 7)]


def test_guard_flags_retries_continue_on_error_no_verify_and_sonar(tool):
    guard = tool("hz-diff-guard")
    found = guard.scan(diff("ci.yml", (1, 1, [
        "+      continue-on-error: true",
        "+      continue-on-error: false",
        "+      continue-on-error: ${{ matrix.os == 'macos-latest' }}",
        "+      run: pytest --reruns 2",
        "+        retries: 3",
        "+      - uses: nick-fields/retry@v3",
        "+      run: git commit --no-verify",
        "+sonar.exclusions=src/**/*.py",
        "+  x = f()  # NOSONAR",
    ])))
    assert rules(found) == [("coe", 1), ("coe", 3), ("no-verify", 7), ("retry", 4), ("retry", 5), ("retry", 6), ("sonar", 8), ("sonar", 9)]


def test_guard_ignores_markdown_and_context_lines(tool):
    guard = tool("hz-diff-guard")
    assert guard.scan(diff("AGENTS.md", (1, 1, ["+No --no-verify and no pytest.mark.skip"]))) == []
    assert guard.scan(diff("a.yml", (1, 1, [" continue-on-error: true", " retries: 3"]))) == []


def test_guard_cli_prints_file_line_and_exit_codes(tool, lib, monkeypatch, capsys):
    guard = tool("hz-diff-guard")
    bad = diff("tests/test_a.py", (9, 9, ["+@pytest.mark.skip"]))
    monkeypatch.setattr(guard, "diff_text", lambda repo, number: bad)
    assert guard.main(["plugins", "38"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("FLAG plugins #38") and "tests/test_a.py:9" in out
    monkeypatch.setattr(guard, "diff_text", lambda repo, number: "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")
    assert guard.main(["393"]) == 0 and capsys.readouterr().out.strip() == "PASS 393"
    monkeypatch.setattr(guard, "diff_text", lambda repo, number: None)
    assert guard.main(["393"]) == 2 and "UNREADABLE" in capsys.readouterr().out


def test_guard_reads_gh_pr_diff(tool, lib, monkeypatch):
    guard = tool("hz-diff-guard")
    seen = []
    monkeypatch.setattr(lib, "gh", lambda *a: (seen.append(a), (0, "diff --git a/x b/x\n"))[1])
    assert guard.diff_text(lib.PLUGINS, "38") and seen == [("pr", "diff", "38", "-R", lib.PLUGINS)]
    monkeypatch.setattr(lib, "gh", lambda *a: (1, ""))
    assert guard.diff_text(lib.CORE, "1") is None


# --- hz-queue add ------------------------------------------------------------------------------------------------------

@pytest.fixture
def queue(tool, lib, home, monkeypatch):
    monkeypatch.setenv("HZ_QUEUE_STATE", str(statedir.root() / "queue.json"))
    q = tool("hz-queue")
    monkeypatch.setattr(q, "pr_head", lambda repo, number: "a" * 40)  # the guarded head (review F2 2026-11-13)
    notes = []
    monkeypatch.setattr(lib, "note", notes.append)
    q.notes = notes
    return q


FLAGGED = [("tests/test_a.py", 9, "skip", "@pytest.mark.skip"), ("ci.yml", 3, "timeout", "timeout-minutes 20 -> 45")]


def test_queue_add_refuses_a_flagged_pr_and_queues_the_clean_one(queue, monkeypatch, capsys):
    monkeypatch.setattr(queue, "guard_findings", lambda repo, number: (FLAGGED, "FLAG 393\n  tests/test_a.py:9") if number == "393" else ([], "PASS"))
    assert queue.main(["add", "acme", "393", "plugins", "38"]) == 1
    captured = capsys.readouterr()
    assert "refused acme #393" in captured.err and "--reviewed" in captured.err and "tests/test_a.py:9" in captured.err
    assert [i["key"] for i in queue.load()["items"]] == ["plugins #38"] and queue.notes == []


def test_queue_policy_binds_checked_head_and_records_no_review(queue, monkeypatch):
    monkeypatch.setattr(queue, "guard_findings", lambda repo, number: (FLAGGED, "FLAG"))
    assert queue.main(["add", "--expected-head", "b" * 40, "393"]) == 1
    assert queue.load()["items"] == []
    assert queue.main(["add", "--expected-head", "a" * 40, "393"]) == 0
    item, = queue.load()["items"]
    assert item["guarded_head"] == item["expected_head"] == "a" * 40
    assert not item["reviewed_head"] and queue.notes == []


def test_queue_add_reviewed_queues_and_logs_the_reason(queue, monkeypatch, capsys):
    monkeypatch.setattr(queue, "guard_findings", lambda repo, number: (FLAGGED, "FLAG 393"))
    assert queue.main(["add", "--reviewed", "the skip is the documented macOS gap", "393"]) == 0
    assert [i["key"] for i in queue.load()["items"]] == ["393"]
    assert len(queue.notes) == 1 and "the skip is the documented macOS gap" in queue.notes[0] and "skip" in queue.notes[0] and "timeout" in queue.notes[0]


def test_queue_add_refuses_an_unreadable_diff_unless_reviewed(queue, monkeypatch, capsys):
    monkeypatch.setattr(queue, "guard_findings", lambda repo, number: (None, "UNREADABLE 393"))
    assert queue.main(["add", "393"]) == 1 and queue.load()["items"] == []
    assert queue.main(["add", "--reviewed", "read the 40k-line diff by hand", "393"]) == 0 and len(queue.load()["items"]) == 1


def test_queue_add_passes_a_clean_pr_without_a_note(queue, monkeypatch):
    monkeypatch.setattr(queue, "guard_findings", lambda repo, number: ([], "PASS 393"))
    assert queue.main(["add", "393"]) == 0 and queue.notes == []


# --- hz-flakes --group -------------------------------------------------------------------------------------------------

def run(id, conclusion="success", attempt=1, hour=1):
    return {"databaseId": id, "attempt": attempt, "conclusion": conclusion, "status": "completed", "headBranch": "main", "event": "push", "headSha": "x", "createdAt": f"2026-11-01T{hour:02d}:00:00Z"}


GREEN = [{"name": "test (ubuntu)", "conclusion": "success"}, {"name": "test (macos-latest)", "conclusion": "success"}, {"name": "CI OK", "conclusion": "success"}]
MAC_RED = [{"name": "test (ubuntu)", "conclusion": "success"}, {"name": "test (macos-latest)", "conclusion": "failure"}, {"name": "CI OK", "conclusion": "success"}]


def grouped(tool, runs, jobs, attempts=None):
    flakes = tool("hz-flakes")
    return flakes, flakes.group(runs, jobs_of=lambda repo, rid: jobs[rid], attempt_of=lambda rid, a: (attempts or {}).get((rid, a), {}), repo="r")


def test_group_counts_runs_per_test_and_job_including_macos_hidden_by_continue_on_error(tool):
    runs = [run(5, hour=5), run(4, hour=4), run(3, hour=3), run(2, hour=2), run(1, hour=1)]
    jobs = {5: GREEN, 4: MAC_RED, 3: MAC_RED, 2: GREEN, 1: GREEN}
    tests = {(4, 1): {"tests": ["tests/a.py::test_x", "tests/a.py::test_y"]}, (3, 1): {"tests": ["tests/a.py::test_x"]}}
    flakes, data = grouped(tool, runs, jobs, tests)
    assert data["tests"] == Counter({"tests/a.py::test_x": 2, "tests/a.py::test_y": 1})
    assert data["jobs"] == Counter({"test (macos-latest)": 2}) and data["hidden"] == Counter({"test (macos-latest)": 2})
    text = flakes.group_report(data)
    assert "tests/a.py::test_x: 2 of 5 runs" in text and "test (macos-latest): 2 of 5 runs (2 hidden by continue-on-error)" in text
    assert "CI OK" not in text


def test_group_streak_counts_full_suite_runs_with_macos_and_stops_at_a_hidden_red_job(tool):
    runs = [run(4, hour=4), run(3, hour=3), run(2, hour=2), run(1, hour=1)]
    flakes, data = grouped(tool, runs, {4: GREEN, 3: GREEN, 2: MAC_RED, 1: GREEN})
    assert data["streak"] == 2 and data["bar"] == 3
    assert "2 of the bar 3, 1 to go" in flakes.group_report(data)
    _, met = grouped(tool, runs[:2] + [run(0, hour=0)], {4: GREEN, 3: GREEN, 0: GREEN})
    assert met["streak"] == 3 and "bar met" in tool("hz-flakes").group_report(met)


def test_group_streak_stops_at_a_rerun_skips_site_only_runs_and_counts_earlier_attempt_failures(tool):
    site = [{"name": "site", "conclusion": "success"}, {"name": "test (macos-latest)", "conclusion": "skipped"}]
    runs = [run(3, hour=3), run(2, hour=2), run(1, attempt=2, hour=1)]
    flakes, data = grouped(tool, runs, {3: GREEN, 2: site, 1: GREEN}, {(1, 1): {"jobs": ["test (ubuntu)"], "tests": ["tests/b.py::test_z"]}})
    assert data["streak"] == 1 and data["not_full"] == 1
    assert data["tests"] == Counter({"tests/b.py::test_z": 1}) and data["jobs"] == Counter({"test (ubuntu)": 1})
    assert "1 newer run(s) without a macOS suite skipped" in flakes.group_report(data)


def test_group_a_red_run_breaks_the_streak_and_compact_lines(tool):
    runs = [run(2, hour=2), run(1, "failure", hour=1)]
    flakes, data = grouped(tool, runs, {2: GREEN, 1: MAC_RED}, {(1, 1): {"tests": ["tests/a.py::test_x"]}})
    assert data["streak"] == 1
    lines = flakes.group_compact(data)
    assert lines[0] == "failing tests (last 2 main runs): tests/a.py::test_x: 1 of 2 runs" and lines[2].endswith("1 of the bar 3")


def test_group_job_conclusions_are_cached_and_main_runs_drops_cancelled(tool, monkeypatch, tmp_path):
    flakes = tool("hz-flakes")
    monkeypatch.setattr(flakes, "CACHE", tmp_path)
    calls = []
    monkeypatch.setattr(flakes, "gh", lambda *a: (calls.append(a), json.dumps({"jobs": [{"name": "a", "conclusion": "failure"}]}))[1])
    assert flakes.run_jobs("o/r", 7) == [{"name": "a", "conclusion": "failure"}]
    assert flakes.run_jobs("o/r", 7) and len(calls) == 1
    listed = [run(1, hour=1), run(2, "cancelled", hour=2), run(3, hour=3)]
    monkeypatch.setattr(flakes, "gh", lambda *a: json.dumps(listed))
    assert [r["databaseId"] for r in flakes.main_runs("o/r", 10)] == [3, 1]


def test_flakes_group_cli(tool, monkeypatch, capsys):
    flakes = tool("hz-flakes")
    monkeypatch.setattr(flakes, "main_runs", lambda repo, n: [run(2, hour=2), run(1, hour=1)])
    monkeypatch.setattr(flakes, "run_jobs", lambda repo, rid: GREEN)
    assert flakes.main(["--group", "--runs", "2"]) == 0
    out = capsys.readouterr().out
    assert "Last 2 finished main runs" in out and "failing tests: none" in out and "2 of the bar 3" in out


# --- hz-hour and hz-next show both ---------------------------------------------------------------------------------------

def test_hz_hour_lists_the_group_fact_and_hz_next_renders_the_flake_lines(tool, monkeypatch):
    hour = tool("hz-hour")
    titles = [t for t, _ in hour.FACTS]
    assert "Failing tests and green streak" in titles and titles.index("Releases") == 6 and titles.index("Main CI (newest first)") == 5
    monkeypatch.setattr(hour, "tool", lambda *a: (0, "Last 10 finished main runs"))
    assert hour.flake_groups() == "Last 10 finished main runs"
    nxt = tool("hz-next")
    lines = nxt.render([], {}, flake_lines=["failing tests (last 10 main runs): none", "full-suite green runs in a row, macOS included: 1 of the bar 3"])
    assert lines[0] == "main CI: unknown" and lines[3:5] == ["failing tests (last 10 main runs): none", "full-suite green runs in a row, macOS included: 1 of the bar 3"]


def test_next_flake_lines_say_when_unavailable(tool, lib, monkeypatch):
    nxt = tool("hz-next")
    monkeypatch.setattr(lib, "load", lambda name: (_ for _ in ()).throw(RuntimeError("no gh")))
    assert nxt.flake_lines()[0].startswith("flakes: unavailable")


# --- hz-next do: lines carry --task ---------------------------------------------------------------------------------------

def test_every_hz_brief_line_in_hz_next_carries_task(tool, lib, home, monkeypatch):
    nxt = tool("hz-next")
    import shlex

    assert nxt.brief_cmd("t", "b/t", "Do it. Done = a PR.") == "hz-brief t b/t --task 'Do it. Done = a PR.'"
    assert nxt.brief_cmd("t", "b/t", "x", "plugins").startswith("hz-brief t b/t --repo plugins --task ")
    items = []
    # main red, no claiming PR
    monkeypatch.setattr(lib, "ghj", lambda *a, default=None: [{"databaseId": 9, "conclusion": "failure", "status": "completed", "headSha": "abcdef012345"}] if a[:2] == ("run", "list") else {"jobs": [{"name": "unit", "conclusion": "failure"}]} if a[:2] == ("run", "view") else [])
    items += nxt.src_main()
    # queue entry that needs work in plugins
    (statedir.root() / "queue.json").write_text(json.dumps({"items": [{"key": "plugins #38", "repo": lib.PLUGINS, "pr": "38", "status": "needs work", "detail": "checks unit; tests t::a"}]}))
    monkeypatch.setenv("HZ_QUEUE_STATE", str(statedir.root() / "queue.json"))
    monkeypatch.setattr(nxt, "queue_running", lambda: True)
    items += nxt.src_queue()
    # an owner ask and a paused row
    (home / "Code/heinzel-management/AGENTS.md").write_text("| make the logo bigger | ask 2026-01-01 00:00 |\n| fix push failures | paused: waiting for a slot |\n")
    items += nxt.src_asks() + nxt.src_paused()
    briefs = [i["do"] for i in items if "hz-brief" in i["do"]]
    assert len(briefs) == 4
    for do in briefs:
        words = shlex.split(do)
        assert "--task" in words and words[words.index("--task") + 1].strip(), do
    assert any("--repo plugins" in d for d in briefs)


def test_quality_item_brief_carries_task(tool, lib, monkeypatch):
    nxt = tool("hz-next")

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"component": {"measures": [{"metric": "coverage", "value": "50"}]}}).encode()

    monkeypatch.setattr(nxt.urllib.request, "urlopen", lambda *a, **k: Reply())
    out = nxt.sonar_repo("plugins", "heinzel-plugins")
    assert "--repo plugins --task" in out[0]["do"]


# --- hz-brief gate -----------------------------------------------------------------------------------------------------------

def test_brief_counts_agent_processes_by_cwd(tool, tmp_path):
    brief = tool("hz-brief")
    ps = "11 /Users/x/.local/bin/claude\n12 /Applications/Claude.app/Contents/MacOS/Claude\n13 /opt/bin/codex\n14 /bin/zsh\n15 /x/claude.app/Contents/MacOS/claude\n"
    assert brief.agent_pids(ps) == [11, 13, 15]
    code = tmp_path / "Code"
    cwds = {11: str(code / "heinzel-logo/src"), 13: str(code / "heinzel-plugins-sounds"), 15: str(code), 16: str(code / "heinzel"), 17: str(code / "heinzel-management"), 18: "/elsewhere", 19: str(code / "other")}
    assert brief.worktree_agents(cwds, code) == [(11, "heinzel-logo"), (13, "heinzel-plugins-sounds")]


def test_brief_full_names_the_measure(tool):
    brief = tool("hz-brief")
    agents = [(i, f"heinzel-t{i}") for i in range(5)]
    assert brief.full(12.0, agents[:4]) == ""
    assert "load 41.0 is over 40" in brief.full(41.0, [])
    assert "5 agents are working" in brief.full(3.0, agents) and "heinzel-t0" in brief.full(3.0, agents)
    assert brief.full(40.0, []) == ""


def brief_home(home):
    clone = home / "Code/heinzel"
    clone.mkdir(parents=True)
    import subprocess

    run = lambda *a: subprocess.run(["git", "-C", str(clone), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    run("remote", "add", "origin", str(clone))
    run("fetch", "-q", "origin", "main")


def test_brief_refuses_when_full_and_force_goes_ahead_and_logs(tool, home, monkeypatch, capsys):
    brief_home(home)
    brief = tool("hz-brief")
    monkeypatch.setattr(brief, "working_agents", lambda code: [(i, f"heinzel-t{i}") for i in range(5)])
    monkeypatch.setattr(brief.os, "getloadavg", lambda: (3.0, 3.0, 3.0))
    with pytest.raises(SystemExit) as stop:
        brief.main(["logo", "feat/logo", "--task", "Bigger logo."])
    assert "refused" in str(stop.value) and "5 agents are working" in str(stop.value)
    assert not (home / "Code/heinzel-logo").exists()
    logged = []
    monkeypatch.setattr(brief.subprocess, "run", lambda cmd, **k: logged.append(cmd) if "hz-note" in str(cmd[0]) else ORIGINAL(cmd, **k))
    assert brief.main(["logo", "feat/logo", "--task", "Bigger logo.", "--force", "owner asked"]) == 0
    assert (home / "Code/heinzel-logo/.git").exists() and "Bigger logo." in capsys.readouterr().out
    assert logged and "owner asked" in logged[0][1] and "forced" in logged[0][1]


def test_brief_refuses_over_load_40_and_an_empty_force_reason(tool, home, monkeypatch):
    brief = tool("hz-brief")
    monkeypatch.setattr(brief, "working_agents", lambda code: [])
    monkeypatch.setattr(brief.os, "getloadavg", lambda: (55.0, 20.0, 10.0))
    for extra in ([], ["--force", "  "]):
        with pytest.raises(SystemExit) as stop:
            brief.main(["logo", "feat/logo", *extra])
        assert "load 55.0 is over 40" in str(stop.value)


import subprocess  # noqa: E402
from steuermaschine_heinzel import state as statedir

ORIGINAL = subprocess.run


def test_group_fetches_logs_only_for_failed_jobs_and_caches_by_run_id(tool, monkeypatch, tmp_path):
    flakes = tool("hz-flakes")
    monkeypatch.setattr(flakes, "CACHE", tmp_path)
    calls = []

    def fake_gh(*args):
        calls.append(args)
        if "--log-failed" in args:
            return "FAILED tests/a.py::test_x"
        return json.dumps({"jobs": [{"name": "a", "conclusion": "success"}]})

    monkeypatch.setattr(flakes, "gh", fake_gh)
    assert flakes.read_attempt("o/r", 5, 1) == {"jobs": [], "tests": []}
    assert not any("--log-failed" in c for c in calls)
    monkeypatch.setattr(flakes, "gh", lambda *a: json.dumps({"jobs": [{"name": "a", "conclusion": "failure"}]}) if "--log-failed" not in a else "FAILED tests/a.py::test_x")
    assert flakes.read_attempt("o/r", 6, 1) == {"jobs": ["a"], "tests": ["tests/a.py::test_x"]}
    monkeypatch.setattr(flakes, "gh", lambda *a: pytest.fail("a cached attempt must not call gh"))
    assert flakes.read_attempt("o/r", 6, 1)["jobs"] == ["a"]
