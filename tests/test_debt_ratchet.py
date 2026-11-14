"""bin/debt.py, the plugin's debt ratchet: a number that is worse, unreadable or unrecorded turns the gate red.

- a coverage number that cannot be read (None) fails; it is never skipped
- mypy or ruff output without its summary (or clean) line fails; it never counts as 0
- a key missing from docs/debt.json fails
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import debt

BEFORE = {"coverage core %": 93.0, "mypy strict findings plugin": 10}


def test_equal_or_better_numbers_pass():
    assert debt.problems({"coverage core %": 93.0, "mypy strict findings plugin": 10}, BEFORE) == []
    assert debt.problems({"coverage core %": 95.0, "mypy strict findings plugin": 3}, BEFORE) == []


def test_worse_numbers_fail():
    found = debt.problems({"coverage core %": 92.0, "mypy strict findings plugin": 11}, BEFORE)
    assert len(found) == 2 and all("WORSE" in line for line in found)


def test_an_unreadable_coverage_number_fails():
    assert debt.problems({"coverage core %": None}, BEFORE) == ["coverage core %: could not be read (no coverage data, or the tool printed no summary line)"]


def test_mypy_or_ruff_output_without_a_summary_line_fails():
    assert debt.count(debt.MYPY[0], "Traceback (most recent call last):\n  boom", debt.MYPY[1]) is None
    assert debt.count(debt.RUFF[0], "error: Failed to parse pyproject.toml", debt.RUFF[1]) is None
    assert debt.problems({"mypy strict findings plugin": None}, BEFORE)[0].endswith("could not be read (no coverage data, or the tool printed no summary line)")


def test_summary_and_clean_lines_are_read():
    assert debt.count(debt.MYPY[0], "x.py:1: error: no\nFound 2025 errors in 99 files (checked 120 source files)\n", debt.MYPY[1]) == 2025
    assert debt.count(debt.MYPY[0], "Success: no issues found in 36 source files\n", debt.MYPY[1]) == 0
    assert debt.count(debt.RUFF[0], "a.py:1:1: F401\nFound 154 errors.\n[*] 82 fixable\n", debt.RUFF[1]) == 154
    assert debt.count(debt.RUFF[0], "All checks passed!\n", debt.RUFF[1]) == 0


def test_a_key_missing_from_the_record_fails():
    assert debt.problems({"environment reads plugin": 77}, BEFORE) == ["environment reads plugin: not recorded in docs/debt.json (record it: bin/debt.py --record)"]


def test_the_recorded_file_names_every_number_the_ratchet_measures():
    import json

    recorded = json.loads(debt.RATCHET.read_text())
    assert set(recorded) == {"coverage core %", "coverage plugin new code %", "coverage plugin moved libraries %", "mypy strict findings core",
                             "mypy strict findings plugin", "ruff findings moved libraries and plugin tests", "environment reads plugin",
                             "old names in plugin strings"}


def test_old_names_count_output_and_paths_but_not_maps_docstrings_or_replaces(tmp_path, monkeypatch):
    src = tmp_path / "plugins" / "heinzel" / "src"
    src.mkdir(parents=True)
    old = "hz" + "-queue"
    (src / "a.py").write_text(f'"""{old} doc."""\nX = "{old} list"\nY = "~/.cache/hz" + "-x"\n@native("q", replaces=("{old}",))\ndef f():\n    """{old}"""\n')
    (src / "moved_map.py").write_text(f'M = "{old}"\n')
    monkeypatch.setattr(debt, "ROOT", tmp_path)
    assert debt.old_names() == 2
    assert debt.env_reads() == 0
    (src / "b.py").write_text('import os\nA = os.environ.get("X")\nB = "os.environ"\n')
    assert debt.env_reads() == 1
