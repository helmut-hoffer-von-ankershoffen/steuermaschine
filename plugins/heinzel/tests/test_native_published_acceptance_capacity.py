"""Exercise independently audited published acceptance recovery without GitHub or release state."""

import base64
import hashlib
import io
import json
import zipfile
from copy import deepcopy
from types import SimpleNamespace

import pytest

from steuermaschine_heinzel.native.release_lib import acceptance_capacity as capacity
from steuermaschine_heinzel.native.release_lib import rejection, runner

VERSION = "0.9.99"
HEAD = "a" * 40
MARKER = "release-" + "b" * 32
NOW = "2026-11-16T13:00:00Z"
RUN = 111
REPO = {"id": 95, "full_name": runner.CORE}


@pytest.fixture
def proof(tmp_path, monkeypatch):
    tag = f"v{VERSION}"
    snapshot = {"version": VERSION, "tag": tag, "commit": HEAD, "wheel_sha256": "c" * 64, "profile_sha256": "d" * 64}
    identity = {"version": VERSION, "app_version": VERSION, "commit": HEAD, "ref": tag, "snapshot": snapshot}
    original = {
        "version": VERSION, "finished": True, "accepted": False, "accepted_verdict": ["original rejection"],
        "snapshot": snapshot, "acceptance_identity": identity, "acceptance_commit": HEAD,
        "acceptance_marker": MARKER, "acceptance_snapshot_mode": True, "acceptance_requested": True,
        "acceptance_run": RUN, "acceptance_run_id": RUN, "acceptance_run_attempt": 1,
    }
    path = tmp_path / "receipt.json"
    raw = json.dumps(original).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    run = {
        "id": RUN, "run_attempt": 1, "head_sha": HEAD, "head_branch": tag, "event": "workflow_dispatch",
        "path": ".github/workflows/acceptance.yml", "display_title": f"Acceptance {VERSION} {MARKER}",
        "repository": REPO, "head_repository": REPO, "status": "completed", "conclusion": "failure",
        "workflow_id": 44, "check_suite_id": 90, "created_at": NOW,
    }
    common = {"run_id": RUN, "run_attempt": 1, "head_sha": HEAD, "status": "completed"}
    linux = common | {
        "id": 201, "name": capacity.LINUX, "runner_name": "GitHub Actions 3", "runner_id": 19,
        "runner_group_name": "GitHub Actions", "runner_group_id": 0, "labels": ["ubuntu-24.04"],
        "conclusion": "success", "started_at": NOW, "completed_at": NOW,
        "steps": [{"name": name, "status": "completed", "conclusion": "success"}
                  for name in ("Run the acceptance harness", "Upload the receipt")],
    }
    mac = common | {"id": 202, "name": capacity.MAC, "runner_name": "", "runner_id": 0, "steps": [],
                    "conclusion": "cancelled", "labels": ["macos-latest"]}
    notes = [{"path": ".github", "blob_href": f"https://github.com/{runner.CORE}/blob/{HEAD}/.github",
              "annotation_level": level, "message": message}
             for level, message in (("failure", capacity.remote.NOT_ACQUIRED), ("notice", capacity.remote.CAPACITY))]
    published = {"expect_version": VERSION, "expect_app_version": VERSION,
                 "core_wheel_sha256": snapshot["wheel_sha256"], "profile_sha256": snapshot["profile_sha256"]}
    ci = {"repository": runner.CORE, "sha": HEAD, "run_id": str(RUN), "runner_os": "Linux",
          "run_attempt": "1", "event": "workflow_dispatch"}
    result = {"verdict": "ACCEPTED", "public_path_proof": True, "published": published, "ci": ci}
    artifact = {"id": 300, "name": "acceptance-result-Linux", "expired": False,
                "workflow_run": {"id": RUN, "head_sha": HEAD, "head_branch": tag,
                                 "repository_id": 95, "head_repository_id": 95}}

    def download(artifact_id):
        assert artifact_id == artifact["id"]
        return probe.archive

    calls = []

    def gh(*args):
        calls.append(args)
        if args[0] == "api":
            return 0, probe.tag_head
        assert args[0:3] == ("workflow", "run", "acceptance.yml")
        assert json.loads(path.read_bytes())["acceptance_requested"] is True
        return probe.post_code, ""

    rel = SimpleNamespace(
        CORE=runner.CORE, paths=lambda: (None, path), tail_receipt=lambda _version: path,
        release_rejection=rejection, acceptance_receipt_matches=runner.acceptance_receipt_matches,
        release_snapshot=SimpleNamespace(capture=lambda _version: probe.published_snapshot,
                                         workflow_supports_snapshot=lambda commit: commit == HEAD),
        write_state=lambda data: path.write_text(json.dumps(data)), now_iso=lambda _offset=0: NOW,
        hzlib=SimpleNamespace(gh=gh),
    )
    probe = SimpleNamespace(rel=rel, path=path, raw=raw, identity=identity, original=original, run=run,
                            latest=deepcopy(run), linux=linux, mac=mac, notes=notes, result=result,
                            artifact=artifact, published_snapshot=deepcopy(snapshot), calls=calls,
                            post_code=0, tag_head=HEAD, receipt_sha=hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(capacity.remote, "get", lambda path: probe.run if path.endswith("/attempts/1") else probe.latest)
    monkeypatch.setattr(capacity.remote, "paged", lambda _path, key: [linux, mac] if key == "jobs" else [artifact])
    monkeypatch.setattr(capacity.remote, "annotations", lambda job: notes if job == mac else [])
    monkeypatch.setattr(capacity.remote, "download", download)
    update_archive(probe)
    return probe


def update_archive(probe):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("result.json", json.dumps(probe.result))
    probe.archive = stream.getvalue()
    probe.artifact["digest"] = f"sha256:{hashlib.sha256(probe.archive).hexdigest()}"


def recover(probe, audit=None):
    return capacity.recover(probe.rel, VERSION, RUN, 1, probe.receipt_sha, audit)


def test_prepare_then_request_preserves_original_and_pins_fresh_dispatch(proof):
    plan = recover(proof)
    assert plan == {"audit_sha256": plan["audit_sha256"], "replacement_marker": None,
                    "mac_job": 202, "linux_artifact": 300, "head": HEAD}
    assert proof.calls == [] and proof.path.read_bytes() == proof.raw
    requested = recover(proof, plan["audit_sha256"])
    state = json.loads(proof.path.read_bytes())
    marker = requested["replacement_marker"]
    assert marker != MARKER and state["acceptance_marker"] == marker
    assert state["accepted"] is False and state["finished"] is False and state["acceptance_run_id"] is None
    assert state["acceptance_identity"] == proof.identity
    archived = state["acceptance_attempts"][0]
    assert archived["acceptance_run_id"] == RUN and archived["acceptance_run_attempt"] == 1
    assert base64.b64decode(archived["receipt_base64"]) == proof.raw
    assert state["acceptance_capacity"]["audit"]["evidence"]["linux_receipt"]["id"] == 300
    assert proof.calls[1][5:] == (
        "--ref", f"v{VERSION}", "-f", f"expected_version={VERSION}", "-f", f"expected_app_version={VERSION}",
        "-f", f"expected_wheel_sha256={'c' * 64}", "-f", f"expected_profile_sha256={'d' * 64}",
        "-f", f"release_attempt={marker}",
    )
    repeated = recover(proof, plan["audit_sha256"])
    assert repeated["request_recorded"] is True and repeated["replacement_marker"] == marker
    assert len(proof.calls) == 2


def test_ambiguous_dispatch_leaves_intent_and_rejects_duplicate_with_updated_digest(proof):
    plan = recover(proof)
    proof.post_code = 1
    with pytest.raises(capacity.remote.InvalidCapacity, match="outcome uncertain"):
        recover(proof, plan["audit_sha256"])
    state = json.loads(proof.path.read_bytes())
    assert state["acceptance_requested"] and state["acceptance_capacity"]["marker"] == state["acceptance_marker"]
    repeated = recover(proof, plan["audit_sha256"])
    assert repeated["request_recorded"] is True
    with pytest.raises(capacity.remote.InvalidCapacity, match="not an eligible rejected release"):
        capacity.recover(proof.rel, VERSION, RUN, 1, hashlib.sha256(proof.path.read_bytes()).hexdigest(), None)
    assert len(proof.calls) == 2


@pytest.mark.parametrize(
    ("target", "key", "value", "error"),
    [
        ("linux", "conclusion", "failure", "Linux acceptance job"),
        ("linux", "steps", [], "never executed"),
        ("mac", "runner_id", 10, "never-acquired"),
        ("mac", "steps", [{"name": "executed"}], "never-acquired"),
        ("mac", "conclusion", "failure", "never-acquired"),
        ("latest", "run_attempt", 2, "workflow identity changed"),
        ("latest", "check_suite_id", 999, "rerun"),
        ("run", "head_branch", "main", "workflow identity"),
        ("published_snapshot", "wheel_sha256", "e" * 64, "snapshot or tagged harness"),
        ("artifact", "digest", "sha256:wrong", "digest differs"),
    ],
)
def test_prepare_refuses_unproven_capacity_or_changed_evidence(proof, target, key, value, error):
    getattr(proof, target)[key] = value
    with pytest.raises(capacity.remote.InvalidCapacity, match=error):
        recover(proof)
    assert proof.path.read_bytes() == proof.raw and proof.calls == []


def test_refuses_unexplained_cancel_and_wrong_linux_receipt(proof):
    note = proof.notes.pop()
    with pytest.raises(capacity.remote.InvalidCapacity, match="specific not-acquired"):
        recover(proof)
    proof.notes.append(note)
    proof.result["ci"]["run_attempt"] = "2"
    update_archive(proof)
    with pytest.raises(capacity.remote.InvalidCapacity, match="did not accept the exact"):
        recover(proof)


def test_request_requires_matching_audit_and_receipt(proof):
    plan = recover(proof)
    with pytest.raises(capacity.remote.InvalidCapacity, match="audit digest differs"):
        recover(proof, "e" * 64)
    proof.path.write_bytes(proof.raw + b" ")
    with pytest.raises(capacity.remote.InvalidCapacity, match="receipt changed"):
        recover(proof, plan["audit_sha256"])
    assert proof.calls == []


def test_tag_drift_after_intent_prevents_dispatch(proof):
    plan = recover(proof)
    proof.tag_head = "e" * 40
    with pytest.raises(capacity.remote.InvalidCapacity, match="tag changed before dispatch"):
        recover(proof, plan["audit_sha256"])
    assert len(proof.calls) == 1


def test_adopt_only_fresh_exact_attempt_and_preserve_normal_verdict(proof, monkeypatch):
    plan = recover(proof)
    marker = recover(proof, plan["audit_sha256"])["replacement_marker"]
    state = json.loads(proof.path.read_bytes())
    run = proof.run | {"id": 112, "display_title": f"Acceptance {VERSION} {marker}", "conclusion": "cancelled"}
    listed = {"databaseId": 112, "attempt": 1, "headSha": HEAD, "createdAt": NOW, "displayTitle": run["display_title"]}
    monkeypatch.setattr(runner, "state", lambda: state)
    monkeypatch.setattr(runner, "runs", lambda *_pos: [listed])
    monkeypatch.setattr(runner.hzlib, "ghj", lambda *_pos, **_opts: run)
    monkeypatch.setattr(runner, "remember", lambda **fields: state.update(fields))
    run["event"] = "push"
    with pytest.raises(runner.Stop, match="workflow identity changed"):
        runner.ours(runner.CORE, "acceptance.yml", "acceptance")
    assert state["acceptance_run_id"] is None
    run["event"] = "workflow_dispatch"
    assert runner.ours(runner.CORE, "acceptance.yml", "acceptance") == listed
    assert state["acceptance_run_id"] == 112 and state["accepted"] is False
    assert runner.ours(runner.CORE, "acceptance.yml", "acceptance")["conclusion"] == "cancelled"
    run["run_attempt"] = 2
    with pytest.raises(runner.Stop, match="run attempt changed"):
        runner.ours(runner.CORE, "acceptance.yml", "acceptance")


def test_corrupt_artifact_cannot_pass_authenticated_zip_shape(proof):
    proof.archive = b"not a ZIP"
    proof.artifact["digest"] = f"sha256:{hashlib.sha256(proof.archive).hexdigest()}"
    with pytest.raises(capacity.remote.InvalidCapacity, match="receipt archive is invalid"):
        recover(proof)


def test_altered_same_snapshot_intent_and_original_run_are_refused(proof):
    plan = recover(proof)
    marker = recover(proof, plan["audit_sha256"])["replacement_marker"]
    state = json.loads(proof.path.read_bytes())
    with pytest.raises(capacity.remote.InvalidCapacity, match="not a fresh run"):
        capacity.validate_replacement(state, proof.run, runner.CORE, RUN)
    state["acceptance_marker"] = MARKER
    with pytest.raises(capacity.remote.InvalidCapacity, match="marker changed"):
        capacity.validate_replacement(state, proof.run, runner.CORE, RUN + 1)
    state["acceptance_marker"] = marker
    state["acceptance_identity"]["snapshot"]["wheel_sha256"] = "e" * 64
    with pytest.raises(capacity.remote.InvalidCapacity, match="snapshot changed"):
        capacity.validate_replacement(state, proof.run, runner.CORE, RUN + 1)


def shard(proof, monkeypatch, *, starved=("update",), mac_verdict="failure"):
    """Turn the fixture into Core's sharded acceptance run: pins, 4+4 shards, one reducer per OS (11 jobs)."""
    def linux_job(job_id, name, steps):
        return proof.linux | {"id": job_id, "name": name,
                              "steps": [{"name": step, "status": "completed", "conclusion": "success"} for step in steps]}
    jobs = [linux_job(210, capacity.PINS, ("Pin the release bytes once for every shard",))]
    jobs += [linux_job(220 + i, name, ("Run the acceptance harness", "Upload the shard receipt"))
             for i, name in enumerate(capacity.LINUX_SHARDS)]
    jobs.append(linux_job(201, capacity.LINUX, ("Reduce the four shard receipts of this OS", "Upload the receipt")))
    starved_jobs = []
    for i, (name, part) in enumerate(zip(capacity.MAC_SHARDS, capacity.SHARDS, strict=True)):
        if part in starved:
            job = proof.mac | {"id": 230 + i, "name": name}
            starved_jobs.append(job)
        else:
            job = proof.linux | {"id": 230 + i, "name": name, "labels": ["macos-latest"]}
        jobs.append(job)
    jobs.append(proof.mac | {"id": 202, "name": capacity.MAC, "conclusion": mac_verdict, "runner_id": 7,
                             "runner_name": "GitHub Actions 9", "labels": ["ubuntu-24.04"]})
    artifacts = [proof.artifact] + [{"id": 400 + i, "name": f"acceptance-shard-Linux-{part}"}
                                    for i, part in enumerate(capacity.SHARDS)]
    proof.jobs, proof.artifacts = jobs, artifacts
    monkeypatch.setattr(capacity.remote, "paged", lambda _path, key: proof.jobs if key == "jobs" else proof.artifacts)
    monkeypatch.setattr(capacity.remote, "annotations",
                        lambda job: proof.notes if any(job is starved_job for starved_job in starved_jobs) else [])
    return jobs


def test_a_sharded_run_with_a_starved_mac_shard_is_recovered_from_its_linux_receipt(proof, monkeypatch):
    shard(proof, monkeypatch)
    plan = recover(proof)
    assert plan["linux_artifact"] == 300 and plan["mac_job"] == 202
    requested = recover(proof, plan["audit_sha256"])
    assert requested["replacement_marker"] != MARKER


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda p: p.jobs.pop(), "incomplete or unexpected"),
        (lambda p: p.jobs[1].update(conclusion="failure"), "Linux acceptance job"),
        (lambda p: p.jobs[0].update(steps=[]), "never executed"),
        (lambda p: p.artifacts.append({"id": 499, "name": "acceptance-result-macOS"}), "only shard receipts"),
        (lambda p: p.artifacts.pop(0), "Linux receipt"),
    ],
)
def test_a_sharded_run_refuses_anything_but_mac_capacity(proof, monkeypatch, change, error):
    shard(proof, monkeypatch)
    change(proof)
    with pytest.raises(capacity.remote.InvalidCapacity, match=error):
        recover(proof)
    assert proof.calls == []


