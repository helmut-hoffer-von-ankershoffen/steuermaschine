"""heinzel-management-98cw: plugin release stamps live in their own receipt, never in the core release receipt, and a
one-time repair moves stamps an older `sm release plugin` left there (dry run by default, backup first)."""

import base64
import json
import subprocess

import pytest

from steuermaschine_heinzel.native.queue_lib import edge, hzlib

STAMPS = {"plugin-heinzel-clock-0.9.8": "2026-11-13T14:20:46Z", "plugin-heinzel-theme-0.1.5": "2026-11-13T14:26:47Z"}


@pytest.fixture
def rel(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HZ_RELEASE_FLOW_DIR", str(tmp_path / "flow"))
    monkeypatch.setattr(hzlib, "_gh_process", lambda *a, **_: subprocess.CompletedProcess(a, 1, "", ""))
    module = edge.fresh(edge.module_of("hz-release"))
    toml = base64.b64encode(b'[project]\nname = "heinzel-resource"\nversion = "0.1.5"\n').decode()
    monkeypatch.setattr(module.hzlib, "gh", lambda *a: (0, toml) if "contents/" in a[1] else (0, ""))
    module.said = []
    monkeypatch.setattr(module.hzlib, "say", lambda text, *a, **k: module.said.append(text))
    monkeypatch.setattr(module.hzlib, "release_holds_present", lambda: True)  # a hold never matters to the plugin receipt
    (tmp_path / ".cache").mkdir()
    return module


def test_a_plugin_release_writes_its_stamp_to_the_plugin_receipt_only(rel, monkeypatch):
    seen = []
    monkeypatch.setattr(rel, "plugin_dispatch", lambda key, dist: rel.remember(**{key: "2026-11-13T14:22:57Z"}))
    monkeypatch.setattr(rel, "pypi_has", lambda *a: bool(seen) or bool(seen.append(1)))
    assert rel.release_plugin("resource", 3600, redispatch=True) == 0
    assert not rel.global_receipt().exists()
    assert json.loads(rel.plugin_receipt().read_text()) == {"plugin-heinzel-resource-0.1.5": "2026-11-13T14:22:57Z"}


def test_a_plugin_release_refuses_while_old_stamps_sit_in_the_core_receipt(rel):
    rel.global_receipt().write_text(json.dumps(STAMPS))
    with pytest.raises(rel.Stop, match=r"sm release repair-receipt --apply"):
        rel.release_plugin("resource", 3600)


def test_the_dry_run_writes_nothing_and_shows_the_plan(rel):
    rel.global_receipt().write_text(json.dumps(STAMPS))
    before = rel.global_receipt().read_bytes()
    assert rel.main(["--repair-receipt"]) == 0
    assert rel.global_receipt().read_bytes() == before and not rel.plugin_receipt().exists()
    assert not list(rel.global_receipt().parent.glob("release.json.bak-*"))
    assert "  -> absent (as after the tag handoff)" in rel.said and rel.said[-1].startswith("DRY-RUN")


def test_apply_backs_up_moves_the_stamps_and_keeps_core_keys(rel):
    tail = rel.tail_receipt("0.9.40")
    tail.parent.mkdir()
    tail.write_text(json.dumps({"version": "0.9.40", "finished": True, "accepted": True, "acceptance_run": 5}))
    rel.plugin_receipt().write_text(json.dumps({"plugin-heinzel-old-0.1.0": "2026-11-01T00:00:00Z"}))
    rel.global_receipt().write_text(json.dumps(STAMPS))
    assert rel.main(["--repair-receipt", "--apply"]) == 0
    assert not rel.global_receipt().exists()
    assert json.loads(rel.plugin_receipt().read_text()) == {"plugin-heinzel-old-0.1.0": "2026-11-01T00:00:00Z", **STAMPS}
    [backup] = rel.global_receipt().parent.glob("release.json.bak-*")
    assert json.loads(backup.read_text()) == STAMPS
    assert any("tail receipt v0.9.40.json version=0.9.40 finished=True accepted=True acceptance_run=5" in line for line in rel.said)
    assert json.loads(tail.read_text())["version"] == "0.9.40"  # never touched
    rel.global_receipt().write_text(json.dumps({"version": "0.9.41", "freeze_token": "t", **STAMPS}))
    assert rel.main(["--repair-receipt", "--apply"]) == 0
    assert json.loads(rel.global_receipt().read_text()) == {"version": "0.9.41", "freeze_token": "t"}
    assert rel.main(["--repair-receipt", "--apply"]) == 0 and rel.said[-1].startswith("nothing to repair")


def test_a_clashing_plugin_stamp_preserves_both_files(rel):
    rel.plugin_receipt().write_text(json.dumps({"plugin-heinzel-clock-0.9.8": "2026-11-13T00:00:00Z"}))
    rel.global_receipt().write_text(json.dumps(STAMPS))
    assert rel.main(["--repair-receipt", "--apply"]) == 1
    assert json.loads(rel.global_receipt().read_text()) == STAMPS


def test_apply_goes_only_with_the_repair(rel):
    with pytest.raises(SystemExit):
        rel.main(["0.9.41", "--apply"])
    with pytest.raises(SystemExit):
        rel.main(["0.9.41", "--repair-receipt"])
