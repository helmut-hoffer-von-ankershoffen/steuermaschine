"""`sm head apply DIR` refuses: batch apply is not available yet (review B1 of the release route, 216c11b).

Through the real command tree, with a scripted `gh` (FakeGh), a throwaway HOME and no real process. The refusal comes
before anything runs: no `gh` call, no process, no file changed in the batch directory, exit 2 (USAGE)."""

import json
import subprocess
from pathlib import Path

import pytest

from steuermaschine import USAGE
from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github.testing import FakeGh
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel.native.admission_lib import focused_slots as s
from steuermaschine_heinzel.native.admission_lib import ops
from steuermaschine_heinzel.plugin import plugin

SHA = "f" * 64


@pytest.fixture(autouse=True)
def nothing_real(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: pytest.fail(f"a real process was started: {argv}"))
    monkeypatch.setattr(subprocess, "Popen", lambda argv, *a, **k: pytest.fail(f"a real process was started: {argv}"))
    monkeypatch.setattr(ops, "apply_batch", lambda *a, **k: pytest.fail("the batch write was reached"))
    monkeypatch.setattr(ops, "apply", lambda *a, **k: pytest.fail("the single-packet apply took a directory"))


def runtime(gh, allow_unreviewed=True):
    stack = stack_of(plugin)
    settings, _ = stack.validate({}, Path("."))
    project = Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=allow_unreviewed)
    return Runtime(project, stack, settings, _providers=fake_providers(commands=gh))


def batch_dir(tmp_path):
    out = tmp_path / "batch"
    out.mkdir()
    (out / ops.PREPARED_FILE).write_text(json.dumps({"schema": 1, "mode": s.BATCH_MODE}) + "\n")
    (out / ops.ARGS_FILE).write_text(json.dumps({"schema": 1, "mode": s.BATCH_MODE, "sha256": SHA}) + "\n")
    return out


def snapshot(root):
    return {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("form", ["text", "json"])
def test_head_apply_refuses_a_batch_directory_and_writes_nothing(tmp_path, form):
    gh = FakeGh()
    out = batch_dir(tmp_path)
    before = snapshot(tmp_path)
    argv = ["head", "apply", str(out), "--approved", SHA]
    done = invoke(runtime(gh), ["--format", "json", *argv] if form == "json" else argv)
    text = done.stdout + done.stderr
    assert done.exit_code == USAGE == 2
    assert "batch apply is not available yet" in text and "Nothing was written" in text
    if form == "json":
        assert "batch apply is not available yet" in json.loads(done.stdout)["error"]
    assert gh.calls == [] and gh.writes() == []
    assert snapshot(tmp_path) == before


def test_head_apply_of_a_packet_file_still_reaches_the_single_packet_apply(tmp_path, monkeypatch):
    packet = tmp_path / "packet.json"
    packet.write_text("{}")
    seen = []
    monkeypatch.setattr(ops, "apply", lambda target, sha: seen.append((target, sha)) or ops.ApplyResult(receipt_path=str(tmp_path / "r.json"), sha256=sha, head="a" * 40))
    rt = runtime(FakeGh())
    rt.providers.store.put_receipt("verdicts", SHA, {"digest": SHA, "verdict": "approve", "reviewer": "reviewer-1"})  # apply needs a verdict
    done = invoke(rt, ["head", "apply", str(packet), "--approved", SHA])
    assert done.exit_code == 0 and seen == [(packet, SHA)]
