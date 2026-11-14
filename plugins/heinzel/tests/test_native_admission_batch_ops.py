"""The native batch form: `sm head prepare --packets ... --core-repos ... --out DIR` (apply of a batch is refused, see test_native_admission_apply_refuses_batch.py).

The batch logic itself (`prepare_batch`, `apply_batch`, `batch_guard`) is the moved code, tested by
test_native_admission_batch.py; this file tests the typed layer over it (ops.py) and the commands.
"""
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel.native.admission_lib import focused_admit as a
from steuermaschine_heinzel.native.admission_lib import focused_slots as s
from steuermaschine_heinzel.native.admission_lib import ops
from steuermaschine_heinzel.plugin import plugin

HEADS = ("a" * 40, "c" * 40)
MAIN = "b" * 40


def runtime(allow_unreviewed=False):
    stack = stack_of(plugin)
    settings, _ = stack.validate({}, Path("."))
    project = Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=allow_unreviewed)
    return Runtime(project, stack, settings, _providers=fake_providers())


class Untouched:
    def __getattr__(self, name):
        raise AssertionError(f"the runtime was used ({name}) before the refusal")


def world(tmp_path, **changes):
    packets, cores = [], []
    for index, _head in enumerate(HEADS):
        packet = tmp_path / f"m{index}" / "packet.json"
        packet.parent.mkdir()
        packet.write_text(json.dumps({"n": index}))
        packets.append(packet)
        cores.append(tmp_path / f"core{index}")
    receipts = [{"kind": "stuck-queued-pr-run", "run": 7}]
    nonconsumers = tmp_path / "nonconsumers.json"
    nonconsumers.write_text(json.dumps({"receipts": receipts}))
    prepared = {"schema": 1, "mode": s.BATCH_MODE, "repository": "o/r", "main": MAIN, "slot": s.NAMES[1], "nonconsumers": receipts,
                "members": [{"head": head, "packet_sha256": hashlib.sha256(packet.read_bytes()).hexdigest(), "pr": pr}
                            for head, packet, pr in zip(HEADS, packets, (745, None), strict=True)]}
    out = tmp_path / "batch"
    out.mkdir()
    (out / ops.PREPARED_FILE).write_text(json.dumps(prepared))
    args = {"schema": 1, "mode": s.BATCH_MODE, "sha256": ops.canonical_sha256(prepared), "packets": [str(p.resolve()) for p in packets],
            "core_repos": [str(c.resolve()) for c in cores], "prs": [745, None], "review_root": str(tmp_path.resolve()),
            "nonconsumers": str(nonconsumers.resolve())} | changes
    (out / ops.ARGS_FILE).write_text(json.dumps(args))
    return out, prepared, args, packets, cores


def heads_by_core(monkeypatch, cores, heads=HEADS):
    table = {str(core.resolve()): head for core, head in zip(cores, heads, strict=True)}
    monkeypatch.setattr(a, "_run", lambda argv, cwd=None, **kw: table[str(Path(cwd).resolve())].encode())


# ----------------------------------------------------------------------------- prepare
def test_prepare_batch_stores_transaction_and_arguments_and_sends_no_probe(tmp_path, monkeypatch):
    out, prepared, args, packets, cores = world(tmp_path)
    (out / ops.PREPARED_FILE).unlink()
    (out / ops.ARGS_FILE).unlink()
    seen = {}

    def fake(rt, entries, review_root, receipts, *, probe=True):
        seen.update(entries=entries, receipts=receipts, probe=probe, rt=rt)
        return prepared, [], None, [], {}

    monkeypatch.setattr(s, "prepare_batch", fake)
    result = ops.prepare_batch(packets, cores, tmp_path, out, prs=[745, 0], nonconsumers=Path(args["nonconsumers"]), runtime="RT")
    assert seen["probe"] is False and seen["rt"] == "RT" and seen["receipts"] == prepared["nonconsumers"]
    assert seen["entries"] == [(packets[0], cores[0], 745), (packets[1], cores[1], None)]  # 0 means pre-open
    assert json.loads((out / ops.ARGS_FILE).read_text()) == args
    assert (out / ops.PREPARED_FILE).read_bytes() == a.canonical(prepared) + b"\n"
    assert result.sha256 == args["sha256"] and [m.head for m in result.members] == list(HEADS)
    assert ops.FIRST_MERGE_ONLY in result.render() and "FIRST merge only" in ops.FIRST_MERGE_ONLY and "turns main red" in ops.FIRST_MERGE_ONLY