def test_a_mac_shard_that_ran_and_failed_is_a_real_rejection(proof, monkeypatch):
    jobs = shard(proof, monkeypatch)
    ran = next(job for job in jobs if job["name"] == capacity.MAC_SHARDS[0])
    ran["conclusion"] = "failure"
    with pytest.raises(capacity.remote.InvalidCapacity, match="never-acquired"):
        recover(proof)


@pytest.mark.parametrize(("starved", "verdict", "error"), [((), "failure", "no Mac shard was starved"),
                                                           (("update",), "success", "did not fail")])
def test_a_sharded_run_needs_starvation_and_a_failed_mac_verdict(proof, monkeypatch, starved, verdict, error):
    shard(proof, monkeypatch, starved=starved, mac_verdict=verdict)
    with pytest.raises(capacity.remote.InvalidCapacity, match=error):
        recover(proof)


def rejected_replacement(proof, run_id):
    """A normal release resume records the replacement's completed, rejected verdict."""
    state = json.loads(proof.path.read_bytes())
    state.update(finished=True, accepted=False, accepted_verdict=[f"run {run_id}: NOT ACCEPTED"],
                 acceptance_run=run_id, acceptance_run_id=run_id, acceptance_run_attempt=1)
    proof.path.write_text(json.dumps(state))
    proof.run.update(id=run_id, display_title=f"Acceptance {VERSION} {state['acceptance_marker']}")
    proof.latest.update(proof.run)
    for job in getattr(proof, "jobs", [proof.linux, proof.mac]):
        job["run_id"] = run_id
    proof.artifact["workflow_run"]["id"] = run_id
    proof.result["ci"]["run_id"] = str(run_id)
    update_archive(proof)
    return state


