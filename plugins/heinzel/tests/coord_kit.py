"""Shared by the tests of the coordinator commands: a runtime on fakes with a sandbox tracking document."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.native import NativeRuntime
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, fake_providers
from steuermaschine_heinzel.plugin import plugin

NOW = 1_800_000_000.0  # 2027-01-15 08:00 UTC


def settings_of(tmp_path: Path, **over) -> dict:
    stack = stack_of(plugin)
    table = {"coord_document": str(tmp_path / "AGENTS.md"), "coord_journal_lock_file": str(tmp_path / "note.lock"), "coord_tracker_dir": str(tmp_path),
             "core_repo_dir": str(tmp_path / "core"), **over}
    settings, _ = stack.validate({"heinzel": {k: v for k, v in table.items() if v is not None}}, tmp_path)
    for name in ("coord_document", "coord_journal_lock_file", "coord_tracker_dir"):  # a test never reaches a real file
        assert settings[name] is None or str(settings[name]).startswith(str(tmp_path)), (name, settings[name])
    return settings


@pytest.fixture
def home(tmp_path):
    """The sandbox of a coordinator test. HOME itself is sandboxed for every test by the root conftest."""
    return tmp_path


def app_runtime(tmp_path: Path, commands=None, allow: bool = True, **over) -> Runtime:
    stack, settings = stack_of(plugin), settings_of(tmp_path, **over)
    providers = stack.providers(settings, fake_providers(commands=commands or FakeCommands()))  # the plugin's extras, as in a real run
    project = Project(name="coordtest", plugin="heinzel", root=tmp_path, plugins=["heinzel"], allow_unreviewed=allow)
    return Runtime(project, stack, settings, _providers=providers)


def native_runtime(tmp_path: Path, commands=None, **over) -> NativeRuntime:
    rt = app_runtime(tmp_path, commands, **over)
    return NativeRuntime(rt.providers, rt.settings, rt.project)


def ok(data="") -> Result:
    return Result(0, data if isinstance(data, str) else json.dumps(data), "")


@pytest.fixture
def unreviewed(monkeypatch):
    """The gate tests must not depend on the live review files: every write command's review is open in such a test."""
    from steuermaschine.native import NativeCommand

    monkeypatch.setattr(NativeCommand, "review_state", lambda self: ("open", f"reviews/{self.review_file} has no verdict yet"))
