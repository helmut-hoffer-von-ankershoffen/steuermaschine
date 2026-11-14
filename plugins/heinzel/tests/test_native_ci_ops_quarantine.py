"""`sm ci quarantine list` and `sm ci quarantine flag`: the registry on main, read; the bead note for a streak, written
once a day. The first part is the former quarantine module's test file, moved with its assertions; the second part
runs the commands through the whole command tree on a scripted gh and bd. Nothing real is read or written.

Not moved from the former file: its two tests of the diff guard (`test_diff_guard_flags_any_quarantine_lane_change`,
`test_diff_guard_flags_quarantine_wiring_in_the_makefile_but_not_other_makefile_lines`). They test the diff guard,
which is another area's; they belong with that port.
"""

import datetime as dt
import json
import re
from pathlib import Path

import pytest
from ci_ops_kit import REPO, World, runtime
from steuermaschine_heinzel.native import ci_ops
from steuermaschine_heinzel.native.ci_ops_lib import quarantine as q

from steuermaschine.native import NativeRuntime, using
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeClock, MemoryStore, invoke
from steuermaschine.testing.fakes import FakeTracker

DAY = dt.date(2026, 11, 8)
TEXT = """
[[quarantine]]
test = "tests/unit/steps_signal_test.py::test_interrupt"
bead = "3qj"
added = 2026-11-07
until = 2026-11-10
reason = "term-exit missing"
"""


def entries():
    found, error = q.parse(TEXT)
    assert error is None
    return found


def report(status, os_="Linux"):
    return {"os": os_, "entries": [{"test": "tests/unit/steps_signal_test.py::test_interrupt", "status": status}]}


# ----------------------------------------------------------------------------- moved tests
def test_count_and_age_and_expiry_lines():
    assert q.registry_lines(entries(), DAY) == ["QUARANTINE 1/8: 3qj steps_signal_test.py::test_interrupt 1d old, 2d left"]
    late = q.registry_lines(entries(), dt.date(2026, 11, 9))
    assert late[1].startswith("QUARANTINE-EXPIRING(3qj)")
    overdue = q.registry_lines(entries(), dt.date(2026, 11, 11))
    assert overdue == [overdue[0]] and overdue[0].startswith("QUARANTINE-OVERDUE(3qj)") and "fails CI OK" in overdue[0]


def test_unreadable_registry_is_reported():
    found, error = q.parse("[[quarantine]\n")
    assert found is None and error


def test_three_failing_days_in_a_row_flag_the_bead_once_a_day(monkeypatch):
    reports = [(DAY - dt.timedelta(days=2), report("fail")), (DAY - dt.timedelta(days=1), report("flaky", "macOS")),
               (DAY, report("pass")), (DAY, report("fail", "macOS"))]
    lines, flags = q.streak_lines(entries(), reports, DAY)
    assert len(lines) == 1 and "failed 3 days in a row" in lines[0] and lines[0].startswith("QUARANTINE-STREAK(3qj)")
    # edge changed: the once-a-day marks are in the store, no longer in a cache directory
    monkeypatch.setattr(q, "EDGE", q.Edge(store=MemoryStore(), clock=FakeClock(), bead_prefix="tracker-"))
    calls = []
    assert len(q.flag_beads(flags, DAY, run=lambda args: calls.append(args) or (0, ""))) == 1
    assert q.flag_beads(flags, DAY, run=lambda args: calls.append(args) or (0, "")) == []
    assert calls[0][:3] == ["bd", "update", "tracker-3qj"]  # CHANGED: this assertion named the former note command


def test_a_full_bead_id_is_not_prefixed_twice(monkeypatch):
    monkeypatch.setattr(q, "EDGE", q.Edge(store=MemoryStore(), clock=FakeClock(), bead_prefix="tracker-"))
    calls = []
    assert len(q.flag_beads([("tracker-3qj", TEST, 3)], DAY, run=lambda args: calls.append(args) or (0, ""))) == 1
    assert calls[0][:3] == ["bd", "update", "tracker-3qj"]
    assert q.full_bead_id("3qj", "") == "3qj" and q.full_bead_id("3qj", "tracker-") == "tracker-3qj"


def test_a_refused_note_is_not_marked_and_not_reported_written(tmp_path, monkeypatch):
    world = lane_world()
    world.other = lambda argv, cwd, timeout: (world.bd.append((argv, cwd)), Result(1, "", "no such issue"))[1]
    rt = runtime(tmp_path, monkeypatch, world)
    done = invoke(rt, ["ci", "quarantine", "flag"])
    assert done.exit_code == 1 and "bead note written" not in done.stdout
    assert done.stdout.splitlines()[-1].startswith(f"bead note NOT written: 3qj {TEST} {TODAY} no such issue")
    assert rt.providers.store.keys(q.FLAGGED) == []
    again = invoke(rt, ["ci", "quarantine", "flag"])
    assert len(world.bd) == 2 and again.exit_code == 1  # tried again: nothing was remembered


