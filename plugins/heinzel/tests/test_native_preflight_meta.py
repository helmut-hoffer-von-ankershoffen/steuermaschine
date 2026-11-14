"""The commit-meta rules: the moved tests of the former commit-meta tool. Assertions unchanged; only the import and the
pattern's source changed (the rules no longer read a fixed clone under the home directory: without a pyproject they
use the built-in pattern)."""
# ruff: noqa: E731  (moved test: the helper lambda keeps its line)
from pathlib import Path

from steuermaschine_heinzel.native.preflight_lib import ci_meta as m

PAT = m.pattern(Path("/nonexistent"))  # was: m.pattern(), the pattern of the clone under the home directory
none = lambda texts: []


def test_long_subject_and_title():
    long = "feat(cli): " + "x" * 70
    p = m.evaluate([long], long, "feat/x", "628", PAT, none)
    assert any(x.startswith("commit: ") and "> 72" in x for x in p)
    assert any(x.startswith("PR title") and "gh pr close 628" in x and "gh pr reopen 628" in x for x in p)


def test_pass_and_nonconventional():
    assert m.evaluate(["fix(web): short"], "fix(web): short", "fix/x", "1", PAT, none) == []
    assert any("not conventional" in x for x in m.evaluate(["Update stuff"], None, "ok", None, PAT, none))
    assert any(x.startswith("branch") for x in m.evaluate([], None, "Bad Branch", None, PAT, none))


def test_terms_hook():
    assert m.evaluate(["fix(web): a"], None, None, None, PAT, lambda t: ["confidential: x"]) == ["confidential: x"]


def test_merge_and_revert_subjects_pass():
    subs = ["Merge branch 'main' into feat/x", "Merge remote-tracking branch 'origin/main'", 'Revert "feat(cli): x"']
    assert m.evaluate(subs, None, "feat/x", None, PAT, none) == []
    assert m.evaluate(["Merged nothing"], None, "feat/x", None, PAT, none)
