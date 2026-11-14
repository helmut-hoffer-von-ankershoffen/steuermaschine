"""A published manifest is not ready for PyPI until its exact official producer has succeeded."""

import hashlib
import json
from pathlib import Path

import pytest

COMMIT = "a" * 40
TAG = "v1.2.3"


@pytest.fixture
def producer(tool, monkeypatch):
    release = tool("hz-release")
    manifest = {
        "schema": 1, "project": "heinzel", "version": "1.2.3", "commit": COMMIT,
        "files": {"heinzel-1.2.3-py3-none-any.whl": "b" * 64, "heinzel-1.2.3.tar.gz": "c" * 64},
        "producer": {"run_id": 123, "attempt": 1, "source_ref": "refs/heads/main", "workflow": ".github/workflows/release-tag.yml"},
    }
    run = {"id": 123, "run_attempt": 1, "repository": {"full_name": release.CORE}, "head_sha": COMMIT,
           "path": ".github/workflows/release-tag.yml", "head_branch": "main", "event": "push",
           "status": "completed", "conclusion": "success"}
    remote = {"manifest": manifest, "run": run, "workflow": 'python scripts/pypi_release.py promote "$TAG" release-artifacts\n',
              "tag": {"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": COMMIT}}, "assets": True}
    calls = []

    def gh(*args):
        calls.append(args)
        endpoint = args[1]
        if "/git/ref/tags/" in endpoint:
            return 0, json.dumps(remote["tag"])
        if "/git/tags/" in endpoint:
            return 0, json.dumps({"object": {"type": "commit", "sha": COMMIT}})
        if "/contents/.github/workflows/pypi.yml?ref=" in endpoint:
            assert endpoint.endswith("?ref=" + COMMIT)
            return 0, remote["workflow"]
        raw = json.dumps(remote["manifest"]) if not isinstance(remote["manifest"], str) else remote["manifest"]
        if "/releases/tags/" in endpoint:
            asset = {"name": "artifact-manifest.json", "id": 456, "digest": "sha256:" + hashlib.sha256(raw.encode()).hexdigest(),
                     "browser_download_url": f"https://github.com/{release.CORE}/releases/download/{TAG}/artifact-manifest.json"}
            assets = [asset] if remote["assets"] else []
            return 0, json.dumps({"tag_name": TAG, "draft": False, "prerelease": False, "assets": assets})
        if "/releases/assets/" in endpoint:
            assert endpoint.endswith("/456")
            return 0, raw
        if "/actions/runs/" in endpoint:
            return 0, json.dumps(remote["run"])
        pytest.fail(f"Unexpected GitHub request: {args}")

    monkeypatch.setattr(release.hzlib, "gh", gh)
    return release, remote, calls


def test_successful_exact_manifest_producer_is_ready(producer):
    release, _, _ = producer
    assert release.release_producer_ready(TAG)


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "pending", "requested"])
def test_published_asset_with_unfinished_producer_is_not_ready(producer, status):
    release, remote, _ = producer
    remote["run"].update(status=status, conclusion=None)
    assert not release.release_producer_ready(TAG)


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "skipped", "neutral", None])
def test_terminal_unsuccessful_producer_stops_without_waiting(producer, conclusion):
    release, remote, _ = producer
    remote["run"]["conclusion"] = conclusion
    with pytest.raises(release.Stop, match="ended"):
        release.release_producer_ready(TAG)


@pytest.mark.parametrize("key,value", [
    ("id", 124), ("run_attempt", 2), ("head_sha", "b" * 40), ("path", ".github/workflows/other.yml"),
    ("head_branch", "other"), ("repository", {"full_name": "other/repository"}), ("event", "pull_request"),
])
def test_wrong_actual_run_identity_is_refused(producer, key, value):
    release, remote, _ = producer
    remote["run"][key] = value
    with pytest.raises(release.Stop, match="run identity"):
        release.release_producer_ready(TAG)


@pytest.mark.parametrize("change", ["schema", "project", "version", "commit", "run", "attempt", "workflow", "ref", "malformed", "old"])
def test_malformed_or_wrong_manifest_identity_fails_closed(producer, change):
    release, remote, _ = producer
    manifest = remote["manifest"]
    if change in {"schema", "project", "version", "commit"}:
        manifest[change] = "wrong"
    elif change in {"run", "attempt", "workflow", "ref"}:
        manifest["producer"][{"run": "run_id", "ref": "source_ref"}.get(change, change)] = "wrong"
    elif change == "old":
        del manifest["producer"]
    else:
        remote["manifest"] = "not JSON"
    with pytest.raises(release.Stop, match="manifest|caller"):
        release.release_producer_ready(TAG)


def test_missing_manifest_for_new_or_unknown_workflow_waits(producer):
    release, remote, _ = producer
    remote["assets"] = False
    assert not release.release_producer_ready(TAG)


def test_exact_historical_publisher_retains_its_original_resume_path(producer):
    release, remote, calls = producer
    remote["workflow"] = (Path(__file__).parent / "fixtures/pypi-legacy.yml").read_text()
    remote["assets"] = False
    assert release.release_producer_ready(TAG)
    assert not any("/releases/" in call[1] or "/actions/runs/" in call[1] for call in calls)
    remote["workflow"] += "# Changed source cannot impersonate the historical publisher.\n"
    assert not release.release_producer_ready(TAG)


