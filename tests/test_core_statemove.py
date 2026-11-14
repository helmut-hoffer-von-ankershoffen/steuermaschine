"""State files move to the names of a new release in one explicit pass, and back; nothing moves at run time."""

import fcntl
import json
import os
from pathlib import Path

import pytest

from steuermaschine import API_VERSION, statemove
from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.hookspecs import hookimpl
from steuermaschine.plugins import Settings, stack_of
from steuermaschine.statemove import MigrationError, Move
from steuermaschine.testing import fake_providers, invoke


@pytest.fixture
def world(tmp_path):
    old, new = tmp_path / "cache", tmp_path / "state" / "proj"
    old.mkdir()
    (old / "q.json").write_text('{"items": [1]}')
    (old / "q.lock").write_text("")
    (old / "train").mkdir()
    (old / "train" / "v1.json").write_text("{}")
    moves = [Move(old / "q.json", new / "queue.json"), Move(old / "q.lock", new / "queue.lock"), Move(old / "train", new / "train"),
             Move(old / "never.json", new / "never.json")]
    return old, new, moves


def snapshot(root: Path) -> dict[str, str]:
    """Every regular file under `root` (links followed by name only) with its content."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            out[str(path.relative_to(root))] = "-> " + os.readlink(path)
        elif path.is_file():
            out[str(path.relative_to(root))] = path.read_text()
    return out


def test_forward_moves_files_and_directories_and_leaves_links(world):
    old, new, moves = world
    lines = []
    done = statemove.forward(moves, lines.append)
    assert done == lines and len(done) == 3
    assert (new / "queue.json").read_text() == '{"items": [1]}' and (new / "train" / "v1.json").exists()
    assert (old / "q.json").is_symlink() and (old / "q.json").read_text() == '{"items": [1]}'
    assert (old / "train").is_symlink() and (old / "train" / "v1.json").exists()
    assert not os.path.lexists(new / "never.json") and not os.path.lexists(old / "never.json")
    assert statemove.pending(moves) == [] and statemove.forward(moves, lines.append) == []  # a second pass does nothing


def test_a_lock_held_during_the_move_still_excludes_through_both_names(world):
    old, new, moves = world
    with (old / "q.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        statemove.forward(moves, lambda _: None)
        for name in (old / "q.lock", new / "queue.lock"):
            with name.open("a") as other, pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_migrate_back_and_migrate_again_keeps_the_state_identical(world):
    old, new, moves = world
    before = snapshot(old)
    statemove.forward(moves, lambda _: None)
    (new / "queue.json").write_text('{"items": [1, 2]}')  # this release wrote through the new name
    (new / "train" / "v2.json").write_text("{}")
    statemove.back(moves, lambda _: None)
    after_back = snapshot(old)
    assert after_back == {**before, "q.json": '{"items": [1, 2]}', "train/v2.json": "{}"}
    assert not any(m.old.is_symlink() for m in moves) and not os.path.lexists(new / "queue.json")
    statemove.forward(moves, lambda _: None)
    assert {k: v for k, v in snapshot(new).items()} == {"queue.json": '{"items": [1, 2]}', "queue.lock": "", "train/v1.json": "{}", "train/v2.json": "{}"}
    assert statemove.back(moves, lambda _: None) and statemove.back(moves, lambda _: None) == []  # back twice: the second does nothing


def test_one_problem_refuses_the_whole_pass_and_nothing_moves(world):
    old, new, moves = world
    new.mkdir(parents=True)
    (new / "queue.lock").write_text("other")
    with pytest.raises(MigrationError, match="both hold state"):
        statemove.forward(moves, lambda _: None)
    assert not (old / "q.json").is_symlink() and not os.path.lexists(new / "queue.json")
    (old / "elsewhere").write_text("")
    (new / "queue.lock").unlink()
    (old / "q.lock").unlink()
    os.symlink(old / "elsewhere", old / "q.lock")
    assert statemove.problems(moves) == [f"{old / 'q.lock'} is a link to {old / 'elsewhere'}, not to {new / 'queue.lock'}"]
    assert statemove.problems(moves, back=True) == [f"{old / 'q.lock'} is a link to {old / 'elsewhere'}, not to {new / 'queue.lock'}"]


def test_another_file_system_is_refused(world, monkeypatch):
    _old, new, moves = world
    real = os.stat

    def stat(path, *args, **kwargs):
        found = real(path, *args, **kwargs)
        if str(path).startswith(str(new.parent)):
            return os.stat_result((*found[:2], found.st_dev + 1, *found[3:]))
        return found

    new.parent.mkdir(parents=True)
    monkeypatch.setattr(statemove.os, "stat", stat)
    assert any("different file systems" in p for p in statemove.problems(moves))


def test_a_directory_made_again_during_the_move_gives_up_its_entries(world, monkeypatch):
    old, new, moves = world
    real = os.rename

    def rename(src, dst):
        real(src, dst)
        if Path(src) == old / "train":  # an older process makes the directory again right after the rename
            (old / "train").mkdir()
            (old / "train" / "late.json").write_text("late")

    monkeypatch.setattr(statemove.os, "rename", rename)
    statemove.forward(moves, lambda _: None)
    assert (old / "train").is_symlink() and (new / "train" / "late.json").read_text() == "late"


def test_back_moves_a_path_this_release_made_first(world):
    old, new, moves = world
    statemove.forward(moves, lambda _: None)
    (new / "never.json").write_text("made here")
    statemove.back(moves, lambda _: None)
    assert (old / "never.json").read_text() == "made here" and not (old / "never.json").is_symlink()
    (new / "queue.json").write_text("x")
    assert statemove.problems(moves, back=True) == [f"{old / 'q.json'} and {new / 'queue.json'} both hold state"]


def test_run_time_reads_the_new_path_only_and_refuses_unmigrated_or_split_state(world):
    old, new, moves = world
    with pytest.raises(MigrationError, match="not migrated"):
        statemove.require_migrated(moves[0])
    assert statemove.require_migrated(moves[3]) == new / "never.json"  # neither exists: the new name, made on first write
    statemove.forward(moves, lambda _: None)
    assert statemove.require_migrated(moves[0]) == new / "queue.json"
    (old / "q.json").unlink()
    (old / "q.json").write_text("written by an older release")
    with pytest.raises(MigrationError, match="both hold state"):
        statemove.require_migrated(moves[0])


class Mover:
    name = "mover"

    def __init__(self, moves):
        self.moves = moves

    @hookimpl
    def sm_api_version(self):
        return API_VERSION

    @hookimpl
    def sm_state_moves(self, settings, home):
        return self.moves


def runtime(moves, home):
    stack = stack_of(Mover(moves))
    project = Project(name="proj", plugin=stack.name, root=Path("."), plugins=stack.names, allow_unreviewed=True)
    return Runtime(project, stack, Settings({}), _providers=fake_providers()), project


def test_the_command_checks_lists_moves_and_moves_back(world, monkeypatch, tmp_path):
    old, _new, moves = world
    rt, project = runtime(moves, tmp_path)
    check = invoke(rt, ["self", "migrate-state", "--check"])
    assert check.exit_code == 3 and "3 of 4 state path(s) still at the old name" in check.stdout
    listed = json.loads(invoke(rt, ["--format", "json", "self", "migrate-state", "--list"]).stdout)
    assert [m["state"] for m in listed["moves"]] == ["old", "old", "old", "none"]
    done = invoke(rt, ["self", "migrate-state"])
    assert done.exit_code == 0 and done.stdout.count("moved ") == 3
    assert (project.home / "state-migrations.log").read_text().count("moved ") == 3
    assert invoke(rt, ["self", "migrate-state", "--check"]).exit_code == 0
    assert invoke(rt, ["self", "migrate-state"]).stdout.strip() == "nothing to move"
    keep = tmp_path / "keep.json"
    keep.write_text(json.dumps({"moves": [{"old": str(old / "train")}]}))  # the target release knows the train's new name
    assert invoke(rt, ["self", "migrate-state", "--back", "--keep", str(keep)]).exit_code == 0
    assert not (old / "q.json").is_symlink() and (old / "train").is_symlink()


def test_the_command_refuses_while_a_writer_runs_and_on_a_problem(world, tmp_path):
    old, new, moves = world
    rt, project = runtime(moves, tmp_path)
    project.home.mkdir(parents=True, exist_ok=True)
    with (project.home / "writers.lock").open("a") as writer:
        fcntl.flock(writer, fcntl.LOCK_SH)
        out = invoke(rt, ["self", "migrate-state", "--wait", "2"])
    assert out.exit_code == 70 and "a write command still runs after 2s" in out.stderr and not (old / "q.json").is_symlink()
    new.mkdir(parents=True)
    (new / "queue.json").write_text("{}")
    out = invoke(rt, ["self", "migrate-state"])
    assert out.exit_code == 70 and "both hold state" in out.stderr


def test_without_moves_there_is_nothing_to_migrate_and_without_a_project_it_refuses():
    from steuermaschine.plugins import stack_of as _stack

    stack = _stack()
    rt = Runtime(Project(name="p", plugin="none", root=Path("."), plugins=[], allow_unreviewed=True), stack, Settings({}), _providers=fake_providers())
    assert invoke(rt, ["self", "migrate-state"]).stdout.strip() == "nothing to move"
    assert invoke(Runtime(None, stack, Settings({}), _providers=fake_providers()), ["self", "migrate-state"]).exit_code == 2
