"""`queue add`, the pre-merge guard and the real merge train under the merge rule, with only `gh` scripted (no queue code replaced)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_native_queue_watch_queue import lib, queue  # noqa: F401  (fixtures)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_native_queue_merge_rule import GREEN, HEAD, MAIN, World, approve  # noqa: E402
from test_pipeline_head_admit import native, runtime  # noqa: E402

from steuermaschine_heinzel.native.queue_lib import hzlib  # noqa: E402


class Github(World):
    """The World of the rule, plus what the merge train asks: the PR's state and merge status, and the merge call itself."""

    def __init__(self, merge_state="CLEAN", **kw):
        super().__init__(**kw)
        self.merge_state, self.calls = merge_state, []

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("pr", "view"):
            return 0, json.dumps({"state": "OPEN", "isDraft": False, "headRefName": "fix/x", "headRefOid": self.head,
                                  "mergeStateStatus": self.merge_state, "statusCheckRollup": self.rollup})
        if args[:2] in (("pr", "merge"), ("pr", "update-branch")):
            return 0, ""
        return super().__call__(*args)

    def merges(self):
        return [c for c in self.calls if c[:2] == ("pr", "merge")]


@pytest.fixture
def merge(queue, lib, tool, monkeypatch, tmp_path, real_main_plan):  # noqa: F811
    """`merge(github)` queues PR 803 with `queue add`, then runs the real train with the queue's own pre-merge guard."""
    rt = runtime(tmp_path)
    train = tool("hz-merge-train")  # a fresh copy: the `queue` fixture replaced the loaded one's `train` with a recorder
    monkeypatch.setattr(lib, "sleep", lambda s: None)
    monkeypatch.setattr(train, "time", type("T", (), {"sleep": staticmethod(lambda s: None), "time": staticmethod(lambda: 0.0)}))

    def go(github):
        monkeypatch.setattr(lib, "gh", github)
        monkeypatch.setattr(train, "gh", github)
        monkeypatch.setattr(queue, "main_state", lambda: "green")
        assert queue.main(["add", "803"]) == 0
        item = next(i for i in queue.load()["items"] if i["pr"] == "803")
        refusal: list[str] = []
        with native(rt):
            guard = queue.plan_guard(item, None, refusal)
            merged, detail = train.train(lib.CORE, "803", queue_core=True, expected_head=item.get("guarded_head") or HEAD, pre_merge_guard=guard)
        return merged, detail, refusal

    go.rt = rt
    return go


def test_a_fresh_green_pr_is_queued_and_merges_at_its_pinned_head(merge):
    github = Github()
    merged, detail, refusal = merge(github)
    assert merged and not refusal and detail == HEAD[:8]
    assert github.merges() == [("pr", "merge", "803", "-R", hzlib.CORE, "--squash", "--match-head-commit", HEAD)]


def test_deferred_vitest_refuses_a_merge_without_protected_full_pr_proof(merge, monkeypatch):
    from steuermaschine_heinzel.native.queue_lib import full_pr

    monkeypatch.setattr(full_pr, "required", lambda _head: {"requires_full_pr": True})
    def refused(*_args):
        raise ValueError("full suite missing")
    monkeypatch.setattr(full_pr, "verify", refused)
    github = Github()
    merged, detail, _ = merge(github)
    assert not merged and not github.merges() and "full suite missing" in detail


def test_deferred_vitest_with_proof_merges_immediately_at_pinned_head(merge, monkeypatch):
    from steuermaschine_heinzel.native.queue_lib import full_pr

    monkeypatch.setattr(full_pr, "required", lambda _head: {"requires_full_pr": True})
    seen = []
    monkeypatch.setattr(full_pr, "verify", lambda pr, head, _receipt: seen.append((pr, head)) or {"base": "b" * 40})
    monkeypatch.setattr(full_pr, "base_unchanged", lambda _proof: True)

    class ImmediateGithub(Github):
        def __call__(self, *args):
            if args[:3] == ("api", "--method", "PUT"):
                self.calls.append(args)
                return 0, ""
            return super().__call__(*args)

    github = ImmediateGithub()
    merged, _, _ = merge(github)
    assert merged and seen == [("803", HEAD)] and not github.merges()
    assert ("api", "--method", "PUT", f"repos/{hzlib.CORE}/pulls/803/merge", "-f", "merge_method=squash", "-f", f"sha={HEAD}") in github.calls


@pytest.mark.parametrize("changed", ["freeze", "base"])
def test_deferred_vitest_refuses_when_proof_outlives_freeze_or_base(merge, monkeypatch, changed):
    from steuermaschine_heinzel.native.queue_lib import full_pr

    monkeypatch.setattr(full_pr, "required", lambda _head: {"requires_full_pr": True})
    after = {"proof": False}
    def verify(*_args):
        after["proof"] = True
        return {"base": "b" * 40}
    monkeypatch.setattr(full_pr, "verify", verify)
    monkeypatch.setattr(full_pr, "base_unchanged", lambda _proof: changed != "base")
    monkeypatch.setattr(hzlib, "merge_frozen", lambda *_: after["proof"] and changed == "freeze")
    github = Github()
    merged, detail, _ = merge(github)
    assert not merged and not any(call[:3] == ("api", "--method", "PUT") for call in github.calls)
    assert "freeze" in detail if changed == "freeze" else "main changed" in detail


