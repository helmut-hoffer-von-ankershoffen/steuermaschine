"""The deferred full Vitest obligation is bound to protected full-PR evidence."""

from types import SimpleNamespace

import pytest

from steuermaschine_heinzel.native.queue_lib import auto_arm, full_pr

HEAD, BASE, MERGE, TREE = (c * 40 for c in "abcd")


@pytest.mark.parametrize(("fields", "expected"), [
    ({}, False),
    ({"ci_must_run": ["some other hosted check"]}, False),
    ({"ci_must_run": [full_pr.LEGACY]}, True),
    ({"requires_full_pr": True}, True),
])
def test_receipt_flag_and_legacy_obligation(native_receipts, fields, expected):
    assert full_pr.required(HEAD) is None
    data = {"head": HEAD, "green": True, **fields}
    native_receipts.put_receipt("checks", HEAD, data)
    assert full_pr.required(HEAD) == (data if expected else None)


def test_invalid_receipt_never_authorizes_unchecked_merge(native_receipts):
    native_receipts.put_receipt("checks", HEAD, {"head": HEAD, "green": False})
    with pytest.raises(ValueError, match="invalid"):
        full_pr.required(HEAD)


@pytest.mark.parametrize("data", [
    {"head": "b" * 40, "green": True},
    {"head": HEAD, "green": 1},
    {"head": HEAD, "green": True, "ci_must_run": "the whole Vitest suite"},
    {"head": HEAD, "green": True, "ci_must_run": [None]},
    {"head": HEAD, "green": True, "requires_full_pr": 1},
])
def test_malformed_or_wrong_head_obligation_fails_closed(native_receipts, data):
    native_receipts.put_receipt("checks", HEAD, data)
    with pytest.raises(ValueError):
        full_pr.required(HEAD)


@pytest.mark.parametrize("proof", [None, {}, {"base": 1}, {"base": "too short"}])
def test_post_proof_base_check_refuses_malformed_evidence(monkeypatch, proof):
    monkeypatch.setattr(full_pr.hzlib, "ghj", lambda *_args, **_kwargs: pytest.fail("invalid proof must not read API"))
    assert not full_pr.base_unchanged(proof)


@pytest.mark.parametrize("bad", [None, {}, {"workflow_runs": []}, {"total_count": 2, "workflow_runs": [{}]}])
def test_full_pr_refuses_missing_or_incomplete_run_inventory(monkeypatch, tmp_path, bad):
    def api(_kind, path, **_kwargs):
        if "git/ref/" in path:
            return {"object": {"sha": BASE}}
        if "/pulls/" in path:
            return {"head": {"sha": HEAD}, "base": {"sha": BASE}, "merge_commit_sha": MERGE}
        if "/commits/" in path:
            return {"commit": {"tree": {"sha": TREE}}}
        return bad

    monkeypatch.setattr(full_pr.hzlib, "ghj", api)
    monkeypatch.setattr(full_pr.main_checkpoint, "isolated_reader", lambda _: pytest.fail("incomplete inventory"))
    with pytest.raises(ValueError, match="complete PR CI run inventory"):
        full_pr.verify("42", HEAD, {"worktree": str(tmp_path)})


@pytest.mark.parametrize("failure", ["invalid-pr", "invalid-head", "missing-main", "missing-pr", "moved-head",
                                     "moved-base", "bad-merge", "missing-tree", "missing-worktree"])
def test_full_pr_refuses_unbound_identity_before_reader(monkeypatch, tmp_path, failure):
    def api(_kind, path, **_kwargs):
        if "git/ref/" in path:
            return None if failure == "missing-main" else {"object": {"sha": BASE}}
        if "/pulls/" in path:
            if failure == "missing-pr":
                return None
            return {"head": {"sha": "e" * 40 if failure == "moved-head" else HEAD},
                    "base": {"sha": "e" * 40 if failure == "moved-base" else BASE},
                    "merge_commit_sha": "bad" if failure == "bad-merge" else MERGE}
        if "/commits/" in path:
            return None if failure == "missing-tree" else {"commit": {"tree": {"sha": TREE}}}
        return {"total_count": 1, "workflow_runs": [{"id": 123, "head_sha": HEAD, "event": "pull_request",
                "status": "completed", "conclusion": "success", "run_attempt": 1}]}

    monkeypatch.setattr(full_pr.hzlib, "ghj", api)
    monkeypatch.setattr(full_pr.main_checkpoint, "isolated_reader", lambda _: pytest.fail("unbound identity"))
    with pytest.raises(ValueError):
        full_pr.verify("bad" if failure == "invalid-pr" else "42", "bad" if failure == "invalid-head" else HEAD,
                       {"worktree": "relative" if failure == "missing-worktree" else str(tmp_path)})


