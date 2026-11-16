"""Full hosted evidence is an independent admission route, never synthetic local JUnit."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from steuermaschine_heinzel.native.queue_lib import core_policy, main_plan
from steuermaschine_heinzel.native.queue_lib import main_checkpoint_reader as reader

CAPACITY_REFUSALS = json.loads((Path(__file__).parent / "fixtures/pr_capacity_adversarial.json").read_text())


@pytest.mark.parametrize("defect", [None, "failed", "rerun", "wrong-head", "running", "wrong-tree", "missing"])
def test_full_pr_requires_original_success_and_exact_protected_proof(tmp_path, monkeypatch, defect):
    head, base, tree = (letter * 40 for letter in "abc")
    run = {"id": 12, "head_sha": head, "run_attempt": 1, "status": "completed", "conclusion": "success"}
    updates = {"failed": {"conclusion": "failure"}, "rerun": {"run_attempt": 3},
               "wrong-head": {"head_sha": base}, "running": {"status": "in_progress"}}
    run.update(updates.get(defect, {}))
    monkeypatch.setattr(core_policy, "at_commit_api", lambda *_: SimpleNamespace(workflow_runs="actions/workflows/ci.yml/runs"))
    monkeypatch.setattr(main_plan.hzlib, "ghj", lambda *_args, **_kwargs: {"workflow_runs": [] if defect == "missing" else [run]})
    requested = []
    def proof(request):
        requested.append(request)
        return {"type": "full-pr/v1", "head": head, "base": base,
                "tree": base if defect == "wrong-tree" else tree, "run": 12, "attempt": 1}
    monkeypatch.setattr(main_plan.main_checkpoint, "isolated_reader", proof)
    if defect in updates or defect == "missing":
        with pytest.raises(ValueError):
            main_plan.full_pr(tmp_path, "851", base, head, tree)
        assert not requested
    else:
        assert main_plan.full_pr(tmp_path, "851", base, head, tree) is (defect is None)
        assert requested[0]["reader_head"] == base
        assert requested[0]["mode"] == "full-pr"


@pytest.mark.parametrize("defect", [None, "legacy-proof", "wrong-attempt", "boolean-original", "wrong-base",
                                   "wrong-tree", "wrong-run", "extra", "unsupported", "actual-failure"])
def test_capacity_requires_distinct_exact_protected_proof(tmp_path, monkeypatch, defect):
    head, base, tree = (letter * 40 for letter in "abc")
    run = {"id": 12, "head_sha": head, "run_attempt": 2, "status": "completed", "conclusion": "success"}
    monkeypatch.setattr(core_policy, "at_commit_api", lambda *_: SimpleNamespace(workflow_runs="actions/workflows/ci.yml/runs"))
    monkeypatch.setattr(main_plan.hzlib, "ghj", lambda *_args, **_kwargs: {"workflow_runs": [run]})
    requested = []

    def proof(request):
        requested.append(request)
        if defect in ("unsupported", "actual-failure"):
            raise ValueError("protected verifier refused")
        result = {"type": "full-pr-capacity/v1", "head": head, "base": base,
                  "tree": tree, "run": 12, "attempt": 2, "original_attempt": 1}
        result.update({"legacy-proof": {"type": "full-pr/v1"}, "wrong-attempt": {"attempt": 3},
                       "boolean-original": {"original_attempt": True}, "wrong-base": {"base": head},
                       "wrong-tree": {"tree": head}, "wrong-run": {"run": 13}, "extra": {"waiver": True}}.get(defect, {}))
        return result

    monkeypatch.setattr(main_plan.main_checkpoint, "isolated_reader", proof)
    if defect in ("unsupported", "actual-failure"):
        with pytest.raises(ValueError, match="protected verifier refused"):
            main_plan.full_pr(tmp_path, "851", base, head, tree)
    else:
        assert main_plan.full_pr(tmp_path, "851", base, head, tree) is (defect is None)
    assert requested == [{"mode": "full-pr-capacity", "core_repo": str(tmp_path), "reader_head": base,
                          "pr": "851", "head": head, "tree": tree, "run": 12}]


@pytest.mark.parametrize("attempt", [True, "2", 0, -1, 3, None])
def test_malformed_or_repeated_recovery_never_calls_reader(tmp_path, monkeypatch, attempt):
    monkeypatch.setattr(core_policy, "at_commit_api", lambda *_: SimpleNamespace(workflow_runs="runs"))
    monkeypatch.setattr(main_plan.hzlib, "ghj", lambda *_args, **_kwargs: {"workflow_runs": [
        {"id": 12, "head_sha": "a" * 40, "run_attempt": attempt, "status": "completed", "conclusion": "success"}]})
    monkeypatch.setattr(main_plan.main_checkpoint, "isolated_reader", lambda *_: pytest.fail("must refuse first"))
    with pytest.raises(ValueError, match="eligible attempt"):
        main_plan.full_pr(tmp_path, "851", "b" * 40, "a" * 40, "c" * 40)


@pytest.mark.parametrize("defect", [None, "unsupported", "focused", "moved-main", "different-tree"])
def test_adapter_only_accepts_full_protected_verdict_and_current_tree(tmp_path, monkeypatch, defect):
    monkeypatch.chdir(tmp_path)
    head, base, tree = (letter * 40 for letter in "abc")
    def require(value, message):
        if not value:
            raise ValueError(message)
    fg = SimpleNamespace(
        Gate=type("Gate", (), {"__init__": lambda self, *_: None}),
        full_pr_jobs=None if defect == "unsupported" else lambda: None,
        WORKFLOW=".github/workflows/ci.yml", git=lambda *_: b"trusted workflow",
        verify=lambda *_: "focused" if defect == "focused" else "Whole-project coverage measured by full PR CI",
        pull_identity=lambda *_: ({}, "d" * 40, head if defect == "moved-main" else base),
    )
    fp = SimpleNamespace(require=require, valid_sha=lambda value: len(value) == 40,
                         object_id=lambda *_: base if defect == "different-tree" else tree)
    request = {"head": head, "reader_head": base, "tree": tree, "pr": "851", "run": 12}
    if defect:
        with pytest.raises(ValueError):
            reader.verified_full_pr(request, fp, fg)
    else:
        assert reader.verified_full_pr(request, fp, fg) == {
            "type": "full-pr/v1", "head": head, "base": base, "tree": tree, "run": 12, "attempt": 1}


@pytest.mark.parametrize("defect", [None, "unsupported", "refused", "wrong-head", "wrong-base", "wrong-tree",
                                   "wrong-run", "wrong-attempt", "boolean-original", "missing-original",
                                   "extra", "moved-main", "changed-tree"])
def test_capacity_adapter_preserves_protected_authority_and_rechecks_tree(tmp_path, monkeypatch, defect):
    monkeypatch.chdir(tmp_path)
    head, base, tree = (letter * 40 for letter in "abc")
    expected = {"type": "full-pr-capacity/v1", "head": head, "base": base, "tree": tree,
                "run": 12, "attempt": 2, "original_attempt": 1}
    calls = []

    def require(value, message):
        if not value:
            raise ValueError(message)

    def capacity(gate, run, read):
        calls.append((gate, run, read))
        if defect == "refused":
            raise ValueError("protected capacity verifier refused")
        result = dict(expected)
        result.update({"wrong-head": {"head": base}, "wrong-base": {"base": head}, "wrong-tree": {"tree": head},
                       "wrong-run": {"run": 13}, "wrong-attempt": {"attempt": 1},
                       "boolean-original": {"original_attempt": True}, "extra": {"waiver": True}}.get(defect, {}))
        if defect == "missing-original":
            result.pop("original_attempt")
        return result

    fg = SimpleNamespace(
        Gate=type("Gate", (), {"__init__": lambda self, *_: None}),
        verify_full_pr_capacity=None if defect == "unsupported" else capacity,
        verify=lambda *_: pytest.fail("capacity must not reuse ordinary proof"),
        WORKFLOW=".github/workflows/ci.yml", git=lambda *_: b"protected workflow",
        pull_identity=lambda *_: ({}, "d" * 40, head if defect == "moved-main" else base),
    )
    fp = SimpleNamespace(require=require, valid_sha=lambda value: len(value) == 40,
                         object_id=lambda *_: head if defect == "changed-tree" else tree)
    request = {"mode": "full-pr-capacity", "head": head, "reader_head": base, "tree": tree, "pr": "851", "run": 12}
    if defect:
        with pytest.raises(ValueError):
            reader.verified_full_pr(request, fp, fg)
    else:
        assert reader.verified_full_pr(request, fp, fg) == expected
    assert len(calls) == (0 if defect == "unsupported" else 1)
    if calls:
        assert calls[0][1:] == (12, reader.read)


@pytest.mark.parametrize("scenario", CAPACITY_REFUSALS)
def test_protected_capacity_refusals_never_fall_back_to_another_route(tmp_path, monkeypatch, scenario):
    """Core executes these adversarial fixtures; sm must preserve every resulting refusal."""
    monkeypatch.chdir(tmp_path)
    head, base, tree = (letter * 40 for letter in "abc")

    def require(value, message):
        if not value:
            raise ValueError(message)

    def refused(*_):
        raise ValueError(scenario)

    fg = SimpleNamespace(
        Gate=type("Gate", (), {"__init__": lambda self, *_: None}),
        verify_full_pr_capacity=refused,
        verify=lambda *_: pytest.fail("capacity refusal must not fall back"),
        WORKFLOW=".github/workflows/ci.yml", git=lambda *_: b"protected workflow",
    )
    fp = SimpleNamespace(require=require, valid_sha=lambda value: len(value) == 40)
    request = {"mode": "full-pr-capacity", "head": head, "reader_head": base, "tree": tree, "pr": "851", "run": 12}
    with pytest.raises(ValueError, match=scenario):
        reader.verified_full_pr(request, fp, fg)


@pytest.mark.parametrize("registry_available", [True, False])
def test_gap_uses_independent_exact_proof_when_plan_missing(tmp_path, monkeypatch, real_main_plan, registry_available):
    head, base, tree = (letter * 40 for letter in "abc")
    monkeypatch.setattr(main_plan, "core_repo", lambda: tmp_path)
    monkeypatch.setattr(main_plan.hzlib, "ghj", lambda *_args, **_kwargs: {"object": {"sha": base}})
    monkeypatch.setattr(main_plan, "merge_tree", lambda *_: tree)
    def registry(*_):
        if not registry_available:
            raise ValueError("unavailable transport")
        return {}
    monkeypatch.setattr(main_plan, "registry", registry)
    requests = []
    monkeypatch.setattr(main_plan, "full_pr", lambda *args: requests.append(args) or True)
    assert main_plan.gap("851", head) == ""
    assert requests == [(tmp_path, "851", base, head, tree)]
    monkeypatch.setattr(main_plan, "full_pr", lambda *_: False)
    assert "no exact full hosted proof" in main_plan.gap("851", head)
