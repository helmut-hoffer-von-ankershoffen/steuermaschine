"""Exercise the one-shot published acceptance recovery without GitHub or release state."""

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
    with pytest.raises(capacity.remote.InvalidCapacity, match="receipt changed"):
        recover(proof, plan["audit_sha256"])
    assert len(proof.calls) == 2


def test_ambiguous_dispatch_leaves_intent_and_rejects_duplicate_with_updated_digest(proof):
    plan = recover(proof)
    proof.post_code = 1
    with pytest.raises(capacity.remote.InvalidCapacity, match="outcome uncertain"):
        recover(proof, plan["audit_sha256"])
    state = json.loads(proof.path.read_bytes())
    assert state["acceptance_requested"] and state["acceptance_capacity"]["marker"] == state["acceptance_marker"]
    proof.receipt_sha = hashlib.sha256(proof.path.read_bytes()).hexdigest()
    with pytest.raises(capacity.remote.InvalidCapacity, match="already has a capacity request"):
        recover(proof, plan["audit_sha256"])
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
