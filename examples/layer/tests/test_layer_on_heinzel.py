"""The second plugin on the big one: `plugins = ["heinzel", "layer"]` (and the layer alone), to show that the order rule
holds for a real project plugin, not only for the toy.

    plugins = ["layer"]               the layer alone: the core plus its settings, its gate and its trap
    plugins = ["heinzel", "layer"]    the layer outside the Heinzelmännchen plugin: LATER ENTRIES ARE OUTER LAYERS

Each rule of docs/DESIGN.md section 5 is checked once against heinzel: collecting hooks base first with the later key
winning, provider extras from the outermost plugin, the wrapper outermost around a heinzel step, native paths
replaced by the later plugin, and every unlisted plugin blocked.
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from steuermaschine import API_VERSION, app
from steuermaschine.commands import lim
from steuermaschine.config import Project
from steuermaschine.engine import Refuse, Step
from steuermaschine.hookspecs import hookimpl
from steuermaschine.plugins import load
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, fake_providers, invoke
from steuermaschine_heinzel.plugin import plugin as heinzel
from steuermaschine_layer.plugin import plugin as layer


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], check=True, capture_output=True)


@pytest.fixture
def project(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / ".git" / "info" / "exclude").write_text("steuermaschine.toml\n")
    monkeypatch.chdir(r)
    monkeypatch.setenv("STEUERMASCHINE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-user-config"))
    for var in ("STEUERMASCHINE_PLUGIN", "STEUERMASCHINE_PROJECT", "SM_FORMAT", "SM_LAYER__FROZEN", "SM_LAYER__WINDOW_OPEN"):
        monkeypatch.delenv(var, raising=False)

    def use(plugins: str, tables: str = ""):
        (r / "steuermaschine.toml").write_text(f'[project]\nname = "on-heinzel"\nplugins = {plugins}\n{tables}')
        return r

    return use


def sm(capsys, *args):
    with pytest.raises(SystemExit) as stop:
        app.main(list(args))
    out = capsys.readouterr()
    return stop.value.code, out.out, out.err


def stack_rt(tmp_path, *, outer=None, tables=None, commands=None):
    objects = {"heinzel": heinzel, "layer": layer, **({outer.name: outer} if outer else {})}
    stack = load(list(objects), objects)
    settings, _ = stack.validate(tables or {}, tmp_path)
    project = Project(name="t", plugin="heinzel", root=tmp_path, plugins=list(objects), allow_unreviewed=True)
    return app.Runtime(project, stack, settings, _providers=stack.providers(settings, fake_providers(commands=commands or FakeCommands())))


class Outer:
    """A third, in-process layer that hands the core's `sm lim` a directory of its own."""

    name = "outer"

    def __init__(self, directory):
        self.directory = directory

    @hookimpl
    def sm_api_version(self):
        return API_VERSION

    @hookimpl
    def sm_provider_extras(self):
        return {lim.DIRECTORY: str(self.directory)}


# ----------------------------------------------------------------------------- through the real entry point
@pytest.mark.parametrize(("plugins", "order"), [('["layer"]', ["layer"]), ('["heinzel", "layer"]', ["heinzel", "layer"])])
def test_the_layer_loads_alone_and_on_heinzel_in_the_listed_order(project, capsys, plugins, order):
    project(plugins)
    code, out, err = sm(capsys, "--format", "json", "self", "plugins")
    assert code == 0, err
    assert [row["name"] for row in json.loads(out) if row["name"] != "core"] == order
    code, out, _ = sm(capsys, "--format", "json", "self", "lint")
    assert code == 0 and json.loads(out) == []  # the help contract holds for both stacks


def test_heinzel_keeps_its_commands_and_the_core_its_own_under_the_layer(project, capsys):
    project('["heinzel", "layer"]')
    code, out, _ = sm(capsys, "--help")
    assert code == 0 and all(f"  {word}" in out for word in ("beads", "queue", "release", "gate", "lim", "doc", "self"))
    project('["layer"]')
    code, out, _ = sm(capsys, "--help")
    assert code == 0 and "  beads" not in out and "  gate" in out  # heinzel is blocked when it is not listed


def test_both_settings_tables_are_validated_side_by_side(project, capsys):
    project('["heinzel", "layer"]', '[layer]\nfrozen = true\n[heinzel]\nlock_heavy_slots = 2\n')
    code, out, err = sm(capsys, "--format", "json", "self", "config")
    assert code == 0, err
    text = json.dumps(json.loads(out))
    assert "frozen" in text and "lock_heavy_slots" in text
    project('["heinzel", "layer"]', '[layer]\nnot_a_setting = 1\n')
    assert sm(capsys, "--format", "json", "self", "config")[0] == 0  # self config always answers; the problem is named
    assert sm(capsys, "lim", "--", "true")[0] != 0  # an unknown key of the outer layer stops every other command


# ----------------------------------------------------------------------------- the order rule against heinzel, in process
def test_the_layers_trap_and_gate_join_heinzels_and_heinzels_pipelines_keep_their_steps(tmp_path):
    rt = stack_rt(tmp_path)
    assert rt.plugin.traps(rt.settings)["head.ship.merge"].startswith("Company rule")
    assert "change-window" in {g.name for g in rt.plugin.gates(rt.settings)}
    admit = next(p for p in rt.plugin.pipelines(rt.settings) if (p.subject, p.verb) == ("head", "admit"))
    assert "window" not in [s.name for s in admit.steps]  # the layer inserts before a `merge` step; heinzel's admit has none


def test_the_layers_wrapper_runs_outside_heinzel_and_vetoes_an_external_step(tmp_path):
    rt = stack_rt(tmp_path, tables={"layer": {"frozen": True}})
    ran = []
    step = Step("publish", lambda ctx: ran.append("publish") or "published", external=True)
    ctx = SimpleNamespace(settings=rt.settings, providers=rt.providers, subject=SimpleNamespace(key="k" * 12), author="t")
    with pytest.raises(Refuse, match="change freeze"):
        rt.plugin.run_step(step, ctx)
    assert ran == []  # neither heinzel nor the core's innermost runner saw the step
    thawed = stack_rt(tmp_path, tables={"layer": {"frozen": False}})
    ctx.settings = thawed.settings
    ctx.providers = thawed.providers
    assert thawed.plugin.run_step(step, ctx) == "published" and ran == ["publish"]


def test_heinzels_extras_reach_the_core_command_and_an_outer_layer_replaces_them(tmp_path):
    commands = FakeCommands()
    commands.on(["make"], Result(0, "".join(f"row {i}\n" for i in range(80)), ""))
    tables = {"heinzel": {"guards_lim_dir": str(tmp_path / "heinzel-logs")}}
    inner = stack_rt(tmp_path, tables=tables, commands=commands)
    found = lim.Limited.model_validate(json.loads(invoke(inner, ["--format", "json", "lim", "-n", "1", "-t", "1", "--", "make"]).stdout))
    assert Path(found.log).parent == tmp_path / "heinzel-logs"
    outer = stack_rt(tmp_path, outer=Outer(tmp_path / "outer-logs"), tables=tables, commands=commands)
    found = lim.Limited.model_validate(json.loads(invoke(outer, ["--format", "json", "lim", "-n", "1", "-t", "1", "--", "make"]).stdout))
    assert Path(found.log).parent == tmp_path / "outer-logs"  # the outermost plugin's key wins