def test_a_tracker_that_fails_is_a_note_not_written(tmp_path, monkeypatch):
    world = lane_world()
    rt = runtime(tmp_path, monkeypatch, world)

    class Broken(FakeTracker):
        def comment(self, *args):
            raise RuntimeError("tracker down")

    rt.providers.tracker = Broken()
    done = invoke(rt, ["ci", "quarantine", "flag"])
    assert done.exit_code == 1 and "tracker down" in done.stdout and rt.providers.store.keys(q.FLAGGED) == []


def test_flag_without_tracker_and_beads_dir_refuses_and_dry_run_still_works(tmp_path, monkeypatch):
    world = lane_world()
    rt = runtime(tmp_path, monkeypatch, world, ci_ops_beads_dir=None)
    refused = invoke(rt, ["ci", "quarantine", "flag"])
    assert refused.exit_code == 1 and "ci_ops_beads_dir" in refused.stderr and world.calls == []
    assert invoke(rt, ["ci", "quarantine", "flag", "--dry-run"]).exit_code == 0


def test_a_passing_day_or_a_gap_breaks_the_streak_and_today_may_be_missing():
    two = [(DAY - dt.timedelta(days=3), report("fail")), (DAY - dt.timedelta(days=2), report("pass")),
           (DAY - dt.timedelta(days=1), report("fail"))]
    assert q.streak_lines(entries(), two, DAY) == ([], [])
    days = q.day_results([(DAY - dt.timedelta(days=n), report("fail")) for n in (1, 2, 3)])
    assert q.streak(days["tests/unit/steps_signal_test.py::test_interrupt"], DAY) == 3


def test_last_results_name_each_os():
    reports = [(DAY, report("pass")), (DAY, report("flaky", "macOS"))]
    assert q.last_results(entries(), reports) == {
        "tests/unit/steps_signal_test.py::test_interrupt": "pass Linux, flaky macOS (2026-11-08)"}


# ----------------------------------------------------------------------------- through the command tree
TEST = "tests/unit/steps_signal_test.py::test_interrupt"
TODAY = dt.datetime.fromtimestamp(FakeClock().now(), dt.UTC).date()  # the fake clock's day
REGISTRY = f'[[quarantine]]\ntest = "{TEST}"\nbead = "3qj"\nadded = {TODAY - dt.timedelta(days=2)}\nuntil = {TODAY + dt.timedelta(days=3)}\nreason = "term-exit missing"\n'
CONTENTS = f"api repos/{REPO}/contents/tests/quarantine.toml?ref=main -H Accept: application/vnd.github.raw"
RUNS = f"run list -R {REPO} --workflow CI --branch main --status completed --limit 30 --json databaseId,createdAt"


def lane_world(registry=REGISTRY, status="fail") -> World:
    """main's registry with one entry, and three CI runs of main (today and the two days before) whose lane failed."""
    world = World()
    world.on("contents/tests/quarantine.toml", registry)
    world.on("run list", [{"databaseId": 70 + n, "createdAt": f"{TODAY - dt.timedelta(days=n)}T03:00:00Z"} for n in range(3)]
             + [{"databaseId": 60, "createdAt": f"{TODAY - dt.timedelta(days=30)}T03:00:00Z"}])

    def download(argv):
        folder = Path(argv[argv.index("-D") + 1]) / "quarantine-report-Linux"
        folder.mkdir(parents=True)
        (folder / "quarantine.json").write_text(json.dumps(report(status)))
        (folder.parent / "quarantine-report-broken").mkdir()
        (folder.parent / "quarantine-report-broken" / "quarantine.json").write_text("not json")
        return Result(0, "", "")

    world.on("run download", download)
    world.bd = []
    world.other = lambda argv, cwd, timeout: (world.bd.append((argv, cwd)), Result(0, "updated\n", ""))[1]
    return world


def download_of(run_id: int) -> str:
    return f"run download {run_id} -R {REPO} --pattern quarantine-report-* -D"


