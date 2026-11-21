"""Bare `sm` (go): NEXT, THEN and ALSO name only live commands; a command that is not in the running tree, or an option
it does not take, is shown as missing instead of offered (heinzel-management-ka69)."""


import pytest

from steuermaschine_heinzel.native import go as area
from steuermaschine_heinzel.native.go_lib import live

ROWS = [
    {"path": "sm release", "group": True, "options": []},
    {"path": "sm release rehearse", "group": True, "options": []},
    {"path": "sm release rehearse start", "group": False, "options": ["--consumers"]},
    {"path": "sm head pr", "group": False, "options": ["--body-file"]},
    {"path": "sm queue why", "group": False, "options": []},
    {"path": "sm why", "group": False, "options": []},
    {"path": "sm ci why", "group": False, "options": [], "hidden": True},
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
    ("sm ci why 12", "sm ci why 12"),  # hidden: a NEXT names visible commands only (h8jg5)
])
def test_a_command_that_is_not_live_is_shown_as_missing(text, command):
    assert live.checker(ROWS)(text) == f"missing: `{command}` is not a visible command; nothing to run for it now (sm why)"


def test_report_checks_next_then_and_also_against_the_running_tree(monkeypatch):
    decided = {"phase": "main green, no candidate", "kind": "cut-ready", "next": "sm release cut", "next_for": None,
               "then": "after the fix: sm release rehearse start --consumers", "trap": "", "waiting": None, "side": [], "facts": [], "unknown": [],
               "also_items": [{"phase": "PR #1", "next": "sm queue why 1", "waiting_on": ""}, {"phase": "PR #2", "next": "sm release open 0.9.40", "waiting_on": ""}]}
    monkeypatch.setattr(area.rules, "decide", lambda *a, **k: dict(decided))
    result = area.report({"prs": []}, 0.0, commands=ROWS)
    assert result.next.startswith("missing: `sm release`")  # `release cut` is written as `release`, a group without a default here
    assert result.then == decided["then"]
    assert [a.next for a in result.also] == ["sm why 1", "missing: `sm release open 0.9.40` is not a visible command; nothing to run for it now (sm why)"]
    assert area.report({"prs": []}, 0.0).next == "sm release"  # outside a running tree only the old names are translated