@pytest.mark.parametrize(("cores", "prs"), [(1, None), (2, [1]), (2, [1, -1]), (3, None)])
def test_prepare_batch_needs_one_checkout_and_one_pr_number_per_packet(tmp_path, cores, prs):
    with pytest.raises(a.AdmissionError, match="one --core-repos checkout and one --prs number"):
        ops.prepare_batch([tmp_path / "a", tmp_path / "b"], [tmp_path / f"c{i}" for i in range(cores)], tmp_path, tmp_path / "out", prs=prs, runtime=Untouched())
    assert not (tmp_path / "out").exists()


def test_moved_prepare_batch_passes_probe_to_its_guard():
    source = inspect.getsource(s.prepare_batch)
    assert "def prepare_batch(rt, entries, review_root, nonconsumers=(), *, probe=True):" in source
    assert "batch_guard(rt, members, desired, proofs, paused=False, probe=probe)" in source


# ----------------------------------------------------------------------------- apply
@pytest.mark.parametrize(("changes", "message"), [
    ({"mode": "x"}, "not a batch transaction"),
    ({"schema": 2}, "not a batch transaction"),
    ({"sha256": "0" * 64}, "another prepared transaction"),
    ({"prs": [745]}, "member count"),
    ({"prs": [745, 9]}, "pr of a member"),
    ({"prs": [None, 745]}, "pr of a member"),
    ({"nonconsumers": None}, "nonconsumer receipts"),
])
def test_apply_batch_refuses_changed_stored_arguments_before_anything_runs(tmp_path, changes, message):
    out, prepared, _, _, _ = world(tmp_path, **changes)
    with pytest.raises(a.AdmissionError, match=message):
        ops.apply_batch(out, ops.canonical_sha256(prepared), runtime=Untouched())


def test_apply_batch_refuses_swapped_or_changed_packets_a_wrong_digest_and_missing_arguments(tmp_path):
    out, prepared, args, packets, _ = world(tmp_path)
    sha = ops.canonical_sha256(prepared)
    (out / ops.ARGS_FILE).write_text(json.dumps(args | {"packets": args["packets"][::-1]}))
    with pytest.raises(a.AdmissionError, match="packet bytes of a member"):
        ops.apply_batch(out, sha, runtime=Untouched())
    (out / ops.ARGS_FILE).write_text(json.dumps(args))
    packets[1].write_text("{}")
    with pytest.raises(a.AdmissionError, match="packet bytes of a member"):
        ops.apply_batch(out, sha, runtime=Untouched())
    for bad, message in (("nope", "apply needs --approved"), ("0" * 64, "not the digest of the stored prepared transaction")):
        with pytest.raises(a.AdmissionError, match=message):
            ops.apply_batch(out, bad, runtime=Untouched())
    (out / ops.ARGS_FILE).unlink()
    with pytest.raises(a.AdmissionError, match="sm head prepare --packets"):
        ops.apply_batch(out, sha, runtime=Untouched())


def test_apply_batch_refuses_a_single_packet_transaction(tmp_path):
    out, prepared, _, _, _ = world(tmp_path)
    single = {k: v for k, v in prepared.items() if k not in ("mode", "members")} | {"head": HEADS[0], "pr": None}
    (out / ops.PREPARED_FILE).write_text(json.dumps(single))
    with pytest.raises(a.AdmissionError, match="not a batch transaction"):
        ops.apply_batch(out, ops.canonical_sha256(single), runtime=Untouched())


def test_apply_batch_checks_every_members_worktree_head(tmp_path, monkeypatch):
    out, prepared, _, _, cores = world(tmp_path)
    heads_by_core(monkeypatch, cores, (HEADS[0], "d" * 40))
    with pytest.raises(a.AdmissionError) as refused:
        ops.apply_batch(out, ops.canonical_sha256(prepared), runtime=Untouched())
    assert str(cores[1].resolve()) in str(refused.value) and f"worktree add -b admit/{HEADS[1][:12]}" in str(refused.value)


