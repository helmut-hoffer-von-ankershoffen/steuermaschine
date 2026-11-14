"""Settings: one typed model, layered sources, and a record of where each value came from.

Precedence, highest first:
    1. command line (--format, --project)
    2. environment: SM_FORMAT, SM_PROJECT__PLUGIN, SM_ALARMS__STALL_MINUTES, SM_<PLUGIN>__<FIELD> ...
       (STEUERMASCHINE_PLUGIN is an alias of SM_PROJECT__PLUGIN for one release; "none" loads no plugin)
    3. the project's steuermaschine.toml: the nearest one from the current directory up (or --project DIR,
       or $STEUERMASCHINE_PROJECT)
    4. the user's ~/.config/steuermaschine/config.toml
    5. the old environment variable a plugin setting names as its compatibility source (plugins.old_variable)
    6. defaults

A file looks like this:

    [project]
    name = "toy"            # names the state directory; two projects on one machine never share state
    plugin = "toy"          # entry point name in the group steuermaschine.plugins; or, for layers:
    # plugins = ["base", "company"]   base first, later entries are outer layers
    base = "origin/main"

    [lanes]                 # how many unfinished heads a lane may hold
    default = 2

    [alarms]
    stall_minutes = 15

    [toy]                   # the plugin's own table, validated by the plugin's settings model
    world = ".sim/world.json"

Unknown keys are an error. The process environment the core reads besides SM_* (launcher pointers, XDG directories)
is one typed model too, `Environment`; this module is the only place in the core that reads os.environ (a test
enforces it). State lives in $STEUERMASCHINE_HOME, else $XDG_STATE_HOME/steuermaschine, else
~/.local/state/steuermaschine, in one directory per project name.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, TomlConfigSettingsSource

FILE = "steuermaschine.toml"
ENV_PREFIX = "SM_"


class ConfigError(Exception):
    pass


class ProjectSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = ""
    plugin: str = ""  # shorthand for plugins = [plugin]
    plugins: list[str] = []  # base first; later entries are outer layers
    base: str = "origin/main"


class AlarmSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stall_minutes: float = 15.0


class Settings(BaseSettings):
    """The core settings. A plugin's table is an extra key here and is validated by the plugin's own model."""

    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX, env_nested_delimiter="__", extra="allow")
    format: Literal["text", "json"] = "text"
    project: ProjectSettings = ProjectSettings()
    lanes: dict[str, int] = {}
    alarms: AlarmSettings = AlarmSettings()


def _var(name: str, description: str) -> Any:
    return Field(None, validation_alias=AliasChoices(name), description=description)


class Environment(BaseSettings):
    """The environment variables the core reads that are not SM_* settings: where state and config live, what the
    launcher set. Read fresh on every call of `environment()`; `sm self config` shows each with its source."""

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=False)
    steuermaschine_home: str | None = _var("STEUERMASCHINE_HOME", "State directory of all projects; wins over XDG_STATE_HOME.")
    xdg_state_home: str | None = _var("XDG_STATE_HOME", "Base of the state directory (default ~/.local/state).")
    steuermaschine_user_config: str | None = _var("STEUERMASCHINE_USER_CONFIG", "The user settings file; set by a stable install's launcher.")
    xdg_config_home: str | None = _var("XDG_CONFIG_HOME", "Base of the user settings file (default ~/.config).")
    steuermaschine_project: str | None = _var("STEUERMASCHINE_PROJECT", "Where to look for the project file instead of the current directory.")
    steuermaschine_plugin: str | None = _var("STEUERMASCHINE_PLUGIN", "Alias of SM_PROJECT__PLUGIN, for one release.")
    steuermaschine_release: str | None = _var("STEUERMASCHINE_RELEASE", "The stable release pointer the launcher started; a daemon loop ends when it moves.")
    steuermaschine_pinned: str | None = _var("STEUERMASCHINE_PINNED", "The release a stable install's launcher pinned this process to.")
    steuermaschine_agent: str | None = _var("STEUERMASCHINE_AGENT", "Default of --as: who drives a pipeline run.")
    sm_caller: str | None = _var("SM_CALLER", "Who runs the command (a coordinator or an agent id), written to the invocation log; unset: unknown.")
    pytest_current_test: str | None = _var("PYTEST_CURRENT_TEST", "Set by pytest: the real process runner then refuses pushes and GitHub writes.")
    sm_test_allow_local_push: str | None = _var("SM_TEST_ALLOW_LOCAL_PUSH", "Tests only: the one command (first two words) the runner may run anyway.")


def environment() -> Environment:
    return Environment()


def environment_sources() -> dict[str, dict[str, Any]]:
    """Each environment variable of `Environment`: its value and its source, for `sm self config`."""
    env = environment()
    out = {}
    for name in Environment.model_fields:
        value = getattr(env, name)
        out[name.upper()] = {"value": value, "source": f"environment {name.upper()}" if value is not None else "default"}
    return out


def prefixed(prefix: str) -> dict[str, str]:
    """SM_<TABLE>__<FIELD> variables of one table as {field: value}: the environment layer of a plugin or core table."""
    return {key[len(prefix):].lower(): value for key, value in os.environ.items() if key.startswith(prefix)}


def child_environment(*, drop: tuple[str, ...] = (), drop_prefixes: tuple[str, ...] = (), **overrides: str) -> dict[str, str]:
    """The environment a child process gets: this process's without the names in `drop` and those that start with one
    of `drop_prefixes`, plus `overrides`. A hand-on, not a settings read."""
    kept = {key: value for key, value in os.environ.items() if key not in drop and not (drop_prefixes and key.startswith(drop_prefixes))}
    return {**kept, **overrides}


def search_path() -> str:
    """This process's $PATH (empty when unset), for a child that gets a minimal environment."""
    return os.environ.get("PATH", "")


