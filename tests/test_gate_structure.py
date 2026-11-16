"""The structural part of the promote gate. These rules hold for ALL slices of the plugin.

- main blocks: moved libraries expose functions only. An `if __name__ == "__main__"` is a second way in that no command
  registers. The invariant is "no entry outside the registry reaches a write".
- process starts: nothing starts an old tool script; processes go through the one runner (no `subprocess`).
- path defaults: no setting defaults to a real path of this machine (that is how a test once wrote into a real file).
- read commands that reach a write: a command registered `writes=False` reaches no GitHub write and no push.

What a slice brought before the rule existed is listed in tests/gate_known.json with its owner. A finding that is
not listed fails; a listed one that is gone fails too, so the file can only shrink. bin/gate_known.py maintains it.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import gate_known

from steuermaschine_heinzel import native

KNOWN = gate_known.load()
RULES = ["main blocks", "process starts", "path defaults", "read commands that reach a write"]


@pytest.fixture(scope="module")
def current():
    return gate_known.current()


@pytest.mark.parametrize("rule", RULES)
def test_no_new_structural_finding_and_no_stale_entry(rule, current):
    found, known = current[rule], set(KNOWN.get(rule, {}))
    assert found - known == set(), f"new finding(s) for '{rule}':\n" + "\n".join(sorted(found - known))
    assert known - found == set(), f"fixed; remove from tests/gate_known.json ('{rule}'): " + ", ".join(sorted(known - found))


def test_every_known_finding_names_its_owner():
    for rule, table in KNOWN.items():
        assert rule in RULES, rule
        for finding, owner in table.items():
            assert owner.strip(), (rule, finding)
    assert json.dumps(KNOWN, indent=1, sort_keys=True) + "\n" == gate_known.FILE.read_text()


def commands():
    return [c for c in native.commands() if hasattr(c, "fn")]


def test_every_write_command_has_code_that_self_reviews_can_follow():
    from steuermaschine.reviewgraph import reach

    writers = [cmd for cmd in commands() if cmd.writes]
    found = reach(writers)
    assert [unit.path for unit in found] == [cmd.path for cmd in writers]  # none without a module on disk
    registry = {"steuermaschine_heinzel/plugin.py", "steuermaschine_heinzel/native/__init__.py", "steuermaschine_heinzel/pipelines/__init__.py"}
    # the registry imports every area: a write path that reaches it reaches the whole plugin, and the list says nothing
    assert not [(unit.path, sorted(registry & set(unit.files))) for unit in found if registry & set(unit.files)]


def test_command_paths_are_unique():
    paths = [c.path for c in commands()]
    assert len(paths) == len(set(paths)), sorted(p for p in paths if paths.count(p) > 1)


def test_every_setting_has_a_description():
    from steuermaschine.plugins import stack_of
    from steuermaschine_heinzel.plugin import plugin

    for name, info in stack_of(plugin).settings_models()["heinzel"].model_fields.items():
        assert info.description, name
