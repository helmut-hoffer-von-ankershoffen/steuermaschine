"""`queue add`, the pre-merge guard and the real merge train under the merge rule, with only `gh` scripted (no queue code replaced)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_native_queue_watch_queue import lib, queue  # noqa: F401  (fixtures)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_native_queue_merge_rule import GREEN, HEAD, World, approve  # noqa: E402
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


def test_a_verdict_for_another_head_does_not_let_the_pr_merge(merge):
    approve(merge.rt, ["uv.lock"], head="c" * 40)
    github = Github(files=["uv.lock"])
    merged, _detail, refusal = merge(github)
    assert not merged and not github.merges() and "no verdict is recorded" in refusal[0]


def test_a_core_head_behind_main_is_updated_by_the_train_and_never_merged(merge):
    github = Github(merge_state="BEHIND")
    merged, detail, refusal = merge(github)
    assert not merged and not github.merges()
    assert "behind main: the branch was updated" in detail
    assert any(c[:2] == ("pr", "update-branch") for c in github.calls)


def test_the_checks_the_rule_needs_are_those_of_the_policy(merge):
    github = Github(rollup=[c for c in GREEN if c["name"] != "merge-gate"], merge_state="CLEAN")
    merged, detail, refusal = merge(github)
    assert not merged and not github.merges()
