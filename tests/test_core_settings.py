"""Typed settings only: the core reads the process environment in `steuermaschine.config` alone (SM_* settings
through pydantic-settings, the other variables through the typed `Environment`), and `sm self config` shows every
value with its source.

Native plugin modules may hand the environment on to a child process (`dict(os.environ)`, `{**os.environ, ...}`):
that is not a settings read. Any other read is a finding unless KNOWN lists it with the review unit that holds it;
an entry goes when its unit is next reviewed. Moved libraries (`native/*_lib/`) keep their text by rule and are
configured from the settings by their native module; they are counted, not scanned (see bin/debt.py)."""

import ast
import json
from pathlib import Path

import steuermaschine
from steuermaschine import config
from steuermaschine.testing import invoke

CORE = Path(steuermaschine.__file__).parent
NATIVE = Path(__file__).resolve().parent.parent / "plugins" / "heinzel" / "src" / "steuermaschine_heinzel" / "native"
KNOWN: set[tuple[str, str]] = set()


def reads(path: Path) -> list[tuple[str, str]]:
    """(file, what) for each read of os.environ / os.getenv that is not a plain hand-on to a child process."""
    tree = ast.parse(path.read_text())
    passthrough = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "dict" and node.args:
            passthrough.add(id(node.args[0]))
        if isinstance(node, ast.Dict):
            passthrough |= {id(v) for k, v in zip(node.keys, node.values, strict=True) if k is None}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv") and getattr(node.value, "id", "") == "os" and id(node) not in passthrough:
            found.append(node)
    out = []
    for node in found:
        parent = next((p for p in ast.walk(tree) for c in ast.iter_child_nodes(p) if c is node), None)
        call = next((p for p in ast.walk(tree) if isinstance(p, (ast.Call, ast.Subscript)) and (getattr(p, "func", None) is parent or getattr(p, "value", None) is node)), None)
        arg = (call.args[0] if isinstance(call, ast.Call) and call.args else getattr(call, "slice", None)) if call is not None else None
        what = arg.value if isinstance(arg, ast.Constant) else getattr(arg, "id", "?")
        out.append((path.name, str(what)))
    return out


def test_the_core_reads_the_environment_only_in_its_settings_layer():
    allowed = {CORE / "config.py", CORE / "testing" / "fakes.py"}  # fakes.py: invoke() restores SM_FORMAT around a test call
    found = [hit for path in sorted(CORE.rglob("*.py")) if path not in allowed for hit in reads(path)]
    assert not found, found


def test_native_modules_read_no_environment_beyond_the_known_list():
    found = {hit for path in sorted(NATIVE.glob("*.py")) for hit in reads(path)}
    assert found <= KNOWN, sorted(found - KNOWN)
    assert found >= KNOWN, sorted(KNOWN - found)  # a fixed read leaves the list


PLUGIN = NATIVE.parent
# The plugin files that may touch os.environ, each a program of its own or a pinned copy:
STANDALONE = {
    "main_checkpoint_reader.py",  # run as `python -I FILE`: clears the git variables of its own process (a write)
    "collect_cases.py",  # run as `python -I FILE` without the package: hands its environment on to git and pytest
    "release_candidate.py",  # the reviewed recorder, pinned byte for byte (APPROVED_SOURCES): reads the Actions run identity
}


def test_the_vendored_recorder_reads_the_environment_only_in_its_actions_entry_points():
    import inspect

    from steuermaschine_heinzel.native.queue_lib import release_candidate

    for name, fn in inspect.getmembers(release_candidate, inspect.isfunction):
        if fn.__module__ == release_candidate.__name__ and name not in {"context", "record", "select_release"}:
            assert "os.environ" not in inspect.getsource(fn), name


def test_the_plugin_reads_the_environment_only_through_typed_models():
    """No os.environ / os.getenv anywhere in the plugin: settings come from the settings layer, process context from
    the typed models of steuermaschine_heinzel/environment.py, a child's environment from config.child_environment."""
    found = []
    for path in sorted(PLUGIN.rglob("*.py")):
        if path.name in STANDALONE:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv") and getattr(node.value, "id", "") == "os":
                found.append(f"{path.relative_to(PLUGIN)}:{node.lineno}")
    assert not found, found


def test_every_old_variable_of_a_setting_is_unique_and_typed():
    from steuermaschine.plugins import old_variables
    from steuermaschine_heinzel.plugin import HeinzelSettings

    names = old_variables(HeinzelSettings)
    assert len(set(names.values())) == len(names), names
    assert {"HZ_QUEUE_STATE", "HZ_MAIN_BATCH_PUBLISH", "HZ_TRAIN_STATE_DIR", "HZ_ALARM_NOTIFY"} <= set(names.values())
    assert all(HeinzelSettings.model_fields[field].description for field in names)


def _machine_paths(value) -> list[str]:
    """The strings in a default that name a real place of a machine: `~...` or an absolute path."""
    if isinstance(value, (list, tuple, set)):
        return [found for item in value for found in _machine_paths(item)]
    text = str(value) if isinstance(value, (str, Path)) else ""
    return [text] if text.startswith(("~", "/")) else []


