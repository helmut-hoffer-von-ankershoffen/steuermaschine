"""`sm-guard`, the Bash hook that stays safe when sm cannot load: the real console script, run with the hook JSON on stdin,
against a toy floor package whose `__init__` raises (so the test also proves that it is never run)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from steuermaschine import guardhook

SM_GUARD = Path(sys.executable).parent / "sm-guard"

FLOOR = '''
FULL = "toyfloor.full:check"
def refusal(command):
    return "Blocked: floor" if "floor-no" in command else None
'''
FULL = '''
def check(command):
    return "Blocked: full" if "full-no" in command else None
'''


@pytest.fixture
def guard(tmp_path):
    pkg = tmp_path / "lib" / "toyfloor"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("raise RuntimeError('the package init must not run')\n")
    (pkg / "rules.py").write_text(FLOOR)
    (pkg / "full.py").write_text(FULL)
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "lib")}

    def run(command=None, raw=None, args=("--floor", "toyfloor.rules")):
        text = raw if raw is not None else json.dumps({"tool_input": {"command": command}})
        return subprocess.run([str(SM_GUARD), *args], input=text, capture_output=True, check=False, text=True, env=env, timeout=30)

    run.pkg = pkg
    return run


def test_allows_what_both_rule_sets_allow(guard):
    done = guard("git status")
    assert done.returncode == 0 and done.stderr == ""


def test_blocks_what_the_floor_or_the_full_rules_refuse(guard):
    assert (guard("floor-no").returncode, guard("floor-no").stderr.strip()) == (2, "Blocked: floor")
    assert (guard("full-no").returncode, guard("full-no").stderr.strip()) == (2, "Blocked: full")


def test_falls_back_to_the_floor_when_the_full_rules_cannot_load(guard):
    (guard.pkg / "full.py").write_text("import a_module_that_does_not_exist\n")
    assert guard("floor-no").returncode == 2
    done = guard("full-no")
    assert done.returncode == 0 and "not available" in done.stderr
    (guard.pkg / "full.py").write_text("def check(command):\n    raise ValueError('boom')\n")
    assert guard("floor-no").returncode == 2 and guard("ls").returncode == 0


def test_fails_closed_when_the_floor_cannot_load_or_raises(guard):
    (guard.pkg / "rules.py").write_text("import a_module_that_does_not_exist\n")
    done = guard("ls")
    assert done.returncode == 2 and "could not be applied" in done.stderr
    (guard.pkg / "rules.py").write_text("def refusal(command):\n    raise ValueError('boom')\n")
    assert guard("ls").returncode == 2
    assert guard("ls", args=("--floor", "no_such_floor_module")).returncode == 2


def test_fails_closed_on_input_it_cannot_read_and_on_bad_arguments(guard):
    for raw in ("", "not json", "[]", '{"tool_input": []}', '{"tool_input": {"command": 5}}'):
        done = guard(raw=raw)
        assert done.returncode == 2 and "could not be read" in done.stderr, raw
    assert guard(raw="{}").returncode == 0  # no command: nothing to refuse
    assert guard("ls", args=()).returncode == 2
    assert guard("ls", args=("--floor", "toyfloor.rules", "--bogus")).returncode == 2


def test_reads_a_file_with_input(guard, tmp_path):
    hook = tmp_path / "hook.json"
    hook.write_text(json.dumps({"tool_input": {"command": "floor-no"}}))
    assert guard(raw="", args=("--floor=toyfloor.rules", f"--input={hook}")).returncode == 2
    assert guard(raw="", args=("--floor", "toyfloor.rules", "--input", str(tmp_path / "missing"))).returncode == 2


def test_imports_nothing_but_the_standard_library(guard, tmp_path):
    probe = ("import sys, io\nsys.stdin = io.StringIO('{\"tool_input\": {\"command\": \"ls\"}}')\n"
             "from steuermaschine import guardhook\n"
             "try:\n    guardhook.main(['--floor', 'toyfloor.rules'])\nexcept SystemExit as done:\n    assert done.code == 0, done.code\n"
             "print('\\n'.join(sorted(sys.modules)))\n")
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "lib")}
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env, timeout=30, check=True)
    foreign = {m.split(".")[0] for m in done.stdout.split()} - set(sys.stdlib_module_names) - {"steuermaschine", "__main__"}
    foreign -= {"sitecustomize", "usercustomize"}  # loaded by `site` at interpreter start (Homebrew Pythons), not by the hook
    assert foreign == set(), foreign


def test_an_unexpected_failure_blocks(monkeypatch, capsys):
    monkeypatch.setattr(guardhook, "decide", lambda argv: 1 / 0)
    with pytest.raises(SystemExit) as done:
        guardhook.main(["--floor", "x"])
    assert done.value.code == 2 and "sm-guard failed" in capsys.readouterr().err


def test_a_top_level_module_can_be_the_floor(tmp_path, monkeypatch):
    (tmp_path / "flatfloor.py").write_text("def refusal(command):\n    return 'Blocked: flat' if command == 'x' else None\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert guardhook.decide(["--floor", "flatfloor"], stdin='{"tool_input": {"command": "x"}}') == (2, "Blocked: flat")
    assert guardhook.decide(["--floor", "flatfloor"], stdin='{"tool_input": {"command": "y"}}') == (0, "")
    assert guardhook.decide(["--floor", "json.nope"], stdin='{"tool_input": {"command": "y"}}')[0] == 2  # no such file in the package
