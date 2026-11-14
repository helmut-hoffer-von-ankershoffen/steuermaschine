"""queue_lib.main_checkpoint: merged_candidate (the historical release anchor) and verify_progress (the focused chain),
on a fake GitHub of dicts and paths; the candidate helpers of release_candidate are replaced by small fakes."""

import hashlib
import io
import json
import zipfile

import policy_kit as kit
import pytest

from steuermaschine_heinzel.native.queue_lib import hzlib
from steuermaschine_heinzel.native.queue_lib import main_checkpoint as mc
from steuermaschine_heinzel.native.queue_lib import release_candidate as rc

HEAD = "1" * 40  # the release PR head
SQUASH = "2" * 40
SOURCE = "3" * 40
BASE = "4" * 40
TREE = "5" * 40
RUN = 900
PR = 55
SRC = {kit.WORKFLOW: "0" * 64, kit.RECORDER: "0" * 64}


def config():
    return {"schema": 2, "pr": PR, "run": RUN, "core_repo": "/core"}


def archive_bytes(proof):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zipped:
        zipped.writestr(rc.PROOF_FILE, json.dumps(proof))
    return buf.getvalue()


def good_proof(**more):
    return {"schema": 1, "repository": hzlib.CORE, "event": "pull_request", "pr": PR, "version": "0.9.41", "run_id": RUN, "run_attempt": 1,
            "source_commit": SOURCE, "head": HEAD, "base": BASE, "tree": TREE, "sources": dict(SRC), **more}


def world(monkeypatch, proof=None, **patch):
    proof = proof or good_proof()
    raw = archive_bytes(proof)
    run = {"id": RUN, "run_attempt": 1, "status": "completed", "conclusion": "success", "event": "pull_request", "head_sha": HEAD, "head_branch": "release/v0.9.41",
           "repository": {"full_name": hzlib.CORE}, "head_repository": {"full_name": hzlib.CORE}, "path": kit.WORKFLOW,
           "created_at": "2026-11-13T10:00:00+00:00", "updated_at": "2026-11-13T10:30:00+00:00"}
    pr = {"number": PR, "state": "closed", "merged": True, "draft": False, "merged_at": "2026-11-13T11:00:00Z", "head": {"sha": HEAD}, "base": {"sha": BASE}, "merge_commit_sha": SQUASH}
    artifact = {"name": rc.ARTIFACT, "expired": False, "id": 7, "workflow_run": {"id": RUN, "head_sha": HEAD}, "digest": "sha256:" + hashlib.sha256(raw).hexdigest(), "size_in_bytes": len(raw)}
    commit = lambda sha, parents: {"sha": sha, "commit": {"tree": {"sha": TREE}}, "parents": [{"sha": p} for p in parents]}  # noqa: E731
    data = {
        f"actions/runs/{RUN}": run, f"pulls/{PR}": pr, f"actions/runs/{RUN}/attempts/1": dict(run),
        f"actions/runs/{RUN}/artifacts?per_page=100": {"total_count": 1, "artifacts": [artifact]},
        f"commits/{SOURCE}": commit(SOURCE, [BASE, HEAD]), f"commits/{HEAD}": commit(HEAD, []), f"commits/{SQUASH}": commit(SQUASH, [BASE]),
    }
    for key, value in patch.items():
        target = data
        *path, last = key.split(".")
        for step in path:
            target = target[step] if step in target else target[int(step)] if step.isdigit() else target[step]
        target[last] = value
    jobs = [{"name": "tests", "run_id": RUN, "run_attempt": 1, "head_sha": HEAD, "status": "completed", "conclusion": "success", "runner_id": 3, "steps": [1]},
            {"name": "optional", "run_id": RUN, "run_attempt": 1, "head_sha": HEAD, "status": "completed", "conclusion": "skipped"}]
    monkeypatch.setattr(rc, "release_version", lambda *a: "0.9.41")
    monkeypatch.setattr(rc, "job_inventory", lambda api, run_id: jobs)
    monkeypatch.setattr(rc, "quarantine_lane", lambda jobs, run_id, head, policy: jobs)
    monkeypatch.setattr(mc.core_policy, "at_commit_api", lambda api, sha: kit.policy(
        commit=sha, jobs={"tests": "success", "optional": "skipped"}))
    return data, raw, jobs


def run_candidate(data, raw, **kw):
    return mc.merged_candidate(kw.get("config", config()), lambda path: data[path], lambda path, limit: raw)


def test_merged_candidate_accepts_the_authenticated_release(monkeypatch):
    data, raw, _jobs = world(monkeypatch)
    out = run_candidate(data, raw)
    assert out == {"head": SQUASH, "tree": TREE, "base": BASE, "run": RUN, "candidate_head": HEAD, "artifact_sha256": hashlib.sha256(raw).hexdigest(), "jobs": 2}


