"""`sm head combine-close`: members of a merged combine pull request. GitHub and the tracker are fakes; no test closes
a real pull request."""

import json

from coord_kit import app_runtime, ok

from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, invoke
from steuermaschine_heinzel.native.merges_lib import combine_close

BODY = "Combine of three.\n\nCombines #701 logo\n  Combines #702 footer\nCombines #703 colours\nSee also #999\n"


def world(state="MERGED", close_rc=0):
    c = FakeCommands()
    states = {"701": "OPEN", "702": "MERGED", "703": "OPEN"}

    def view(argv):
        if argv[3] == "717":
            return ok({"body": BODY, "state": state, "mergeCommit": {"oid": "045d3a30aabbccdd"}})
        return ok(states[argv[3]] + "\n")

    c.on(["gh", "pr", "view"], view)
    c.on(["gh", "pr", "close"], Result(close_rc, "", "" if close_rc == 0 else "gh: Not Found (HTTP 404)"))
    c.on(["bd", "search"], lambda argv: ok("management-ab12 logo bigger\nmanagement-ab12 again\n" if argv[2] == "#701" else ""))
    c.on(["bd", "note"], ok())
    return c


def writes(c):
    return [call for call in c.calls if call[:3] == ["gh", "pr", "close"] or call[:2] == ["bd", "note"]]


def test_members_are_the_combines_lines_of_the_body():
    assert combine_close.members(BODY) == [701, 702, 703] and combine_close.members(None) == []


def test_a_dry_run_lists_and_writes_nothing(tmp_path):
    c = world()
    done = invoke(app_runtime(tmp_path, c), ["head", "combine-close", "717"])
    assert done.exit_code == 0 and done.stdout.splitlines() == [
        "#701: OPEN -> close", '  bead management-ab12: note "merged via, not yet released"', "#702: MERGED", "#703: OPEN -> close", "dry run; pass --apply"]
    assert writes(c) == []


def test_apply_closes_open_members_with_the_comment_and_notes_the_beads(tmp_path):
    c = world()
    done = invoke(app_runtime(tmp_path, c), ["--format", "json", "head", "combine-close", "717", "--apply", "--repo", "o/r"])
    doc = combine_close.CombineClose.model_validate(json.loads(done.stdout))
    assert doc.message == "merged via #717 at 045d3a30a" and [m.closed for m in doc.members] == [True, False, True] and doc.members[0].noted == ["management-ab12"]
    assert writes(c) == [["gh", "pr", "close", "701", "-R", "o/r", "--comment", "merged via #717 at 045d3a30a"],
                         ["bd", "note", "management-ab12", "merged via #717 at 045d3a30a; merged via, not yet released"],
                         ["gh", "pr", "close", "703", "-R", "o/r", "--comment", "merged via #717 at 045d3a30a"]]


def test_an_unmerged_or_unreadable_combine_is_refused_and_nothing_is_written(tmp_path):
    c = world(state="OPEN")
    done = invoke(app_runtime(tmp_path, c), ["head", "combine-close", "717", "--apply"])
    assert done.exit_code == 1 and "#717 is OPEN, not merged: refusing" in done.stderr and writes(c) == []
    c = FakeCommands()
    done = invoke(app_runtime(tmp_path, c), ["head", "combine-close", "717", "--apply"])
    assert done.exit_code == 1 and "cannot read #717" in done.stderr and writes(c) == []


def test_a_failed_close_is_reported_and_exits_1(tmp_path):
    done = invoke(app_runtime(tmp_path, world(close_rc=1)), ["head", "combine-close", "717", "--apply"])
    assert done.exit_code == 1 and "CLOSE FAILED: Not Found (HTTP 404)" in done.stdout


def test_a_combine_without_members(tmp_path):
    c = FakeCommands()
    c.on(["gh", "pr", "view"], ok({"body": "", "state": "MERGED", "mergeCommit": None}))
    done = invoke(app_runtime(tmp_path, c), ["head", "combine-close", "5"])
    assert "#5 names no member" in done.stdout


def test_apply_without_a_configured_tracker_refuses_before_the_first_write(tmp_path):
    c = world()
    done = invoke(app_runtime(tmp_path, c, coord_document=None, coord_tracker_dir=None), ["head", "combine-close", "717", "--apply"])
    assert done.exit_code == 2 and "coord_document" in done.stderr and writes(c) == []