def test_list_in_text_and_json_and_it_downloads_each_run_once(tmp_path, monkeypatch):
    world = lane_world()
    rt = runtime(tmp_path, monkeypatch, world)
    shown = invoke(rt, ["ci", "quarantine", "list"])
    assert shown.exit_code == 0 and shown.stdout.splitlines() == [
        f"3qj  {TEST}", f"    added {TODAY - dt.timedelta(days=2)} (2 d), until {TODAY + dt.timedelta(days=3)} (3 d left); last lane: fail Linux ({TODAY})",
        "    term-exit missing",
        f"QUARANTINE-STREAK(3qj) steps_signal_test.py::test_interrupt failed 3 days in a row in the lane: P0, fix the cause before {TODAY + dt.timedelta(days=3)} (bead flagged)"]
    lines = [re.sub(r" -D .*", " -D", line) for line in world.gh_lines()]
    assert lines == [CONTENTS, RUNS, download_of(70), download_of(71), download_of(72)]  # the run of 30 days ago is not fetched
    world.calls.clear()
    doc = json.loads(invoke(rt, ["--format", "json", "ci", "quarantine", "list"]).stdout)
    listed = ci_ops.QuarantineList.model_validate(doc)
    assert listed.view == "status" and listed.max_entries == 8 and len(listed.entries) == 1
    entry = listed.entries[0]
    assert (entry.bead, entry.test, entry.age_days, entry.days_left, entry.last_lane) == ("3qj", TEST, 2, 3, f"fail Linux ({TODAY})")
    assert world.gh_lines() == [CONTENTS, RUNS] and world.bd == []  # the reports are remembered per run; a list never notes a bead


def test_line_and_digest_views_write_no_note(tmp_path, monkeypatch):
    world = lane_world()
    rt = runtime(tmp_path, monkeypatch, world)
    line = invoke(rt, ["ci", "quarantine", "list", "--line"])
    assert line.stdout == "QUARANTINE 1/8: 3qj steps_signal_test.py::test_interrupt 2d old, 3d left\n"
    digest = ci_ops.QuarantineList.model_validate(json.loads(invoke(rt, ["--format", "json", "ci", "quarantine", "list", "--digest"]).stdout))
    assert digest.view == "digest" and len(digest.lines) == 2 and digest.lines[1].startswith("QUARANTINE-STREAK(3qj)")
    assert world.bd == [] and rt.providers.store.keys(q.FLAGGED) == []


def test_flag_is_off_until_its_review_is_approved_and_list_is_on(tmp_path, monkeypatch):
    from steuermaschine.native import NativeCommand

    # an unreviewed state, set explicitly: the test must not depend on what reviews/ci-*.md says today
    monkeypatch.setattr(NativeCommand, "review_state", lambda self: ("open", f"the last line of reviews/{self.review_file} is not exactly the verdict line"))
    world = lane_world()
    gated = runtime(tmp_path, monkeypatch, world, reviewed=False)
    for argv in (["ci", "quarantine", "flag"], ["ci", "quarantine", "flag", "--dry-run"]):
        off = invoke(gated, argv)
        assert off.exit_code == 70 and "independent review is open" in off.stderr and "reviews/ci-quarantine-flag.md" in off.stderr
    assert world.calls == []
    assert invoke(gated, ["ci", "quarantine", "list", "--line"]).exit_code == 0


def test_flag_makes_exactly_these_calls_in_this_order_and_notes_a_bead_once_a_day(tmp_path, monkeypatch):
    world = lane_world()
    rt = runtime(tmp_path, monkeypatch, world)
    done = invoke(rt, ["--format", "json", "ci", "quarantine", "flag"])
    assert done.exit_code == 0, done
    result = ci_ops.QuarantineFlag.model_validate(json.loads(done.stdout))
    assert result.live and [(n.bead, n.test, n.day) for n in result.notes] == [("3qj", TEST, str(TODAY))] and len(result.lines) == 2
    assert [re.sub(r" -D .*", " -D", " ".join(call[1:])) if call[0] == "gh" else " ".join(call[:3]) for call in world.calls] == [
        CONTENTS, RUNS, download_of(70), download_of(71), download_of(72), "bd update tracker-3qj"]  # the one write is the last call
    (argv, cwd), = world.bd
    assert argv[3] == "--append-notes" and re.fullmatch(rf"\d\d:\d\d quarantine lane: {re.escape(TEST)} failed 3 days in a row \(all attempts or flaky\); "
                                                        r"P0: fix the root cause before the entry's until", argv[4])
    assert cwd == str(tmp_path / "beads")
    again = invoke(rt, ["ci", "quarantine", "flag"])
    assert len(world.bd) == 1 and "bead note written" not in again.stdout and "QUARANTINE-STREAK(3qj)" in again.stdout


def test_flag_dry_run_says_what_is_due_and_writes_nothing(tmp_path, monkeypatch):
    world = lane_world()
    rt = runtime(tmp_path, monkeypatch, world)
    shown = invoke(rt, ["ci", "quarantine", "flag", "--dry-run"])
    assert shown.exit_code == 0 and shown.stdout.splitlines()[-1] == f"bead note due (dry run, nothing written): 3qj {TEST}"
    assert world.bd == [] and rt.providers.store.keys(q.FLAGGED) == []
    live = invoke(rt, ["ci", "quarantine", "flag"])
    assert live.stdout.splitlines()[-1] == f"bead note written: 3qj {TEST}" and len(world.bd) == 1


