"""`sm go` items for the engine pipeline runs (`sm ship`, `sm release cut`): one item per state of a run."""

from __future__ import annotations

import pytest

from steuermaschine_heinzel.native.go_lib.decide import pipeline_items
from steuermaschine_heinzel.native.go_lib.words import Words

MAIN = "a" * 40


def item(kind, phase, nxt=None, rank=None, waiting=None):
    return {"kind": kind, "phase": phase, "next": nxt, "rank": rank, "waiting": waiting}


def one(**run):
    base = {"pipeline": "head.admit", "key": "k" * 40, "label": "fix/x", "arg": "/wt", "status": "waiting", "phase": "pr"}
    (found,) = pipeline_items({**base, **run}, MAIN, Words(), item)
    return found


def test_a_running_step_with_a_live_process_says_to_start_nothing_else():
    found = one(status="running", pid=7, running="pr", alive=True)
    assert found["kind"] == "pipeline-running" and "Start nothing else on fix/x" in found["trap"]


def test_a_running_step_whose_process_is_gone_is_interrupted():
    assert one(status="running", pid=7, running="pr", alive=False)["kind"] == "pipeline-interrupted"


def test_a_head_run_for_an_older_head_is_stale():
    found = one(worktree_head="b" * 40)
    assert found["kind"] == "pipeline-stale" and "moved on to bbbbbbbbb" in found["phase"]


def test_a_release_cut_bound_to_another_main_is_stale_until_an_external_step_is_done():
    stale = one(pipeline="release.cut", fingerprint={"main": "c" * 40})
    assert stale["kind"] == "pipeline-stale" and "main moved cccccccc" in stale["phase"]
    assert one(pipeline="release.cut", fingerprint={"main": "c" * 40}, external_done=True)["kind"] == "pipeline-waiting"


@pytest.mark.parametrize(("run", "kind"), [
    ({"status": "refused", "pipeline": "release.cut", "phase": "resume: compose failed"}, "pipeline-failed"),
    ({"status": "refused", "next": "sm ship /wt"}, "pipeline-refused"),
    ({"status": "review", "review": {"brief": "b.md", "file": "f", "sha256": "s"}}, "pipeline-review"),
    ({"status": "dry_run", "next": "sm ship /wt --live"}, "pipeline-ready"),
    ({"status": "waiting", "waiting_on": "the push check"}, "pipeline-waiting"),
])
def test_every_other_state_has_its_own_item(run, kind):
    found = one(**run)
    assert found["kind"] == kind
    if kind == "pipeline-refused":
        assert found["next"] == "sm ship /wt"
    if kind == "pipeline-ready":
        assert found["next"] == "sm ship /wt --live"


# ----------------------------------------------------------------------------- the probe that feeds them
def test_the_probe_keeps_unfinished_runs_with_their_process_and_worktree_facts(monkeypatch):
    import os
    from types import SimpleNamespace

    from steuermaschine_heinzel.native.go_lib import probes

    heads = {"/wt": "b" * 40}

    def sh(argv, timeout=5):
        if argv[2] not in heads:
            raise RuntimeError("no worktree")
        return heads[argv[2]] + "\n"

    runs = [
        {"pipeline": "head.admit", "key": "k", "status": "finished"},  # done: not listed
        {"pipeline": "head.admit", "key": "a" * 40, "arg": "/wt", "status": "running", "pid": os.getpid(), "steps": {"pr": {"status": "done"}}},
        {"pipeline": "head.admit", "key": "c" * 40, "arg": "/gone", "status": "waiting"},
        {"pipeline": "release.cut", "key": "0.9.52", "status": "running", "pid": 99999999},
        {"pipeline": "head.admit", "key": "d" * 40, "status": "running", "running": "packet", "pid": 1},  # the deleted admission's steps
        {"pipeline": "head.admit", "key": "e" * 40, "status": "waiting", "steps": {"prepare": {"status": "done"}}},
        "not a run",
    ]
    rows = probes.probe_pipelines(SimpleNamespace(runs=runs, sh=sh))
    assert [r["key"] for r in rows] == ["a" * 40, "c" * 40, "0.9.52"]
    assert rows[0]["alive"] is True and rows[0]["worktree_head"] == "b" * 40 and rows[0]["external_done"] is True
    assert rows[1]["worktree_head"] == "" and rows[2]["alive"] is False
    monkeypatch.setattr(os, "kill", lambda pid, sig: (_ for _ in ()).throw(PermissionError()))
    assert probes._alive(1) is True
