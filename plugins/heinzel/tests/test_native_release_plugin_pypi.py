"""`sm release plugin`: PyPI lists a fresh upload moments after the run ends; the step polls a bounded window first."""

import base64
import subprocess

import pytest

from steuermaschine_heinzel.native.queue_lib import edge, hzlib


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def rel(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HZ_RELEASE_FLOW_DIR", str(tmp_path / "flow"))
    monkeypatch.setattr(hzlib, "_gh_process", lambda *a, **_: subprocess.CompletedProcess(a, 1, "", ""))
    module = edge.fresh(edge.module_of("hz-release"))
    clock = Clock()
    monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(module.hzlib, "sleep", clock.sleep)
    toml = base64.b64encode(b'[project]\nname = "heinzel-resource"\nversion = "0.1.5"\n').decode()
    monkeypatch.setattr(module.hzlib, "gh", lambda *a: (0, toml) if "contents/" in a[1] else (0, ""))
    monkeypatch.setattr(module, "plugin_dispatch", lambda *a, **k: None)
    monkeypatch.setattr(module.hzlib, "say", lambda *a, **k: None)
    module.clock = clock
    return module


def scripted(rel, monkeypatch, conclusion, on_pypi_after):
    run = {"databaseId": 43442192106, "status": "completed", "conclusion": conclusion}
    monkeypatch.setattr(rel, "plugin_run", lambda *a: run)
    start = rel.clock.now
    monkeypatch.setattr(rel, "pypi_has", lambda *a: on_pypi_after is not None and rel.clock.now - start >= on_pypi_after)


def test_a_success_run_before_pypi_lists_the_version_keeps_polling_and_releases(rel, monkeypatch):
    scripted(rel, monkeypatch, "success", on_pypi_after=90)
    assert rel.release_plugin("resource", 3600, redispatch=True) == 0


def test_a_success_run_without_the_version_after_the_bounded_window_stops(rel, monkeypatch):
    scripted(rel, monkeypatch, "success", on_pypi_after=None)
    with pytest.raises(rel.Stop, match=r"ended success but heinzel-resource 0\.1\.5 is not on PyPI after 5 minutes"):
        rel.release_plugin("resource", 3600, redispatch=True)
    assert rel.PYPI_PROPAGATION <= rel.clock.now - 1_000_000.0 < rel.PYPI_PROPAGATION + 2 * rel.POLL


def test_a_failed_run_stops_at_once(rel, monkeypatch):
    scripted(rel, monkeypatch, "failure", on_pypi_after=None)
    with pytest.raises(rel.Stop, match=r"ended failure without heinzel-resource 0\.1\.5 on PyPI"):
        rel.release_plugin("resource", 3600, redispatch=True)
    assert rel.clock.now == 1_000_000.0