@pytest.mark.parametrize("patch,message", [
    ({"pulls/55.head": {"sha": "bad"}}, "head or actual squash missing"),
    ({"pulls/55.merge_commit_sha": None}, "head or actual squash missing"),
    ({"pulls/55.state": "open"}, "not authenticated merged PR"),
    ({"pulls/55.merged": False}, "not authenticated merged PR"),
    ({"pulls/55.draft": True}, "not authenticated merged PR"),
    ({"pulls/55.merged_at": None}, "not authenticated merged PR"),
    ({"pulls/55.number": 1}, "not authenticated merged PR"),
    ({f"actions/runs/{RUN}.head_branch": "release/v9.9.9"}, "noncanonical merged release"),
    ({f"actions/runs/{RUN}.run_attempt": 2}, "genuine original candidate success"),
    ({f"actions/runs/{RUN}.conclusion": "failure"}, "genuine original candidate success"),
    ({f"actions/runs/{RUN}.event": "push"}, "genuine original candidate success"),
    ({f"actions/runs/{RUN}.path": "other"}, "genuine original candidate success"),
    ({f"actions/runs/{RUN}.created_at": None}, "timestamps missing"),
    ({f"actions/runs/{RUN}.updated_at": "2026-11-13T09:00:00+00:00"}, "timestamps reversed"),
    ({f"actions/runs/{RUN}/attempts/1.conclusion": "cancelled"}, "original attempt identity changed"),
    ({f"actions/runs/{RUN}/artifacts?per_page=100.total_count": 5}, "truncated candidate artifacts"),
    ({f"actions/runs/{RUN}/artifacts?per_page=100.artifacts": []}, "truncated candidate artifacts"),
    ({f"actions/runs/{RUN}/artifacts?per_page=100.total_count": 0}, "candidate source artifact missing"),
])
def test_merged_candidate_refusals_by_evidence(monkeypatch, patch, message):
    if message == "candidate source artifact missing":
        data, raw, _jobs = world(monkeypatch)
        data[f"actions/runs/{RUN}/artifacts?per_page=100"] = {"total_count": 0, "artifacts": []}
    else:
        data, raw, _jobs = world(monkeypatch, **patch)
    with pytest.raises(rc.CandidateError, match=message):
        run_candidate(data, raw)


def artifact_patch(**more):
    return {f"actions/runs/{RUN}/artifacts?per_page=100.artifacts": None, **more}


@pytest.mark.parametrize("change,message", [
    (lambda a: a.update(expired=True), "wrong candidate source artifact"),
    (lambda a: a.update(id=0), "wrong candidate source artifact"),
    (lambda a: a.update(workflow_run={"id": 1, "head_sha": HEAD}), "wrong candidate source artifact"),
    (lambda a: a.update(digest="sha256:00"), "digest mismatch"),
    (lambda a: a.update(size_in_bytes=1), "digest mismatch"),
])
def test_merged_candidate_artifact_refusals(monkeypatch, change, message):
    data, raw, _jobs = world(monkeypatch)
    change(data[f"actions/runs/{RUN}/artifacts?per_page=100"]["artifacts"][0])
    with pytest.raises(rc.CandidateError, match=message):
        run_candidate(data, raw)


def test_merged_candidate_refuses_a_duplicated_artifact_and_a_head_without_a_policy(monkeypatch):
    data, raw, _jobs = world(monkeypatch)
    block = data[f"actions/runs/{RUN}/artifacts?per_page=100"]
    block["artifacts"] = block["artifacts"] * 2
    block["total_count"] = 2
    with pytest.raises(rc.CandidateError, match="missing or duplicated"):
        run_candidate(data, raw)
    data, raw, _jobs = world(monkeypatch)

    def no_policy(api, sha):
        raise rc.CandidateError(f"scripts/release_policy.json missing at {sha}")

    monkeypatch.setattr(mc.core_policy, "at_commit_api", no_policy)
    with pytest.raises(rc.CandidateError, match=r"release_policy\.json missing"):
        run_candidate(data, raw)


