"""Two real plugins on one project: the toy as base, the layer on top (plugins = ["toy", "layer"])."""

import json
import subprocess

import pytest

from steuermaschine import app
from steuermaschine.testing import assert_conformant
from steuermaschine_layer.plugin import plugin as layer
from steuermaschine_toy.plugin import plugin as toy


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "commit", "-q", "--allow-empty", "-m", "init")
    git(r, "checkout", "-q", "-b", "feat/hello")
    (r / "hello.txt").write_text("hi\n")
    git(r, "add", "hello.txt")
    git(r, "commit", "-q", "-m", "hello")
    (r / "steuermaschine.toml").write_text('[project]\nname = "layered"\nplugins = ["toy", "layer"]\n')
    (r / ".git" / "info" / "exclude").write_text("steuermaschine.toml\n.sim/\n")
    monkeypatch.chdir(r)
    monkeypatch.setenv("STEUERMASCHINE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-user-config"))
    for var in ("STEUERMASCHINE_PLUGIN", "STEUERMASCHINE_PROJECT", "SM_FORMAT", "SM_LAYER__FROZEN", "SM_LAYER__WINDOW_OPEN"):
        monkeypatch.delenv(var, raising=False)
    return r


def sm(capsys, *args):
    with pytest.raises(SystemExit) as stop:
        app.main(list(args))
    out = capsys.readouterr()
    return stop.value.code, out.out, out.err


def test_conformance_of_the_layer_on_its_base():
    assert_conformant(layer, {}, on=[toy])


def test_both_plugins_are_loaded_in_the_order_of_the_settings(repo, capsys):
    code, out, _ = sm(capsys, "--format", "json", "self", "plugins")
    rows = json.loads(out)
    assert code == 0 and [(r["name"], r["layer"]) for r in rows] == [("toy", "base"), ("layer", "outermost")]
    assert rows[1]["wrappers"] == ["sm_step_run"] and "sm_pipelines" in rows[0]["hooks"]
    code, out, _ = sm(capsys, "self", "config")
    assert "toy.world" in out and "layer.frozen = False" in out


def test_the_layer_adds_a_step_a_gate_a_trap_and_audit_receipts_to_the_toys_pipeline(repo, tmp_path, capsys):
    _, _, err = sm(capsys, "head", "ship", ".", "--plan")
    assert "[4/6 window] open" in err
    sm(capsys, "head", "ship", ".", "--live")
    sm(capsys, "sim", "ci", "green", ".")
    code, _out, err = sm(capsys, "head", "ship", ".", "--live")
    assert code == 0 and "[4/6 window] done: change window is open" in err and "[5/6 merge] done" in err
    audit = sorted(p.name.split(".")[1] for p in (tmp_path / "state" / "layered" / "receipts" / "audit").glob("*.json"))
    assert audit == ["merge", "pr", "push", "tag"]  # one receipt per step that wrote outside


def test_a_closed_window_refuses_by_gate_and_the_layers_trap_replaces_the_toys(repo, capsys, monkeypatch):
    sm(capsys, "head", "ship", ".", "--live")
    sm(capsys, "sim", "ci", "green", ".")
    monkeypatch.setenv("SM_LAYER__WINDOW_OPEN", "false")
    code, out, _ = sm(capsys, "head", "ship", ".", "--live")
    assert code == 1 and "gate 'change-window' does not hold: the change window is closed" in out
    monkeypatch.setenv("SM_LAYER__WINDOW_OPEN", "true")
    monkeypatch.setenv("SM_LAYER__FROZEN", "true")
    code, out, _ = sm(capsys, "head", "ship", ".", "--live")
    assert code == 1 and "change freeze: 'merge' would write outside this machine" in out
    assert "TRAP     Company rule: merge only inside the change window" in out
    code, out, _ = sm(capsys, "go")
    assert "layer: change freeze is ON" in out


def test_without_the_layer_in_the_list_it_is_not_loaded(repo, capsys):
    (repo / "steuermaschine.toml").write_text('[project]\nname = "layered"\nplugin = "toy"\n')
    _, out, _ = sm(capsys, "--format", "json", "self", "plugins")
    assert [r["name"] for r in json.loads(out)] == ["toy"]
    _code, _, err = sm(capsys, "head", "ship", ".", "--plan")
    assert "window" not in err
