"""Fixtures of the moved tool tests (from the tools' own tests/conftest.py). A test asks for a tool by its old file
name and gets a fresh copy of the module it moved to; the assertions of the moved tests are unchanged."""

import subprocess

import pytest
from moved_support import load
from steuermaschine_heinzel.native.queue_lib import hzlib


@pytest.fixture(autouse=True)
def no_real_github(monkeypatch):
    """No test reads or writes real rulesets. `hzlib.gh` fails, so the merge gate's reads say "unknown" and flip does nothing."""
    monkeypatch.setattr(hzlib, "gh", lambda *_: (1, ""))
    monkeypatch.setattr(hzlib, "_gh_process", lambda *a, **_: subprocess.CompletedProcess(a, 1, "", ""))


@pytest.fixture(autouse=True)
def unconfigured_edges(monkeypatch):
    """A moved test sees the edges as a directly called tool does: no release checkout or records dir set by a native
    command of an earlier test (`configure_release` and `configure` keep module state)."""
    from steuermaschine_heinzel.native.queue_lib import edge as queue_edge
    from steuermaschine_heinzel.native.release_lib import edge as release_edge

    monkeypatch.setattr(release_edge, "_conf", {})
    monkeypatch.setattr(queue_edge, "_records", [])


@pytest.fixture(autouse=True)
def isolated_release_flow(tmp_path_factory, monkeypatch):
    """No test reads the real tag-on-green-main flag or writes a real release flow record."""
    monkeypatch.setenv("HZ_RELEASE_FLOW_DIR", str(tmp_path_factory.mktemp("release-flow")))
    monkeypatch.delenv("HZ_RELEASE_TAG_ON_MAIN", raising=False)


@pytest.fixture(autouse=True)
def isolated_alarms(tmp_path_factory, monkeypatch):
    """No test reads or writes the real alarm state, and no test can push the owner."""
    monkeypatch.setenv("HZ_ALARM_STATE", str(tmp_path_factory.mktemp("alarm") / "alarm.json"))
    monkeypatch.setenv("HZ_ALARM_NOTIFY", "off")
    monkeypatch.setenv("HZ_REHEARSE_STATE", str(tmp_path_factory.mktemp("rehearse") / "rehearse.json"))
    monkeypatch.delenv("HZ_TRAIN_NOTIFY", raising=False)


@pytest.fixture
def tool():
    return load


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway HOME with ~/.cache and a tracking document, so no test touches the real one."""
    (tmp_path / ".cache").mkdir()
    (tmp_path / "Code/heinzel-management").mkdir(parents=True)
    (tmp_path / "Code/heinzel-management/AGENTS.md").write_text("## 9. Log\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path
