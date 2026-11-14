"""Recovery releases only the coordinator's pre-publication hold, with durable resume ownership."""

import copy
import fcntl
import json
import stat
import urllib.error

import pytest


@pytest.fixture
def pending(tool, home, monkeypatch):
    rel = tool("hz-release")
    rel.remember(version="1.2.3")
    rel.acquire_snapshot_freeze("1.2.3")
    reply = {"data": {"repository": {"ref": None, "release": None, "pullRequests": {"nodes": [
        {"number": 477, "state": "OPEN", "headRefName": "release/v1.2.3", "baseRefName": "main",
         "headRefOid": "a" * 40, "isDraft": False, "autoMergeRequest": None}]}}}}
    def github(*args):
        if args[:2] == ("pr", "ready"):
            reply["data"]["repository"]["pullRequests"]["nodes"][0]["isDraft"] = "--undo" in args
            return 0, ""
        return 0, json.dumps(reply)
    monkeypatch.setattr(rel.hzlib, "gh", github)
    def absent(*a, **kw):
        raise urllib.error.HTTPError(a[0], 404, "absent", {}, None)
    monkeypatch.setattr(rel.urllib.request, "urlopen", absent)
    return rel, reply


def test_pause_preserves_receipt_and_other_holds_and_resume_owns_same_token(pending, monkeypatch):
    rel, _ = pending
    old = rel.state()
    own = rel.snapshot_hold_path()
    incident = rel.paths()[0]
    incident.write_text("incident")
    other = own.parent / ("b" * 32)
    other.write_text("other release")
    assert rel.main(["1.2.3", "--pause"]) == 0
    assert not own.exists()
    assert incident.read_text() == "incident" and other.read_text() == "other release"
    data = rel.state()
    assert all(data[k] == v for k, v in old.items())
    assert data["hold_events"][-1]["action"] == "pause"
    assert rel.main(["1.2.3", "--pause"]) == 0  # idempotent after crash/retry
    def next_version(request):
        assert own.read_text() == "1.2.3"  # reacquired before even the first network read
        assert rel.state()["hold_events"][-1]["action"] == "resume"
        raise rel.Stop("stop after ownership observation")
    monkeypatch.setattr(rel, "next_version", next_version)
    assert rel.main(["1.2.3", "--snapshot-acceptance"]) == 1
    assert rel.state()["freeze_token"] == old["freeze_token"]
    assert [e["action"] for e in rel.state()["hold_events"]] == ["pause-request", "pause", "resume"]
    assert incident.exists() and other.exists()


@pytest.mark.parametrize("change", ["merged", "closed", "auto_merge", "tag", "release", "missing_field", "ambiguous_pr", "bad_commit", "errors", "no_repo"])
def test_advanced_or_unknown_github_state_cannot_pause(pending, change):
    rel, reply = pending
    repo = reply["data"]["repository"]
    pr = repo["pullRequests"]["nodes"][0]
    if change in ("merged", "closed"):
        pr["state"] = change.upper()
    elif change == "auto_merge":
        pr["autoMergeRequest"] = {"enabledAt": "2026-01-01T00:00:00Z"}
    elif change in ("tag", "release"):
        repo["ref" if change == "tag" else "release"] = {"name": "v1.2.3"}
    elif change == "missing_field":
        del repo["ref"]
    elif change == "ambiguous_pr":
        repo["pullRequests"]["nodes"].append(pr.copy())
    elif change == "bad_commit":
        pr["headRefOid"] = "main"
    elif change == "errors":
        reply["errors"] = [{"message": "unknown"}]
    else:
        reply["data"]["repository"] = None
    before = rel.paths()[1].read_bytes()
    assert rel.main(["1.2.3", "--pause"]) == 1
    assert rel.snapshot_hold_path().exists() and rel.paths()[1].read_bytes() == before


@pytest.mark.parametrize("field", ["pypi", "compose", "snapshot", "acceptance", "acceptance_marker", "acceptance_run", "unknown_future_stage", "finished"])
def test_downstream_receipt_or_finished_release_cannot_pause(pending, field, monkeypatch):
    rel, _ = pending
    rel.remember(**{field: True})
    monkeypatch.setattr(rel.hzlib, "gh", lambda *a: pytest.fail("must reject before network"))
    assert rel.main(["1.2.3", "--pause"]) == 1
    assert rel.snapshot_hold_path().exists()