def path_with(first: str) -> str:
    """$PATH with `first` in front, for a child that must find a configured binary first."""
    return f"{first}{os.pathsep}{search_path()}"


def variable(name: str) -> str | None:
    """The value of the old environment variable a plugin setting names as its compatibility source
    (`plugins.old_variable`); None when unset or empty. Read by the settings layer only."""
    return os.environ.get(name) or None


def secret(variable: str) -> str:
    """The value of an environment variable a setting names (a token); empty when unset. Never printed."""
    return os.environ.get(variable, "") if variable else ""


@dataclass
class Project:
    name: str
    plugin: str
    root: Path  # the directory of the file that named the project; relative paths in settings start here
    base: str = "origin/main"
    lanes: dict[str, Any] = field(default_factory=dict)
    stall_minutes: float = 15.0
    raw_settings: dict[str, Any] = field(default_factory=dict)  # plugin name -> its table, file layers merged, not yet validated
    plugins: list[Any] = field(default_factory=list)  # every loaded plugin, base first
    # Tests only, and only as a constructor argument: no setting, environment variable or file can reach it
    # (config.load never passes it). It lets a test run a native write command without a review verdict.
    allow_unreviewed: bool = False
    format: str = "text"
    layers: list[Any] = field(default_factory=list)  # [(source name, dict)] highest first, for `sm self config`

    @property
    def home(self) -> Path:
        return state_home() / self.name

    def source_of(self, *path: str) -> str:
        for name, data in self.layers:
            cur = data
            for key in path:
                cur = cur.get(key) if isinstance(cur, dict) else None
                if cur is None:
                    break
            else:
                return str(name)
        return "default"


def state_home() -> Path:
    env = environment()
    if env.steuermaschine_home:
        return Path(env.steuermaschine_home)
    return Path(env.xdg_state_home or str(Path.home() / ".local" / "state")) / "steuermaschine"


def user_config() -> Path:
    """The user's file. A stable install keeps it beside the release ($STEUERMASCHINE_USER_CONFIG, set by its launcher),
    so code and config switch in one step and a rollback restores both."""
    env = environment()
    if env.steuermaschine_user_config:
        return Path(env.steuermaschine_user_config)
    return Path(env.xdg_config_home or str(Path.home() / ".config")) / "steuermaschine" / "config.toml"


