"""`sm note`, `sm beads stamp`, `sm beads stamp-released` and the two functions other areas import. The assertions of
the former note and stamp tests moved here; processes are the fakes' (no bd, no gh, no git, no network)."""

import json
import re

import pytest
from coord_kit import NOW, app_runtime, home, native_runtime, ok  # noqa: F401  (fixtures)

from steuermaschine import journal as core_journal
from steuermaschine.native import using
from steuermaschine.testing import FakeCommands, invoke
from steuermaschine_heinzel.native.coord_lib import journal as s
from steuermaschine_heinzel.native.coord_lib.journal import note, stamp


# ----------------------------------------------------------------------------- note
def test_note_appends_one_dated_line_with_squeezed_spaces(tmp_path):
    (tmp_path / "AGENTS.md").write_text("## 8. Log\n")
    rt = app_runtime(tmp_path)
    assert invoke(rt, ["note", "first   line", "of text"]).stdout == "noted\n"
    invoke(rt, ["note", "second"])
    lines = (tmp_path / "AGENTS.md").read_text().splitlines()
    assert lines[0] == "## 8. Log"
    assert lines[1].endswith(" first line of text") and lines[1].startswith("- ")
    assert re.fullmatch(r"- \d\d-\d\d \d\d:\d\d first line of text", lines[1])
    assert lines[2].endswith(" second")


def test_note_without_text_fails_with_usage(tmp_path):
    rt = app_runtime(tmp_path)
    assert invoke(rt, ["note"]).exit_code == 2
    done = invoke(rt, ["note", "   "])
    assert done.exit_code == 2 and "sm note" in done.stderr
    assert not (tmp_path / "AGENTS.md").exists()


def test_note_json_is_one_document_of_the_model(tmp_path):
    done = invoke(app_runtime(tmp_path), ["--format", "json", "note", "a", "b"])
    doc = s.Noted.model_validate(json.loads(done.stdout))
    assert doc.line.endswith(" a b") and doc.document == str(tmp_path / "AGENTS.md")


def test_the_function_note_writes_the_same_line_format_and_returns_it(tmp_path):
    with using(native_runtime(tmp_path)):
        got = note("queue: #12   merged")
    assert got.line == core_journal.line_of("queue: #12 merged", NOW)
    assert (tmp_path / "AGENTS.md").read_text() == got.line + "\n"
    with using(native_runtime(tmp_path)), pytest.raises(ValueError):
        note(" ")


def test_a_document_without_a_final_newline_gets_one_before_the_line(tmp_path):
    (tmp_path / "AGENTS.md").write_text("## 8. Log\n- old line")
    with using(native_runtime(tmp_path)):
        note("new")
    assert (tmp_path / "AGENTS.md").read_text().splitlines()[1:] == ["- old line", core_journal.line_of("new", NOW)]


def test_the_journal_lock_file_defaults_next_to_the_document(tmp_path):
    core_journal.append(tmp_path / "LOG.md", "x", NOW)
    assert (tmp_path / "LOG.md.lock").exists()


# ----------------------------------------------------------------------------- stamp: pure parts (assertions moved)
def test_earliest_tag_ignores_non_release_and_orders_semver():
    assert s.earliest_tag(["v0.1.10", "v0.1.9", "nightly", "v0.2.0"]) == "v0.1.9"
    assert s.earliest_tag(["nightly"]) == ""


def test_lines_and_label_helpers():
    assert s.merged_line(12, "abcdef0123", "2026-11-08") == "merged_in: PR #12 sha=abcdef01 at=2026-11-08"
    assert s.released_line("v0.1.0", "t").startswith("released_in: v0.1.0")
    assert s.merged_prs(["x", "merged-7", "merged-x"]) == [7]
    assert s.is_released(["released-v0.1.0"]) and not s.is_released(["merged-7"])


