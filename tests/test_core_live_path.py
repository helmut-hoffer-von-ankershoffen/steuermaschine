"""Regression tests for the night `sm` lost its plugin on the live path twice.

1. A user config is present and no project file is up the tree: the plugin must load (from any directory).
2. A plugin that cannot load never degrades to core commands silently: non-zero exit and a loud line, except `sm self`.
"""

import pytest

from steuermaschine import API_VERSION, INTERNAL, app, plugins
from steuermaschine.hookspecs import hookimpl


class Demo:
    name = "demo"

    @hookimpl
    def sm_api_version(self):
        return API_VERSION

    @hookimpl
    def sm_commands(self, runtime):
        import typer

        sub = typer.Typer()

        @sub.command("hello")
        def hello() -> None:
            typer.echo("hello from demo")

        @sub.callback()
        def root() -> None:
            pass

        return sub


@pytest.fixture
def user_only(tmp_path, monkeypatch):
    """HOME with a user config naming plugin demo; the current directory has no steuermaschine.toml above it."""
    plugins.register(Demo())
    conf = tmp_path / "xdg" / "steuermaschine"
    conf.mkdir(parents=True)
    (conf / "config.toml").write_text('[project]\nname = "p"\nplugin = "demo"\n')
    work = tmp_path / "some" / "other" / "repo"
    work.mkdir(parents=True)
    monkeypatch.chdir(work)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("STEUERMASCHINE_HOME", str(tmp_path / "state"))
    for var in ("STEUERMASCHINE_PLUGIN", "STEUERMASCHINE_PROJECT", "SM_FORMAT", "SM_PROJECT__PLUGIN"):
        monkeypatch.delenv(var, raising=False)
    return conf / "config.toml"


def run(args, capsys):
    with pytest.raises(SystemExit) as stop:
        app.main(args)
    out = capsys.readouterr()
    return stop.value.code, out.out, out.err


def test_user_config_alone_selects_the_project_and_loads_the_plugin(user_only, capsys):
    rt = app.runtime()
    assert rt.project is not None and rt.project.name == "p" and rt.plugin is not None and rt.plugin.names == ["demo"]
    assert rt.project.source_of("project", "plugin").startswith("user file")
    code, out, _ = run(["hello"], capsys)
    assert code == 0 and "hello from demo" in out


def test_a_project_file_up_the_tree_wins_over_the_user_config(user_only, tmp_path, capsys):
    (tmp_path / "some" / "steuermaschine.toml").write_text('[project]\nname = "near"\nplugin = "demo"\n')
    rt = app.runtime()
    assert rt.project.name == "near" and rt.project.source_of("project", "name").startswith("project file")


def test_a_plugin_that_cannot_load_is_loud_and_nonzero_for_every_command_but_self(user_only, capsys):
    user_only.write_text('[project]\nname = "p"\nplugin = "not-installed"\n')
    for args in (["hello"], ["go"], [], ["head", "status"], ["--help"]):
        code, out, err = run(args, capsys)
        assert code == INTERNAL and "NOT loaded" in err and "nothing was run" in err and out == "", args
    code, out, _ = run(["self", "config"], capsys)
    assert code == 0 and "NOT loaded" in out


def test_an_unknown_plugin_setting_is_an_error_not_a_silent_core(user_only, capsys):
    user_only.write_text('[project]\nname = "p"\nplugin = "demo"\n\n[demo]\nold_switch = true\n')
    code, _, err = run(["hello"], capsys)
    assert code == INTERNAL and "NOT loaded" in err and "old_switch" in err


def test_a_broken_settings_file_is_loud_and_nonzero(user_only, capsys):
    user_only.write_text('[project]\nname = "p"\nplugin = "demo"\n\n[alarms]\nstall_minutes = "soon"\n')
    code, _, err = run(["hello"], capsys)
    assert code == INTERNAL and "NOT valid" in err


def test_core_alone_only_on_purpose(user_only, monkeypatch, capsys):
    monkeypatch.setenv("STEUERMASCHINE_PLUGIN", "none")
    code, out, err = run(["--help"], capsys)
    assert code == 0 and "hello" not in out and "NOT loaded" not in err


def test_no_config_anywhere_says_so(user_only, capsys):
    user_only.unlink()
    code, out, _ = run(["go"], capsys)
    assert code == 2 and "no project" in out
