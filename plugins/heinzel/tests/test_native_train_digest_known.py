"""The candidate table's check classification and the registry of KNOWN failures (train_lib/digest.py)."""

from datetime import date, datetime

import pytest

from steuermaschine_heinzel.native.train_lib import digest as d


def check(name, conclusion=None, status="COMPLETED"):
    return {"name": name, "conclusion": conclusion, "status": status}


@pytest.mark.parametrize(("roll", "want"), [
    (None, ("pending", [])),
    ([check("Focused web-1", "FAILURE")], ("pending", [])),
    ([check("lint", None, "IN_PROGRESS")], ("pending", [])),
    ([check("lint", "SUCCESS"), check("Focused x", "FAILURE")], ("ready", [])),
    ([check("lint", "CANCELLED"), check("CI OK", "FAILURE")], ("deferred", [])),
    ([check("lint", "FAILURE"), check("CI OK", "FAILURE"), check("e2e", "TIMED_OUT")], ("failing", ["e2e", "lint"])),
    ([check("CI OK", "FAILURE"), check("coverage-floor", "FAILURE")], ("failing", ["CI OK", "coverage-floor"])),
])
def test_classify_pr(roll, want):
    assert d.classify_pr(roll) == want


def test_is_focused_tolerates_no_name():
    assert not d.is_focused(None) and d.is_focused("Focused unit")


def write(tmp_path, text):
    path = tmp_path / "known.toml"
    path.write_text(text)
    return path


def test_known_entries_dates_beads_and_defaults(tmp_path):
    path = write(tmp_path, """
[[known]]
match = "Flaky Login"
bead = "ab12"
note = "n"
until = 2026-11-20
[[known]]
match = "added only"
bead = "heinzel-management-cd34"
added = "2026-11-01"
class = "infra"
[[known]]
match = "no date"
bead = "ef56"
[[known]]
match = "not a bead"
bead = "error: bd failed"
""")
    rows = d.known_entries(path)
    assert [r["bead"] for r in rows] == ["ab12", "heinzel-management-cd34", "ef56"]
    assert rows[0]["until"] == date(2026, 11, 20) and rows[1]["until"] == date(2026, 11, 8) and rows[1]["class"] == "infra"
    assert rows[2]["until"] is None and d.overdue(rows[2])
    today = date(2026, 11, 13)
    assert d.load_known(path, today) == [("flaky login", "ab12", "n")]
    assert d.known_bead(["e2e: Flaky Login journey"], d.load_known(path, today)) == "ab12"
    assert d.known_bead(["other"], d.load_known(path, today)) is None and d.known_bead(["x"], None) is None


def test_date_forms():
    assert d._date(datetime(2026, 1, 2, 3, 4)) == date(2026, 1, 2)
    assert d._date(date(2026, 1, 2)) == date(2026, 1, 2)
    assert d._date("not a date") is None and d._date(None) is None


def test_missing_or_broken_registry_is_empty_unless_strict(tmp_path):
    assert d.known_entries(tmp_path / "none.toml") == []
    broken = write(tmp_path, "known = [")
    assert d.known_entries(broken) == []
    with pytest.raises(ValueError):
        d.known_entries(broken, strict=True)


@pytest.mark.parametrize("text", [
    'known = "x"',
    '[[known]]\nmatch = ""\nbead = "ab12"',
    '[[known]]\nmatch = "m"\nbead = "not valid!"',
])
def test_strict_rejects_bad_entries(tmp_path, text):
    with pytest.raises(ValueError):
        d.known_entries(write(tmp_path, text), strict=True)