def test_full_proof_binds_run_head_current_base_and_tree(monkeypatch, tmp_path):
    import policy_kit

    policy_reads = []

    def policy_at(sha):
        policy_reads.append(sha)
        return policy_kit.policy(commit=sha, workflow=".github/workflows/protected-checks.yml")

    monkeypatch.setattr(full_pr.core_facts, "at", policy_at)
    runs_path = (f"repos/{full_pr.hzlib.CORE}/actions/workflows/protected-checks.yml/runs"
                 f"?event=pull_request&head_sha={HEAD}&per_page=100")

    def api(*args, **_kwargs):
        path = args[1]
        if "git/ref/" in path:
            return {"object": {"sha": BASE}}
        if "/pulls/" in path:
            return {"head": {"sha": HEAD}, "base": {"sha": BASE}, "merge_commit_sha": MERGE}
        if "/commits/" in path:
            return {"commit": {"tree": {"sha": TREE}}}
        assert path == runs_path
        return {"total_count": 1, "workflow_runs": [{"id": 123, "head_sha": HEAD, "event": "pull_request",
                 "status": "completed", "conclusion": "success", "run_attempt": 1}]}

    monkeypatch.setattr(full_pr.hzlib, "ghj", api)
    calls = []
    def reader(request):
        calls.append(request)
        return {"type": "full-pr/v1", "head": HEAD, "base": BASE, "tree": TREE, "run": 123, "attempt": 1}
    monkeypatch.setattr(full_pr.main_checkpoint, "isolated_reader", reader)
    proof = full_pr.verify("42", HEAD, {"worktree": str(tmp_path)})
    assert proof["run"] == 123
    assert policy_reads == [BASE]
    assert calls == [{"mode": "full-pr", "core_repo": str(tmp_path), "reader_head": BASE,
                      "head": HEAD, "tree": TREE, "pr": 42, "run": 123}]
    monkeypatch.setattr(full_pr.main_checkpoint, "isolated_reader", lambda _: {**proof, "base": "e" * 40})
    with pytest.raises(ValueError, match="differs"):
        full_pr.verify("42", HEAD, {"worktree": str(tmp_path)})

    monkeypatch.setattr(full_pr.core_facts, "at", lambda sha: policy_kit.policy(commit=sha, repository="other/repository"))
    with pytest.raises(ValueError, match="different repository"):
        full_pr.verify("42", HEAD, {"worktree": str(tmp_path)})


def test_missing_or_stale_ci_never_supplies_full_proof(monkeypatch, tmp_path):
    def api(*args, **_kwargs):
        path = args[1]
        if "git/ref/" in path:
            return {"object": {"sha": BASE}}
        if "/pulls/" in path:
            return {"head": {"sha": HEAD}, "base": {"sha": BASE}, "merge_commit_sha": MERGE}
        if "/commits/" in path:
            return {"commit": {"tree": {"sha": TREE}}}
        return {"total_count": 1, "workflow_runs": [{"id": 123, "head_sha": HEAD, "event": "pull_request",
                 "status": "completed", "conclusion": "failure", "run_attempt": 1}]}

    monkeypatch.setattr(full_pr.hzlib, "ghj", api)
    monkeypatch.setattr(full_pr.main_checkpoint, "isolated_reader", lambda _: pytest.fail("failed CI cannot be proof"))
    with pytest.raises(ValueError, match="no successful"):
        full_pr.verify("42", HEAD, {"worktree": str(tmp_path)})


@pytest.mark.parametrize("already_armed", [False, True])
@pytest.mark.parametrize("operator_authorized", [False, True])
def test_queue_entry_requiring_immediate_merge_is_never_armed(monkeypatch, already_armed, operator_authorized):
    item = {"repo": auto_arm.hzlib.CORE, "pr": "42", "key": "core:42", "guarded_head": HEAD}
    if operator_authorized:
        item["guard_authorization"] = {"head": HEAD, "base": BASE}
    info = {"state": "OPEN", "headRefOid": HEAD, "author": {"login": "owner"},
            "statusCheckRollup": [], "autoMergeRequest": {} if not already_armed else {"enabled": True}}
    monkeypatch.setattr(auto_arm.hzlib, "ghj", lambda *_args, **_kwargs: info)
    monkeypatch.setattr(auto_arm, "authors", lambda: {"owner"})
    monkeypatch.setattr(auto_arm.exact_queue, "queue_candidate", lambda _: None)
    monkeypatch.setattr(auto_arm, "freeze_without_candidate", lambda _: "")
    monkeypatch.setattr(auto_arm, "main_hold", lambda *_: "")
    monkeypatch.setattr(auto_arm.hzlib, "tool_required_checks", lambda _: set())
    monkeypatch.setattr(auto_arm, "merge_rule", lambda *_: ("", False))
    monkeypatch.setattr(auto_arm, "armed_on_github", lambda _: [])
    monkeypatch.setattr(full_pr, "required", lambda _: None if operator_authorized else {"requires_full_pr": True})
    actions = []
    monkeypatch.setattr(auto_arm, "arm", lambda *_: pytest.fail("deferred PR must not arm"))
    monkeypatch.setattr(auto_arm, "disarm", lambda *_: actions.append("disarm") or True)
    def train(*_args, **kwargs):
        assert kwargs["immediate_merge"] is operator_authorized
        actions.append("immediate")
        return False, "proof unavailable"

    monkeypatch.setattr(auto_arm.hzlib, "load", lambda _: SimpleNamespace(train=train))
    q = SimpleNamespace(deps_met=lambda *_: True, assess=lambda *_: "ready", log=lambda *_: None,
                        update=lambda *_args, **_kwargs: None)
    auto_arm.one(q, item, [item])
    assert actions == (["disarm"] if already_armed else ["immediate"])


