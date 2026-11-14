"""release resume: a run of an unmarked dispatch (PyPI, compose, plugin release) is adopted only when it is exactly
what was dispatched: the workflow, event workflow_dispatch, the dispatched head SHA, created after the dispatch. Runs
looked at and not taken are recorded with the reason, the adopted one by ID (Fable review F7)."""

import subprocess

import pytest

from steuermaschine_heinzel.native.queue_lib import edge, hzlib

SHA, OTHER = "a" * 40, "b" * 40


def row(run_id, sha=SHA, event="workflow_dispatch", created="2026-11-13T13:31:00Z", status="in_progress", conclusion=None):
    return {"databaseId": run_id, "headSha": sha, "event": event, "createdAt": created, "status": status, "conclusion": conclusion,
            "displayTitle": "Compose", "attempt": 1}


@pytest.fixture
def rel(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HZ_RELEASE_FLOW_DIR", str(tmp_path / "flow"))
    monkeypatch.setattr(hzlib, "_gh_process", lambda *a, **_: subprocess.CompletedProcess(a, 1, "", ""))
    module = edge.fresh(edge.module_of("hz-release"))
    module.remember(version="0.9.41")
    world = {"head": SHA, "listed": [], "by_id": {}, "calls": []}

    def gh(*args):
        world["calls"].append(args)
        if args[0] == "api" and "/commits/" in args[1]:
            return 0, world["head"] + "\n"
        if args[:2] == ("workflow", "run"):
            return 0, ""
        raise AssertionError(args)

    def ghj(*args, default=None):
        if args[:2] == ("run", "list"):
            assert "event" in args[-1]
            return world["listed"]
        if args[0] == "api" and "/actions/runs/" in args[1]:
            return world["by_id"].get(int(args[1].rsplit("/", 1)[1]), default)
        raise AssertionError(args)

    monkeypatch.setattr(module.hzlib, "gh", gh)
    monkeypatch.setattr(module.hzlib, "ghj", ghj)
    monkeypatch.setattr(module, "now_iso", lambda offset=0: "2026-11-13T13:30:00Z")
    module.world = world
    return module


def test_the_dispatch_records_what_was_asked_and_drops_an_earlier_adoption(rel):
    rel.remember(compose_run_id=111, compose_adopted={"run": 111})
    rel.dispatch(rel.PLUGINS, "compose.yml", "main", "compose", version="0.9.41", core_wheel="w.whl")
    data = rel.state()
    assert data["compose_run_id"] is None and data["compose"] == "2026-11-13T13:30:00Z"
    assert data["compose_dispatch"] == {"repo": rel.PLUGINS, "workflow": "compose.yml", "ref": "main", "sha": SHA, "event": "workflow_dispatch",
                                        "inputs": {"version": "0.9.41", "core_wheel": "w.whl"}}


def test_only_the_exact_run_is_adopted_and_the_others_are_recorded_with_their_reason(rel):
    rel.dispatch(rel.PLUGINS, "compose.yml", "main", "compose")
    rel.world["listed"] = [row(1, created="2026-11-13T13:00:00Z"), row(2, event="schedule"), row(3, sha=OTHER), row(4)]
    run = rel.ours(rel.PLUGINS, "compose.yml", "compose")
    assert run["databaseId"] == 4
    data = rel.state()
    assert data["compose_run_id"] == 4 and data["compose_adopted"]["run"] == 4 and data["compose_adopted"]["sha"] == SHA
    assert data["compose_rejected"] == {"2": "not a workflow_dispatch run", "3": f"head {OTHER[:12]} is not the dispatched {SHA[:12]}"}


def test_once_adopted_the_run_is_read_by_its_id_and_its_identity_is_checked(rel):
    rel.dispatch(rel.PLUGINS, "compose.yml", "main", "compose")
    rel.world["listed"] = [row(4)]
    rel.ours(rel.PLUGINS, "compose.yml", "compose")
    rel.world["listed"] = [row(5)]  # a later run is never taken over the adopted one
    rel.world["by_id"][4] = {"id": 4, "path": ".github/workflows/compose.yml", "head_sha": SHA, "event": "workflow_dispatch", "status": "completed",
                             "conclusion": "failure", "created_at": "2026-11-13T13:31:00Z", "run_attempt": 1}
    assert rel.ours(rel.PLUGINS, "compose.yml", "compose")["databaseId"] == 4
    rel.world["by_id"][4] = rel.world["by_id"][4] | {"head_sha": OTHER}
    with pytest.raises(rel.Stop, match="identity changed"):
        rel.ours(rel.PLUGINS, "compose.yml", "compose")


def test_two_runs_that_could_be_the_dispatch_are_refused_never_guessed(rel):
    rel.dispatch(rel.PLUGINS, "release.yml", "main", "plugin-heinzel-audio-0.1.5", plugin="heinzel-audio")
    rel.world["listed"] = [row(7), row(8)]  # plugin B dispatched at the same head: the list cannot show the inputs
    with pytest.raises(rel.Stop, match=r"ambiguous release\.yml runs 7, 8"):
        rel.ours(rel.PLUGINS, "release.yml", "plugin-heinzel-audio-0.1.5")
    assert rel.state().get("plugin-heinzel-audio-0.1.5_run_id") is None


def test_no_matching_run_yet_waits(rel):
    rel.dispatch(rel.PLUGINS, "compose.yml", "main", "compose")
    rel.world["listed"] = [row(3, sha=OTHER)]
    assert rel.ours(rel.PLUGINS, "compose.yml", "compose") is None


def test_an_unresolvable_ref_is_not_dispatched(rel):
    rel.world["head"] = "not-a-sha"
    with pytest.raises(rel.Stop, match=r"cannot resolve main of compose\.yml"):
        rel.dispatch(rel.PLUGINS, "compose.yml", "main", "compose")
    assert not any(c[:2] == ("workflow", "run") for c in rel.world["calls"])


def test_a_receipt_from_before_the_binding_keeps_the_timestamp_route(rel, monkeypatch):
    rel.remember(compose="2026-11-13T13:30:00Z")  # in flight before this change: no dispatch record
    monkeypatch.setattr(rel, "runs", lambda repo, wf: [row(9)])
    assert rel.ours(rel.PLUGINS, "compose.yml", "compose")["databaseId"] == 9


def test_a_marked_dispatch_keeps_its_marker_binding(rel):
    rel.remember(acceptance_marker="m1", acceptance_commit=SHA)
    rel.dispatch(rel.CORE, "acceptance.yml", "main", "acceptance")
    assert "acceptance_dispatch" not in rel.state() and not any(c[0] == "api" for c in rel.world["calls"])
