"""Loading plugins and asking them: a thin layer over pluggy.

Plugins are found through Python entry points in the group `steuermaschine.plugins` and loaded only when the
project's settings list them, in that order (see hookspecs.py for the layering rule). `Stack` is what the rest of
the core talks to: it calls the hooks and merges the results by the documented rules.
"""

from __future__ import annotations

import inspect
import os
from dataclasses import dataclass, replace
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pluggy
from pydantic import ValidationError

from . import API_VERSION
from . import config as config_env
from .hookspecs import PROJECT, PROVIDER_HOOKS, Core, Specs
from .protocols import Providers

GROUP = "steuermaschine.plugins"


@dataclass
class GoItem:
    """One thing `go` may put on the NEXT line. Lower rank wins."""

    rank: int
    phase: str
    next: str = ""
    waiting_on: str = ""
    trap: str = ""


@dataclass
class Alarm:
    key: str
    level: str  # alert | warn
    text: str
    next: str = ""


class PluginError(Exception):
    pass


class Settings(dict[str, Any]):
    """All loaded plugins' settings in one mapping; a later plugin's key replaces an earlier one's. `.of(name)` is
    one plugin's table, `.tables` all of them."""

    def __init__(self, tables: dict[str, dict[str, Any]] | None = None):
        super().__init__()
        self.tables = dict(tables or {})
        for table in self.tables.values():
            self.update(table)

    def of(self, name: str) -> dict[str, Any]:
        return self.tables.get(name, {})


def old_variable(name: str, on: str | None = None) -> dict[str, Any]:
    """Field metadata of a plugin setting that an older program configured through the environment variable `name`:
    the variable still sets the field when neither a file nor SM_<PLUGIN>__<FIELD> does (a running service keeps its
    environment across an upgrade), and `sm self config` names it as the source.
    `on`: the old program's switch was on only for exactly this value (`HZ_X == "1"`); the old variable then sets a
    bool field to whether it equals `on`, so `true` or `yes` keep it off as they always did.
    Use: Field(..., json_schema_extra=old_variable("X"))."""
    return {"old_variable": name} if on is None else {"old_variable": name, "old_variable_on": on}


def old_switch(model: Any, field: str) -> str | None:
    """The one value that turned the old variable of a bool setting on (`old_variable(..., on=...)`), or None."""
    extra = model.model_fields[field].json_schema_extra
    on = extra.get("old_variable_on") if isinstance(extra, dict) else None
    return on if isinstance(on, str) else None


def old_variables(model: Any) -> dict[str, str]:
    """{field: old environment variable} of a settings model."""
    out = {}
    for field, info in model.model_fields.items():
        extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
        if isinstance(extra.get("old_variable"), str):
            out[field] = str(extra["old_variable"])
    return out


_REGISTERED: dict[str, object] = {}


def register(plugin: object, name: str | None = None) -> None:
    """For tests and embedding: make a plugin available by name without an installed entry point."""
    _REGISTERED[str(name or getattr(plugin, "name", type(plugin).__name__.lower()))] = plugin


def available() -> dict[str, str]:
    found = {ep.name: ep.value for ep in entry_points(group=GROUP)}
    found.update({name: f"registered:{type(p).__name__}" for name, p in _REGISTERED.items()})
    return found


