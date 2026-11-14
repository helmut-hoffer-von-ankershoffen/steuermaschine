"""Bare `sm` (go): NEXT, THEN and ALSO name only live commands; a command that is not in the running tree, or an option
it does not take, is shown as missing instead of offered (heinzel-management-ka69)."""

from pathlib import Path

import pytest

from steuermaschine.app import Runtime, command_tree
from steuermaschine.config import Project
from steuermaschine.helpdoc import tree
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers
from steuermaschine_heinzel.native import go as area
from steuermaschine_heinzel.native.go_lib import live
from steuermaschine_heinzel.plugin import plugin

ROWS = [
    {"path": "sm release", "group": True, "options": []},
    {"path": "sm release rehearse", "group": True, "options": []},
    {"path": "sm release rehearse start", "group": False, "options": ["--consumers"]},
    {"path": "sm head pr", "group": False, "options": ["--body-file"]},
    {"path": "sm queue why", "group": False, "options": []},
]


@pytest.mark.parametrize("text", [
    "sm release rehearse start --consumers",
    "cd /w/x && sm head pr bead-1 'fix(web): a -- title' --body-file B.md",
    "after the fix is on main: sm release rehearse start --consumers",
    "sm queue why 690",
    "the queue tick (launchd, every 5 min)",
    "",
])
def test_live_commands_and_plain_text_are_unchanged(text):
    assert live.checker(ROWS)(text) == text


@pytest.mark.parametrize("text, command", [
    ("sm release cut", "sm release cut"),                                       # only a group: no such leaf
    ("sm release rehearse start --consumers --all", "sm release rehearse start --consumers --all"),  # an option it does not take
    ("after the verdict: sm head apply X --approved Y", "sm head apply X --approved Y"),
    ("sm release open 0.9.40 --release-dir CLONE", "sm release open 0.9.40 --release-dir CLONE"),
])
def test_a_command_that_is_not_live_is_shown_as_missing(text, command):
    assert live.checker(ROWS)(text) == f"missing: `{command}` is not a live command (sm tools gaps); nothing to run for it now"


def test_report_checks_next_then_and_also_against_the_running_tree(monkeypatch):
    decided = {"phase": "main green, no candidate", "kind": "cut-ready", "next": "sm release cut", "next_for": None,
               "then": "after the fix: sm release rehearse start --consumers", "trap": "", "waiting": None, "side": [], "facts": [], "unknown": [],
               "also_items": [{"phase": "PR #1", "next": "sm queue why 1", "waiting_on": ""}, {"phase": "PR #2", "next": "sm release open 0.9.40", "waiting_on": ""}]}
    monkeypatch.setattr(area.rules, "decide", lambda *a, **k: dict(decided))
    result = area.report({"prs": []}, 0.0, commands=ROWS)
    assert result.next.startswith("missing: `sm release cut`")
    assert result.then == decided["then"]
    assert [a.next for a in result.also] == ["sm queue why 1", "missing: `sm release open 0.9.40` is not a live command (sm tools gaps); nothing to run for it now"]
    assert area.report({"prs": []}, 0.0).next == "sm release cut"  # outside a running tree nothing is checked


def test_against_the_real_tree_rehearse_and_cut_are_live_and_a_made_up_command_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": {}}, Path("."))
    rt = Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, settings, _providers=fake_providers())
    rows = tree(command_tree(rt), "sm")
    check = live.checker(rows)
    for command in ("sm release rehearse start --consumers", "sm mini verify --minimum 0.9.40", "sm go", "sm ci faillog 803", "sm release pr 0.9.41 --head X --dry-run"):
        assert check(command) == command
    assert check("sm release cut") == "sm release cut"  # the cut pipeline is live
    assert check("sm release nosuch").startswith("missing:")
