"""Shared by the test_native_tick_*.py files: a runtime on fakes that has the tick area's commands, before and
after the area is registered in the plugin (native/__init__.py MODULES, HeinzelSettings bases)."""

from pathlib import Path

from steuermaschine_heinzel import native as native_pkg
from steuermaschine_heinzel.native import tick
from steuermaschine_heinzel.plugin import HeinzelSettings, plugin

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github.testing import FakeGh
from steuermaschine.helpdoc import missing
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers

REPO = "example/core"


def build(tmp_path, monkeypatch, commands=None, clock=None, **tick_settings):
    """A runtime whose every path is under tmp_path. `tick_settings` are fields of TickSettings."""
    monkeypatch.setattr(native_pkg, "MODULES", [*[m for m in native_pkg.MODULES if m is not tick], tick])
    for name in ("state", "diagnostics", "research", "beads"):
        (tmp_path / name).mkdir(exist_ok=True)
    table = {"repo": REPO, "legacy_state_dir": str(tmp_path / "state"), "lock_history": str(tmp_path / "gate.jsonl")}
    values = {"tick_diagnostics_dir": tmp_path / "diagnostics", "tick_tool_venv": tmp_path / "venv", "tick_research_dir": tmp_path / "research",
              "tick_beads_dir": tmp_path / "beads", "tick_bead_prefix": "tracker-", **tick_settings}
    for name, value in values.items():
        if value is None:
            continue
        if name in HeinzelSettings.model_fields:  # the mixin is registered: an ordinary setting
            table[name] = value if isinstance(value, int | float) else str(value)
        else:  # not yet: the command reads the mixin's default
            monkeypatch.setattr(tick.TickSettings.model_fields[name], "default", value)
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": table}, tmp_path)
    over = {"commands": commands or FakeGh()}
    if clock is not None:
        over["clock"] = clock
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, settings, _providers=fake_providers(**over))


def help_of(function) -> str:
    return function.__sm_native__.help


def contract_gaps(*functions) -> dict:
    """Letters of the help contract each command lacks (empty: none)."""
    return {f.__sm_native__.path: missing(help_of(f)) for f in functions if missing(help_of(f))}