# ----------------------------------------------------------------------------- stamp: the world
def world(labels=None, pr=None, release=None, beads=None, tags="v0.1.0\nnightly"):
    c = FakeCommands()
    state = {"labels": list(labels or [])}

    def label_add(argv):
        state["labels"].append(argv[-1])
        return ok()

    c.on(["bd", "show"], lambda argv: ok([{"labels": state["labels"]}]))
    c.on(["bd", "update"], ok())
    c.on(["bd", "label", "add"], label_add)
    c.on(["bd", "list"], ok(beads or []))
    c.on(["gh", "pr", "view"], ok(pr if pr is not None else {"state": "MERGED", "mergeCommit": {"oid": "a" * 40}, "mergedAt": "T"}))
    c.on(["gh", "release", "view"], ok(release if release is not None else {"isDraft": False, "publishedAt": "P"}))
    c.on(["git", "tag"], ok(tags))
    return c


def bd_calls(c):
    return [tuple(call[:3]) for call in c.calls if call[0] == "bd"]


def test_unmerged_pr_is_refused_and_nothing_written(tmp_path):
    c = world(pr={"state": "OPEN"})
    with using(native_runtime(tmp_path, c)):
        got = stamp("abc", merged=5)
    assert got.exit_code == 1 and got.refused == "PR #5 in helmut-hoffer-von-ankershoffen/heinzel is not merged"
    assert not any(call[0] == "bd" for call in c.calls)


def test_merged_stamp_is_idempotent(tmp_path):
    c = world()
    with using(native_runtime(tmp_path, c)):
        assert stamp("abc", merged=5).exit_code == 0
        kinds = bd_calls(c)
        assert ("bd", "update", "heinzel-management-abc") in kinds and ("bd", "label", "add") in kinds
        n = len([k for k in kinds if k[:2] == ("bd", "update")])
        again = stamp("abc", merged=5)  # label now present: no second note
    assert len([k for k in bd_calls(c) if k[:2] == ("bd", "update")]) == n
    assert again.rows == [] and again.render() == ["abc: already stamped"]


def test_the_stamp_note_carries_the_time_the_line_and_the_label(tmp_path):
    c = world()
    with using(native_runtime(tmp_path, c)):
        got = stamp("heinzel-management-abc", merged=5, released="0.1.0")
    update = next(call for call in c.calls if call[:2] == ["bd", "update"])
    assert update[2:4] == ["heinzel-management-abc", "--append-notes"] and re.fullmatch(r"\d\d:\d\d merged_in: PR #5 sha=aaaaaaaa at=T", update[4])
    assert [r.label for r in got.rows] == ["merged-5", "released-v0.1.0"] and all(r.status == "stamped" for r in got.rows)
    assert got.render() == ["heinzel-management-abc: merged_in: PR #5 sha=aaaaaaaa at=T", "heinzel-management-abc: released_in: v0.1.0 released_at=P"]


def test_unpublished_release_is_refused(tmp_path):
    c = world(release={"isDraft": True, "publishedAt": "x"})
    with using(native_runtime(tmp_path, c)):
        got = stamp("abc", released="0.1.0")
    assert got.exit_code == 1 and got.refused == "release v0.1.0 is not published" and not bd_calls(c)


def test_a_tracker_failure_is_reported_as_failed(tmp_path):
    c = world()
    c.on(["bd", "label", "add"], ok().__class__(1, "", "locked"))
    with using(native_runtime(tmp_path, c)):
        got = stamp("abc", merged=5)
    assert got.exit_code == 1 and got.rows[0].status == "failed" and "FAILED" in got.render()[0]


def test_unreadable_answers_count_as_not_merged_not_published_not_labelled(tmp_path):
    c = world()
    c.on(["gh", "pr", "view"], ok("not json"))
    with using(native_runtime(tmp_path, c)):
        assert stamp("abc", merged=5).refused
    c = world()
    c.on(["gh", "release", "view"], ok("not json"))
    c.on(["bd", "show"], ok("[]"))
    with using(native_runtime(tmp_path, c)):
        assert stamp("abc", released="v0.1.0").refused
        assert not s.Stamper(native_runtime(tmp_path, c)).has_label("abc", "x")


def test_sweep_stamps_only_merged_unreleased(tmp_path):
    beads = [{"id": "heinzel-management-a", "labels": ["merged-5"]}, {"id": "heinzel-management-b", "labels": ["merged-6", "released-v0.1.0"]},
             {"id": "heinzel-management-c", "labels": []}]
    c = world(beads=beads)
    with using(native_runtime(tmp_path, c)):
        got = s.stamp_released()
    assert [(b.bead, b.rows[0].label) for b in got.beads] == [("heinzel-management-a", "released-v0.1.0")]
    assert "1 bead" in got.render()[-1]
    assert ["git", "tag", "--contains", "a" * 40] in c.calls