def test_apply_batch_recomputes_compares_and_applies_once(tmp_path, monkeypatch):
    out, prepared, args, _, cores = world(tmp_path)
    heads_by_core(monkeypatch, cores)
    calls = []

    def fake_prepare(rt, entries, review_root, receipts, **kwargs):
        calls.append(("prepare", [(str(p), str(c), pr) for p, c, pr in entries], str(review_root), receipts, kwargs))
        return dict(prepared), ["members"], None, [], {"proofs": 1}

    def fake_apply(rt, prepared_, members, source, proofs, approval):
        calls.append(("apply", members, proofs, approval))
        return tmp_path / "receipt.json"

    monkeypatch.setattr(s, "prepare_batch", fake_prepare)
    monkeypatch.setattr(s, "apply_batch", fake_apply)
    result = ops.apply_batch(out, args["sha256"], runtime="RT")
    assert calls[0] == ("prepare", list(zip(args["packets"], args["core_repos"], args["prs"], strict=True)), args["review_root"], prepared["nonconsumers"], {})
    assert calls[1] == ("apply", ["members"], {"proofs": 1}, args["sha256"])  # re-prepare with the moved default: the probe is sent
    assert result.receipt_path == str(tmp_path / "receipt.json") and result.head == HEADS[0]
    monkeypatch.setattr(s, "prepare_batch", lambda *x, **k: (prepared | {"slot": s.NAMES[2]}, [], None, [], {}))
    monkeypatch.setattr(s, "apply_batch", lambda *x: pytest.fail("the write path was reached"))
    with pytest.raises(a.AdmissionError, match="prepared transaction changed; reprepare and review"):
        ops.apply_batch(out, args["sha256"], runtime="RT")


# ----------------------------------------------------------------------------- the rule of 2026-11-12
def test_first_merge_only(tmp_path):
    _, prepared, _, _, _ = world(tmp_path)
    ops.first_merge_only(prepared, MAIN)
    with pytest.raises(a.AdmissionError) as refused:
        ops.first_merge_only(prepared, "e" * 40)
    text = str(refused.value)
    assert "main moved (eeeeeeee)" in text and "remaining plans are void" in text and "sm head packet bind" in text and "Do not merge another member" in text
    with pytest.raises(a.AdmissionError, match="not a batch transaction"):
        ops.first_merge_only({"main": MAIN}, MAIN)


def test_first_merge_only_holds_for_the_recorded_batch():
    recorded = json.loads((Path(__file__).parent / "fixtures/admission/recorded/batch1/prepare.out").read_text())
    ops.first_merge_only(recorded, recorded["main"])
    with pytest.raises(a.AdmissionError, match="main moved"):
        ops.first_merge_only(recorded, recorded["members"][0]["head"])


# ----------------------------------------------------------------------------- the commands
def test_command_prepare_batch_text_and_json(tmp_path, monkeypatch):
    out, prepared, args, packets, cores = world(tmp_path)
    monkeypatch.setattr(s, "prepare_batch", lambda rt, entries, root, receipts, *, probe=True: (prepared, [], None, [], {}))
    monkeypatch.setattr(s, "Runtime", lambda core: "RT")
    argv = ["head", "prepare", "--packets", str(packets[0]), "--packets", str(packets[1]), "--core-repos", str(cores[0]), "--core-repos", str(cores[1]),
            "--prs", "745", "--prs", "0", "--review-root", str(tmp_path), "--out", str(out), "--nonconsumers", args["nonconsumers"]]
    text = invoke(runtime(True), argv)
    assert text.exit_code == 0 and f"sha256 {args['sha256']}" in text.stdout and "FIRST merge only" in text.stdout
    doc = invoke(runtime(True), ["--format", "json", *argv])
    assert ops.BatchPrepareResult.model_validate(json.loads(doc.stdout)).sha256 == args["sha256"]


@pytest.mark.parametrize(("extra", "message"), [
    (["--pr", "5"], "stay single-packet"), (["--resulting-main-only"], "stay single-packet"), (["--reviewed-protected-producer"], "stay single-packet"),
    (["--core-repo", "x"], "stay single-packet"), (["PACKET.json"], "stay single-packet"), ([], "batch needs --core-repos"),
])
def test_command_batch_excludes_the_single_packet_options(tmp_path, extra, message):
    done = invoke(runtime(True), ["head", "prepare", *extra, "--packets", "a.json", "--packets", "b.json", "--review-root", str(tmp_path)])
    assert done.exit_code == 1 and message in (done.stdout + done.stderr)


def test_command_single_packet_refuses_batch_options(tmp_path):
    for extra in (["--core-repos", "x"], ["--prs", "1"], ["--out", str(tmp_path)]):
        done = invoke(runtime(True), ["head", "prepare", "p.json", "--core-repo", str(tmp_path), "--review-root", str(tmp_path), *extra])
        assert done.exit_code == 1 and "single-packet mode needs PACKET and --core-repo" in (done.stdout + done.stderr)
