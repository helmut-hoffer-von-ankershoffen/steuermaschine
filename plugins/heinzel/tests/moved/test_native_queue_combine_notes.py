import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from steuermaschine_heinzel.native.queue_lib import combine_notes as notes  # noqa: E402

BAD = "Merged via this PR: #732, #737\n\nSummary: copy fixes."
GOOD = "Combines #732 fix(web): real web state\nCombines #737 fix(web): installer copy\n\nMerged via this PR: #732, #737"


def test_pr_770_style_body_is_refused():
    assert notes.problems("chore: compose #732 into a-cli", BAD)


def test_body_with_a_line_per_member_passes():
    assert notes.problems("chore: compose #732 into a-cli", GOOD) == []


def test_member_listed_without_its_line_is_named():
    out = notes.problems("chore: compose #1 into x", "Combines #1 fix(web): a\n\nMerged via this PR: #1, #2")
    assert out and "#2" in out[0]


def test_ordinary_pr_is_not_a_combine():
    assert notes.problems("fix(web): a thing", "Plain body mentioning #12.") == []


def test_hz_combine_meta_marks_a_combine():
    assert notes.problems("feat(web): x", "<!-- hz-combine area=a -->")


def test_ordinary_compose_wording_is_not_a_combine():
    assert notes.problems("fix(release): compose missing standard apps automatically", "Plain body.") == []


def test_plugin_slash_list_body_is_refused():
    assert notes.problems("feat(plugins): combine of 4 plugin changes", "Combines #69 / #79 / #80 / #81")
