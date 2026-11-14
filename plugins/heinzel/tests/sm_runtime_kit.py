"""A runtime on fake providers with the plugin's settings table filled from a dict, for the command-tree tests of
`ci flakes` and `mini loops`."""

from pathlib import Path

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers
from steuermaschine_heinzel.plugin import plugin


def runtime_with(tmp_path: Path, conf: dict | None = None, reviewed: bool = True, **providers) -> Runtime:
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": dict(conf or {})}, tmp_path)
    project = Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=reviewed)
    return Runtime(project, stack, settings, _providers=fake_providers(**providers))