def test_merged_candidate_archive_and_proof_refusals(monkeypatch):
    data, raw, _jobs = world(monkeypatch)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zipped:
        zipped.writestr("other.json", "{}")
    other = buf.getvalue()
    data[f"actions/runs/{RUN}/artifacts?per_page=100"]["artifacts"][0].update(digest="sha256:" + hashlib.sha256(other).hexdigest(), size_in_bytes=len(other))
    with pytest.raises(rc.CandidateError, match="unexpected candidate source archive"):
        run_candidate(data, other)
    for proof, message in ((good_proof(head="zz"), "malformed candidate source proof"), (good_proof(schema=2), "candidate source identity differs"),
                           (good_proof(pr=1), "source identity"), (good_proof(version="9"), "source identity"), (good_proof(base="6" * 40), "source identity"),
                           (good_proof(run_attempt=2), "source identity"), (good_proof(event="push"), "source identity")):
        data, raw, _jobs = world(monkeypatch, proof=proof)
        with pytest.raises(rc.CandidateError, match=message):
            run_candidate(data, raw)
    data, raw, _jobs = world(monkeypatch, proof=good_proof())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zipped:
        zipped.writestr(rc.PROOF_FILE, "[1]")
    arr = buf.getvalue()
    data[f"actions/runs/{RUN}/artifacts?per_page=100"]["artifacts"][0].update(digest="sha256:" + hashlib.sha256(arr).hexdigest(), size_in_bytes=len(arr))
    with pytest.raises(rc.CandidateError, match="malformed candidate source proof"):
        run_candidate(data, arr)


@pytest.mark.parametrize("patch,message", [
    ({f"commits/{HEAD}.commit": {"tree": {"sha": "9" * 40}}}, "squash trees differ"),
    ({f"commits/{SQUASH}.sha": "x"}, "squash trees differ"),
    ({f"commits/{SOURCE}.parents": [{"sha": BASE}]}, "parent differs"),
    ({f"commits/{SQUASH}.parents": [{"sha": HEAD}]}, "parent differs"),
])
def test_merged_candidate_commit_refusals(monkeypatch, patch, message):
    data, raw, _jobs = world(monkeypatch, **patch)
    with pytest.raises(rc.CandidateError, match=message):
        run_candidate(data, raw)


def test_merged_candidate_workload_refusals(monkeypatch):
    data, raw, jobs = world(monkeypatch)
    monkeypatch.setattr(rc, "quarantine_lane", lambda jobs, run_id, head, policy: jobs[:1])
    with pytest.raises(rc.CandidateError, match="workload inventory differs"):
        run_candidate(data, raw)
    monkeypatch.setattr(rc, "quarantine_lane", lambda jobs, run_id, head, policy: jobs)
    for change in ({"conclusion": "failure"}, {"run_attempt": 2}, {"head_sha": "x"}, {"status": "queued"}):
        data, raw, jobs = world(monkeypatch)
        jobs[0].update(change)
        with pytest.raises(rc.CandidateError, match="failed, changed, skipped or reran"):
            run_candidate(data, raw)
    for change in ({"runner_id": 0}, {"steps": []}):
        data, raw, jobs = world(monkeypatch)
        jobs[0].update(change)
        with pytest.raises(rc.CandidateError, match="never started"):
            run_candidate(data, raw)


def test_merged_candidate_refuses_evidence_that_changes_during_validation(monkeypatch):
    data, raw, _jobs = world(monkeypatch)
    reads = {"n": 0}

    def api(path):
        if path == f"actions/runs/{RUN}":
            reads["n"] += 1
            if reads["n"] == 2:
                return {**data[path], "updated_at": "2026-11-13T12:00:00+00:00"}
        return data[path]

    with pytest.raises(rc.CandidateError, match="changed during validation"):
        mc.merged_candidate(config(), api, lambda path, limit: raw)


# ---------- verify_progress ----------

TIP, MID, ANCHOR, INCIDENT = "a" * 40, "b" * 40, "c" * 40, "d" * 40
ANCHOR_BASE, ANCHOR_TREE = "e" * 40, "f" * 40


def chain_world(monkeypatch, links=((TIP, ANCHOR),), **over):
    plans = {cursor: ({"base": base}, f"tree-{cursor[0]}") for cursor, base in links}
    plans[ANCHOR] = ({"base": ANCHOR_BASE}, ANCHOR_TREE)
    state = {"tip": TIP, "later": TIP}
    reads = {"main": 0}

    def api(path):
        if path == "git/ref/heads/main":
            reads["main"] += 1
            return {"object": {"sha": state["tip"] if reads["main"] == 1 else state["later"]}}
        if path.startswith("compare/"):
            left, right = path[len("compare/"):].split("...")
            if left == INCIDENT:
                return over.get("anchor_compare", {"status": "ahead", "merge_base_commit": {"sha": INCIDENT}})
            if left == right:
                return over.get("skip_compare", {"status": "identical"})
            return over.get("interval_compare", {"status": "ahead", "merge_base_commit": {"sha": left}})
        raise AssertionError(path)

    monkeypatch.setattr(mc, "main_run", lambda api_, head: 1000 + ord(head[0]))
    monkeypatch.setattr(mc, "actual_main_ci", lambda api_, head, run: {"ci": head})
    monkeypatch.setattr(mc, "merged_candidate", lambda cfg, api_, read: {"head": ANCHOR, "base": ANCHOR_BASE, "tree": ANCHOR_TREE})
    monkeypatch.setattr(mc, "selected_main", lambda api_, registry, head: plans[head])
    return api, plans, state