def recover_current(proof, run_id, audit=None):
    digest = hashlib.sha256(proof.path.read_bytes()).hexdigest()
    return capacity.recover(proof.rel, VERSION, run_id, 1, digest, audit)


def test_each_distinct_capacity_loss_requires_a_fresh_audit_and_preserves_every_receipt(proof, monkeypatch):
    shard(proof, monkeypatch)
    raw_receipts, markers, audits = [], [], []
    for run_id in (RUN, RUN + 1, RUN + 2):
        if run_id != RUN:
            rejected_replacement(proof, run_id)
        raw = proof.path.read_bytes()
        raw_receipts.append(raw)
        plan = recover_current(proof, run_id)
        audits.append(plan["audit_sha256"])
        assert proof.path.read_bytes() == raw
        if len(audits) > 1:
            with pytest.raises(capacity.remote.InvalidCapacity, match="audit digest differs"):
                recover_current(proof, run_id, audits[-2])
        requested = recover_current(proof, run_id, audits[-1])
        markers.append(requested["replacement_marker"])
        assert markers[-1] != "release-" + audits[-1][:32]
    state = json.loads(proof.path.read_bytes())
    assert len(set(markers)) == len(set(audits)) == 3
    assert [base64.b64decode(row["receipt_base64"]) for row in state["acceptance_attempts"]] == raw_receipts
    assert [json.loads(raw)["accepted_verdict"] for raw in raw_receipts] == [
        ["original rejection"], [f"run {RUN + 1}: NOT ACCEPTED"], [f"run {RUN + 2}: NOT ACCEPTED"]]
    assert len(proof.calls) == 6  # one tag verification and one explicit dispatch per reviewed rejection
    assert state["accepted"] is False and state["finished"] is False