def find(start: str | None = None) -> Path | None:
    """The project file: from `start` (or $STEUERMASCHINE_PROJECT, or the current directory) upwards."""
    if start is None and (env := environment().steuermaschine_project):
        start = env
    here = Path(start or os.getcwd()).expanduser().resolve()
    if here.is_file():
        return here
    for d in (here, *here.parents):
        if (d / FILE).is_file():
            return d / FILE
    return None


def _toml(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None


def _env_layer() -> dict[str, Any]:
    """Environment variables as a nested dict, for the record of sources (the values themselves come from pydantic-settings)."""
    out: dict[str, Any] = {}
    for key, value in os.environ.items():
        if key.startswith(ENV_PREFIX):
            cur = out
            parts = key[len(ENV_PREFIX):].lower().split("__")
            for part in parts[:-1]:
                cur = cur.setdefault(part, {})
            if isinstance(cur, dict):
                cur[parts[-1]] = value
    if alias := environment().steuermaschine_plugin:
        out.setdefault("project", {}).setdefault("plugin", alias)
    return out


def load(start: str | None = None, cli: dict[str, Any] | None = None, known_plugins: set[str] | None = None) -> Project | None:
    """Resolve all layers. Returns None when neither a project file nor a user file names a project."""
    project_file, user_file = find(start), user_config()
    project_data, user_data = _toml(project_file), _toml(user_file if user_file.is_file() else None)
    cli = {k: v for k, v in (cli or {}).items() if v is not None}

    class Layered(Settings):
        @classmethod
        def settings_customise_sources(cls, settings_cls: type[BaseSettings], init_settings: PydanticBaseSettingsSource, env_settings: PydanticBaseSettingsSource,
                                       dotenv_settings: PydanticBaseSettingsSource, file_secret_settings: PydanticBaseSettingsSource) -> tuple[PydanticBaseSettingsSource, ...]:
            files = [TomlConfigSettingsSource(settings_cls, toml_file=f) for f in (project_file, user_file) if f is not None and f.is_file()]
            return (init_settings, env_settings, *files)

    try:
        s = Layered(**cli)
    except ValidationError as exc:
        where = project_file or user_file
        raise ConfigError(f"settings are not valid ({where}): " + "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())) from None
    if alias := environment().steuermaschine_plugin:
        s.project.plugin, s.project.plugins = os.environ.get(f"{ENV_PREFIX}PROJECT__PLUGIN") or alias, []
    if s.project.plugin and s.project.plugins and s.project.plugin != s.project.plugins[0]:
        raise ConfigError(f"[project] sets both plugin = \"{s.project.plugin}\" and plugins = {s.project.plugins}: use plugins alone (base first)")
    names = s.project.plugins or ([s.project.plugin] if s.project.plugin else [])
    core_only = names in (["none"], ["core"])
    names = [] if core_only else names
    if not s.project.name or not (names or core_only):
        if not project_data and not user_data:
            return None
        raise ConfigError(f"{project_file or user_file}: [project] name and plugin are required")
    extras = s.model_extra or {}
    allowed = (known_plugins or set()) | set(names) | {"github", "sonar"}  # core tables read by steuermaschine.github.cli
    if unknown := sorted(k for k in extras if k not in allowed):
        raise ConfigError(f"unknown table(s) {', '.join('[' + k + ']' for k in unknown)} in {project_file or user_file}; "
                          f"core tables are [project], [lanes], [alarms], [github], [sonar]; a plugin's table is named after the plugin ({', '.join('[' + n + ']' for n in names) or 'none loaded'})")
    named = project_file if project_data.get("project") else user_file
    root = named.parent if named is not None else Path.cwd()
    layers = [("command line", cli), ("environment", _env_layer()), (f"project file {project_file}", project_data), (f"user file {user_file}", user_data)]
    tables = {n: extras[n] for n in names if isinstance(extras.get(n), dict)}
    return Project(name=s.project.name, plugin="+".join(names) or "none", root=root, base=s.project.base, lanes=dict(s.lanes),
                   stall_minutes=s.alarms.stall_minutes, raw_settings=tables, format=s.format, layers=layers, plugins=names)
