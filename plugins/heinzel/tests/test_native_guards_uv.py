"""`sm guard uv` through the command tree on fakes: a fake uv records its argv, the guard refuses live heinzel tool changes.

Moved from the former PATH shim's tests (bead ppww): the same arguments and assertions. Changed: the fake uv is a scripted
`FakeCommands` rule (the argv after the real uv's path is what the old test read from its `calls` file), the old log file of
allowed live uses is a record in the store, and the unreadable parts of the old tests that exercised another tool
(the digest detector) are not here.
"""

import json
import os

import guards_runtime as g
import pytest

from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, invoke
from steuermaschine_heinzel.native.guards import UvRun
from steuermaschine_heinzel.native.guards_lib import uvguard

REAL = "/fake/bin/uv"


@pytest.fixture
def env(monkeypatch, tmp_path):
    conf = g.register(monkeypatch, {"guards_real_uv": REAL})
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # the live uv tool directories are the home's
    for key in [k for k in os.environ if k.startswith(("UV_", "HZ_"))]:
        monkeypatch.delenv(key)
    commands = FakeCommands()
    commands.on([REAL], Result(0, "", ""))
    runtime = g.runtime_of(tmp_path, commands=commands)
    return monkeypatch, tmp_path, runtime, commands, conf


def run(env_, *args, **extra):
    monkeypatch, _, runtime, commands, _ = env_
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    done = invoke(runtime, ["guard", "uv", "--", *args])
    return done, [" ".join(call[1:]) for call in commands.calls if call[0] == REAL]


@pytest.mark.parametrize("sub", ["install", "upgrade", "uninstall", "reinstall"])
@pytest.mark.parametrize("pkg", ["heinzel==0.9.32", "heinzel", "heinzel-plugin-x", "heinzel[ui]"])
def test_refused_live(env, sub, pkg):
    r, calls = run(env, "tool", sub, "--python", "3.13", pkg)
    assert r.exit_code == 3 and "REFUSED" in r.stderr and calls == []


def test_refused_upgrade_all_and_live_dirs(env):
    home = env[1] / "home"
    (home / ".local/share/uv/tools/heinzel").mkdir(parents=True)
    assert run(env, "tool", "upgrade", "--all")[0].exit_code == 3
    r, calls = run(env, "tool", "install", "heinzel", UV_TOOL_DIR=str(home / ".local/share/uv/tools"), UV_TOOL_BIN_DIR="/x")
    assert r.exit_code == 3 and calls == []


def test_isolated_passes(env):
    r, calls = run(env, "tool", "install", "heinzel==1", UV_TOOL_DIR="/tmp/a", UV_TOOL_BIN_DIR="/tmp/b")
    assert r.exit_code == 0 and calls == ["tool install heinzel==1"]
    assert r.stdout == ""  # uv's own output stays byte-exact: the guard adds no empty line


def test_allow_logs(env):
    store = env[2].providers.store
    r, calls = run(env, "tool", "install", "heinzel", HZ_ALLOW_LIVE_UV="1")
    assert r.exit_code == 0 and len(calls) == 1
    (key,) = store.keys("heinzel.guards.live_uv")
    assert "tool install heinzel" in " ".join(store.load("heinzel.guards.live_uv", key)["args"])


@pytest.mark.parametrize("args", [["tool", "run", "heinzel", "--help"], ["tool", "install", "ruff"], ["pip", "install", "heinzel"], ["run", "x.py"], ["tool", "list"]])
def test_other_passthrough(env, args):
    r, calls = run(env, *args)
    assert r.exit_code == 0 and calls == [" ".join(args)]


@pytest.mark.parametrize("args", [
    ["--directory", "/x", "tool", "install", "heinzel"],
    ["--color", "always", "tool", "upgrade", "--all"],
    ["-q", "tool", "install", "heinzel"],
    ["--project", "/x", "-p", "3.13", "tool", "reinstall", "heinzel"],
    ["tool", "install", "heinzel @ git+https://github.com/o/heinzel@v1"],
    ["tool", "install", "git+https://github.com/o/heinzel@v1"],
    ["tool", "install", "./dist/heinzel-0.9.33-py3-none-any.whl"],
    ["tool", "install", "Heinzel"],
    ["tool", "install", "heinzel[extra]"],
    ["tool", "install", "--from", "heinzel", "x"],
    ["tool", "install", "ruff", "--with", "heinzel-google"],
    ["tool", "upgrade", "-a"],
])
def test_refused_more(env, args):
    live = env[1] / "home/.local/share/uv/tools/heinzel"
    live.mkdir(parents=True)
    r, calls = run(env, *args)
    assert r.exit_code == 3 and calls == []


def test_local_source_dir(env):
    src = env[1] / "src"
    src.mkdir()
    (src / "pyproject.toml").write_text('[project]\nname = "heinzel"\n')
    assert run(env, "tool", "install", str(src))[0].exit_code == 3
    other = env[1] / "other"
    other.mkdir()
    (other / "pyproject.toml").write_text('[project]\nname = "ruffish"\n')
    assert run(env, "tool", "install", str(other))[0].exit_code == 0


def test_upgrade_all_without_live_tool_passes(env):
    assert run(env, "tool", "upgrade", "--all")[0].exit_code == 0


def test_the_refusal_names_no_old_tool_and_the_exit_code_is_uvs(env):
    refused, _ = run(env, "tool", "install", "heinzel")
    assert "hz-" not in refused.stderr.replace("HZ_ALLOW", "") and "isolate" in refused.stderr.lower()
    env[3].on([REAL], Result(5, "", ""))
    done, calls = run(env, "tool", "list")
    assert done.exit_code == 5 and calls == ["tool list"]


def test_in_json_and_the_decision_pieces(env):
    done = invoke(env[2], ["--format", "json", "guard", "uv", "--", "tool", "list"])
    doc = UvRun.model_validate(json.loads(done.stdout))
    assert doc.decision == "pass" and doc.command == [REAL, "tool", "list"] and doc.code == 0
    assert uvguard.outside("/tmp/x/", __import__("pathlib").Path("/h/.local/share/uv/tools"), __import__("pathlib").Path("/h/.local/bin"))
    assert not uvguard.outside("", __import__("pathlib").Path("/a"), __import__("pathlib").Path("/b"))
