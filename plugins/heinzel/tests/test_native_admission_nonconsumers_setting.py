"""`sm head prepare|blockers` take the reviewed nonconsumer receipts from the setting admission_nonconsumers_file
when --nonconsumers is not given; a refusal over active runs names both the setting and the flag."""
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel.native.admission_lib import focused_admit as a
from steuermaschine_heinzel.native.admission_lib import ops
from steuermaschine_heinzel.plugin import plugin

HINT = "--nonconsumers FILE or the setting admission_nonconsumers_file"


def runtime(table=None):
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": table or {}}, Path("."))
    project = Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=True)
    return Runtime(project, stack, settings, _providers=fake_providers())


def prepare_argv(tmp_path, *extra):
    return ["head", "prepare", str(tmp_path / "packet.json"), "--core-repo", str(tmp_path), "--review-root", str(tmp_path), *extra]


def capture_prepare(monkeypatch):
    seen = {}

    def fake(*args, **kwargs):
        seen["nonconsumers"] = kwargs.get("nonconsumers")
        raise a.AdmissionError("stop here")
    monkeypatch.setattr(ops, "prepare", fake)
    return seen


def test_prepare_uses_the_setting_when_no_flag_is_given(tmp_path, monkeypatch):
    seen = capture_prepare(monkeypatch)
    invoke(runtime({"admission_nonconsumers_file": str(tmp_path / "receipts.json")}), prepare_argv(tmp_path))
    assert seen["nonconsumers"] == tmp_path / "receipts.json"


def test_the_flag_wins_over_the_setting(tmp_path, monkeypatch):
    seen = capture_prepare(monkeypatch)
    invoke(runtime({"admission_nonconsumers_file": str(tmp_path / "set.json")}), prepare_argv(tmp_path, "--nonconsumers", str(tmp_path / "flag.json")))
    assert seen["nonconsumers"] == tmp_path / "flag.json"


def test_no_setting_and_no_flag_means_no_receipts(tmp_path, monkeypatch):
    seen = capture_prepare(monkeypatch)
    invoke(runtime(), prepare_argv(tmp_path))
    assert seen["nonconsumers"] is None


def test_the_setting_expands_the_home_directory(tmp_path, monkeypatch):
    seen = capture_prepare(monkeypatch)
    invoke(runtime({"admission_nonconsumers_file": "~/receipts.json"}), prepare_argv(tmp_path))
    assert seen["nonconsumers"] == Path.home() / "receipts.json"


def test_blockers_use_the_setting_too(tmp_path, monkeypatch):
    seen = {}

    def fake(core, nonconsumers=None, runtime=None):
        seen["nonconsumers"] = nonconsumers
        return ops.BlockersResult(blocking=[])
    monkeypatch.setattr(ops, "blockers", fake)
    done = invoke(runtime({"admission_nonconsumers_file": str(tmp_path / "r.json")}), ["head", "blockers", "--core-repo", str(tmp_path)])
    assert done.exit_code == 0 and seen["nonconsumers"] == tmp_path / "r.json"


def test_the_wait_on_an_uncovered_run_names_the_setting_and_the_flag(tmp_path, monkeypatch):
    seen = []

    def blockers(core, nonconsumers=None, runtime=None):
        seen.append(nonconsumers)
        return ops.BlockersResult(blocking=[ops.BlockingRun(
            id=9, status="queued", event="pull_request", head_branch="x", path=".github/workflows/ci.yml", head_sha="a" * 40)])
    monkeypatch.setattr(ops, "blockers", blockers)
    monkeypatch.setattr(ops, "prepare", lambda *args, **kwargs: pytest.fail("prepare ran while a run blocks"))
    done = invoke(runtime({"admission_nonconsumers_file": str(tmp_path / "r.json")}), prepare_argv(tmp_path, "--wait"))
    text = done.stdout + done.stderr
    assert done.exit_code == 75 and "uncovered active run 9 queued x (pull_request)" in text and HINT in text
    assert set(seen) == {tmp_path / "r.json"}


def test_a_refusal_over_an_active_run_names_the_setting_and_the_flag(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise a.AdmissionError("unknown active Actions run; admission refused: 9 queued pull_request x .github/workflows/ci.yml")
    monkeypatch.setattr(ops, "prepare", refuse)
    done = invoke(runtime(), prepare_argv(tmp_path))
    text = done.stdout + done.stderr
    assert done.exit_code == 1 and HINT in text and "sm head blockers" in text


def test_other_refusals_carry_no_receipts_hint(tmp_path, monkeypatch):
    capture_prepare(monkeypatch)
    done = invoke(runtime(), prepare_argv(tmp_path))
    assert done.exit_code == 1 and "stop here" in (done.stdout + done.stderr) and HINT not in (done.stdout + done.stderr)
