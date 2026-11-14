"""The conformance kit: what every plugin must satisfy. A plugin's test suite calls
`assert_conformant(plugin, settings)`; a layer plugin names the plugins it sits on: `on=[base_plugin]`.

It checks statically and never runs a step:
- the plugin loads: every @hookimpl matches a hook specification by name and arguments (pluggy), and
  `sm_api_version` returns the core's version
- its settings model forbids unknown keys and describes every field
- every provider it returns satisfies its Protocol (the type pluggy itself cannot check)
- pipelines: unique step names; an external step states what it changes and can print its plan
- every trap key and every gate a step names exists in the composed stack
- the whole command tree with the stack loaded passes the help contract and the plugins' own lint rules
- tool map rows fit the model
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import PROG, models
from ..engine import ReviewStop
from ..plugins import PluginError, Settings, stack_of
from ..protocols import PROTOCOLS


def check_plugin(plugin: Any, settings: dict[str, Any] | None = None, base_providers: Any = None, on: list[Any] | tuple[Any, ...] = ()) -> list[str]:
    from .. import helpdoc
    from ..app import Runtime, command_tree
    from ..config import Project
    from .fakes import fake_providers

    name = getattr(plugin, "name", type(plugin).__name__.lower())
    try:
        stack = stack_of(*on, plugin)
    except PluginError as exc:
        return [str(exc)]
    problems: list[str] = []
    if not name.replace("-", "").replace("_", "").isalnum():
        problems.append(f"name '{name}' must be a plain word")
    model = stack.settings_models().get(name)
    if model is not None and model.model_config.get("extra") != "forbid":
        problems.append("the settings model must set extra='forbid': an unknown key in the config file has to be an error")
    for key, info in (model.model_fields if model is not None else {}).items():
        if not info.description:
            problems.append(f"setting {key}: needs a description (it is the help a person reads)")
    if not isinstance(settings, Settings):
        try:  # the plugin's own table as given; the plugins it sits on get their defaults
            settings, _ = stack.validate({name: dict(settings or {})}, Path("."))
        except PluginError as exc:
            return [*problems, str(exc)]
    providers = stack.providers(settings, base_providers or fake_providers())
    for field, proto in PROTOCOLS.items():
        obj = getattr(providers, field)
        if obj is not None and not isinstance(obj, proto):
            problems.append(f"provider {field} ({type(obj).__name__}) does not satisfy {proto.__name__}")
    gates = {g.name for g in stack.gates(settings)}
    known = set()
    try:
        pipelines = stack.pipelines(settings)
    except PluginError as exc:
        pipelines = []
        problems.append(str(exc))
    for pl in pipelines:
        names = [s.name for s in pl.steps]
        if len(set(names)) != len(names):
            problems.append(f"{pl.id}: step names repeat")
        for s in pl.steps:
            known.add(f"{pl.id}.{s.name}")
            if isinstance(s, ReviewStop):
                continue
            if s.external and (s.plan is None or s.changes.startswith("nothing")):
                problems.append(f"{pl.id}.{s.name}: an external step needs `plan` and a `changes` text that says what it writes")
            for g in s.gates:
                if g not in gates:
                    problems.append(f"{pl.id}.{s.name}: gate '{g}' is not defined")
    for key in stack.traps(settings):
        if key not in known:
            problems.append(f"trap '{key}' names no step (known: {', '.join(sorted(known)) or 'none'})")
    rt = Runtime(Project(name="conformance", plugin=stack.name, root=Path("."), plugins=stack.names), stack, settings, _providers=providers)
    rows = [row for row in helpdoc.tree(command_tree(rt), PROG) if not row["hidden"]]
    problems += [f"help contract: {row['path']} misses {row['missing']}" for row in rows if row["missing"]]
    for rule in stack.lint_rules():
        problems += [f"help rule: {row['path']}: {found}" for row in rows if (found := rule(row))]
    for row in stack.tool_map():
        try:
            models.ToolMapRow.model_validate(row if isinstance(row, dict) else row.model_dump())
        except Exception as exc:
            problems.append(f"tool_map row {row!r}: {exc}")
    return problems


def assert_conformant(plugin: Any, settings: dict[str, Any] | None = None, base_providers: Any = None, on: list[Any] | tuple[Any, ...] = ()) -> None:
    problems = check_plugin(plugin, settings, base_providers, on)
    assert not problems, "plugin is not conformant:\n  " + "\n  ".join(problems)
