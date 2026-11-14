"""`sm head apply` and `sm release admit apply` refuse without an approving verdict receipt by a reviewer other than the
author, and `sm review verdict` records a verdict for a digest a prepare produced when no run is waiting (2026-11-14).

Fakes only: a throwaway HOME, the in-memory store, no real process; the registry write itself is replaced."""

import json
import subprocess
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github.testing import FakeGh
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel.native.admission_lib import ops
from steuermaschine_heinzel.native.release_admit_lib import release_ops
from steuermaschine_heinzel.plugin import plugin

SHA = "a" * 64


@pytest.fixture(autouse=True)
def nothing_real(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("STEUERMASCHINE_AGENT", "builder-1")
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: pytest.fail(f"a real process was started: {argv}"))
    monkeypatch.setattr(subprocess, "Popen", lambda argv, *a, **k: pytest.fail(f"a real process was started: {argv}"))
    applied = []
    monkeypatch.setattr(ops, "apply", lambda packet, sha: applied.append(("head", sha)) or ops.ApplyResult(receipt_path="r", sha256=sha, head="b" * 40))
    monkeypatch.setattr(release_ops, "apply", lambda packet, sha: applied.append(("release", sha)) or ops.ApplyResult(receipt_path="r", sha256=sha, head="b" * 40))
    monkeypatch.setattr(ops, "prepare", lambda *a, **k: ops.PrepareResult(sha256=SHA, head="b" * 40, main="c" * 40, slot="PLANS", pr=None,
                                                                          prepared_path="p", args_path="a", prepared={}))
    monkeypatch.setattr(release_ops, "prepare", lambda *a, **k: release_ops.ReleasePrepareResult(
        sha256=SHA, version="1.0.0", head="b" * 40, main="c" * 40, slot="PLANS", recovery=False, prepared_path="p", args_path="a", prepared={}))
    return applied


@pytest.fixture
def applied(nothing_real):
    return nothing_real


def runtime():
    stack = stack_of(plugin)
    settings, _ = stack.validate({}, Path("."))
    project = Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=True)
    return Runtime(project, stack, settings, _providers=fake_providers(commands=FakeGh()))


def prepare(rt, which):
    argv = ["head", "prepare", "p.json", "--core-repo", "c", "--review-root", "r"] if which == "head" else \
        ["release", "admit", "prepare", "p.json", "--core-repo", "c", "--review-root", "r"]
    assert invoke(rt, argv).exit_code == 0


def apply_(rt, which, sha=SHA):
    argv = ["head", "apply", "p.json", "--approved", sha] if which == "head" else ["release", "admit", "apply", "p.json", "--approved", sha]
    return invoke(rt, argv)


def verdict(rt, reviewer, *flags):
    return invoke(rt, ["review", "verdict", SHA, *(flags or ("--approve",)), "--reviewer", reviewer])


@pytest.mark.parametrize("which", ["head", "release"])
def test_apply_refuses_without_a_recorded_verdict(applied, which):
    rt = runtime()
    prepare(rt, which)
    done = apply_(rt, which)
    assert done.exit_code == 1 and "no verdict is recorded" in done.stderr and f"review verdict {SHA}" in done.stderr
    assert applied == []


@pytest.mark.parametrize("which", ["head", "release"])
def test_apply_refuses_a_rejecting_verdict(applied, which):
    rt = runtime()
    prepare(rt, which)
    assert verdict(rt, "reviewer-1", "--reject").exit_code == 0
    done = apply_(rt, which)
    assert done.exit_code == 1 and "'reject'" in done.stderr
    assert applied == []


@pytest.mark.parametrize("which", ["head", "release"])
def test_apply_refuses_a_verdict_by_the_author(applied, which):
    rt = runtime()
    prepare(rt, which)
    rt.providers.store.put_receipt("verdicts", SHA, {"digest": SHA, "verdict": "approve", "reviewer": "builder-1"})  # written by hand
    done = apply_(rt, which)
    assert done.exit_code == 1 and "who also produced these bytes" in done.stderr
    assert applied == []


@pytest.mark.parametrize("which", ["head", "release"])
def test_apply_goes_ahead_with_an_independent_approval_recorded_without_a_run(applied, which):
    rt = runtime()
    prepare(rt, which)  # no run is waiting: the prepare registered the digest
    assert verdict(rt, "reviewer-1").exit_code == 0
    done = apply_(rt, which)
    assert done.exit_code == 0 and applied == [(which, SHA)]


def test_verdict_by_the_preparing_agent_is_refused():
    rt = runtime()
    prepare(rt, "head")
    done = verdict(rt, "builder-1")
    assert done.exit_code == 1 and "different agent" in done.stderr
    assert rt.providers.store.get_receipt("verdicts", SHA) is None


def test_verdict_for_an_unknown_digest_is_still_refused():
    done = verdict(runtime(), "reviewer-1")
    assert done.exit_code == 2 and "no run is waiting" in done.stderr


def test_verdict_json_for_a_prepared_digest(applied):
    rt = runtime()
    prepare(rt, "head")
    done = invoke(rt, ["--format", "json", "review", "verdict", SHA, "--approve", "--reviewer", "reviewer-1"])
    assert done.exit_code == 0 and json.loads(done.stdout)["reviewer"] == "reviewer-1"


@pytest.mark.parametrize("which", ["head", "release"])
def test_a_missing_verdict_refuses_before_any_lock_or_hold(applied, monkeypatch, which):
    from steuermaschine_heinzel.native import admission, release_admit, release_window

    def taken(*args, **kwargs):
        pytest.fail("the window lock or the admission hold was taken before the verdict was checked")

    monkeypatch.setattr(admission, "admission_hold", taken)
    monkeypatch.setattr(release_admit, "admission_hold", taken)
    monkeypatch.setattr(release_window, "held", taken)
    done = apply_(runtime(), which)
    assert done.exit_code == 1 and "no verdict is recorded" in done.stderr and applied == []