def test_post_proof_base_check_refuses_moved_or_unreadable_main(monkeypatch):
    monkeypatch.setattr(full_pr.hzlib, "ghj", lambda *_args, **_kwargs: {"object": {"sha": BASE}})
    assert full_pr.base_unchanged({"base": BASE})
    assert not full_pr.base_unchanged({"base": HEAD})
    monkeypatch.setattr(full_pr.hzlib, "ghj", lambda *_args, **_kwargs: None)
    assert not full_pr.base_unchanged({"base": BASE})


def test_auto_guard_refuses_competing_arm_after_initial_inventory(monkeypatch):
    from steuermaschine_heinzel.native import queue_hold

    monkeypatch.setattr(queue_hold, "current", lambda: None)
    item = {"repo": auto_arm.hzlib.CORE, "pr": "42", "key": "core:42", "guarded_head": HEAD}
    info = {"state": "OPEN", "headRefOid": HEAD, "author": {"login": "owner"}, "statusCheckRollup": []}
    monkeypatch.setattr(auto_arm.hzlib, "ghj", lambda *_args, **_kwargs: info)
    monkeypatch.setattr(auto_arm, "authors", lambda: {"owner"})
    monkeypatch.setattr(auto_arm.exact_queue, "queue_candidate", lambda _: None)
    monkeypatch.setattr(auto_arm, "freeze_without_candidate", lambda _: "")
    monkeypatch.setattr(auto_arm, "main_hold", lambda *_: "")
    monkeypatch.setattr(auto_arm.hzlib, "tool_required_checks", lambda _: set())
    monkeypatch.setattr(auto_arm, "merge_rule", lambda *_: ("", False))
    inventory = iter([[], ["43"]])
    monkeypatch.setattr(auto_arm, "armed_on_github", lambda _: next(inventory))
    monkeypatch.setattr(full_pr, "required", lambda _: {"requires_full_pr": True})
    guards = []
    def train(*_args, **kwargs):
        guards.append(kwargs["pre_merge_guard"]())
        return False, "held"
    monkeypatch.setattr(auto_arm.hzlib, "load", lambda _: SimpleNamespace(train=train))
    q = SimpleNamespace(deps_met=lambda *_: True, assess=lambda *_: "ready", log=lambda *_: None,
                        update=lambda *_args, **_kwargs: None)
    auto_arm.one(q, item, [item])
    assert guards == [False]


@pytest.mark.parametrize("excepted,expected", [(False, False), (True, True)])
def test_auto_guard_rechecks_new_queue_hold_and_entire_exception_inventory(monkeypatch, excepted, expected):
    from steuermaschine_heinzel.native import queue_hold

    item = {"repo": auto_arm.hzlib.CORE, "pr": "42", "key": "core:42", "guarded_head": HEAD, "status": "queued"}
    info = {"state": "OPEN", "headRefOid": HEAD, "author": {"login": "owner"}, "statusCheckRollup": []}
    monkeypatch.setattr(auto_arm.hzlib, "ghj", lambda *_args, **_kwargs: info)
    monkeypatch.setattr(auto_arm, "authors", lambda: {"owner"})
    monkeypatch.setattr(auto_arm.exact_queue, "queue_candidate", lambda _: None)
    monkeypatch.setattr(auto_arm, "freeze_without_candidate", lambda _: "")
    monkeypatch.setattr(auto_arm, "main_hold", lambda *_: "")
    monkeypatch.setattr(auto_arm.hzlib, "tool_required_checks", lambda _: set())
    monkeypatch.setattr(auto_arm, "merge_rule", lambda *_: ("", False))
    monkeypatch.setattr(auto_arm, "armed_on_github", lambda _: [])
    monkeypatch.setattr(full_pr, "required", lambda _: {"requires_full_pr": True})
    now = {"hold": None}
    monkeypatch.setattr(queue_hold, "current", lambda: now["hold"])
    guards = []
    def train(*_args, **kwargs):
        guard = kwargs["pre_merge_guard"]
        guards.append(guard())
        now["hold"] = queue_hold.QueueHold(reason="operator", since=1, excepted=[queue_hold.queued_key(item)] if excepted else [])
        guards.append(guard())
        return False, "held"
    monkeypatch.setattr(auto_arm.hzlib, "load", lambda _: SimpleNamespace(train=train))
    q = SimpleNamespace(deps_met=lambda *_: True, assess=lambda *_: "ready", log=lambda *_: None,
                        update=lambda *_args, **_kwargs: None, load=lambda: {"items": [item]})
    auto_arm.one(q, item, [item])
    assert guards == [True, expected]