def test_official_direct_tag_producer_is_supported(producer):
    release, remote, _ = producer
    remote["manifest"]["producer"].update(source_ref=f"refs/tags/{TAG}", workflow=".github/workflows/publish.yml")
    remote["run"].update(head_branch=TAG, path=".github/workflows/publish.yml")
    assert release.release_producer_ready(TAG)


def test_annotated_tag_is_peeled_before_source_comparison(producer):
    release, remote, calls = producer
    remote["tag"]["object"] = {"type": "tag", "sha": "b" * 40}
    assert release.release_producer_ready(TAG)
    assert any("/git/tags/" in call[1] for call in calls)


def test_pypi_dispatch_waits_for_success_and_is_requested_only_once(producer, monkeypatch):
    release, remote, _ = producer
    remote["run"].update(status="in_progress", conclusion=None)
    dispatched, sleeps = [], []
    monkeypatch.setattr(release, "pypi_has", lambda *_: bool(dispatched))
    monkeypatch.setattr(release, "ours", lambda *_: None)
    def sleep(seconds):
        sleeps.append(seconds)
        remote["run"].update(status="completed", conclusion="success")
    def dispatch(*args):
        assert remote["run"]["status"] == "completed" and remote["run"]["conclusion"] == "success"
        dispatched.append(args)
    monkeypatch.setattr(release.hzlib, "sleep", sleep)
    monkeypatch.setattr(release, "dispatch", dispatch)
    release.pypi_step("heinzel", "1.2.3", TAG, 60)
    assert sleeps == [release.POLL]
    assert dispatched == [(release.CORE, "pypi.yml", TAG, "pypi")]


@pytest.mark.parametrize("wrong", ["failure", "identity"])
def test_failed_or_wrong_producer_cannot_dispatch_pypi(producer, monkeypatch, wrong):
    release, remote, _ = producer
    remote["run"]["conclusion" if wrong == "failure" else "id"] = "failure" if wrong == "failure" else 124
    monkeypatch.setattr(release, "pypi_has", lambda *_: False)
    monkeypatch.setattr(release, "ours", lambda *_: None)
    monkeypatch.setattr(release, "dispatch", lambda *_: pytest.fail("must not dispatch"))
    monkeypatch.setattr(release.hzlib, "sleep", lambda *_: pytest.fail("must fail without waiting"))
    with pytest.raises(release.Stop):
        release.pypi_step("heinzel", "1.2.3", TAG, 60)


def test_producer_wait_consumes_the_existing_pypi_timeout(producer, monkeypatch):
    release, _, _ = producer
    clock, waits = [100.0], []
    monkeypatch.setattr(release.time, "time", lambda: clock[0])
    monkeypatch.setattr(release, "pypi_has", lambda *_: False)
    monkeypatch.setattr(release, "ours", lambda *_: None)
    monkeypatch.setattr(release, "dispatch", lambda *_: None)
    def wait(label, done, timeout, check=None):
        waits.append((label, timeout))
        clock[0] += 40 if label == "release producer" else 0
    monkeypatch.setattr(release, "wait", wait)
    release.pypi_step("heinzel", "1.2.3", TAG, 60)
    assert waits == [("release producer", 60.0), ("PyPI heinzel 1.2.3", 20.0)]


@pytest.mark.parametrize("already_published", [True, False])
def test_existing_publication_or_pypi_run_resumes_without_new_producer_adoption(producer, monkeypatch, already_published):
    release, _, _ = producer
    checks = iter([already_published, True])
    monkeypatch.setattr(release, "pypi_has", lambda *_: next(checks))
    monkeypatch.setattr(release, "ours", lambda *_: {"databaseId": 999})
    monkeypatch.setattr(release, "release_producer_ready", lambda *_: pytest.fail("must preserve resume"))
    monkeypatch.setattr(release, "dispatch", lambda *_: pytest.fail("must not redispatch"))
    release.pypi_step("heinzel", "1.2.3", TAG, 60)


@pytest.mark.parametrize("field,value", [
    ("ref", "refs/heads/v1.2.3"), ("object", {"type": "commit", "sha": "wrong"}),
    ("object", {"type": "commit", "sha": int("1" * 40)}), ("object", {"type": "tag", "sha": int("1" * 40)}),
])
def test_wrong_or_malformed_exact_tag_ref_stops(producer, field, value):
    release, remote, _ = producer
    remote["tag"][field] = value
    with pytest.raises(release.Stop, match="tag"):
        release.release_producer_ready(TAG)


def test_exhausted_producer_wait_cannot_dispatch_after_shared_deadline(producer, monkeypatch):
    release, _, _ = producer
    clock = [100.0]
    monkeypatch.setattr(release.time, "time", lambda: clock[0])
    monkeypatch.setattr(release, "pypi_has", lambda *_: False)
    monkeypatch.setattr(release, "ours", lambda *_: None)
    monkeypatch.setattr(release, "dispatch", lambda *_: pytest.fail("deadline expired before dispatch"))
    monkeypatch.setattr(release, "wait", lambda *_args: clock.__setitem__(0, 161.0))
    with pytest.raises(release.Stop, match="timed out before dispatch"):
        release.pypi_step("heinzel", "1.2.3", TAG, 60)
