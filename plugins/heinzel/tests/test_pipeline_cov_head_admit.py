"""`sm head admit` step by step: the refusals, waits and adoptions of each step function, called with an engine context.

The composed native commands are replaced through `pipelines.common.OVERRIDE` (each has its own tests); what is
tested here is what the pipeline makes of their results and of the facts on this machine (state store, packet
records, prepared transactions, open PRs).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.engine import Ctx, Refuse, Subject, Wait
from steuermaschine.github.testing import FakeGh
from steuermaschine.native import NativeRuntime, using
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers
from steuermaschine.testing.fakes import FakeVcs
from steuermaschine_heinzel.native.admission_lib.ops import canonical_sha256
from steuermaschine_heinzel.native.preflight import SUMMARY_KIND
from steuermaschine_heinzel.native.queue import QueueEntry, QueueList
from steuermaschine_heinzel.native.ship import HeadResult
from steuermaschine_heinzel.pipelines import common, head_admit
from steuermaschine_heinzel.plugin import plugin

HEAD = "e9c943810ea75cfad155a431a2f168b2a9559828"
OTHER = "f00d943810ea75cfad155a431a2f168b2a955982"
MAIN = "1947731c" + "0" * 32
BRANCH = "fix/update-all"


class BrokenRemote(FakeVcs):
    def remote_head(self, worktree: str, branch: str) -> str | None:
        raise RuntimeError("git ls-remote: could not resolve host")


@pytest.fixture(autouse=True)
def clean_overrides() -> Iterator[None]:
    common.OVERRIDE.clear()
    yield
    common.OVERRIDE.clear()


def runtime(tmp_path: Path, gh: FakeGh | None = None, vcs: FakeVcs | None = None) -> Runtime:
    stack = stack_of(plugin)
    raw = {"heinzel": {"legacy_state_dir": str(tmp_path / "state"), "core_repo_dir": str(tmp_path / "core"), "repo": "o/core"}}
    settings, _ = stack.validate(raw, tmp_path)
    if vcs is None:
        vcs = FakeVcs(head=HEAD, branch=BRANCH, base=MAIN)
        vcs.remote["main"] = MAIN
    project = Project(name="t", plugin="heinzel", root=tmp_path, plugins=["heinzel"], allow_unreviewed=True)
    return Runtime(project, stack, settings, _providers=fake_providers(commands=gh or FakeGh(), vcs=vcs))


def context(rt: Runtime, wt: Path, inputs: dict[str, Any] | None = None, data: dict[str, Any] | None = None) -> Ctx:
    subject = Subject(key=HEAD, label=f"{BRANCH} {HEAD[:9]}", fingerprint={"head": HEAD, "main": MAIN},
                      facts={"worktree": str(wt), "branch": BRANCH, "head": HEAD, "base": MAIN, "main": MAIN})
    return Ctx(providers=rt.providers, settings={"repo": "o/core"}, subject=subject, inputs=dict(inputs or {}), data=dict(data or {}),
               live=True, author="test", say=lambda _text: None)


def native(rt: Runtime) -> using:
    return using(NativeRuntime(rt.providers, rt.settings, rt.project))


# ----------------------------------------------------------------------------- identify
def test_identify_refuses_when_main_on_the_remote_cannot_be_read(tmp_path):
    (tmp_path / "wt").mkdir()
    vcs = BrokenRemote(head=HEAD, branch=BRANCH, base=MAIN)
    with pytest.raises(Refuse, match="cannot read main on the remote: git ls-remote: could not resolve host"):
        head_admit.identify(fake_providers(vcs=vcs), {}, str(tmp_path / "wt"))


def test_identify_refuses_without_an_exact_main_commit_and_binds_head_and_main_otherwise(tmp_path):
    (tmp_path / "wt").mkdir()
    vcs = FakeVcs(head=HEAD, branch=BRANCH, base=MAIN)
    with pytest.raises(Refuse, match="no exact commit"):
        head_admit.identify(fake_providers(vcs=vcs), {}, str(tmp_path / "wt"))
    vcs.remote["main"] = "abc1234"
    with pytest.raises(Refuse, match="a plan binds one main"):
        head_admit.identify(fake_providers(vcs=vcs), {}, str(tmp_path / "wt"))
    vcs.remote["main"] = MAIN
    subject = head_admit.identify(fake_providers(vcs=vcs), {}, str(tmp_path / "wt"))
    assert subject.fingerprint == {"head": HEAD, "main": MAIN} and subject.facts["main"] == MAIN


# ----------------------------------------------------------------------------- preflight
def test_a_stored_ready_preflight_at_this_main_is_adopted(tmp_path):
    rt = runtime(tmp_path)
    ctx = context(rt, tmp_path / "wt")
    assert head_admit.preflight_probe(ctx) is None  # nothing stored yet
    rt.providers.store.save(SUMMARY_KIND, HEAD[:8], {"head": HEAD, "ready": True, "base": OTHER})
    assert head_admit.preflight_probe(ctx) is None  # ready, but at another main
    rt.providers.store.save(SUMMARY_KIND, HEAD[:8], {"head": HEAD, "ready": True, "base": MAIN})
    assert head_admit.preflight_probe(ctx) == f"READY at main {MAIN[:8]}"


def test_a_stored_preflight_that_skipped_checks_is_not_adopted_without_a_reason(tmp_path):
    """Security: `head preflight --skip coverage --skip deps` then admit must not pass checks no reviewer saw."""
    rt = runtime(tmp_path)
    rt.providers.store.save(SUMMARY_KIND, HEAD[:8], {"head": HEAD, "ready": True, "base": MAIN, "skipped": ["coverage", "deps"]})
    ctx = context(rt, tmp_path / "wt")
    assert head_admit.preflight_probe(ctx) is None and "preflight_skipped" not in ctx.data  # the full preflight runs instead


def test_a_reasoned_skip_is_adopted_and_bound_into_the_packet_review(tmp_path):
    import hashlib

    head_admit._DECIDED.clear()
    rt = runtime(tmp_path)
    rt.providers.store.save(SUMMARY_KIND, HEAD[:8], {"head": HEAD, "ready": True, "base": MAIN, "skipped": ["coverage"]})
    ctx = context(rt, tmp_path / "wt", {"skip_reason": "coverage needs the Linux runner; hosted CI measures it"})
    assert head_admit.preflight_probe(ctx) == f"READY at main {MAIN[:8]}"
    assert ctx.data["preflight_skipped"] == {"checks": ["coverage"], "reason": "coverage needs the Linux runner; hosted CI measures it"}
    path = tmp_path / "r" / "packet.json"
    path.parent.mkdir()
    path.write_text('{"head": "x"}')
    packet = hashlib.sha256(path.read_bytes()).hexdigest()
    ctx.data.update(packet_sha256=packet, packet=str(path), packet_dir=str(path.parent), packet_review="/r/REVIEW.md")
    sha, brief, file = head_admit.packet_review(ctx)
    record = path.parent / "preflight-skips.json"
    assert file == str(record) and sha == hashlib.sha256(record.read_bytes()).hexdigest() and sha != packet  # a packet-only verdict does not match
    assert json.loads(record.read_text()) == {"packet_sha256": packet, "preflight_skipped": ["coverage"],
                                              "reason": "coverage needs the Linux runner; hosted CI measures it"}
    assert "LEFT CHECKS OUT (coverage)" in brief and "hosted CI measures it" in brief
    rt.providers.store.save(SUMMARY_KIND, HEAD[:8], {"head": HEAD, "ready": True, "base": MAIN})
    assert head_admit.preflight_probe(ctx) and "preflight_skipped" not in ctx.data  # a later full preflight clears it


def test_a_reasoned_skip_without_a_packet_reviewer_is_not_adopted(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    rt.providers.store.save(SUMMARY_KIND, HEAD[:8], {"head": HEAD, "ready": True, "base": MAIN, "skipped": ["deps"]})
    monkeypatch.setattr(head_admit, "manifest_skip", lambda ctx: "not needed: merge tier")
    ctx = context(rt, tmp_path / "wt", {"skip_reason": "a reason nobody would read"})
    assert head_admit.preflight_probe(ctx) is None


def test_a_stored_preflight_with_hosted_lanes_needs_the_validation_run(tmp_path):
    rt = runtime(tmp_path)
    rt.providers.store.save(SUMMARY_KIND, HEAD[:8], {"head": HEAD, "ready": True, "base": MAIN, "hosted_lanes": ["web-1", "web-2"],
                                                     "next": "sm head validate /wt --lanes web-1,web-2"})
    with pytest.raises(Refuse, match=r"hosted lanes \(web-1, web-2\)") as no:
        head_admit.preflight_probe(context(rt, tmp_path / "wt"))
    assert no.value.next_cmd == "sm head validate /wt --lanes web-1,web-2"
    validated = context(rt, tmp_path / "wt", {"validated": "run 43465502139"})  # free text is no evidence
    with native(rt), pytest.raises(Refuse, match="takes the run ids"):
        head_admit.preflight_probe(validated)


def test_hosted_lanes_without_a_suggested_command_name_head_validate(tmp_path):
    ctx = context(runtime(tmp_path), tmp_path / "wt")
    with pytest.raises(Refuse) as no:
        head_admit._hosted(ctx, ["web-1"], "")
    assert no.value.next_cmd == f"sm head validate {tmp_path / 'wt'}"


# ----------------------------------------------------------------------------- packet
def wave(tmp_path: Path, ctx: Ctx, **record: Any) -> Path:
    directory = head_admit.packet_dir(ctx)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "wave.json").write_text(json.dumps(record))
    return directory


def test_a_packet_built_for_this_head_and_main_is_adopted_and_remembered(tmp_path):
    import hashlib

    rt = runtime(tmp_path)
    sel = tmp_path / "sel.json"
    sel.write_text('{"python": ["tests/unit/a_test.py"], "web": []}')
    ctx = context(rt, tmp_path / "wt", {"review_root": str(tmp_path / "review"), "sel": str(sel)},
                  data={"preflight_sel_sha256": hashlib.sha256(sel.read_bytes()).hexdigest()})  # the preflight step ran with these bytes
    assert head_admit.packet_dir(ctx) == tmp_path / "review" / "fix-update-all" / f"m-{MAIN[:8]}-{HEAD[:8]}"
    assert head_admit.packet_probe(ctx) is None  # no record
    directory = wave(tmp_path, ctx, head=OTHER, main=MAIN, packet_sha256="a" * 64)
    assert head_admit.packet_probe(ctx) is None  # another head
    wave(tmp_path, ctx, head=HEAD, main=MAIN)
    assert head_admit.packet_probe(ctx) is None  # no digest recorded
    wave(tmp_path, ctx, head=HEAD, main=MAIN, packet_sha256="b" * 64)
    assert head_admit.packet_probe(ctx) is None  # no sel.json: not known to be built from this selection
    (directory / "sel.json").write_text('{"python": ["tests/unit/a_test.py"]}')  # preopen's copy, empty lanes dropped
    assert head_admit.packet_probe(ctx) == f"packet {'b' * 12} in {directory}"
    digest = ctx.data["preflight_sel_sha256"]
    assert ctx.data == {"packet_dir": str(directory), "packet": str(directory / "packet.json"), "packet_sha256": "b" * 64,
                        "packet_review": str(directory / "REVIEW.md"), "preflight_sel_sha256": digest, "packet_sel_sha256": digest}


def test_the_packet_review_brief_names_the_packet_and_the_builders_review(tmp_path):
    import hashlib

    path = tmp_path / "r" / "packet.json"
    path.parent.mkdir()
    path.write_text('{"head": "x"}')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()  # the bytes of packet.json, recomputed: what admission binds
    ctx = context(runtime(tmp_path), tmp_path / "wt", data={"packet_sha256": digest, "packet": str(path), "packet_review": "/r/REVIEW.md"})
    sha, brief, file = head_admit.packet_review(ctx)
    assert (sha, file) == (digest, str(path))
    assert str(path) in brief and "/r/REVIEW.md" in brief and MAIN[:12] in brief


def test_a_missing_review_root_is_a_usage_refusal_that_names_the_input(tmp_path):
    ctx = context(runtime(tmp_path), tmp_path / "wt")
    with pytest.raises(Refuse, match="the input 'review_root' is missing") as no:
        head_admit.packet_dir(ctx)
    assert no.value.code == 2 and "--set review_root=VALUE" in no.value.message


# ----------------------------------------------------------------------------- prepare
def test_a_prepared_transaction_for_this_packet_is_adopted(tmp_path):
    rt = runtime(tmp_path)
    directory = tmp_path / "packet"
    directory.mkdir()
    (directory / "packet.json").write_text("{}")
    ctx = context(rt, tmp_path / "wt", data={"packet_dir": str(directory), "packet": str(directory / "packet.json")})
    assert head_admit.prepare_probe(ctx) is None  # no arguments stored
    (directory / "prepare.args.json").write_text("not json")
    assert head_admit.prepare_probe(ctx) is None
    (directory / "prepare.args.json").write_text(json.dumps({"packet": str(directory / "packet.json")}))
    assert head_admit.prepare_probe(ctx) is None  # arguments, but no prepare.out
    doc = {"head": HEAD, "main": MAIN, "slot": "FOCUSED_PLAN_1"}
    (directory / "prepare.out").write_text(json.dumps(doc) + "\n")
    (directory / "prepare.args.json").write_text(json.dumps({"packet": str(tmp_path / "elsewhere" / "packet.json")}))
    assert head_admit.prepare_probe(ctx) is None  # prepared for another packet
    (directory / "prepare.args.json").write_text(json.dumps({"packet": str(directory / "packet.json")}))
    assert head_admit.prepare_probe(ctx) == f"prepared {canonical_sha256(doc)[:12]}"


# ----------------------------------------------------------------------------- PR
def test_the_pr_probe_finds_only_the_pr_open_at_this_exact_head(tmp_path):
    rows = [{"number": 700, "headRefOid": OTHER}, {"number": 812, "headRefOid": HEAD}]
    gh = FakeGh().on(f"--head {BRANCH}", [{"number": 700, "headRefOid": OTHER}], rows)
    ctx = context(runtime(tmp_path, gh), tmp_path / "wt")
    assert head_admit.pr_probe(ctx) is None and "pr" not in ctx.data  # only an older head of the branch is open
    assert head_admit.pr_probe(ctx) == "PR #812 is open at this head" and ctx.data["pr"] == 812


@pytest.mark.parametrize("exit_code, kind, text", [(75, Wait, "sm head pr: main CI is red; wait"), (1, Refuse, "sm head pr refused: push refused; hook failed")])
def test_a_head_pr_that_did_not_open_the_pr_waits_or_refuses(tmp_path, exit_code, kind, text):
    rt = runtime(tmp_path)
    lines = ["main CI is red", "wait"] if exit_code == 75 else ["push refused"]
    common.OVERRIDE["head pr"] = lambda **kw: HeadResult(action="pr", target=kw["bead"], verdict="wait" if exit_code == 75 else "failed", lines=lines,
                                                         errors=[] if exit_code == 75 else ["hook failed"], exit=exit_code)
    ctx = context(rt, tmp_path / "wt", {"bead": "hz-412", "title": "fix(web): update all"})
    with native(rt), pytest.raises(kind, match=text):
        head_admit.pr(ctx)
    assert "pr" not in ctx.data


def test_head_pr_flagged_by_the_diff_guard_is_open_but_not_queued(tmp_path):
    gh = FakeGh().on(f"--head {BRANCH}", [{"number": 812, "headRefOid": HEAD}])
    rt = runtime(tmp_path, gh)
    seen: dict[str, Any] = {}

    def pr(**kw: Any) -> HeadResult:
        seen.update(kw)
        return HeadResult(action="pr", target=kw["bead"], verdict="needs_review", lines=["flagged"], exit=0)

    common.OVERRIDE["head pr"] = pr
    body = tmp_path / "body.md"
    ctx = context(rt, tmp_path / "wt", {"bead": "hz-412", "title": "fix(web): update all", "body_file": str(body)})
    with native(rt):
        assert head_admit.pr(ctx) == "PR #812 open; the diff guard flagged it"
    assert ctx.data["guard"] == "needs_review" and seen["body_file"] == body and seen["reason"] is None and seen["live"] is True


def test_head_pr_needs_bead_and_title(tmp_path):
    rt = runtime(tmp_path)
    with native(rt), pytest.raises(Refuse, match="the input 'title' is missing"):
        head_admit.pr(context(rt, tmp_path / "wt", {"bead": "hz-412"}))


# ----------------------------------------------------------------------------- queue
def test_the_queue_step_needs_a_pr_and_a_review_of_a_flagged_one(tmp_path):
    rt = runtime(tmp_path)
    added: list[dict[str, Any]] = []
    common.OVERRIDE["queue add"] = lambda **kw: added.append(kw) or HeadResult(action="add", target="812", verdict="done")
    no_pr = context(rt, tmp_path / "wt")
    assert head_admit.queue_probe(no_pr) is None
    with native(rt), pytest.raises(Refuse, match="no PR is known for this head") as no:
        head_admit.queue(no_pr)
    assert no.value.next_cmd == f"sm head admit {tmp_path / 'wt'}"

    flagged = context(rt, tmp_path / "wt", data={"pr": 812, "guard": "needs_review"})
    with native(rt), pytest.raises(Refuse, match="the diff guard flagged PR #812") as no:
        head_admit.queue(flagged)
    assert no.value.next_cmd == "sm head guard 812" and added == []

    reviewed = context(rt, tmp_path / "wt", {"reviewed": "review: /r/REVIEW.md", "bead": "hz-412"}, {"pr": 812, "guard": "needs_review"})
    with native(rt):
        assert head_admit.queue(reviewed) == f"PR #812 queued at {HEAD[:12]}"
    assert added == [{"pr": ["812"], "bead": "hz-412", "reviewed": "review: /r/REVIEW.md", "reviewed_head": HEAD}]


def test_the_queue_probe_adopts_a_pr_already_in_the_queue(tmp_path):
    rt = runtime(tmp_path)
    common.OVERRIDE["queue list"] = lambda **kw: QueueList(entries=[QueueEntry(key="core#700", repo="core", pr="700", status="queued"),
                                                                    QueueEntry(key="core#812", repo="core", pr="812", status="merging")])
    with native(rt):
        assert head_admit.queue_probe(context(rt, tmp_path / "wt", data={"pr": 812})) == "PR #812 is in the queue (merging)"
        assert head_admit.queue_probe(context(rt, tmp_path / "wt", data={"pr": 900})) is None
