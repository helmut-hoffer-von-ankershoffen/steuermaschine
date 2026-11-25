"""bin/debt.py, the plugin's debt ratchet: a number that is worse, unreadable or unrecorded turns the gate red.

- a coverage number that cannot be read (None) fails; it is never skipped
- mypy or ruff output without its summary (or clean) line fails; it never counts as 0
- a key missing from docs/debt.json fails
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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
    assert set(recorded) == {f"coverage {group} % [{platform}]" for group in ("core", "plugin new code", "plugin moved libraries")
                             for platform in ("darwin", "linux")} | {"mypy strict findings core",
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


LINUX = {"coverage core % [darwin]": 95.0, "coverage core % [linux]": 93.0}


def test_each_platform_is_gated_against_its_own_baseline():
    assert debt.problems({"coverage core % [linux]": 93.0}, LINUX) == []  # below darwin's 95, equal to its own
    assert debt.problems({"coverage core % [linux]": 92.0}, LINUX) == ["coverage core % [linux]: 92.0 is WORSE than recorded 93.0"]
    assert debt.problems({"coverage core % [darwin]": 94.0}, LINUX) != []


def test_a_platform_without_a_baseline_is_not_gated_on_coverage_but_other_numbers_still_are():
    now = {"coverage core % [win]": 10.0, "mypy strict findings plugin": 11}
    assert debt.problems(now, BEFORE | {"coverage core % [darwin]": 95.0}) == ["mypy strict findings plugin: 11 is WORSE than recorded 10"]
    assert debt.ungated(now, LINUX) == ["coverage core % [win]"]
    for platform in ("darwin", "linux"):  # a missing baseline on a platform the project builds on is red
        key = f"coverage core % [{platform}]"
        assert debt.problems({key: 90.0}, {}) == [f"{key}: not recorded in docs/debt.json (record it: bin/debt.py --record)"] and debt.ungated({key: 90.0}, {}) == []
    assert debt.problems({"coverage core % [win]": None}, LINUX) != []  # unreadable stays red
    assert debt.problems({"coverage core % [win]": 10.0}, LINUX, strict=True) == ["coverage core % [win]: not recorded in docs/debt.json (record it: bin/debt.py --record)"]


def test_the_metrics_carry_the_platform_of_the_run(monkeypatch):
    monkeypatch.setattr(debt, "coverage", lambda include, omit="": 90.0)
    assert set(debt.coverage_metrics()) == {f"coverage {group} % [{debt.PLATFORM}]" for group in ("core", "plugin new code", "plugin moved libraries")}


def test_record_keeps_other_platforms_and_refuses_to_lower_a_number(tmp_path, monkeypatch, capsys):
    import json

    ratchet = tmp_path / "debt.json"
    ratchet.write_text(json.dumps({"coverage core % [other]": 80.0, "coverage core % [" + debt.PLATFORM + "]": 95.0, "mypy strict findings plugin": 10}))
    monkeypatch.setattr(debt, "RATCHET", ratchet)
    monkeypatch.setattr(debt, "ROOT", tmp_path)
    monkeypatch.setattr(debt, "coverage_metrics", lambda: {f"coverage core % [{debt.PLATFORM}]": 94.0})
    monkeypatch.setattr(debt, "source_metrics", lambda: {"mypy strict findings plugin": 10})
    monkeypatch.setattr(sys, "argv", ["debt.py", "--record"])
    with pytest.raises(SystemExit):
        debt.main()
    assert json.loads(ratchet.read_text())[f"coverage core % [{debt.PLATFORM}]"] == 95.0  # nothing lowered silently
    monkeypatch.setattr(sys, "argv", ["debt.py", "--record", "--lower"])
    debt.main()
    recorded = json.loads(ratchet.read_text())
    assert recorded[f"coverage core % [{debt.PLATFORM}]"] == 94.0 and recorded["coverage core % [other]"] == 80.0


@pytest.mark.parametrize("flags", [["--gate"], ["--record", "--lower"]])
@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "1e999", "true", "false", "null", '"95"', "-1", "101"])
def test_malformed_platform_threshold_refused_without_writing(tmp_path, monkeypatch, raw, flags):
    path = tmp_path / "debt.json"
    original = '{"coverage core % [linux]": ' + raw + '}'
    path.write_text(original)
    monkeypatch.setattr(debt, "RATCHET", path)
    monkeypatch.setattr(debt, "coverage_metrics", lambda: {"coverage core % [linux]": 90})
    monkeypatch.setattr(debt, "source_metrics", dict)
    monkeypatch.setattr(sys, "argv", ["debt.py", *flags])
    with pytest.raises(SystemExit) as exc:
        debt.main()
    assert exc.value.code == 1
    assert path.read_text() == original


@pytest.mark.parametrize("document", ['[]', '{"count": 1.5}', '{"count": 4, "count": 14}'])
def test_invalid_baseline_structure(tmp_path, document):
    path = tmp_path / "debt.json"
    path.write_text(document)
    with pytest.raises(ValueError):
        debt.read_baseline(path)


@pytest.mark.parametrize("raw", ["nan", "inf", "-1", "101"])
def test_impossible_coverage_measurement_is_unreadable(monkeypatch, raw):
    monkeypatch.setattr(debt, "run", lambda *args: raw)
    assert debt.coverage("src/*") is None