@pytest.mark.parametrize(("target", "key", "value", "error"), [
    ("mac", "runner_id", 7, "never-acquired"),
    ("linux", "conclusion", "failure", "Linux acceptance job"),
    ("latest", "run_attempt", 2, "workflow identity changed"),
    ("published_snapshot", "profile_sha256", "e" * 64, "snapshot or tagged harness"),
])
def test_a_later_failure_must_pass_all_original_capacity_evidence(proof, target, key, value, error):
    recover(proof, recover(proof)["audit_sha256"])
    rejected_replacement(proof, RUN + 1)
    raw = proof.path.read_bytes()
    getattr(proof, target)[key] = value
    with pytest.raises(capacity.remote.InvalidCapacity, match=error):
        recover_current(proof, RUN + 1)
    assert proof.path.read_bytes() == raw and len(proof.calls) == 2


@pytest.mark.parametrize("damage", ["receipt", "encoding", "audit", "duplicate_run", "duplicate_marker", "corrected_harness", "extra_history"])
def test_corrupt_or_cross_route_history_cannot_authorize_another_run(proof, damage):
    recover(proof, recover(proof)["audit_sha256"])
    state = rejected_replacement(proof, RUN + 1)
    if damage == "receipt":
        state["acceptance_attempts"][0]["receipt_base64"] = base64.b64encode(b"{}").decode()
    elif damage == "encoding":
        state["acceptance_attempts"][0]["receipt_base64"] = "not base64!"
    elif damage == "audit":
        state["acceptance_capacity"]["audit"]["failed_run"] = 999
    elif damage == "duplicate_run":
        state.update(acceptance_run=RUN, acceptance_run_id=RUN)
    elif damage == "duplicate_marker":
        state["acceptance_marker"] = MARKER
        state["acceptance_capacity"]["marker"] = MARKER
    elif damage == "corrected_harness":
        state["acceptance_identity"]["commit"] = "e" * 40
    else:
        state["acceptance_attempts"].insert(0, {"acceptance_run_id": 999})
    proof.path.write_text(json.dumps(state))
    raw = proof.path.read_bytes()
    with pytest.raises(capacity.remote.InvalidCapacity):
        recover_current(proof, state["acceptance_run_id"])
    assert proof.path.read_bytes() == raw and len(proof.calls) == 2


