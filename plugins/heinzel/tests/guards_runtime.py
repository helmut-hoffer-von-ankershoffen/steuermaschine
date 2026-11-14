"""A runtime on fake providers for the guards' command-tree tests, with the guards module registered by monkeypatch
(until the lead adds it to MODULES) and its settings answered from a dict (until the mixin is in the plugin's table)."""

from pathlib import Path

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers
from steuermaschine_heinzel import native as native_pkg
from steuermaschine_heinzel.native import guards
from steuermaschine_heinzel.plugin import plugin


def register(monkeypatch, conf: dict | None = None) -> dict:
    if guards not in native_pkg.MODULES:
        monkeypatch.setattr(native_pkg, "MODULES", [*native_pkg.MODULES, guards])
    conf = {} if conf is None else conf
    monkeypatch.setattr(guards, "setting", lambda name: conf.get(name, guards.GuardsSettings.model_fields[name].default))
    return conf


def runtime_of(tmp_path: Path, reviewed: bool = True, table: dict | None = None, **providers) -> Runtime:
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": table or {}}, tmp_path)
    project = Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=reviewed)
    return Runtime(project, stack, settings, _providers=stack.providers(settings, fake_providers(**providers)))  # the plugin's extras, as in a real run
