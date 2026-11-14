"""The state files of the scripted era that `go` adopts read-only (go_lib/legacy.py): absent, readable, malformed."""

import os

from steuermaschine_heinzel.native.go_lib import legacy


def test_queue_log_tail_reads_the_last_lines_or_nothing(tmp_path):
    assert legacy.queue_log_tail(tmp_path) == []
    (tmp_path / "queue.log").write_text("".join(f"line {n}\n" for n in range(100)))
    assert legacy.queue_log_tail(tmp_path, 3) == ["line 97", "line 98", "line 99"]


def test_deferred_runs_takes_the_first_word_of_each_line(tmp_path):
    assert legacy.deferred_runs(tmp_path) == set()
    (tmp_path / "ci-deferred.txt").write_text("123 pr 4\n\n456\n")
    assert legacy.deferred_runs(tmp_path) == {"123", "456"}


def test_release_holds_and_the_freeze(tmp_path):
    assert legacy.release_holds(tmp_path) == [] and not legacy.release_holds_present(tmp_path)
    assert not legacy.incident_frozen(tmp_path) and not legacy.merge_frozen(tmp_path) and legacy.incident_since(tmp_path) is None
    holds = tmp_path / "release-holds"
    holds.mkdir()
    assert not legacy.release_holds_present(tmp_path)
    (holds / "0.9.40").write_text("")
    assert legacy.release_holds(tmp_path) == ["0.9.40"] and legacy.release_holds_present(tmp_path) and legacy.merge_frozen(tmp_path)
    (tmp_path / "merge-freeze").write_text("")
    assert legacy.incident_frozen(tmp_path) and isinstance(legacy.incident_since(tmp_path), float)
    (tmp_path / "gate.hold").write_text("")
    assert sorted(legacy.hold_files(tmp_path)) == ["0.9.40", "gate.hold", "merge-freeze"]


def test_a_hold_path_that_is_a_file_or_a_dangling_freeze_link_blocks(tmp_path):
    (tmp_path / "release-holds").write_text("not a directory")
    assert legacy.release_holds_present(tmp_path) and legacy.release_holds(tmp_path) == []
    os.symlink(tmp_path / "gone", tmp_path / "merge-freeze")
    assert legacy.incident_frozen(tmp_path)