def test_corrected_harness_history_without_capacity_intent_is_not_capacity_recovery(proof):
    state = proof.original | {"acceptance_attempts": [{"acceptance_run_id": 99}]}
    proof.path.write_text(json.dumps(state))
    with pytest.raises(capacity.remote.InvalidCapacity, match="corrected-harness history"):
        recover_current(proof, RUN)
    assert proof.calls == []


def test_adoption_cannot_cycle_back_to_an_older_consumed_run(proof):
    recover(proof, recover(proof)["audit_sha256"])
    rejected_replacement(proof, RUN + 1)
    recover_current(proof, RUN + 1, recover_current(proof, RUN + 1)["audit_sha256"])
    state = json.loads(proof.path.read_bytes())
    forged = proof.run | {"id": RUN, "display_title": f"Acceptance {VERSION} {state['acceptance_marker']}"}
    with pytest.raises(capacity.remote.InvalidCapacity, match="archived run"):
        capacity.validate_replacement(state, forged, runner.CORE, RUN)


def test_recorded_request_is_idempotent_after_failure_without_network_or_receipt_write(proof, monkeypatch):
    plan = recover(proof)
    recover(proof, plan["audit_sha256"])
    rejected_replacement(proof, RUN + 1)
    raw = proof.path.read_bytes()
    monkeypatch.setattr(capacity.remote, "get", lambda *_: pytest.fail("must not query provider on duplicate request"))
    result = recover(proof, plan["audit_sha256"])
    assert result["request_recorded"] is True
    assert proof.path.read_bytes() == raw and len(proof.calls) == 2


