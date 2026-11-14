"""The process shapes of sm itself (`steuermaschine.smprocs`): which `ps` lines are sm, and what they run. The live
process shapes are proven by the project plugin's test that starts a real `sm gate run`; these are the pure rules."""

import pytest

from steuermaschine import smprocs


def test_entries_interpreters_and_install_roots():
    shapes = smprocs.Shapes(("/opt/sm",))
    assert shapes.verbs("/opt/sm/current/venv/bin/python /opt/sm/current/venv/bin/steuermaschine --format json gate run -- pytest x") == ["gate", "run"]
    assert shapes.verbs("sm daemon tick --loop 1") == ["daemon", "tick"]
    assert shapes.verbs("python3 -X dev -m steuermaschine release cut") == ["release", "cut"]
    assert shapes.is_sm("/opt/sm/current/venv/bin/python -c 'print(1)'") and not shapes.is_sm("python -c 'import sm'")
    assert not shapes.is_sm("pytest tests") and not shapes.is_sm("") and shapes.verbs("bash -lc true") is None
    assert shapes.runs("sm gate run -- make", [("gate", "run")]) and not shapes.runs("sm gate status", [("gate", "run")])


def test_install_root_and_establish(tmp_path):
    assert smprocs.install_root("/s/releases/r1/venv/bin/python") == smprocs.Path("/s")
    assert smprocs.install_root("/s/current/venv/bin/python") == smprocs.Path("/s")
    assert smprocs.install_root("/w/.venv/bin/python") == smprocs.Path("/w") and smprocs.install_root("/usr/bin/python3") is None
    assert smprocs.establish([tmp_path], "/usr/bin/python3").roots[0] == str(tmp_path.absolute())
    with pytest.raises(smprocs.Unestablished, match="none is configured"):
        smprocs.establish([], "/usr/bin/python3")
    with pytest.raises(smprocs.Unestablished, match="not found"):
        smprocs.establish([tmp_path / "missing"], "/usr/bin/python3")