def test_a_passing_lane_flags_nothing(tmp_path, monkeypatch):
    world = lane_world(status="pass")
    rt = runtime(tmp_path, monkeypatch, world)
    done = ci_ops.QuarantineFlag.model_validate(json.loads(invoke(rt, ["--format", "json", "ci", "quarantine", "flag"]).stdout))
    assert done.notes == [] and len(done.lines) == 1 and world.bd == []


def test_a_tracker_provider_gets_the_note_instead_of_the_cli(tmp_path, monkeypatch):
    world = lane_world()
    rt = runtime(tmp_path, monkeypatch, world)
    rt.providers.tracker = FakeTracker()
    assert invoke(rt, ["ci", "quarantine", "flag"]).exit_code == 0
    assert rt.providers.tracker.calls == [("comment", "tracker-3qj")] and world.bd == []


def test_unparsable_empty_and_unreadable_registries(tmp_path, monkeypatch):
    broken = invoke(runtime(tmp_path, monkeypatch, lane_world(registry="[[quarantine]\n")), ["ci", "quarantine", "list"])
    assert broken.exit_code == 1 and broken.stdout.startswith("QUARANTINE-INVALID tests/quarantine.toml unreadable: ")
    rt = runtime(tmp_path, monkeypatch, lane_world(registry="[[quarantine]\n"))
    assert invoke(rt, ["ci", "quarantine", "list", "--digest"]).stdout.startswith("QUARANTINE-INVALID")
    with using(NativeRuntime(rt.providers, rt.settings, rt.project)):
        assert ci_ops.quarantine_release_line().startswith("QUARANTINE-INVALID")
    empty = invoke(runtime(tmp_path, monkeypatch, lane_world(registry="# no entries\n")), ["ci", "quarantine", "list"])
    assert empty.exit_code == 0 and empty.stdout == "quarantine: 0/8 entries on main\n"
    world = lane_world()
    world.fail = {"contents/tests/quarantine.toml"}  # CHANGED (review C4): a registry that cannot be read is said, exit 1, not "0/8 entries"
    unreadable = invoke(runtime(tmp_path, monkeypatch, world), ["ci", "quarantine", "list"])
    assert unreadable.exit_code == 1 and unreadable.stdout.startswith("QUARANTINE-UNREADABLE")
    for view in ("--line", "--digest"):
        shown = invoke(runtime(tmp_path, monkeypatch, world), ["ci", "quarantine", "list", view])
        assert shown.exit_code == 1 and shown.stdout.startswith("QUARANTINE-UNREADABLE")
    flag = invoke(runtime(tmp_path, monkeypatch, world), ["ci", "quarantine", "flag"])
    assert flag.exit_code == 1 and world.bd == []
    gone = lane_world()
    gone.on("contents/tests/quarantine.toml", Result(1, "", "gh: Not Found (HTTP 404)"))  # main has no registry: no entries
    assert invoke(runtime(tmp_path, monkeypatch, gone), ["ci", "quarantine", "list"]).stdout == "quarantine: 0/8 entries on main\n"


def test_the_functions_other_areas_call(tmp_path, monkeypatch):
    world = lane_world()
    rt = runtime(tmp_path, monkeypatch, world)
    with using(NativeRuntime(rt.providers, rt.settings, rt.project)):
        line = ci_ops.quarantine_release_line()
        assert line == "QUARANTINE 1/8: 3qj steps_signal_test.py::test_interrupt 2d old, 3d left (non-blocking lane; no rerun or waiver for these)"
        assert world.gh_lines() == [CONTENTS]  # the release line reads the registry only
        lines = ci_ops.quarantine_digest()  # CHANGED (review C7): the digest other areas bind never writes a note
        assert len(lines) == 2 and world.bd == []


def test_a_run_list_that_is_no_json_reads_as_no_reports(tmp_path, monkeypatch):
    world = lane_world()
    world.on("run list", "rate limited")
    shown = invoke(runtime(tmp_path, monkeypatch, world), ["ci", "quarantine", "list"])
    assert shown.exit_code == 0 and "last lane: no report yet" in shown.stdout and "STREAK" not in shown.stdout


@pytest.mark.parametrize("left,word", [(0, "QUARANTINE-EXPIRING(3qj)"), (-1, "QUARANTINE-OVERDUE(3qj)")])
def test_expiring_and_overdue_entries_in_the_digest(tmp_path, monkeypatch, left, word):
    registry = REGISTRY.replace(str(TODAY + dt.timedelta(days=3)), str(TODAY + dt.timedelta(days=left)))
    digest = invoke(runtime(tmp_path, monkeypatch, lane_world(registry=registry, status="pass")), ["ci", "quarantine", "list", "--digest"])
    assert word in digest.stdout