def test_only_verified_request_chooses_nonce_and_duplicate_retains_it(proof, monkeypatch):
    generated = []
    nonce = "e" * 32

    def fresh_uuid():
        generated.append(nonce)
        return SimpleNamespace(hex=nonce)

    monkeypatch.setattr(capacity.uuid, "uuid4", fresh_uuid)
    plan = recover(proof)
    assert plan["replacement_marker"] is None and generated == []
    with pytest.raises(capacity.remote.InvalidCapacity, match="audit digest differs"):
        recover(proof, "f" * 64)
    assert generated == []
    proof.path.write_bytes(proof.raw + b" ")
    with pytest.raises(capacity.remote.InvalidCapacity, match="receipt changed"):
        recover(proof, plan["audit_sha256"])
    assert generated == []
    proof.path.write_bytes(proof.raw)
    requested = recover(proof, plan["audit_sha256"])
    assert requested["replacement_marker"] == "release-" + nonce
    assert requested["replacement_marker"] != "release-" + plan["audit_sha256"][:32]
    assert generated == [nonce]
    state = json.loads(proof.path.read_bytes())
    assert state["acceptance_capacity"]["marker"] == requested["replacement_marker"]
    monkeypatch.setattr(capacity.uuid, "uuid4", lambda: pytest.fail("duplicate must reuse the stored marker"))
    duplicate = recover(proof, plan["audit_sha256"])
    assert duplicate["request_recorded"] is True
    assert duplicate["replacement_marker"] == requested["replacement_marker"]
    assert len(proof.calls) == 2


def test_corrected_harness_can_be_adopted_but_cannot_be_replaced_via_capacity(proof):
    recover(proof, recover(proof)["audit_sha256"])
    state = rejected_replacement(proof, RUN + 1)
    # The ordinary corrected-harness route retains the capacity intent as history, with a new identity and marker.
    state["acceptance_identity"] = state["acceptance_identity"] | {"ref": "main", "commit": "e" * 40}
    state["acceptance_marker"] = "release-" + "f" * 32
    state["acceptance_commit"] = "e" * 40
    capacity.validate_replacement(state, {}, runner.CORE, RUN + 2)
    proof.path.write_text(json.dumps(state))
    with pytest.raises(capacity.remote.InvalidCapacity, match="exact tagged harness"):
        recover_current(proof, RUN + 1)
    assert len(proof.calls) == 2
