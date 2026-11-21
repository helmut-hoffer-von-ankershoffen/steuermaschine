"""The toy project end to end through the command line, on a real git repository and the simulated forge.

It proves the core is reusable: another plugin, another pipeline (push -> PR -> CI -> merge -> tag), no review stop,
and state that never mixes with another project on the same machine.
"""

import json
import subprocess

import pytest

from steuermaschine import app
from steuermaschine.testing import assert_conformant
from steuermaschine_toy.plugin import plugin


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
    (r / "steuermaschine.toml").write_text('[project]\nname = "toy-demo"\nplugin = "toy"\n\n[lanes]\ndefault = 1\n')
    (r / ".git" / "info" / "exclude").write_text("steuermaschine.toml\n.sim/\n")
    monkeypatch.chdir(r)
    monkeypatch.setenv("STEUERMASCHINE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-user-config"))
    for var in ("STEUERMASCHINE_PLUGIN", "STEUERMASCHINE_PROJECT", "SM_FORMAT"):
        monkeypatch.delenv(var, raising=False)
    return r


def sm(capsys, *args):
    with pytest.raises(SystemExit) as stop:
        app.main(list(args))
    out = capsys.readouterr()
    return stop.value.code, out.out, out.err


def test_conformance():
    assert_conformant(plugin, {"world": "/nonexistent/world.json"})


def test_from_a_commit_to_a_tag(repo, capsys):
    code, out, _ = sm(capsys)  # bare sm is `sm go`
    assert code == 0 and "PHASE    idle" in out

    code, out, err = sm(capsys, "head", "ship", ".")
    assert code == 0 and "DRY RUN STOP" in err and "git push origin HEAD:refs/heads/feat/hello" in err
    assert out.splitlines()[1] == f"NEXT     sm head ship {repo.resolve()} --live"
    assert not (repo / ".sim" / "world.json").exists()  # a dry run wrote nothing outside the state directory

    code, out, _ = sm(capsys, "head", "ship", ".", "--live")
    assert code == 75 and "WAITING  CI on" in out and "TRAP     A red CI is never rerun here" in out
    world = json.loads((repo / ".sim" / "world.json").read_text())
    assert world["prs"][0]["state"] == "open" and world["tags"] == {}

    code, out, _ = sm(capsys, "go")
    assert code == 0 and "ci: CI is running" in out and "NEXT     nothing runnable now" in out

    assert sm(capsys, "sim", "ci", "green", ".")[0] == 0
    code, out, err = sm(capsys, "head", "ship", ".", "--live")
    assert code == 0 and "head.ship finished" in out and "[1/5 push] already done" in err
    world = json.loads((repo / ".sim" / "world.json").read_text())
    assert world["prs"][0]["state"] == "merged" and len(world["tags"]) == 1

    code, out, _ = sm(capsys, "--format", "json", "head", "status", "--all")
    assert json.loads(out)[0]["status"] == "finished"


def test_red_ci_refuses_and_a_second_head_waits_for_the_lane(repo, capsys):
    sm(capsys, "head", "ship", ".", "--live")
    sm(capsys, "sim", "ci", "red", ".")
    code, out, _ = sm(capsys, "head", "ship", ".", "--live")
    assert code == 1 and "CI is red: unit tests" in out
    (repo / "more.txt").write_text("x\n")
    git(repo, "add", "more.txt")
    git(repo, "commit", "-q", "-m", "more")
    code, out, _ = sm(capsys, "head", "ship", ".")
    assert code == 1 and "lane 'default' is full" in out


def test_two_projects_on_one_machine_do_not_share_state(repo, tmp_path, capsys):
    sm(capsys, "head", "ship", ".", "--live")
    assert (tmp_path / "state" / "toy-demo" / "journals").is_dir()  # the run lives in its subject journal
    (repo / "steuermaschine.toml").write_text('[project]\nname = "other"\nplugin = "toy"\n')
    code, out, _ = sm(capsys, "go")
    assert code == 0 and "PHASE    idle" in out and "project other" in out


def test_a_typo_in_the_plugin_table_stops_everything_but_self(repo, capsys):
    (repo / "steuermaschine.toml").write_text('[project]\nname = "toy-demo"\nplugin = "toy"\n\n[toy]\nwrold = "x"\n')
    code, _, err = sm(capsys, "go")
    assert code == 70 and "unknown setting [toy] wrold" in err
    assert sm(capsys, "self", "config")[0] == 0