@pytest.mark.parametrize("value", [True, "2026-11-99T06:34:57Z"])
def test_malformed_plugin_dispatch_timestamp_cannot_pause(pending, monkeypatch, value):
    rel, _ = pending
    rel.remember(**{"plugin-heinzel-resource-0.1.4": value})
    monkeypatch.setattr(rel.hzlib, "gh", lambda *a: pytest.fail("must reject before network"))
    assert rel.main(["1.2.3", "--pause"]) == 1
    assert rel.snapshot_hold_path().exists()


def test_shared_lock_blocks_pause_during_any_release_command(pending, monkeypatch):
    rel, _ = pending
    monkeypatch.setattr(rel.hzlib, "gh", lambda *a: pytest.fail("must reject before network"))
    with rel.paths()[1].with_suffix(".lock").open("a") as other_command:
        fcntl.flock(other_command, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert rel.main(["1.2.3", "--pause"]) == 1
    assert rel.snapshot_hold_path().exists()


def test_remote_identity_race_retains_hold_and_evidence(pending, monkeypatch):
    rel, reply = pending
    reply["data"]["repository"]["pullRequests"]["nodes"][0]["isDraft"] = True
    changed = copy.deepcopy(reply)
    changed["data"]["repository"]["pullRequests"]["nodes"][0]["headRefOid"] = "c" * 40
    replies = iter([reply, changed])
    monkeypatch.setattr(rel.hzlib, "gh", lambda *a: (0, json.dumps(next(replies))))
    assert rel.main(["1.2.3", "--pause"]) == 1
    assert rel.snapshot_hold_path().exists()
    assert rel.state()["hold_events"][-1]["head"] == "a" * 40
    assert rel.state()["hold_events"][-1]["action"] == "pause-request"


def test_durable_evidence_precedes_unlink_and_new_incident_survives(pending, monkeypatch):
    rel, _ = pending
    original = rel.unlink_pinned_hold
    def raced_unlink(*args):
        assert rel.state()["hold_events"][-1]["action"] == "pause"
        rel.paths()[0].write_text("new incident")
        original(*args)
    monkeypatch.setattr(rel, "unlink_pinned_hold", raced_unlink)
    assert rel.main(["1.2.3", "--pause"]) == 0
    assert rel.paths()[0].read_text() == "new incident"


def test_receipt_write_failure_retains_hold(pending, monkeypatch):
    rel, _ = pending
    before = rel.paths()[1].read_bytes()
    def fail(*a):
        raise OSError("disk full")
    monkeypatch.setattr(rel.os, "replace", fail)
    assert rel.main(["1.2.3", "--pause"]) == 1
    assert rel.snapshot_hold_path().exists() and rel.paths()[1].read_bytes() == before


@pytest.mark.parametrize("kind", ["wrong_version", "symlink", "missing", "token", "root_symlink"])
def test_hold_ownership_must_be_proven(pending, kind, monkeypatch):
    rel, _ = pending
    own = rel.snapshot_hold_path()
    if kind == "wrong_version":
        own.write_text("2.0.0")
    elif kind == "symlink":
        target = own.parent / "foreign"
        target.write_text("1.2.3")
        own.unlink()
        own.symlink_to(target)
    elif kind == "missing":
        own.unlink()
    elif kind == "token":
        rel.remember(freeze_token="../../incident")
    else:
        root = own.parent
        moved = root.with_name("actual-holds")
        root.rename(moved)
        root.symlink_to(moved)
    monkeypatch.setattr(rel.hzlib, "gh", lambda *a: pytest.fail("ownership rejection before network"))
    assert rel.main(["1.2.3", "--pause"]) == 1


@pytest.mark.parametrize("kind", ["github_error", "pypi_error", "pypi_exists"])
def test_network_failure_or_published_pypi_retains_hold(pending, kind, monkeypatch):
    rel, _ = pending
    if kind == "github_error":
        monkeypatch.setattr(rel.hzlib, "gh", lambda *a: (1, ""))
    elif kind == "pypi_error":
        def fail(*a, **kw):
            raise urllib.error.URLError("offline")
        monkeypatch.setattr(rel.urllib.request, "urlopen", fail)
    else:
        class Published:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
        monkeypatch.setattr(rel.urllib.request, "urlopen", lambda *a, **kw: Published())
    assert rel.main(["1.2.3", "--pause"]) == 1
    assert rel.snapshot_hold_path().exists()


def test_ambiguous_draft_conversion_keeps_hold_and_resumes_original_readiness(pending, monkeypatch):
    rel, reply = pending
    github = rel.hzlib.gh
    def ambiguous(*args):
        result = github(*args)
        return (1, "lost reply") if args[:2] == ("pr", "ready") else result
    monkeypatch.setattr(rel.hzlib, "gh", ambiguous)
    assert rel.main(["1.2.3", "--pause"]) == 1
    assert rel.snapshot_hold_path().exists()
    assert rel.state()["hold_events"][-1]["action"] == "pause-request"
    assert reply["data"]["repository"]["pullRequests"]["nodes"][0]["isDraft"]
    monkeypatch.setattr(rel.hzlib, "gh", github)
    rel.resume_paused_hold()
    assert not reply["data"]["repository"]["pullRequests"]["nodes"][0]["isDraft"]
    assert rel.snapshot_hold_path().exists()


def test_originally_draft_pr_is_not_made_ready_by_resume(pending):
    rel, reply = pending
    pr = reply["data"]["repository"]["pullRequests"]["nodes"][0]
    pr["isDraft"] = True
    assert rel.main(["1.2.3", "--pause"]) == 0
    rel.resume_paused_hold()
    assert pr["isDraft"] and rel.snapshot_hold_path().exists()
    assert rel.state()["hold_events"][-1]["action"] == "resume"


def test_changed_pr_head_on_resume_stops_with_hold_reacquired(pending):
    rel, reply = pending
    assert rel.main(["1.2.3", "--pause"]) == 0
    reply["data"]["repository"]["pullRequests"]["nodes"][0]["headRefOid"] = "b" * 40
    with pytest.raises(rel.Stop, match="identity changed"):
        rel.resume_paused_hold()
    assert rel.snapshot_hold_path().exists()
    assert rel.state()["hold_events"][-1]["action"] == "pause"


def test_failed_readiness_keeps_hold_and_retry_reconciles(pending, monkeypatch):
    rel, reply = pending
    assert rel.main(["1.2.3", "--pause"]) == 0
    github = rel.hzlib.gh
    def ambiguous(*args):
        result = github(*args)
        return (1, "lost reply") if args[:2] == ("pr", "ready") else result
    monkeypatch.setattr(rel.hzlib, "gh", ambiguous)
    with pytest.raises(rel.Stop, match="readiness"):
        rel.resume_paused_hold()
    assert rel.snapshot_hold_path().exists()
    monkeypatch.setattr(rel.hzlib, "gh", github)
    rel.resume_paused_hold()
    assert rel.state()["hold_events"][-1]["action"] == "resume"


@pytest.mark.parametrize("replacement", ["directory", "file"])
def test_observed_owned_hold_replacement_is_never_removed(pending, monkeypatch, replacement):
    rel, _ = pending
    original = rel.unlink_pinned_hold
    own = rel.snapshot_hold_path()
    def replace_then_unlink(*args):
        if replacement == "directory":
            own.parent.rename(own.parent.with_name("original-holds"))
            own.parent.mkdir()
        else:
            own.rename(own.with_name("original-hold"))
        own.write_text("independent replacement")
        original(*args)
    monkeypatch.setattr(rel, "unlink_pinned_hold", replace_then_unlink)
    assert rel.main(["1.2.3", "--pause"]) == 1
    assert own.read_text() == "independent replacement"


def test_directory_replacement_at_unlink_cannot_redirect_deletion(pending, monkeypatch):
    rel, _ = pending
    own = rel.snapshot_hold_path()
    unlink = rel.os.unlink
    def race(path, *, dir_fd=None):
        if dir_fd is not None:
            own.parent.rename(own.parent.with_name("pinned-original-holds"))
            own.parent.mkdir()
            own.write_text("independent replacement")
        return unlink(path, dir_fd=dir_fd)
    monkeypatch.setattr(rel.os, "unlink", race)
    assert rel.main(["1.2.3", "--pause"]) == 0
    assert own.read_text() == "independent replacement"
    assert not (own.parent.with_name("pinned-original-holds") / own.name).exists()


def test_unlink_failure_retains_draft_pr_and_owned_hold_for_retry(pending, monkeypatch):
    rel, reply = pending
    unlink = rel.os.unlink
    def fail(path, *, dir_fd=None):
        if dir_fd is not None:
            raise OSError("denied")
        return unlink(path)
    monkeypatch.setattr(rel.os, "unlink", fail)
    assert rel.main(["1.2.3", "--pause"]) == 1
    assert rel.snapshot_hold_path().exists()
    assert reply["data"]["repository"]["pullRequests"]["nodes"][0]["isDraft"]
    monkeypatch.setattr(rel.os, "unlink", unlink)
    assert rel.main(["1.2.3", "--pause"]) == 0


def test_wrong_release_request_cannot_restore_paused_pr_readiness(pending, monkeypatch):
    rel, reply = pending
    assert rel.main(["1.2.3", "--pause"]) == 0
    monkeypatch.setattr(rel.hzlib, "gh", lambda *a: pytest.fail("wrong version before network/readiness"))
    assert rel.main(["1.2.4", "--snapshot-acceptance"]) == 1
    assert not rel.snapshot_hold_path().exists()
    assert reply["data"]["repository"]["pullRequests"]["nodes"][0]["isDraft"]


def test_resume_directory_sync_failure_never_restores_readiness_and_retry_resyncs(pending, monkeypatch):
    rel, reply = pending
    assert rel.main(["1.2.3", "--pause"]) == 0
    fsync = rel.os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(rel.os.fstat(fd).st_mode):
            raise OSError("directory fsync failed")
        fsync(fd)
    monkeypatch.setattr(rel.os, "fsync", fail_directory)
    for _ in range(2):  # retry with a visible hold must repeat the directory durability barrier
        with pytest.raises(rel.Stop, match="durably acquire"):
            rel.resume_paused_hold()
        assert rel.snapshot_hold_path().exists()
        assert reply["data"]["repository"]["pullRequests"]["nodes"][0]["isDraft"]
        assert rel.state()["hold_events"][-1]["action"] == "pause"
    monkeypatch.setattr(rel.os, "fsync", fsync)
    rel.resume_paused_hold()
    assert not reply["data"]["repository"]["pullRequests"]["nodes"][0]["isDraft"]


def test_acquisition_syncs_hold_directory_and_parent_before_return(pending, monkeypatch):
    rel, _ = pending
    synced = []
    fsync = rel.os.fsync
    def observe(fd):
        metadata = rel.os.fstat(fd)
        if stat.S_ISDIR(metadata.st_mode):
            synced.append((metadata.st_dev, metadata.st_ino))
        fsync(fd)
    monkeypatch.setattr(rel.os, "fsync", observe)
    rel.acquire_snapshot_freeze("1.2.3")
    for path in (rel.hzlib.release_holds(), rel.hzlib.release_holds().parent):
        metadata = path.stat()
        assert (metadata.st_dev, metadata.st_ino) in synced


def test_prepare_only_reacquires_exact_paused_hold_before_remote_and_performs_no_release_step(pending, monkeypatch):
    rel, reply = pending
    plugin_key = "plugin-heinzel-resource-0.1.4"
    dispatch_timestamp = "2026-11-11T06:34:57Z"
    rel.remember(**{plugin_key: dispatch_timestamp})
    assert rel.main(['1.2.3', '--pause']) == 0
    assert rel.state()[plugin_key] == dispatch_timestamp
    github = rel.hzlib.gh
    def observed(*args):
        assert rel.owned_hold_exists('1.2.3')
        return github(*args)
    monkeypatch.setattr(rel.hzlib, 'gh', observed)
    monkeypatch.setattr(rel, 'release_core', lambda *args: pytest.fail('prepare must not run release steps'))
    monkeypatch.setattr(rel, 'next_version', lambda *args: pytest.fail('prepare must not select a new version'))
    incident = rel.paths()[0]
    incident.write_text('incident')
    token = rel.state()['freeze_token']
    assert rel.main(['1.2.3', '--prepare-only']) == 0
    assert rel.owned_hold_exists('1.2.3') and rel.state()['freeze_token'] == token
    assert not reply['data']['repository']['pullRequests']['nodes'][0]['isDraft']
    audit = rel.state()['hold_events'][-1]
    assert audit['mode'] == 'prepare-only' and audit['pr'] == 477 and audit['head'] == 'a' * 40
    assert incident.read_text() == 'incident'
    assert rel.state()[plugin_key] == dispatch_timestamp
    events = rel.state()['hold_events']
    assert rel.main(['1.2.3', '--prepare-only']) == 0
    assert rel.state()['hold_events'] == events


@pytest.mark.parametrize('change', ['head', 'api', 'finished', 'version', 'downstream'])
def test_prepare_only_rejects_changed_or_unknown_release_identity(pending, monkeypatch, change):
    rel, reply = pending
    assert rel.main(['1.2.3', '--pause']) == 0
    if change == 'head':
        reply['data']['repository']['pullRequests']['nodes'][0]['headRefOid'] = 'b' * 40
    elif change == 'api':
        monkeypatch.setattr(rel.hzlib, 'gh', lambda *args: (1, ''))
    elif change == 'finished':
        rel.remember(finished=True)
    elif change == 'version':
        rel.remember(version='1.2.4')
    else:
        rel.remember(pypi='started')
    assert rel.main(['1.2.3', '--prepare-only']) == 1
    assert not any(event.get('mode') == 'prepare-only' for event in rel.state()['hold_events'])
    if change in {'head', 'api'}:
        assert rel.owned_hold_exists('1.2.3')


def test_prepare_only_does_not_make_an_originally_draft_release_ready(pending):
    rel, reply = pending
    pr = reply['data']['repository']['pullRequests']['nodes'][0]
    pr['isDraft'] = True
    assert rel.main(['1.2.3', '--pause']) == 0
    assert rel.main(['1.2.3', '--prepare-only']) == 1
    assert pr['isDraft']


@pytest.mark.parametrize('options', [['patch'], ['1.2.3', '--pause'], ['1.2.3', '--snapshot-acceptance'],
                                    ['1.2.3', '--intro', 'intro'], ['--plugin', 'clock']])
def test_prepare_only_rejects_combined_release_actions(tool, options):
    rel = tool('hz-release')
    with pytest.raises(SystemExit):
        rel.main([*options, '--prepare-only'])


def test_before_release_branch_pause_preserves_other_holds_and_resumes(pending, monkeypatch):
    rel, reply = pending
    repo = reply["data"]["repository"]
    repo["pullRequests"]["nodes"] = []
    repo["branch"] = None
    own = rel.snapshot_hold_path()
    incident = rel.paths()[0]
    incident.touch()
    github = rel.hzlib.gh

    def only_reads(*args):
        assert args[:2] == ("api", "graphql")
        return github(*args)

    monkeypatch.setattr(rel.hzlib, "gh", only_reads)
    assert rel.main(["1.2.3", "--pause"]) == 0
    assert rel.main(["1.2.3", "--pause"]) == 0
    assert not own.exists() and incident.exists()
    assert rel.main(["1.2.3", "--prepare-only"]) == 1
    assert not own.exists()
    with pytest.raises(rel.Stop):
        rel.pretag_identity("1.2.3")
    rel.resume_paused_hold()
    assert own.read_text() == "1.2.3" and incident.exists()
    assert rel.state()["hold_events"][-1]["action"] == "resume"


@pytest.mark.parametrize("branch", ["existing", "unknown", "appears"])
def test_before_release_pr_pause_keeps_hold_if_branch_is_not_proven_absent(pending, branch, monkeypatch):
    rel, reply = pending
    repo = reply["data"]["repository"]
    repo["pullRequests"]["nodes"] = []
    if branch != "unknown":
        repo["branch"] = {"name": "release/v1.2.3"} if branch == "existing" else None
    if branch == "appears":
        changed = copy.deepcopy(reply)
        changed["data"]["repository"]["branch"] = {"name": "release/v1.2.3"}
        replies = iter([reply, changed])
        monkeypatch.setattr(rel.hzlib, "gh", lambda *args: (0, json.dumps(next(replies))))
    assert rel.main(["1.2.3", "--pause"]) == 1
    assert rel.snapshot_hold_path().exists()


def test_before_release_pr_resume_refuses_new_pr_after_reacquiring_hold(pending):
    rel, reply = pending
    repo = reply["data"]["repository"]
    pr = repo["pullRequests"]["nodes"].pop()
    repo["branch"] = None
    own = rel.snapshot_hold_path()
    assert rel.main(["1.2.3", "--pause"]) == 0
    repo["pullRequests"]["nodes"].append(pr)
    with pytest.raises(rel.Stop, match="identity changed"):
        rel.resume_paused_hold()
    assert own.read_text() == "1.2.3"
    assert rel.state()["hold_events"][-1]["action"] == "pause"
    assert pr["isDraft"] is False