def test_queue_add_queues_without_evaluating_the_rule_and_the_guard_refuses_a_head_behind_main(merge, queue):  # noqa: F811
    github = Github(status="behind")  # the rule would refuse; `queue add` must not
    merged, detail, refusal = merge(github)
    assert not merged and not github.merges()
    assert refusal and refusal[0].startswith("merge rule:") and "update the branch" in refusal[0]
    assert queue.load()["items"][0]["status"] == "queued"


def test_a_pr_touching_ci_yml_waits_for_the_verdict_and_merges_with_it(merge):
    github = Github(files=[".github/workflows/ci.yml"])
    merged, detail, refusal = merge(github)
    assert not merged and not github.merges() and "no verdict is recorded" in refusal[0] and ".github/workflows/ci.yml" in refusal[0]
    approve(merge.rt, [".github/workflows/ci.yml"])
    github = Github(files=[".github/workflows/ci.yml"])
    merged, detail, refusal = merge(github)
    assert merged and not refusal and github.merges()


def test_a_verdict_for_other_content_does_not_let_the_pr_merge(merge):
    approve(merge.rt, ["uv.lock"], version="2")
    github = Github(files=["uv.lock"])
    merged, _detail, refusal = merge(github)
    assert not merged and not github.merges() and "no verdict is recorded" in refusal[0]


def test_a_core_head_behind_main_is_updated_by_the_train_and_never_merged(merge):
    github = Github(merge_state="BEHIND")
    merged, detail, refusal = merge(github)
    assert not merged and not github.merges()
    assert "behind main: the branch was updated" in detail
    assert any(c[:2] == ("pr", "update-branch") for c in github.calls)


def test_the_checks_the_rule_needs_are_the_rulesets_not_the_legacy_ones(merge):
    refused = Github(rollup=[GREEN[0]], merge_state="CLEAN")  # Sonar absent
    merged, detail, refusal = merge(refused)
    assert not merged and not refused.merges()


def test_a_pr_with_the_legacy_checks_red_does_not_merge_though_ci_ok_and_sonar_are_green(merge):
    """A red check holds whether the ruleset requires it or not: coverage-floor was red while CI OK was green."""
    from test_native_queue_merge_rule import LEGACY_RED

    github = Github(rollup=[*GREEN, *LEGACY_RED])
    merged, detail, refusal = merge(github)
    assert not merged and not github.merges() and "coverage-floor" in detail


# ----------------------------------------------------------------------------- the queue's own update-branch
PINNED, PARENT = "1" * 40, "2" * 40


class Updated(Github):
    """The PR whose branch the queue updated: HEAD is a commit with parents (PINNED, PARENT); PARENT is (not) on main."""

    def __init__(self, parents, on_main=True, **kw):
        super().__init__(**kw)
        self.parents, self.on_main = parents, on_main

    def __call__(self, *args):
        text = " ".join(args)
        if "/commits/" in text:
            return 0, json.dumps({"parents": [{"sha": p} for p in self.parents]})
        if "/compare/" in text and text.endswith("...main"):
            return 0, json.dumps({"status": "ahead" if self.on_main else "diverged"})
        return super().__call__(*args)


@pytest.fixture
def pinned_item(queue, lib, monkeypatch):  # noqa: F811
    monkeypatch.setattr(lib, "gh", Github())
    monkeypatch.setattr(queue, "main_state", lambda: "green")
    assert queue.main(["add", "803"]) == 0
    queue.update("803", guarded_head=PINNED)
    return next(i for i in queue.load()["items"] if i["pr"] == "803")


def test_a_head_that_is_main_merged_into_the_pinned_head_is_repinned_and_stays_queued(queue, lib, monkeypatch, pinned_item):  # noqa: F811
    monkeypatch.setattr(lib, "gh", Updated([PINNED, PARENT]))
    assert queue.repin_after_update(pinned_item, PINNED) == HEAD
    assert pinned_item["guarded_head"] == HEAD and queue.load()["items"][0]["guarded_head"] == HEAD
    assert queue.repin_after_update(pinned_item, HEAD) == HEAD  # already current: nothing to do


@pytest.mark.parametrize("parents, on_main", [([PINNED], True), ([PARENT, "3" * 40], True), ([PINNED, PARENT, "3" * 40], True),
                                              ([PINNED, PARENT], False)])
def test_any_other_new_head_keeps_the_pin_so_the_train_refuses_it(queue, lib, monkeypatch, pinned_item, parents, on_main):  # noqa: F811
    monkeypatch.setattr(lib, "gh", Updated(parents, on_main=on_main))
    assert queue.repin_after_update(pinned_item, PINNED) == PINNED
    assert pinned_item["guarded_head"] == PINNED
