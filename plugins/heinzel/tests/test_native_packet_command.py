"""`sm head manifest` (the pair packet a release cut reads): argument rules, PR-number lookup and refusals, on fakes."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_pipeline_head_admit import HEAD, native, runtime

from steuermaschine.engine import Refuse
from steuermaschine.github.testing import FakeGh
from steuermaschine_heinzel.native import packet
from steuermaschine_heinzel.native.packet_lib import ops

RESULT = ops.ManifestResult(status="built", head=HEAD, base="b" * 40, tree="t" * 40, packet_sha256="p", manifest_sha256="m", main_manifest_sha256="mm")


def call(tmp_path, gh=None, **kw):
    rt = runtime(tmp_path, gh)
    args = dict(target=None, verify=None, core_repo=tmp_path, base="origin/main", selection=None, junit=[], production=[], review=None,
                review_root=None, layout="indent", output=None)
    with native(rt):
        return packet.head_manifest(**{**args, **kw})


def test_exactly_one_of_a_target_or_verify(tmp_path):
    for kw in ({}, {"target": "fix/x", "verify": tmp_path / "p.json"}):
        with pytest.raises(Refuse, match="exactly one of TARGET or --verify"):
            call(tmp_path, **kw)


def test_a_build_needs_an_output_and_reaches_the_builder(tmp_path, monkeypatch):
    with pytest.raises(Refuse, match="needs --output"):
        call(tmp_path, target="fix/x")
    seen = {}
    monkeypatch.setattr(ops, "build_packet", lambda core, head, base, out, **kw: seen.update(head=head, out=out, kw=kw) or RESULT)
    assert call(tmp_path, target="fix/x", output=tmp_path / "p.json").status == "built"
    assert seen["head"] == "fix/x" and seen["kw"]["layout"] == "indent"


def test_verify_reaches_the_verifier(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "verify_packet", lambda core, path, **kw: ops.ManifestResult(**{**RESULT.model_dump(), "status": "byte_equal"}))
    assert call(tmp_path, verify=tmp_path / "p.json").status == "byte_equal"


def test_a_refusal_of_the_builder_is_a_refusal_with_its_summary(tmp_path, monkeypatch):
    def refuse(*a, **kw):
        raise ops.PacketRefused("base is not main", ["checked: tree"])

    monkeypatch.setattr(ops, "verify_packet", refuse)
    with pytest.raises(Refuse, match="refused: base is not main"):
        call(tmp_path, verify=tmp_path / "p.json")


def test_a_pr_number_becomes_its_exact_head_and_an_unreadable_one_refuses(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(ops, "build_packet", lambda core, head, *a, **kw: seen.update(head=head) or RESULT)
    gh = FakeGh().on("pr view 812", {"headRefOid": HEAD})
    call(tmp_path, gh, target="812", output=tmp_path / "p.json")
    assert seen["head"] == HEAD
    with pytest.raises(Refuse, match="cannot read the head of PR 813"):
        call(tmp_path, FakeGh().on("pr view 813", {"headRefOid": "x"}), target="813", output=tmp_path / "p.json")


def test_without_a_core_repository_it_refuses_naming_the_setting(tmp_path):
    rt = runtime(tmp_path)
    rt.settings["core_repo_dir"] = None
    with native(rt), pytest.raises(Refuse, match="core_repo_dir"):
        packet.head_manifest(target="x", verify=None, core_repo=None, base="origin/main", selection=None, junit=[], production=[], review=None,
                             review_root=None, layout="indent", output=Path("o"))


def test_the_result_renders_in_text_and_sets_the_exit_code(tmp_path, monkeypatch):
    from steuermaschine.testing import invoke

    different = ops.ManifestResult(**{**RESULT.model_dump(), "status": "different", "review_path": "REVIEW.md", "review_sha256": "r" * 64,
                                      "output": "out.json", "summary": ["checked"], "differences": ["packet differs"]})
    monkeypatch.setattr(ops, "verify_packet", lambda core, path, **kw: different)
    rt = runtime(tmp_path)
    with native(rt):
        text = invoke(rt, ["head", "manifest", "--verify", str(tmp_path / "p.json"), "--core-repo", str(tmp_path)])
        data = invoke(rt, ["--format", "json", "head", "manifest", "--verify", str(tmp_path / "p.json"), "--core-repo", str(tmp_path)])
    assert text.exit_code == 1 and "packet differs" in text.stdout
    assert data.exit_code == 1 and '"status": "different"' in data.stdout.replace('":"', '": "')
    for status, line in (("built", "wrote o.json sha256 p"), ("byte_equal", "BYTE-EQUAL sha256 p")):
        found = ops.ManifestResult(**{**RESULT.model_dump(), "status": status, "output": "o.json"})
        assert found.exit_code == 0 and found.render()[-1] == line and found.report().endswith(line + "\n")


def test_the_builder_refuses_an_unknown_layout_and_a_review_without_its_root(tmp_path):
    with pytest.raises(ops.PacketRefused, match="unknown layout nope"):
        ops.build_packet(tmp_path, "fix/x", "origin/main", tmp_path / "p.json", layout="nope")
    with pytest.raises(ops.PacketRefused, match="--review needs --review-root"):
        ops.build_packet(tmp_path, "fix/x", "origin/main", tmp_path / "p.json", review="REVIEW.md")
