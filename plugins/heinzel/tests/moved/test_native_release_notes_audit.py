import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from steuermaschine_heinzel.native.release_lib import notes_audit as a  # noqa: E402

BODY = "chore: compose #732 into a-cli (#770)\n\nMerged via this PR: #732, #737\n"
LINES = "x (#9)\n\nCombines #5 fix(web): a\nCombines #6 test(web): b\n"
LOG = "# Changelog\n\n## [0.9.40]\n\n- **web:** a ([#5](u))\n\n## [0.9.39]\n\n- old ([#732](u))\n"


def test_both_body_forms_name_their_members():
    assert a.member_numbers(BODY) == {"732", "737"}
    assert a.member_numbers(LINES) == {"5", "6"}


def test_members_absent_from_the_newest_section_are_reported():
    assert a.missing([LINES], LOG) == ["6"]


def test_older_sections_do_not_count():
    assert a.missing([BODY], LOG) == ["732", "737"]


def test_no_combines_means_nothing_to_check():
    assert a.missing(["fix(web): plain (#1)"], LOG) == []