class Stack:
    """The loaded plugins of one project, base first."""

    def __init__(self, pm: pluggy.PluginManager, names: list[str]):
        self.pm, self.names = pm, list(names)
        self.hook = pm.hook

    @property
    def name(self) -> str:
        return "+".join(self.names)

    def _collect(self, hook: str, **kwargs: Any) -> list[Any]:
        """Every plugin's result, base first (pluggy itself calls the last registered first)."""
        return list(reversed(getattr(self.hook, hook)(**kwargs)))

    # ------------------------------------------------------------------ settings
    def settings_models(self) -> dict[str, Any]:
        out = {}
        for name in self.names:
            impl = getattr(self.pm.get_plugin(name), "sm_settings", None)
            if impl is not None:
                out[name] = impl()
        return out

    def validate(self, raw: dict[str, dict[str, Any]], base_dir: Any, layers: list[Any] | None = None) -> tuple[Settings, dict[str, Any]]:
        """Each plugin's table: file layers (merged in `raw`), then SM_<PLUGIN>__<FIELD> from the environment,
        then the old environment variable a field names (`old_variable`, read only when nothing above set the field),
        validated by the plugin's model. Unknown keys are an error. Path fields are expanded; a relative path starts
        at the directory of the config file. Returns (settings, {"plugin.key": where the value came from})."""
        models, tables, sources, problems = self.settings_models(), {}, {}, []
        for name in self.names:
            given = dict(raw.get(name) or {})
            model = models.get(name)
            if model is None:
                if given:
                    problems.append(f"plugin '{name}' has no settings, but [{name}] sets: {', '.join(sorted(given))}")
                continue
            for key in given:
                sources[f"{name}.{key}"] = next((src for src, data in (layers or []) if isinstance(data.get(name), dict) and key in data[name]), "file")
            prefix = f"SM_{name.upper().replace('-', '_')}__"
            for key, value in config_env.prefixed(prefix).items():
                given[key] = value
                sources[f"{name}.{key}"] = f"environment {prefix}{key.upper()}"
            for field, old in old_variables(model).items():
                if field not in given and (old_value := config_env.variable(old)) is not None:
                    on = old_switch(model, field)
                    given[field] = old_value if on is None else old_value == on
                    sources[f"{name}.{field}"] = f"environment {old} (old name of {prefix}{field.upper()})"
            try:
                checked = model.model_validate(given)
            except ValidationError as exc:
                for e in exc.errors():
                    field = ".".join(str(p) for p in e["loc"])
                    problems.append(f"unknown setting [{name}] {field} (known: {', '.join(model.model_fields)})" if e["type"] == "extra_forbidden"
                                    else f"[{name}] {field}: {e['msg']}")
                continue
            out = checked.model_dump()
            for field, info in model.model_fields.items():
                if info.annotation in (Path, Path | None) and out[field] is not None:
                    path = Path(os.path.expanduser(str(out[field])))
                    out[field] = str(path if path.is_absolute() else (Path(base_dir) / path).resolve())
                sources.setdefault(f"{name}.{field}", "default")
            tables[name] = out
        if problems:
            raise PluginError("; ".join(problems))
        return Settings(tables), sources

    # ------------------------------------------------------------------ providers
    def providers(self, settings: Any, base: Providers) -> Providers:
        changes = {}
        for hook, field in PROVIDER_HOOKS.items():
            got = getattr(self.hook, hook)(settings=settings, base=base)
            if got is not None:
                changes[field] = got
        extra = dict(base.extra)
        for part in self._collect("sm_provider_extras", settings=settings, base=replace(base, **changes)):
            extra.update(part or {})
        return replace(base, **changes, extra=extra)

    # ------------------------------------------------------------------ pipelines, gates, traps
    def pipelines(self, settings: Any) -> list[Any]:
        by_id = {}
        for part in self._collect("sm_pipelines", settings=settings):
            for pl in part or []:
                by_id[pl.id] = pl
        for pl in by_id.values():
            for part in self._collect("sm_steps", pipeline=pl, settings=settings):
                for ins in part or []:
                    names = [s.name for s in pl.steps]
                    anchor = ins.after or ins.before
                    if anchor not in names:
                        raise PluginError(f"sm_steps: step '{ins.step.name}' wants to go {'after' if ins.after else 'before'} '{anchor}', "
                                          f"which {pl.id} does not have (it has: {', '.join(names)})")
                    if ins.step.name not in names:
                        pl.steps.insert(names.index(anchor) + (1 if ins.after else 0), ins.step)
        return list(by_id.values())

    def gates(self, settings: Any) -> list[Any]:
        by_name = {}
        for part in self._collect("sm_gates", settings=settings):
            by_name.update({g.name: g for g in part or []})
        return list(by_name.values())

    def traps(self, settings: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for part in self._collect("sm_traps", settings=settings):
            out.update(part or {})
        return out

    def run_step(self, step: Any, ctx: Any) -> str:
        return str(self.hook.sm_step_run(step=step, ctx=ctx))

    # ------------------------------------------------------------------ commands
    def clis(self, runtime: Any) -> list[Any]:
        return [app for app in self._collect("sm_commands", runtime=runtime) if app is not None]

    def native(self, settings: Any) -> list[Any]:
        """Native commands and group helps of all plugins, base first; a later plugin's path replaces an earlier one's."""
        by_key = {}
        for part in self._collect("sm_native", settings=settings):
            for item in part or []:
                by_key[(type(item).__name__, item.path)] = item
        return list(by_key.values())

    def native_paths(self, settings: Any) -> set[Any]:
        out = set()
        for part in self._collect("sm_native_paths", settings=settings):
            out |= set(part or ())
        return out

    # ------------------------------------------------------------------ go, alarms, tool map, lint
    def go_items(self, providers: Any, settings: Any) -> list[Any]:
        return [i for part in self._collect("sm_go_items", providers=providers, settings=settings) for i in part or []]

    def go_report(self, report: dict[str, Any], providers: Any, settings: Any) -> None:
        self._collect("sm_go_report", report=report, providers=providers, settings=settings)

    def alarms(self, providers: Any, settings: Any) -> list[Any]:
        return [a for part in self._collect("sm_alarms", providers=providers, settings=settings) for a in part or []]

    def tool_map(self) -> list[Any]:
        return [r for part in self._collect("sm_tool_map") for r in part or []]

    def state_moves(self, settings: Any, home: Any) -> list[Any]:
        return [m for part in self._collect("sm_state_moves", settings=settings, home=home) for m in part or []]

    def lint_rules(self) -> list[Any]:
        return [r for part in self._collect("sm_help_lint_rules") for r in part or []]

    # ------------------------------------------------------------------ about itself
    def describe(self) -> list[dict[str, Any]]:
        rows = []
        for position, name in enumerate(self.names, 1):
            plugin = self.pm.get_plugin(name)
            hooks = sorted(impl.function.__name__ for caller in self.pm.get_hookcallers(plugin) or [] for impl in caller.get_hookimpls() if impl.plugin is plugin)
            wrappers = sorted(impl.function.__name__ for caller in self.pm.get_hookcallers(plugin) or [] for impl in caller.get_hookimpls()
                              if impl.plugin is plugin and (impl.wrapper or impl.hookwrapper))
            rows.append({"order": position, "name": name, "layer": "base" if position == 1 else "outermost" if position == len(self.names) else "middle",
                         "source": available().get(name, ""), "hooks": hooks, "wrappers": wrappers})
        return rows


def hook_table() -> list[dict[str, Any]]:
    rows = []
    for name, fn in inspect.getmembers(Specs, inspect.isfunction):
        if not name.startswith("sm_"):
            continue
        opts = getattr(fn, PROJECT + "_spec", {})
        kind = "provider (outermost result wins)" if name in PROVIDER_HOOKS else "wrapper" if name == "sm_step_run" else \
            "first result" if opts.get("firstresult") else "collecting (all results, base first)"
        args = [a for a in inspect.signature(fn).parameters if a != "self"]
        rows.append({"hook": name, "kind": kind, "arguments": args, "doc": inspect.getdoc(fn) or ""})
    return rows


def manager() -> pluggy.PluginManager:
    pm = pluggy.PluginManager(PROJECT)
    pm.add_hookspecs(Specs)
    pm.register(Core(), name="core")
    return pm


def load(names: list[str], objects: dict[str, object] | None = None) -> Stack:
    """Load exactly the named plugins, in this order. Every other installed plugin is blocked. Raises PluginError
    for a plugin that is not installed, implements an unknown hook, has a wrong hook signature or another API version."""
    pm = manager()
    known = {**available(), **{n: "given" for n in (objects or {})}}
    for other in set(known) - set(names):
        pm.set_blocked(other)
    for name in names:
        try:
            if objects and name in objects:
                pm.register(objects[name], name=name)
            elif name in _REGISTERED:
                pm.register(_REGISTERED[name], name=name)
            elif name in known:
                pm.load_setuptools_entrypoints(GROUP, name=name)
            else:
                raise PluginError(f"plugin '{name}' is not installed (entry point group {GROUP}); installed: {', '.join(sorted(known)) or 'none'}")
            if pm.get_plugin(name) is None:
                raise PluginError(f"plugin '{name}' did not register (its entry point must be an object or module with @hookimpl functions)")
        except pluggy.PluginValidationError as exc:
            raise PluginError(f"plugin '{name}' does not fit the hook specifications: {exc}") from None
        except ImportError as exc:
            raise PluginError(f"plugin '{name}' cannot be imported: {exc}") from None
    try:
        pm.check_pending()  # a function marked @hookimpl that matches no specification
    except pluggy.PluginValidationError as exc:
        raise PluginError(f"unknown hook: {exc}. `sm self hooks` lists the specifications") from None
    for name in names:
        impl = getattr(pm.get_plugin(name), "sm_api_version", None)
        version = impl() if impl is not None else None
        if version != API_VERSION:
            raise PluginError(f"plugin '{name}' is written for plugin API {version if version is not None else '(sm_api_version is missing)'}, "
                              f"this core has API {API_VERSION}")
    return Stack(pm, names)


def stack_of(*plugins: object) -> Stack:
    """A stack of plugin objects, base first; each needs a `name`. For tests."""
    objects = {getattr(p, "name", type(p).__name__.lower()): p for p in plugins}
    return load(list(objects), objects)
