"""`sm head admit` takes the protected-producer route by itself, and leaves nothing out against the hand route.

A head whose packet pins a focused-CI source at another blob than main's is prepared with
`--reviewed-protected-producer` only when an independent verdict approves the record that names the changed sources;
prepare.out then gets its own verdict. The ordinary route is unchanged: no new argument reaches `head prepare`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from test_pipeline_admit_cut import HEAD, MAIN, AdmitFakes, admit, inputs, runtime, sha_of, verdict
from test_pipeline_cov_head_admit import context
from test_pipeline_cov_head_admit import native as native_rt
from test_pipeline_cov_head_admit import runtime as plain_runtime

from steuermaschine import verdicts
from steuermaschine.engine import Refuse
from steuermaschine.github.testing import FakeGh
from steuermaschine_heinzel.native.admission_lib.ops import PrepareResult, canonical_sha256
from steuermaschine_heinzel.pipelines import common, head_admit

CI, MAKE = ".github/workflows/ci.yml", "Makefile"
OLD, NEW, SAME = "1" * 40, "2" * 40, "3" * 40


@pytest.fixture(autouse=True)
def clean():
    common.OVERRIDE.clear()
    head_admit._DECIDED.clear()
    yield
    common.OVERRIDE.clear()
    head_admit._DECIDED.clear()


def ls_tree(pins: dict[str, str]) -> str:
    return "".join(f"100644 blob {blob}\t{path}\0" for path, blob in pins.items())


def gh(at_main: dict[str, str] | int) -> FakeGh:
    """Main pins CI and the Makefile at these blobs (git ls-tree of the worktree); every other read has no answer."""
    return FakeGh().on(f"ls-tree -r -z {MAIN}", at_main if isinstance(at_main, int) else ls_tree(at_main)).on("--head fix/update-all", [], [{"number": 812, "headRefOid": HEAD}])


class ProtectedFakes(AdmitFakes):
    """As AdmitFakes, with a packet that pins the execution sources and a prepare that records the route it was asked for."""

    def __init__(self, tmp_path: Path, execution: dict[str, str]):
        super().__init__(tmp_path)
        self.execution, self.prepared_with = execution, []

    def preopen(self, **kw: Any):
        built = super().preopen(**kw)
        out = Path(kw["out"])
        (out / "packet.json").write_text(json.dumps({"manifest": {"head": kw["branch"], "base": kw["main"], "execution": self.execution}}))
        record = json.loads((out / "wave.json").read_text()) | {"packet_sha256": sha_of(out / "packet.json")}
        (out / "wave.json").write_text(json.dumps(record))
        return built.model_copy(update={"packet_sha256": record["packet_sha256"]})

    def prepare(self, **kw: Any):
        protected = kw.pop("reviewed_protected_producer", None)
        self.prepared_with.append(protected)
        done = super().prepare(**kw)
        doc = {**done.prepared, "protected_producer": bool(protected)}
        Path(done.prepared_path).write_text(json.dumps(doc) + "\n")
        Path(done.args_path).write_text(json.dumps({"packet": str(Path(kw["packet"]).resolve()), "protected": bool(protected)}))
        return PrepareResult(sha256=canonical_sha256(doc), head=done.head, main=done.main, slot=done.slot, pr=done.pr,
                             prepared_path=done.prepared_path, args_path=done.args_path, prepared=doc)


def start(tmp_path: Path, execution: dict[str, str], at_main: dict[str, str] | int):
    rt, wt = runtime(tmp_path, gh(at_main)), tmp_path / "wt"
    wt.mkdir()
    return rt, wt, ProtectedFakes(tmp_path, execution)


# ----------------------------------------------------------------------------- the whole route through the engine
def test_a_head_that_changes_a_protected_source_takes_the_route_by_itself_with_two_separate_verdicts(tmp_path):
    rt, wt, fakes = start(tmp_path, {CI: NEW, MAKE: SAME}, {CI: OLD, MAKE: SAME})
    first = admit(rt, wt, *inputs(tmp_path))
    review = first["review"]
    record = Path(review["file"])
    assert first["status"] == "review" and review["step"] == "packet review" and record.name == "protected-producer.json"
    packet = record.parent / "packet.json"
    assert json.loads(record.read_text()) == {"packet_sha256": sha_of(packet), "protected_producer": [CI]}  # the Makefile is unchanged
    assert review["sha256"] == sha_of(record) != sha_of(packet)
    brief = rt.providers.agents.briefs[review["brief"]]
    assert f"CHANGES PROTECTED focused-CI sources ({CI})" in brief and "--reviewed-protected-producer" in brief and str(record) in brief

    verdict(rt, review["sha256"])
    second = admit(rt, wt, "--approved", review["sha256"])
    stop = second["review"]
    assert second["status"] == "review" and stop["step"] == "apply review", second
    assert fakes.prepared_with == [True]  # the route was taken without a hand command
    prepared = json.loads(Path(stop["file"]).read_text())
    assert prepared["protected_producer"] is True and stop["sha256"] == canonical_sha256(prepared) != review["sha256"]  # a separate verdict
    assert "PROTECTED-PRODUCER ROUTE" in rt.providers.agents.briefs[stop["brief"]] and CI in rt.providers.agents.briefs[stop["brief"]]
    refused = admit(rt, wt, "--approved", stop["sha256"], "--live")
    assert refused["status"] == "refused" and "no verdict is recorded" in refused["phase"]  # the packet verdict does not cover prepare.out
    assert "head apply" not in [c[0] for c in fakes.calls]

    verdict(rt, stop["sha256"])
    done = admit(rt, wt, "--approved", stop["sha256"], "--live")
    assert done["status"] == "finished", done
    assert ("head apply", stop["sha256"]) in fakes.calls


def test_a_verdict_on_the_packet_alone_does_not_open_the_route(tmp_path):
    rt, wt, fakes = start(tmp_path, {CI: NEW}, {CI: OLD})
    first = admit(rt, wt, *inputs(tmp_path))
    packet = sha_of(Path(first["review"]["file"]).parent / "packet.json")
    refused = admit(rt, wt, "--approved", packet)
    assert refused["status"] == "refused" and "is not the digest of the bytes under review" in refused["phase"]
    assert fakes.prepared_with == []


def test_the_ordinary_route_is_unchanged_and_passes_no_new_argument(tmp_path):
    rt, wt, fakes = start(tmp_path, {CI: SAME, MAKE: SAME}, {CI: SAME, MAKE: SAME})
    first = admit(rt, wt, *inputs(tmp_path))
    assert first["review"]["file"].endswith("packet.json") and first["review"]["sha256"] == sha_of(Path(first["review"]["file"]))
    verdict(rt, first["review"]["sha256"])
    second = admit(rt, wt, "--approved", first["review"]["sha256"])
    assert second["review"]["step"] == "apply review" and fakes.prepared_with == [None]  # the keyword is not passed at all
    assert "PROTECTED-PRODUCER" not in rt.providers.agents.briefs[second["review"]["brief"]]
    assert not (Path(first["review"]["file"]).parent / head_admit.PROTECTED_RECORD).exists()


# ----------------------------------------------------------------------------- the step functions
def setup(tmp_path: Path, execution: dict[str, str], at_main: dict[str, str] | int, data: dict[str, Any] | None = None):
    rt = plain_runtime(tmp_path, gh=gh(at_main))
    directory = tmp_path / "packet"
    directory.mkdir()
    packet = directory / "packet.json"
    packet.write_text(json.dumps({"manifest": {"execution": execution}}))
    ctx = context(rt, tmp_path / "wt", {"review_root": str(tmp_path)},
                  {"packet_dir": str(directory), "packet": str(packet), "packet_sha256": sha_of(packet), "packet_review": "REVIEW.md", **(data or {})})
    return rt, ctx, directory


def approve(rt: Any, digest: str, reviewer: str = "reviewer-1", kind: str = "approve") -> None:
    rt.providers.store.put_receipt("verdicts", digest, {"digest": digest, "verdict": kind, "reviewer": reviewer})
    verdicts.record_check(rt.providers.store, HEAD, {"base": MAIN})  # the packet stop binds the head: its cheap checks ran green


def test_the_changed_sources_are_the_pins_that_differ_from_main_and_a_new_pin(tmp_path):
    _rt, ctx, _dir = setup(tmp_path, {CI: NEW, MAKE: SAME, "tests/_new.py": NEW}, {CI: OLD, MAKE: SAME})
    assert head_admit.protected_producer(ctx) == [CI, "tests/_new.py"]
    assert f"ls-tree -r -z {MAIN} -- {CI} {MAKE} tests/_new.py" in ctx.providers.commands.lines()[-1]  # main's exact commit, the packet's paths


@pytest.mark.parametrize("packet", ["not json", "{}", '{"manifest": {}}', '{"manifest": {"execution": []}}', '{"manifest": {"execution": {"a": 1}}}'])
def test_a_packet_without_readable_pins_is_the_ordinary_route_and_reads_nothing(tmp_path, packet):
    _rt, ctx, directory = setup(tmp_path, {}, 128)
    (directory / "packet.json").write_text(packet)
    assert head_admit.protected_producer(ctx) == [] and ctx.providers.commands.lines() == []


def test_unreadable_pins_of_main_refuse_instead_of_guessing_a_route(tmp_path):
    _rt, ctx, _dir = setup(tmp_path, {CI: NEW}, 128)
    with pytest.raises(Refuse, match="whether this head changes one is unknown"):
        head_admit.protected_producer(ctx)


def test_the_record_also_binds_a_preflight_skip(tmp_path):
    skips = {"checks": ["coverage"], "reason": "hosted CI measures it"}
    _rt, ctx, directory = setup(tmp_path, {CI: NEW}, {CI: OLD}, {"preflight_skipped": skips})
    sha, brief, file = head_admit.packet_review(ctx)
    record = directory / head_admit.PROTECTED_RECORD
    assert file == str(record) and sha == sha_of(record)
    assert json.loads(record.read_text()) == {"packet_sha256": sha_of(directory / "packet.json"), "protected_producer": [CI],
                                              "preflight_skipped": ["coverage"], "reason": "hosted CI measures it"}
    assert "LEFT CHECKS OUT (coverage)" in brief and "CHANGES PROTECTED" in brief
    assert not (directory / "preflight-skips.json").exists()  # one record, one digest


def prepare_with(seen: list[Any]):
    def prepare(**kw: Any):
        seen.append(kw.get("reviewed_protected_producer", "absent"))
        return PrepareResult(sha256="d" * 64, head=HEAD, main=MAIN, slot="s", pr=None, prepared_path="p", args_path="a", prepared={})
    return prepare


def test_prepare_takes_the_route_only_on_an_independent_approve_of_exactly_these_sources(tmp_path):
    rt, ctx, directory = setup(tmp_path, {CI: NEW}, {CI: OLD})
    seen: list[Any] = []
    common.OVERRIDE["head prepare"] = prepare_with(seen)
    with native_rt(rt):
        with pytest.raises(Refuse, match="the protected-producer route stays closed"):  # no record, no verdict
            head_admit.prepare(ctx)
        digest, _brief, _file = head_admit.packet_review(ctx)
        with pytest.raises(Refuse, match="stays closed"):  # the record alone is not a review
            head_admit.prepare(ctx)
        approve(rt, sha_of(directory / "packet.json"))
        with pytest.raises(Refuse, match="A verdict on packet\\.json alone does not open it"):
            head_admit.prepare(ctx)
        assert seen == []
        approve(rt, digest)
        assert head_admit.prepare(ctx).endswith("on the protected-producer route") and seen == [True]
        (directory / head_admit.PROTECTED_RECORD).write_text(json.dumps({"packet_sha256": "0" * 64, "protected_producer": [CI]}))
        with pytest.raises(Refuse, match="stays closed"):  # the reviewed bytes are no longer the ones on disk
            head_admit.prepare(ctx)


@pytest.mark.parametrize(("reviewer", "kind"), [("test", "approve"), ("reviewer-1", "reject")])
def test_a_reject_or_the_drivers_own_verdict_keeps_the_route_closed(tmp_path, reviewer, kind):
    rt, ctx, _dir = setup(tmp_path, {CI: NEW}, {CI: OLD})
    seen: list[Any] = []
    common.OVERRIDE["head prepare"] = prepare_with(seen)
    with native_rt(rt):
        digest, _brief, _file = head_admit.packet_review(ctx)
        approve(rt, digest, reviewer, kind)  # the context's author is "test"
        with pytest.raises(Refuse, match="stays closed"):
            head_admit.prepare(ctx)
    assert seen == []


def test_a_verdict_for_fewer_sources_does_not_cover_a_later_wider_change(tmp_path):
    rt, ctx, directory = setup(tmp_path, {CI: NEW, MAKE: SAME}, {CI: OLD, MAKE: SAME})
    digest, _brief, _file = head_admit.packet_review(ctx)
    approve(rt, digest)
    assert head_admit.reviewed_protected(ctx) == [CI]
    ctx.providers.commands.on(f"ls-tree -r -z {MAIN}", ls_tree({CI: OLD, MAKE: OLD}))  # main as another reader would see it: two differ
    with pytest.raises(Refuse, match=f"{CI}, {MAKE}"):
        head_admit.reviewed_protected(ctx)
    assert directory.joinpath(head_admit.PROTECTED_RECORD).exists()


def stored(directory: Path, protected: bool, flag: Any = "same") -> dict[str, Any]:
    doc = {"head": HEAD, "main": MAIN, "slot": "S", "packet_sha256": sha_of(directory / "packet.json"),
           "protected_producer": protected if flag == "same" else flag}
    (directory / "prepare.out").write_text(json.dumps(doc) + "\n")
    (directory / "prepare.args.json").write_text(json.dumps({"packet": str(directory / "packet.json"), "protected": protected}))
    return doc


def test_a_prepared_transaction_of_the_other_route_is_not_adopted(tmp_path):
    rt, ctx, directory = setup(tmp_path, {CI: NEW}, {CI: OLD})
    doc = stored(directory, protected=True)
    assert head_admit.prepare_probe(ctx) is None  # no verdict names the sources yet
    digest, _brief, _file = head_admit.packet_review(ctx)
    approve(rt, digest)
    assert head_admit.prepare_probe(ctx) == f"prepared {canonical_sha256(doc)[:12]} on the protected-producer route"
    stored(directory, protected=False)
    assert head_admit.prepare_probe(ctx) is None  # prepared without the route: prepare runs again


def test_an_ordinary_head_does_not_adopt_a_transaction_prepared_on_the_route(tmp_path):
    _rt, ctx, directory = setup(tmp_path, {CI: SAME}, {CI: SAME})
    stored(directory, protected=True)
    assert head_admit.prepare_probe(ctx) is None
    doc = stored(directory, protected=False)
    assert head_admit.prepare_probe(ctx) == f"prepared {canonical_sha256(doc)[:12]}"


@pytest.mark.parametrize(("execution", "flag", "text"), [
    ({CI: NEW}, False, "changes protected sources"),
    ({CI: NEW}, None, "changes protected sources"),
    ({CI: SAME}, True, "changes no protected source"),
])
def test_the_apply_review_and_the_apply_refuse_a_transaction_on_the_wrong_route(tmp_path, execution, flag, text):
    rt, ctx, directory = setup(tmp_path, execution, {CI: OLD if execution[CI] == NEW else SAME})
    if execution[CI] == NEW:
        approve(rt, head_admit.packet_review(ctx)[0])
    stored(directory, protected=bool(flag), flag=flag)
    common.OVERRIDE["head apply"] = lambda **_kw: pytest.fail("apply must not run")
    with pytest.raises(Refuse, match=text):
        head_admit.apply_review(ctx)
    ctx.data["apply_sha256"] = "d" * 64
    with native_rt(rt), pytest.raises(Refuse, match="prepare again"):
        head_admit.apply(ctx)


def test_an_unreadable_prepared_transaction_refuses(tmp_path):
    _rt, ctx, directory = setup(tmp_path, {CI: SAME}, {CI: SAME})
    (directory / "prepare.out").write_text("[1]")
    with pytest.raises(Refuse, match="cannot read the prepared transaction"):
        head_admit._same_route(ctx, directory / "prepare.out")


def test_the_record_digest_is_of_its_exact_bytes(tmp_path):
    text = head_admit._protected_bytes("a" * 64, [CI], None)
    assert text.endswith("\n") and json.loads(text) == {"packet_sha256": "a" * 64, "protected_producer": [CI]}
    assert hashlib.sha256(text.encode()).hexdigest() != hashlib.sha256(head_admit._protected_bytes("a" * 64, [CI, MAKE], None).encode()).hexdigest()


def test_an_approve_without_the_heads_check_receipt_keeps_the_route_closed(tmp_path):
    rt, ctx, _dir = setup(tmp_path, {CI: NEW}, {CI: OLD})
    digest, _brief, _file = head_admit.packet_review(ctx)
    verdicts.need_check(rt.providers.store, digest, HEAD, "sm check WT", base=MAIN)
    rt.providers.store.put_receipt("verdicts", digest, {"digest": digest, "verdict": "approve", "reviewer": "reviewer-1"})
    with pytest.raises(Refuse, match="no check receipt exists for exactly that commit"):
        head_admit.reviewed_protected(ctx)
    verdicts.record_check(rt.providers.store, HEAD, {"base": MAIN})
    assert head_admit.reviewed_protected(ctx) == [CI]