def test_sweep_skips_other_versions_unmerged_prs_and_unreadable_lists(tmp_path):
    beads = [{"id": "heinzel-management-a", "labels": ["merged-5"]}]
    with using(native_runtime(tmp_path, world(beads=beads))):
        assert s.stamp_released(version="0.2.0").stamped == 0
    with using(native_runtime(tmp_path, world(beads=beads, pr={"state": "OPEN"}))):
        assert s.stamp_released().stamped == 0
    c = world()
    c.on(["bd", "list"], ok("not json"))
    with using(native_runtime(tmp_path, c)):
        assert s.stamp_released().stamped == 0
    c = world()
    c.on(["bd", "list"], ok({"issues": beads}))
    with using(native_runtime(tmp_path, c)):
        assert s.stamp_released(dry_run=True).beads[0].render() == ["would stamp heinzel-management-a: released_in: v0.1.0 released_at=P"]


# ----------------------------------------------------------------------------- the commands
def test_beads_stamp_command_in_text_and_json(tmp_path):
    done = invoke(app_runtime(tmp_path, world()), ["beads", "stamp", "abc", "--merged", "5"])
    assert done.exit_code == 0 and done.stdout == "abc: merged_in: PR #5 sha=aaaaaaaa at=T\n"
    done = invoke(app_runtime(tmp_path, world()), ["--format", "json", "beads", "stamp", "abc", "--released", "0.1.0", "--dry-run"])
    doc = s.Stamped.model_validate(json.loads(done.stdout))
    assert doc.rows[0].status == "would_stamp" and doc.rows[0].label == "released-v0.1.0"


def test_beads_stamp_command_refuses_without_a_stamp_and_for_an_open_pr(tmp_path):
    done = invoke(app_runtime(tmp_path, world()), ["beads", "stamp", "abc"])
    assert done.exit_code == 2 and "--merged PR and/or --released VERSION" in done.stderr
    done = invoke(app_runtime(tmp_path, world(pr={"state": "OPEN"})), ["beads", "stamp", "abc", "--merged", "5"])
    assert done.exit_code == 1 and "is not merged" in done.stderr


def test_beads_stamp_released_command_in_text_and_json(tmp_path):
    beads = [{"id": "heinzel-management-a", "labels": ["merged-5"]}]
    done = invoke(app_runtime(tmp_path, world(beads=beads)), ["beads", "stamp-released", "--dry-run"])
    assert done.stdout.splitlines() == ["would stamp heinzel-management-a: released_in: v0.1.0 released_at=P", "sweep: 1 bead(s) stamped released"]
    done = invoke(app_runtime(tmp_path, world(beads=beads)), ["--format", "json", "beads", "stamp-released", "--version", "0.1.0"])
    assert s.StampSweep.model_validate(json.loads(done.stdout)).stamped == 1


# ----------------------------------------------------------------------------- configuration
def test_without_a_configured_document_note_refuses_as_a_usage_error(tmp_path):
    done = invoke(app_runtime(tmp_path, coord_document=None), ["note", "x"])
    assert done.exit_code == 2 and "coord_document" in done.stderr and list(tmp_path.iterdir()) == []


def test_lock_and_tracker_default_to_the_document_and_its_directory(tmp_path):
    from steuermaschine_heinzel.native.coord_lib import base

    settings = {"coord_document": str(tmp_path / "repo" / "AGENTS.md")}
    assert base.journal_lock(settings) == tmp_path / "repo" / "AGENTS.md.lock" and base.tracker_dir(settings) == tmp_path / "repo"
    assert base.child_env({"coord_tool_path": ["/x/bin"]})["PATH"].startswith("/x/bin:")


def test_note_with_an_argument_that_cannot_be_encoded_leaves_the_document_byte_identical(tmp_path):
    doc = tmp_path / "AGENTS.md"
    doc.write_text("## 8. Log\n- 11-12 22:09 line\n")
    before = doc.read_bytes()
    done = invoke(app_runtime(tmp_path), ["note", "caf\udce9 cut"])
    assert done.exit_code != 0 and doc.read_bytes() == before
