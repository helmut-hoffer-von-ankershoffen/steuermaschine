"""lftt: ghost queued runs (GitHub can neither cancel nor delete them) block every prepare. `sm head admit` takes a
reviewed receipts file as the input `nonconsumers` and passes it to prepare (apply reads it back from
prepare.args.json), and a prepare that waits for or refuses active runs names the reviewed receipts file in
admission_nonconsumers_dir that covers all of them."""

import json
import os
from pathlib import Path

import pytest
from test_native_admission_nonconsumers_setting import prepare_argv, runtime
from test_pipeline_cov_head_admit import context
from test_pipeline_cov_head_admit import runtime as admit_runtime

from steuermaschine.testing import invoke
from steuermaschine_heinzel.native.admission_lib import focused_admit as a
from steuermaschine_heinzel.native.admission_lib import ops
from steuermaschine_heinzel.pipelines import common, head_admit


def run(rid):
    return ops.BlockingRun(id=rid, status="queued", event="pull_request", head_branch="x", path=".github/workflows/ci.yml", head_sha="a" * 40)


def receipts(path: Path, *runs: int, age: int = 0) -> Path:
    path.write_text(json.dumps({"receipts": [{"run": rid, "kind": "stuck-queued-pr-run"} for rid in runs]}))
    os.utime(path, (1_800_000_000 - age, 1_800_000_000 - age))
    return path


@pytest.fixture
def ghosts(monkeypatch):
    """Runs 7 and 8 are active and nobody's: every prepare refuses over them."""
    blocking = [run(7), run(8)]
    monkeypatch.setattr(ops, "blockers", lambda core, nonconsumers=None, runtime=None: ops.BlockersResult(blocking=blocking))

    def refuse(*args, **kwargs):
        raise a.AdmissionError("unknown active Actions run; admission refused: 7 queued x (pull_request)")

    monkeypatch.setattr(ops, "prepare", refuse)
    return blocking


def test_a_refusal_names_the_newest_reviewed_file_that_covers_every_blocking_run(tmp_path, ghosts):
    folder = tmp_path / "receipts"
    folder.mkdir()
    receipts(folder / "partial.json", 7)
    receipts(folder / "old.json", 7, 8, age=500)
    newest = receipts(folder / "new.json", 7, 8, 9, age=10)
    (folder / "broken.json").write_text("{")
    done = invoke(runtime({"admission_nonconsumers_dir": str(folder)}), prepare_argv(tmp_path))
    assert done.exit_code == 1 and f"the reviewed receipts file {newest} covers every blocking run (7, 8)" in done.stderr
    assert f"--nonconsumers {newest} (sm head admit: --set nonconsumers={newest})" in done.stderr


def test_the_file_already_in_use_is_not_named_again_and_nothing_found_keeps_the_general_hint(tmp_path, ghosts):
    folder = tmp_path / "receipts"
    folder.mkdir()
    used = receipts(folder / "used.json", 7, 8)
    done = invoke(runtime({"admission_nonconsumers_dir": str(folder)}), prepare_argv(tmp_path, "--nonconsumers", str(used)))
    assert done.exit_code == 1 and "covers every blocking run" not in done.stderr and "--nonconsumers FILE or the setting" in done.stderr
    assert "covers every" not in invoke(runtime(), prepare_argv(tmp_path)).stderr  # no directory set: no search


def test_the_wait_names_the_covering_file(tmp_path, ghosts, monkeypatch):
    folder = tmp_path / "receipts"
    folder.mkdir()
    cover = receipts(folder / "ghosts.json", 7, 8)
    done = invoke(runtime({"admission_nonconsumers_dir": str(folder), "admission_wait_max_minutes": 0}), prepare_argv(tmp_path, "--wait"))
    assert done.exit_code == 75 and f"the reviewed receipts file {cover} covers every blocking run (7, 8)" in done.stderr


def test_unreadable_blockers_or_none_at_all_name_no_file(tmp_path, ghosts, monkeypatch):
    folder = tmp_path / "receipts"
    folder.mkdir()
    receipts(folder / "ghosts.json", 7, 8)

    def unreadable(core, nonconsumers=None, runtime=None):
        raise a.AdmissionError("truncated inventory")

    monkeypatch.setattr(ops, "blockers", unreadable)
    assert "covers every" not in invoke(runtime({"admission_nonconsumers_dir": str(folder)}), prepare_argv(tmp_path)).stderr
    monkeypatch.setattr(ops, "blockers", lambda core, nonconsumers=None, runtime=None: ops.BlockersResult(blocking=[]))
    assert "covers every" not in invoke(runtime({"admission_nonconsumers_dir": str(folder)}), prepare_argv(tmp_path)).stderr


def test_head_admit_passes_the_nonconsumers_input_to_prepare(tmp_path):
    seen = {}

    def prepare(**kwargs):
        seen.update(kwargs)
        return ops.PrepareResult(sha256="d" * 64, head="h", main="m", slot="s", pr=None, prepared_path="p", args_path="a", prepared={})

    common.OVERRIDE["head prepare"] = prepare
    try:
        rt = admit_runtime(tmp_path)
        from test_pipeline_cov_head_admit import native

        with native(rt):
            ctx = context(rt, tmp_path, {"review_root": str(tmp_path), "nonconsumers": "~/ghosts.json"}, {"packet": str(tmp_path / "packet.json")})
            head_admit.prepare(ctx)
            assert seen["nonconsumers"] == Path.home() / "ghosts.json"
            head_admit.prepare(context(rt, tmp_path, {"review_root": str(tmp_path)}, {"packet": str(tmp_path / "packet.json")}))
            assert seen["nonconsumers"] is None  # the command's own default (the setting admission_nonconsumers_file) applies
    finally:
        common.OVERRIDE.clear()
    assert "nonconsumers" in head_admit.INPUTS


def test_receipts_of_reads_a_file_as_prepare_does(tmp_path):
    assert ops.receipts_of(receipts(tmp_path / "r.json", 5)) == [{"run": 5, "kind": "stuck-queued-pr-run"}]
