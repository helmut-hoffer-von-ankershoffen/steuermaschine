"""k7fj: `sm head admit` after a packet reject rebuilds the packet once the selection changed, with a new brief."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_pipeline_cov_head_admit import HEAD, MAIN, clean_overrides, context, native, runtime  # noqa: F401  (the autouse fixture)

from steuermaschine.engine import Refuse
from steuermaschine_heinzel.native.preflight import SUMMARY_KIND, HeadPreflight
from steuermaschine_heinzel.pipelines import common, head_admit


def _setup(tmp_path: Path) -> tuple[Any, Any, Path, list[dict[str, Any]]]:
    rt = runtime(tmp_path)
    sel = tmp_path / "sel.json"
    sel.write_text(json.dumps({"python": ["tests/unit/a_test.py"]}))
    ctx = context(rt, tmp_path / "wt", {"review_root": str(tmp_path / "review"), "sel": str(sel), "core_repo": str(tmp_path)})
    builds: list[dict[str, Any]] = []
    edits: list[str] = []
    ctx.data["edits"] = edits  # a test seam: the next preflight run edits the selection mid-run

    def preopen(**kw: Any) -> SimpleNamespace:
        out = Path(kw["out"])
        out.mkdir(parents=True)
        (out / "packet.json").write_text(json.dumps({"head": HEAD, "selection": json.loads(Path(kw["selection"]).read_text())}))
        (out / "sel.json").write_text(Path(kw["selection"]).read_text())
        builds.append(kw)
        return SimpleNamespace(directory=str(out), packet_sha256=hashlib.sha256((out / "packet.json").read_bytes()).hexdigest())

    def preflight(**kw: Any) -> HeadPreflight:
        builds.append({"preflight": json.loads(Path(kw["sel"]).read_text())})
        ran = hashlib.sha256(Path(kw["sel"]).read_bytes()).hexdigest()  # the bytes the preflight read, as the real one reports them
        if edit := edits.pop(0) if edits else None:  # someone edits the selection while the preflight runs
            Path(kw["sel"]).write_text(edit)
        return HeadPreflight(title="head", ready=True, verdict="READY", head=HEAD, base=MAIN, selection_sha256=ran)

    common.OVERRIDE["head packet preopen"] = preopen
    common.OVERRIDE["head preflight"] = preflight
    return rt, ctx, sel, builds


def test_a_rejected_packet_with_an_unchanged_selection_refuses_and_names_the_way_out(tmp_path):
    rt, ctx, _sel, builds = _setup(tmp_path)
    with native(rt):
        head_admit.preflight(ctx)
        head_admit.packet(ctx)
        digest, _brief, _file = head_admit.packet_review(ctx)
        rt.providers.store.put_receipt("verdicts", digest, {"digest": digest, "verdict": "reject", "reviewer": "r1", "notes": "add b_test"})
        with pytest.raises(Refuse) as no:
            head_admit.packet_review(ctx)
        assert "rejected by r1: add b_test" in no.value.message and "--set sel=" in no.value.next_cmd
        assert len(builds) == 2  # one preflight, one packet


def test_a_rejected_packet_is_kept_aside_and_rebuilt_from_the_changed_selection(tmp_path):
    rt, ctx, sel, builds = _setup(tmp_path)
    with native(rt):
        head_admit.preflight(ctx)
        head_admit.packet(ctx)
        old, _brief, _file = head_admit.packet_review(ctx)
        rt.providers.store.put_receipt("verdicts", old, {"digest": old, "verdict": "reject", "reviewer": "r1", "notes": "add b_test"})
        sel.write_text(json.dumps({"python": ["tests/unit/a_test.py", "tests/unit/b_test.py"]}))
        new, brief, file = head_admit.packet_review(ctx)
        assert new != old and len(builds) == 4  # R3: the changed selection was preflighted, then the packet built
        assert builds[2] == {"preflight": {"python": ["tests/unit/a_test.py", "tests/unit/b_test.py"]}}
        out = head_admit.packet_dir(ctx)
        assert file == str(out / "packet.json") and "b_test" in (out / "packet.json").read_text()
        kept = out.with_name(out.name + ".rejected-1")
        assert (kept / "packet.json").is_file()
        assert f"replaces {old[:12]}, rejected by r1: add b_test" in brief and str(kept) in brief
        assert head_admit.packet_review(ctx)[0] == new  # no verdict on the new bytes yet: the same digest, no new build
        assert len(builds) == 4
        head_admit.packet(ctx)  # a packet built for its own reason (main moved) answers no earlier reject
        assert "replaces" not in head_admit.packet_review(ctx)[1]


def test_a_stored_preflight_is_adopted_only_for_the_same_selection_bytes(tmp_path):
    """R3 (the reviewer's probe): a READY computed for another selection is not adopted; it runs again."""
    rt, ctx, sel, _builds = _setup(tmp_path)
    summary = {"head": HEAD, "ready": True, "base": MAIN}
    rt.providers.store.save(SUMMARY_KIND, HEAD[:8], summary)  # an older summary names no selection digest
    assert head_admit.preflight_probe(ctx) is None
    rt.providers.store.save(SUMMARY_KIND, HEAD[:8], {**summary, "selection_sha256": hashlib.sha256(sel.read_bytes()).hexdigest()})
    assert head_admit.preflight_probe(ctx) == f"READY at main {MAIN[:8]}"
    sel.write_text(json.dumps({"python": ["tests/unit/b_test.py"]}))  # the selection changed: a different set was never preflighted
    assert head_admit.preflight_probe(ctx) is None


def test_a_selection_changed_after_the_preflight_step_never_becomes_a_packet(tmp_path):
    """R3b (the reviewer's probe): preflight(A), then sel=B, then the packet step: refused, built neither by preopen
    nor by adopting a packet that was built from B; after a READY preflight of B it goes on."""
    rt, ctx, sel, builds = _setup(tmp_path)
    with native(rt):
        head_admit.preflight(ctx)  # A
        sel.write_text(json.dumps({"python": ["tests/unit/b_test.py"]}))  # B, after the preflight step was done
        with pytest.raises(Refuse) as no:
            head_admit.packet(ctx)
        assert "is not the one the adopted preflight ran with" in no.value.message and "sm head preflight" in no.value.next_cmd
        assert [b for b in builds if "out" in b] == []  # preopen never ran
        out = head_admit.packet_dir(ctx)  # a packet built from B by hand is not adopted either
        out.mkdir(parents=True)
        (out / "wave.json").write_text(json.dumps({"head": HEAD, "main": MAIN, "packet_sha256": "c" * 64}))
        (out / "sel.json").write_text(sel.read_text())
        assert head_admit.packet_probe(ctx) is None
        rt.providers.store.save(SUMMARY_KIND, HEAD[:8], {"head": HEAD, "ready": True, "base": MAIN,
                                                         "selection_sha256": hashlib.sha256(sel.read_bytes()).hexdigest()})
        assert head_admit.packet_probe(ctx) == f"packet {'c' * 12} in {out}"  # B is preflighted now: the packet of B is adopted
        (out / "sel.json").write_text(json.dumps({"python": ["tests/unit/a_test.py"]}))
        assert head_admit.packet_probe(ctx) is None  # a packet of another selection is not


def test_a_selection_edited_while_the_preflight_runs_is_never_preflighted(tmp_path):
    """R3c (the reviewer's probe): A at the start, B written during the run: no READY is bound, no packet is built."""
    rt, ctx, sel, builds = _setup(tmp_path)
    ctx.data.pop("edits").append(json.dumps({"python": ["tests/unit/b_test.py"]}))
    with native(rt):
        with pytest.raises(Refuse) as no:
            head_admit.preflight(ctx)
        assert "the selection changed while the preflight ran" in no.value.message
        assert "preflight_sel_sha256" not in ctx.data
        with pytest.raises(Refuse):
            head_admit.packet(ctx)  # nothing preflighted: the packet step refuses too
        assert [b for b in builds if "out" in b] == []
        head_admit.preflight(ctx)  # B, unchanged during its own run: READY for B
        assert ctx.data["preflight_sel_sha256"] == hashlib.sha256(sel.read_bytes()).hexdigest()


def test_an_edit_reverted_while_preopen_runs_sets_the_packet_aside(tmp_path):
    """S22: preopen copies B, the file is back at A when it ends: the packet's own sel.json is not the preflighted one."""
    rt, ctx, sel, _builds = _setup(tmp_path)
    original = common.OVERRIDE["head packet preopen"]

    def flapping(**kw):
        a = sel.read_text()
        sel.write_text(json.dumps({"python": ["tests/unit/b_test.py"]}))
        built = original(**kw)
        sel.write_text(a)
        return built

    with native(rt):
        head_admit.preflight(ctx)
        common.OVERRIDE["head packet preopen"] = flapping
        with pytest.raises(Refuse, match="changed while the packet was built"):
            head_admit.packet(ctx)
        out = head_admit.packet_dir(ctx)
        assert not out.exists() and out.with_name(out.name + ".failed-1").is_dir()  # set aside, never adopted