def verify(api, plans, **kw):
    import hashlib as h

    def segment(core, tip, head, base, run, registry):
        plan, tree = plans[head]
        return kw.get("proof", lambda *a: None)(head) or {"type": "focused-main/v1", "base": base, "head": head, "tree": tree, "run": run, "attempt": 1,
                                                          "manifest_sha256": h.sha256(mc.canonical(plan)).hexdigest()}

    return mc.verify_progress(config(), {"headSha": INCIDENT, "databaseId": 4}, {}, api, lambda p, limit: b"", segment, check_registry=kw.get("check"))


def test_verify_progress_walks_the_chain_back_to_the_anchor(monkeypatch, capsys):
    api, plans, _state = chain_world(monkeypatch, links=((TIP, MID), (MID, ANCHOR)))
    checked = []
    out = verify(api, plans, check=checked.append)
    assert out["type"] == "merged-release-focused-continuity/v1" and out["head"] == TIP and checked == [TIP]
    assert [p["head"] for p in out["segments"]] == [ANCHOR, MID, TIP] and out["incident"] == {"head": INCIDENT, "run": 4}
    assert "queue.checkpoint.phase" in capsys.readouterr().err


def test_verify_progress_single_interval_without_registry_check(monkeypatch):
    api, plans, _state = chain_world(monkeypatch)
    assert [p["head"] for p in verify(api, plans)["segments"]] == [ANCHOR, TIP]


@pytest.mark.parametrize("kwargs,message", [
    (dict(anchor_compare={"status": "diverged"}), "does not descend from the retained incident"),
    (dict(anchor_compare={"status": "ahead", "merge_base_commit": {"sha": "x"}}), "does not descend"),
    (dict(interval_compare={"status": "behind"}), "not an ancestor-bound interval"),
    (dict(skip_compare={"status": "diverged"}), "skips before the release checkpoint"),
])
def test_verify_progress_chain_refusals(monkeypatch, kwargs, message):
    api, plans, _state = chain_world(monkeypatch, **kwargs)
    with pytest.raises(rc.CandidateError, match=message):
        verify(api, plans)


def test_verify_progress_other_refusals(monkeypatch, capsys):
    api, plans, state = chain_world(monkeypatch)
    with pytest.raises(rc.CandidateError, match="missing retained original failure identity"):
        mc.verify_progress(config(), {"headSha": "bad"}, {}, api, None, None)
    api, plans, state = chain_world(monkeypatch)
    monkeypatch.setattr(mc, "selected_main", lambda api_, registry, head: ({"base": "9" * 40}, ANCHOR_TREE) if head == ANCHOR else plans[head])
    with pytest.raises(rc.CandidateError, match="release main scope differs"):
        verify(api, plans)
    api, plans, state = chain_world(monkeypatch, links=((TIP, MID), (MID, TIP)))
    with pytest.raises(rc.CandidateError, match="cycles or exceeds bound"):
        verify(api, plans)
    api, plans, state = chain_world(monkeypatch)
    state["later"] = "9" * 40
    with pytest.raises(rc.CandidateError, match="main or latest workflow advanced"):
        verify(api, plans)
    api, plans, state = chain_world(monkeypatch)
    state["tip"] = "bad"
    with pytest.raises(rc.CandidateError, match="invalid current main"):
        verify(api, plans)


def test_verify_progress_refuses_mismatched_reader_evidence_and_reports_the_pair(monkeypatch, capsys):
    api, plans, _state = chain_world(monkeypatch)
    with pytest.raises(rc.CandidateError, match="returned mismatched evidence"):
        verify(api, plans, proof=lambda head: {"type": "other"})
    assert "pair_refused" in capsys.readouterr().err
    api, plans, _state = chain_world(monkeypatch)

    def boom(head):
        raise ValueError("reader failed")

    with pytest.raises(ValueError, match="reader failed"):
        verify(api, plans, proof=boom)


def test_verify_progress_refuses_when_the_reader_pool_cannot_start(monkeypatch):
    api, plans, _state = chain_world(monkeypatch)

    class Broken:
        def __init__(self, max_workers):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def submit(self, *a):
            raise RuntimeError("cannot start thread")

        def shutdown(self, wait):
            pass

    monkeypatch.setattr(mc, "ThreadPoolExecutor", Broken)
    with pytest.raises(rc.CandidateError, match="could not start"):
        verify(api, plans)
