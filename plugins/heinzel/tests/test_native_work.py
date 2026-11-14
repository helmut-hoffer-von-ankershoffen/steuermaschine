"""`sm work ...`: a second name for `beads` and `focus`. Same commands, same gate, both names work."""

import json

from coord_kit import app_runtime, ok  # noqa: F401  (fixtures)
from test_native_coord_tasks import bd, world

from steuermaschine.app import command_tree
from steuermaschine.testing import invoke


def paths(group, prefix=()):
    out = set()
    for name, command in group.commands.items():
        out |= {(*prefix, name)} | (paths(command, (*prefix, name)) if hasattr(command, "commands") else set())
    return out


def test_work_holds_every_beads_command_and_the_focus_group(tmp_path):
    tree = command_tree(app_runtime(tmp_path, world()))
    below = lambda name: {p[1:] for p in paths(tree) if p[0] == name and len(p) > 1}  # noqa: E731
    assert below("work") == below("beads") | {("focus", *p) for p in below("focus")} | {("focus",)}
    assert {("tasks", "add"), ("tasks", "ls"), ("brief",), ("sweep",), ("focus", "add"), ("focus", "done"), ("focus", "list")} <= below("work")
    assert {"beads", "focus", "work"} <= set(tree.commands)  # the old names stay


def test_the_alias_runs_the_same_command_object(tmp_path):
    tree = command_tree(app_runtime(tmp_path, world()))
    assert tree.commands["work"].commands["tasks"].commands["add"] is tree.commands["beads"].commands["tasks"].commands["add"]
    assert tree.commands["work"].commands["focus"].commands["add"] is tree.commands["focus"].commands["add"]


def test_work_tasks_add_creates_the_bead_like_beads_tasks_add(tmp_path):
    c = world()
    done = invoke(app_runtime(tmp_path, c), ["work", "tasks", "add", "Logo cut off", "-p", "1"])
    assert done.stdout == "xy9\n"
    assert bd(c)[0][:4] == ["bd", "create", "--title", "Logo cut off"]


def test_work_tasks_ls_reads_like_beads_tasks_ls(tmp_path):
    rows = [{"id": "heinzel-management-abc", "title": "T", "status": "open", "priority": 2}]
    new = invoke(app_runtime(tmp_path, world(rows)), ["--format", "json", "work", "tasks", "ls", "all"])
    old = invoke(app_runtime(tmp_path, world(rows)), ["--format", "json", "beads", "tasks", "ls", "all"])
    assert new.stdout == old.stdout and json.loads(new.stdout)


def test_work_focus_add_list_done_share_the_focus_list(tmp_path):
    rt = app_runtime(tmp_path, world())
    wt = str(tmp_path / "head")
    assert invoke(rt, ["work", "focus", "add", wt, "--for", "1.0.0", "--note", "n"]).exit_code == 0
    listed = invoke(rt, ["--format", "json", "focus", "list"]).stdout
    assert wt in listed and listed == invoke(rt, ["--format", "json", "work", "focus", "list"]).stdout
    assert invoke(rt, ["work", "focus", "done", wt]).exit_code == 0
    assert wt not in invoke(rt, ["--format", "json", "work", "focus", "list"]).stdout


def test_work_help_names_both_old_words():
    from steuermaschine_heinzel.native import work

    assert "beads" in work.WORK.help and "focus" in work.WORK.help
