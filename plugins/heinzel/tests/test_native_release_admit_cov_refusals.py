"""`sm release admit prepare|apply` through the command line: the result of the release operations, and every error
they raise turned into one refusal that names it. The operations themselves are tested in test_native_release_admit*.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel.native.admission_lib import ops as admission
from steuermaschine_heinzel.native.admission_lib.ops import ApplyResult
from steuermaschine_heinzel.native.release_admit_lib import release_ops
from steuermaschine_heinzel.native.release_admit_lib.release_ops import ReleasePrepareResult
from steuermaschine_heinzel.plugin import plugin

HEAD = "e9c943810ea75cfad155a431a2f168b2a9559828"
MAIN = "1947731c" + "0" * 32


def runtime(tmp_path: Path) -> Runtime:
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": {"legacy_state_dir": str(tmp_path / "state"), "repo": "o/core"}}, tmp_path)
    project = Project(name="t", plugin="heinzel", root=tmp_path, plugins=["heinzel"], allow_unreviewed=True)
    return Runtime(project, stack, settings, _providers=fake_providers())


def prepare_args(tmp_path: Path) -> list[str]:
    return ["release", "admit", "prepare", str(tmp_path / "packet.json"), "--core-repo", str(tmp_path / "core"), "--review-root", str(tmp_path / "review")]


def test_prepare_returns_the_stored_transaction(tmp_path, monkeypatch):
    seen: dict = {}

    def prepare(packet, core_repo, review_root, nonconsumers=None):
        seen.update(packet=packet, core_repo=core_repo, review_root=review_root, nonconsumers=nonconsumers)
        doc = {"head": HEAD, "main": MAIN, "slot": "RELEASE_MAIN"}
        return ReleasePrepareResult(sha256=admission.canonical_sha256(doc), version="0.9.41", head=HEAD, main=MAIN, slot="RELEASE_MAIN",
                                    recovery=False, prepared_path=str(tmp_path / "prepare.out"), args_path=str(tmp_path / "prepare.args.json"),
                                    prepared=doc)

    monkeypatch.setattr(release_ops, "prepare", prepare)
    out = invoke(runtime(tmp_path), ["--format", "json", *prepare_args(tmp_path), "--nonconsumers", str(tmp_path / "stuck.json")])
    assert out.exit_code == 0, out
    doc = json.loads(out.stdout)
    assert doc["head"] == HEAD and doc["slot"] == "RELEASE_MAIN" and doc["sha256"] == admission.canonical_sha256({"head": HEAD, "main": MAIN, "slot": "RELEASE_MAIN"})
    assert seen == {"packet": tmp_path / "packet.json", "core_repo": tmp_path / "core", "review_root": tmp_path / "review",
                    "nonconsumers": tmp_path / "stuck.json"}


@pytest.mark.parametrize("error", [release_ops.a.AdmissionError("main moved since the packet"), OSError("packet.json: no such file"),
                                   ValueError("version 0.9.41 is not the release head's"), KeyError("slot")])
def test_prepare_refuses_with_the_error_and_writes_no_result(tmp_path, monkeypatch, error):
    def prepare(*_args, **_kw):
        raise error

    monkeypatch.setattr(release_ops, "prepare", prepare)
    out = invoke(runtime(tmp_path), prepare_args(tmp_path))
    assert out.exit_code == 1 and out.stdout.strip() == ""
    assert f"release-main admission refused: {error}" in out.stderr


def test_apply_returns_the_receipt_of_the_approved_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(release_ops, "apply", lambda packet, approved: ApplyResult(receipt_path=str(tmp_path / "receipt.json"), sha256=approved, head=HEAD))
    rt = runtime(tmp_path)
    rt.providers.store.put_receipt("verdicts", "a" * 64, {"verdict": "approve", "reviewer": "reviewer-1"})
    out = invoke(rt, ["--format", "json", "release", "admit", "apply", str(tmp_path / "packet.json"), "--approved", "a" * 64])
    assert out.exit_code == 0, out
    doc = json.loads(out.stdout)
    assert doc["receipt_path"] == str(tmp_path / "receipt.json") and doc["sha256"] == "a" * 64


def test_apply_refuses_a_digest_that_was_not_approved(tmp_path, monkeypatch):
    def apply(packet, approved):
        raise release_ops.a.AdmissionError(f"approved digest {approved[:12]} does not match prepare.out")

    monkeypatch.setattr(release_ops, "apply", apply)
    rt = runtime(tmp_path)
    rt.providers.store.put_receipt("verdicts", "b" * 64, {"verdict": "approve", "reviewer": "reviewer-1"})
    out = invoke(rt, ["release", "admit", "apply", str(tmp_path / "packet.json"), "--approved", "b" * 64])
    assert out.exit_code == 1 and f"release-main admission refused: approved digest {'b' * 12} does not match prepare.out" in out.stderr