def test_no_setting_defaults_to_a_real_path_of_a_machine():
    """AGENTS.md: a per-machine value defaults to None (the command refuses and names the key) or to nothing; its
    value lives in the machine's config (bin/promote carries it from migrate-values.toml). Every settings model."""
    from steuermaschine.config import AlarmSettings, ProjectSettings
    from steuermaschine.github.settings import GitHubSettings, SonarSettings
    from steuermaschine_heinzel.plugin import HeinzelSettings
    from steuermaschine_layer.plugin import LayerSettings
    from steuermaschine_toy.plugin import ToySettings

    found = {f"{model.__name__}.{name}": paths
             for model in (ProjectSettings, AlarmSettings, GitHubSettings, SonarSettings, HeinzelSettings, ToySettings, LayerSettings)
             for name, field in model.model_fields.items()
             if (paths := _machine_paths(field.get_default(call_default_factory=True)))}
    assert found == {}


def test_environment_is_typed_and_shown_with_its_source(monkeypatch, tmp_path):
    monkeypatch.setenv("STEUERMASCHINE_HOME", str(tmp_path))
    monkeypatch.setenv("SM_GITHUB__REPO", "o/n")
    assert config.state_home() == tmp_path
    (tmp_path / "steuermaschine.toml").write_text('[project]\nname = "p"\nplugins = ["toy"]\n')
    from steuermaschine.app import runtime

    rt = runtime(str(tmp_path))
    data = json.loads(invoke(rt, ["--format", "json", "self", "config"]).stdout)
    assert data["environment"]["STEUERMASCHINE_HOME"] == {"value": str(tmp_path), "source": "environment STEUERMASCHINE_HOME"}
    assert data["core"]["github.repo"] == {"value": "o/n", "source": "environment SM_GITHUB__REPO"}
    assert data["core"]["project.plugin"]["source"].startswith("project file")
    assert data["core"]["sonar.host"]["source"] == "default"
    assert all("value" in v and "source" in v for v in data["settings"].values()) and "toy.world" in data["settings"]


def test_a_secret_named_by_a_setting_is_read_only_when_named(monkeypatch):
    monkeypatch.setenv("SOME_TOKEN", "t")
    assert (config.secret("SOME_TOKEN"), config.secret("")) == ("t", "")


def test_an_old_variable_sets_a_plugin_setting_only_when_nothing_else_does(monkeypatch, tmp_path):
    from pydantic import BaseModel, ConfigDict, Field

    from steuermaschine import API_VERSION
    from steuermaschine.hookspecs import hookimpl
    from steuermaschine.plugins import old_variable, old_variables, stack_of

    class OldSettings(BaseModel):
        model_config = ConfigDict(extra="forbid")
        every: int = Field(0, description="Minutes.", json_schema_extra=old_variable("OLD_EVERY"))
        flag: bool = Field(False, description="A switch.", json_schema_extra=old_variable("OLD_FLAG"))
        exact: bool = Field(False, description="A switch the old program turned on with 1 only.", json_schema_extra=old_variable("OLD_EXACT", on="1"))

    class Old:
        name = "old"

        @hookimpl
        def sm_api_version(self):
            return API_VERSION

        @hookimpl
        def sm_settings(self):
            return OldSettings

    assert old_variables(OldSettings) == {"every": "OLD_EVERY", "flag": "OLD_FLAG", "exact": "OLD_EXACT"}
    stack = stack_of(Old())
    for name in ("OLD_EVERY", "OLD_FLAG", "OLD_EXACT", "SM_OLD__EVERY", "SM_OLD__EXACT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OLD_FLAG", "")  # empty is unset
    settings, sources = stack.validate({}, tmp_path)
    assert settings.of("old") == {"every": 0, "flag": False, "exact": False} and sources["old.every"] == "default"
    monkeypatch.setenv("OLD_EVERY", "7")
    monkeypatch.setenv("OLD_FLAG", "1")
    settings, sources = stack.validate({}, tmp_path)
    assert settings.of("old") == {"every": 7, "flag": True, "exact": False}
    for value, expected in (("true", False), ("0", False), ("1", True)):  # the old switch was on for exactly "1"
        monkeypatch.setenv("OLD_EXACT", value)
        assert stack.validate({}, tmp_path)[0].of("old")["exact"] is expected
    monkeypatch.setenv("SM_OLD__EXACT", "true")  # the new variable is a plain bool
    assert stack.validate({}, tmp_path)[0].of("old")["exact"] is True
    monkeypatch.delenv("SM_OLD__EXACT")
    monkeypatch.delenv("OLD_EXACT")
    assert sources["old.every"] == "environment OLD_EVERY (old name of SM_OLD__EVERY)"
    settings, sources = stack.validate({"old": {"every": 3}}, tmp_path)  # a file wins
    assert settings.of("old")["every"] == 3 and sources["old.every"] == "file"
    monkeypatch.setenv("SM_OLD__EVERY", "5")  # and so does the new variable
    settings, sources = stack.validate({}, tmp_path)
    assert settings.of("old")["every"] == 5 and sources["old.every"] == "environment SM_OLD__EVERY"


def test_a_child_environment_drops_and_overrides_names(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "x")
    monkeypatch.setenv("GIT_WORK_TREE", "y")
    monkeypatch.setenv("KEEP_ME", "k")
    monkeypatch.setenv("PATH", "/bin")
    env = config.child_environment(drop_prefixes=("GIT_",), EXTRA="e")
    assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env and env["KEEP_ME"] == "k" and env["EXTRA"] == "e"
    env = config.child_environment(drop=("GIT_DIR",))
    assert "GIT_DIR" not in env and env["GIT_WORK_TREE"] == "y"
    assert config.search_path() == "/bin" and config.path_with("/first") == "/first" + __import__("os").pathsep + "/bin"
