"""Public release snapshots fail closed on changed bytes, source identity and dispatch attribution."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from steuermaschine_heinzel.native.release_lib import snapshot as snapshot
from steuermaschine_heinzel import state as statedir


def release(**assets):
    return {"draft": False, "prerelease": False, "assets": [{"name": name, "digest": f"sha256:{digest}"} for name, digest in assets.items()]}


def profile():
    wheel = "a" * 64
    data = {"format": 2, "core_version": "1.2.3", "artifacts": [{"name": "heinzel", "version": "1.2.3", "filename": "heinzel-1.2.3-py3-none-any.whl", "sha256": wheel, "url": f"https://github.com/{snapshot.hzlib.PLUGINS}/releases/download/app-v1.2.3/heinzel-1.2.3-py3-none-any.whl"}]}
    raw = json.dumps(data).encode()
    app = release(**{"standard.json": hashlib.sha256(raw).hexdigest(), "heinzel-1.2.3-py3-none-any.whl": wheel})
    return data, raw, app


def test_snapshot_binds_all_app_assets_to_the_core_release():
    _, raw, app = profile()
    assert snapshot.validate_profile(raw, "1.2.3", "a" * 64, app) == hashlib.sha256(raw).hexdigest()
    with pytest.raises(snapshot.InvalidSnapshot, match="different Core"):
        snapshot.validate_profile(raw, "1.2.3", "b" * 64, app)
    app["assets"][1]["digest"] = "sha256:" + "b" * 64
    with pytest.raises(snapshot.InvalidSnapshot, match="differs"):
        snapshot.validate_profile(raw, "1.2.3", "a" * 64, app)


@pytest.mark.parametrize("mutation", ["bytes", "unpublished", "duplicate", "missing_digest", "no_core", "foreign_url"])
def test_snapshot_rejects_replaced_or_ambiguous_release_identity(mutation):
    data, raw, app = profile()
    if mutation == "bytes":
        raw += b" "
    elif mutation == "unpublished":
        app["draft"] = True
    elif mutation == "duplicate":
        app["assets"].append(app["assets"][0])
    elif mutation == "missing_digest":
        app["assets"][0]["digest"] = None
    else:
        if mutation == "no_core":
            data["artifacts"][0]["name"] = "other"
        else:
            data["artifacts"][0]["url"] = "https://example.invalid/wheel"
        raw = json.dumps(data).encode()
        app["assets"][0]["digest"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    with pytest.raises(snapshot.InvalidSnapshot):
        snapshot.validate_profile(raw, "1.2.3", "a" * 64, app)


def test_capture_verifies_tag_commit_and_profile_bytes(monkeypatch):
    _, raw, app = profile()
    monkeypatch.setattr(snapshot.hzlib, "gh", lambda *a: (0, "c" * 40))
    core = release(**{"heinzel-1.2.3-py3-none-any.whl": "a" * 64}) | {"tag_name": "v1.2.3"}
    monkeypatch.setattr(snapshot.hzlib, "ghj", lambda *a, **kw: app if "/app-v" in a[1] else core)
    monkeypatch.setattr(snapshot, "fetch_profile", lambda url: raw)
    captured = snapshot.capture("1.2.3")
    assert captured == {"version": "1.2.3", "tag": "v1.2.3", "commit": "c" * 40,
                        "wheel_sha256": "a" * 64, "profile_sha256": hashlib.sha256(raw).hexdigest()}
    monkeypatch.setattr(snapshot.hzlib, "gh", lambda *a: (0, "moving-main"))
    with pytest.raises(snapshot.InvalidSnapshot, match="source commit"):
        snapshot.capture("1.2.3")


def held_core(**changes):
    core = release(**{"heinzel-1.2.3-py3-none-any.whl": "a" * 64, "artifact-manifest.json": "e" * 64})
    return core | {"tag_name": "v1.2.3", "prerelease": True} | changes


def test_capture_accepts_the_held_core_release_with_its_final_app(monkeypatch):
    _, raw, app = profile()
    monkeypatch.setattr(snapshot.hzlib, "gh", lambda *a: (0, "c" * 40))
    monkeypatch.setattr(snapshot.hzlib, "ghj", lambda *a, **kw: app if "/app-v" in a[1] else held_core())
    monkeypatch.setattr(snapshot, "fetch_profile", lambda url: raw)
    assert snapshot.capture("1.2.3")["wheel_sha256"] == "a" * 64


@pytest.mark.parametrize("case", ["draft", "draft_final", "foreign_tag", "no_tag", "no_manifest", "manifest_digest",
                                  "no_wheel", "wheel_digest", "flag_not_bool"])
def test_only_the_genuine_held_core_release_is_accepted(case):
    core = held_core()
    if case == "draft":
        core["draft"] = True
    elif case == "draft_final":
        core |= {"draft": True, "prerelease": False}
    elif case == "foreign_tag":
        core["tag_name"] = "edge-v1.2.3"
    elif case == "no_tag":
        del core["tag_name"]
    elif case == "no_manifest":
        core["assets"] = [a for a in core["assets"] if a["name"] != "artifact-manifest.json"]
    elif case == "manifest_digest":
        core["assets"][1]["digest"] = "md5:x"
    elif case == "no_wheel":
        core["assets"] = core["assets"][1:]
    elif case == "wheel_digest":
        core["assets"][0]["digest"] = None
    else:
        core["prerelease"] = "true"
    with pytest.raises(snapshot.InvalidSnapshot):
        snapshot.core_wheel_digest(core, "1.2.3")


def test_final_core_needs_no_manifest_and_every_other_release_stays_final_only():
    final = release(**{"heinzel-1.2.3-py3-none-any.whl": "a" * 64}) | {"tag_name": "v1.2.3"}
    assert snapshot.core_wheel_digest(final, "1.2.3") == "a" * 64
    assert snapshot.core_wheel_digest(held_core(), "1.2.3") == "a" * 64
    with pytest.raises(snapshot.InvalidSnapshot, match="published and final"):
        snapshot.asset_digest(held_core(), "heinzel-1.2.3-py3-none-any.whl")
    _, raw, app = profile()
    app["prerelease"] = True  # a held app is not the collection's latest: acceptance would fail its preflight
    with pytest.raises(snapshot.InvalidSnapshot, match="published and final"):
        snapshot.validate_profile(raw, "1.2.3", "a" * 64, app)


def test_a_held_core_whose_wheel_changed_stops_against_the_pinned_snapshot(tool, home, monkeypatch):
    rel = tool("hz-release")
    assert rel.paths()[1] == statedir.root() / "release.json"
    _, raw, app = profile()
    monkeypatch.setattr(snapshot.hzlib, "gh", lambda *a: (0, "c" * 40))
    core = held_core()
    monkeypatch.setattr(snapshot.hzlib, "ghj", lambda *a, **kw: app if "/app-v" in a[1] else core)
    monkeypatch.setattr(snapshot, "fetch_profile", lambda url: raw)
    monkeypatch.setattr(snapshot, "workflow_supports_snapshot", lambda commit: True)
    first = rel.pinned_snapshot("1.2.3")
    assert rel.state()["snapshot"] == first and first["wheel_sha256"] == "a" * 64
    core["assets"][0]["digest"] = "sha256:" + "f" * 64
    with pytest.raises(rel.Stop):
        rel.pinned_snapshot("1.2.3")
    assert rel.state()["snapshot"] == first


def test_acceptance_correlates_both_marker_and_source_commit(tool, home, monkeypatch):
    rel = tool("hz-release")
    rel.remember(acceptance="2026-01-01T00:00:00Z", acceptance_marker="release-ours", acceptance_commit="c" * 40)
    wrong = {"databaseId": 1, "createdAt": "2026-01-02T00:00:00Z", "headSha": "c" * 40, "displayTitle": "Acceptance 1.2.3 release-other"}
    ours = wrong | {"databaseId": 2, "displayTitle": "Acceptance 1.2.3 release-ours"}
    wrong_commit = ours | {"databaseId": 3, "headSha": "d" * 40}
    monkeypatch.setattr(rel, "runs", lambda *a: [wrong, ours, wrong_commit])
    with pytest.raises(rel.Stop, match="ref changed"):
        rel.ours(rel.CORE, "acceptance.yml", "acceptance")
    monkeypatch.setattr(rel, "runs", lambda *a: [wrong, ours])
    assert rel.ours(rel.CORE, "acceptance.yml", "acceptance")["databaseId"] == 2


def test_another_unfinished_release_cannot_replace_snapshot(tool, home, monkeypatch):
    rel = tool("hz-release")
    rel.remember(version="1.2.3", finished=False, snapshot={"commit": "c" * 40})
    monkeypatch.setattr(rel, "next_version", lambda request: ("1.2.4", "patch"))
    monkeypatch.setattr(rel, "has_tag", lambda repo, tag: False)  # no tag: a clear Not Found
    with pytest.raises(rel.Stop, match="unfinished"):
        rel.release_core("1.2.4", "", 1, True)
    assert rel.state()["snapshot"]["commit"] == "c" * 40


def test_changed_snapshot_on_resume_stops_before_unfreezing(tool, home, monkeypatch):
    rel = tool("hz-release")
    rel.remember(snapshot={"tag": "v1.2.3", "commit": "c" * 40})
    monkeypatch.setattr(snapshot, "capture", lambda v: {"tag": "v1.2.3", "commit": "d" * 40})
    monkeypatch.setattr(snapshot, "workflow_supports_snapshot", lambda tag: True)
    freeze = rel.paths()[0]
    freeze.touch()
    with pytest.raises(rel.Stop, match="changed"):
        rel.pinned_snapshot("1.2.3")
    assert freeze.exists()


def test_unsupported_tagged_workflow_keeps_freeze(tool, home, monkeypatch):
    rel = tool("hz-release")
    monkeypatch.setattr(snapshot, "capture", lambda v: {"tag": "v1.2.3", "commit": "c" * 40})
    monkeypatch.setattr(snapshot, "workflow_supports_snapshot", lambda tag: False)
    freeze = rel.paths()[0]
    freeze.touch()
    with pytest.raises(rel.Stop, match="cannot enforce"):
        rel.pinned_snapshot("1.2.3")
    assert freeze.exists()


def test_only_the_matching_started_run_releases_freeze(tool, home, monkeypatch):
    rel = tool("hz-release")
    rel.acquire_snapshot_freeze("1.2.3")
    freeze = rel.snapshot_hold_path()
    snap = {"tag": "v1.2.3", "commit": "c" * 40, "wheel_sha256": "a" * 64, "profile_sha256": "b" * 64}
    monkeypatch.setattr(rel.hzlib, "gh", lambda *a: (0, "c" * 40))
    monkeypatch.setattr(snapshot, "workflow_supports_snapshot", lambda commit: True)
    seen = {"poll": 0, "marker": None}
    def dispatch(repo, workflow, ref, key, **inputs):
        assert freeze.exists()
        assert ref == "v1.2.3"
        assert inputs["expected_wheel_sha256"] == "a" * 64
        assert inputs["expected_profile_sha256"] == "b" * 64
        seen["marker"] = inputs["release_attempt"]
        rel.remember(acceptance="2026-01-01T00:00:00Z")
    def runs(*a):
        if seen["marker"] is None:
            return []
        seen["poll"] += 1
        marker = "wrong" if seen["poll"] == 1 else seen["marker"]
        return [{"attempt": 1, "databaseId": 42, "createdAt": "2026-01-02T00:00:00Z", "headSha": "c" * 40,
                 "displayTitle": f"Acceptance 1.2.3 {marker}", "status": "completed" if seen["poll"] >= 3 else "queued"}]
    sleeps = []
    def sleep(seconds):
        sleeps.append(freeze.exists())
    monkeypatch.setattr(rel, "dispatch", dispatch)
    monkeypatch.setattr(rel, "runs", runs)
    def run_by_id(*a, **kw):
        row = runs()[0]
        return {"id": row["databaseId"], "path": ".github/workflows/acceptance.yml",
                "head_sha": row["headSha"], "display_title": row["displayTitle"],
                "run_attempt": 1, "created_at": row["createdAt"], "status": row["status"], "conclusion": row.get("conclusion")}
    monkeypatch.setattr(rel.hzlib, "ghj", run_by_id)
    monkeypatch.setattr(rel.hzlib, "sleep", sleep)
    assert rel.acceptance_step("1.2.3", 10, snap) == 42
    assert sleeps == [True, False]
    assert not freeze.exists()


def test_a_requested_dispatch_is_never_blindly_repeated_on_resume(tool, home, monkeypatch):
    rel = tool("hz-release")
    rel.remember(acceptance_identity={"version": "1.2.3", "snapshot": {"commit": "c" * 40}, "commit": "c" * 40}, acceptance_requested=True, acceptance_marker="release-ours", acceptance_commit="c" * 40)
    monkeypatch.setattr(rel, "runs", lambda *a: [])
    monkeypatch.setattr(rel, "dispatch", lambda *a, **kw: pytest.fail("must wait for the persisted marker"))
    clock = iter([0, 1])
    monkeypatch.setattr(rel.time, "time", lambda: next(clock))
    with pytest.raises(rel.Stop, match="timed out"):
        rel.acceptance_step("1.2.3", 0, {"commit": "c" * 40})


def test_snapshot_release_uses_a_separate_name_from_manual_holds(tool, home):
    rel = tool("hz-release")
    rel.paths()[0].write_text("manual hold")
    rel.acquire_snapshot_freeze("1.2.3")
    assert rel.snapshot_hold_path() != rel.paths()[0]
    rel.release_snapshot_freeze()
    assert rel.paths()[0].read_text() == "manual hold"
    assert rel.hzlib.merge_frozen()


def test_an_incident_hold_created_during_release_is_never_unlinked(tool, home):
    rel = tool("hz-release")
    rel.acquire_snapshot_freeze("1.2.3")
    own = rel.snapshot_hold_path()
    # A separate writer creates a new incident sentinel immediately before this release drops its own hold.
    rel.paths()[0].write_text("incident hold")
    rel.release_snapshot_freeze()
    assert not own.exists()
    assert rel.paths()[0].read_text() == "incident hold"
    assert rel.hzlib.merge_frozen()


def test_a_second_core_release_process_cannot_enter_the_pipeline(tool, home):
    import fcntl

    rel = tool("hz-release")
    lock_path = rel.paths()[1].with_suffix(".lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert rel.main(["1.2.3", "--snapshot-acceptance"]) == 1
    assert not rel.paths()[0].exists()


@pytest.mark.parametrize("arguments", [["1.2.3"], ["--plugin", "clock"]])
def test_core_and_plugin_commands_share_one_receipt_lock(tool, home, monkeypatch, arguments):
    import fcntl

    rel = tool("hz-release")
    monkeypatch.setattr(rel, "release_core", lambda *a: pytest.fail("must not enter a competing core release"))
    monkeypatch.setattr(rel, "release_plugin", lambda *a: pytest.fail("must not enter a competing plugin release"))
    with rel.paths()[1].with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert rel.main(arguments) == 1
    assert not rel.paths()[1].exists()


@pytest.mark.parametrize("corrupt", ['{"version":', '[]', 'null'])
def test_corrupt_receipt_cannot_start_a_plugin_or_reset_release_state(tool, home, monkeypatch, corrupt):
    rel = tool("hz-release")
    rel.paths()[1].write_text(corrupt)
    monkeypatch.setattr(rel, "release_plugin", lambda *a: pytest.fail("corruption must stop before release actions"))
    assert rel.main(["--plugin", "clock"]) == 1
    assert rel.paths()[1].read_text() == corrupt


def test_atomic_receipt_replacement_never_exposes_a_partial_document(tool, home, monkeypatch):
    rel = tool("hz-release")
    before = {"version": "1.2.3", "snapshot": {"commit": "c" * 40}}
    rel.write_state(before)
    actual_replace = rel.os.replace
    observations = []
    def replace(source, destination):
        observations.append(rel.state())
        assert json.loads(Path(source).read_text()) == before | {"acceptance_run_id": 42}
        actual_replace(source, destination)
        observations.append(rel.state())
    monkeypatch.setattr(rel.os, "replace", replace)
    rel.remember(acceptance_run_id=42)
    assert observations == [before, before | {"acceptance_run_id": 42}]
    assert not list(rel.paths()[1].parent.glob(".hz-release-*"))


def test_failed_receipt_replace_preserves_previous_snapshot(tool, home, monkeypatch):
    rel = tool("hz-release")
    rel.remember(version="1.2.3", snapshot={"commit": "c" * 40})
    before = rel.paths()[1].read_bytes()
    def fail_replace(*args):
        raise OSError("storage failure")
    monkeypatch.setattr(rel.os, "replace", fail_replace)
    with pytest.raises(rel.Stop, match="atomically"):
        rel.remember(snapshot={"commit": "d" * 40})
    assert rel.paths()[1].read_bytes() == before
    assert not list(rel.paths()[1].parent.glob(".hz-release-*"))


def test_missing_receipt_with_an_active_hold_fails_closed(tool, home):
    rel = tool("hz-release")
    root = rel.hzlib.release_holds()
    root.mkdir()
    (root / ("a" * 32)).write_text("1.2.3")
    with pytest.raises(rel.Stop, match="receipt is missing"):
        rel.state()


def test_started_acceptance_is_resumed_by_id_after_newer_runs_push_it_out_of_listing(tool, home, monkeypatch):
    rel = tool("hz-release")
    rel.remember(acceptance="2026-01-01T00:00:00Z", acceptance_marker="release-ours", acceptance_commit="c" * 40)
    row = {"databaseId": 42, "createdAt": "2026-01-02T00:00:00Z", "headSha": "c" * 40,
           "displayTitle": "Acceptance 1.2.3 release-ours", "status": "queued"}
    monkeypatch.setattr(rel, "runs", lambda *a: [row])
    assert rel.ours(rel.CORE, "acceptance.yml", "acceptance")["databaseId"] == 42
    assert rel.state()["acceptance_run_id"] == 42
    monkeypatch.setattr(rel, "runs", lambda *a: pytest.fail("persisted runs must not depend on newest-ten listings"))
    monkeypatch.setattr(rel.hzlib, "ghj", lambda *a, **kw: {"id": 42, "path": ".github/workflows/acceptance.yml",
        "head_sha": "c" * 40, "display_title": "Acceptance 1.2.3 release-ours",
        "created_at": "2026-01-02T00:00:00Z", "status": "completed", "conclusion": "success"})
    assert rel.ours(rel.CORE, "acceptance.yml", "acceptance")["status"] == "completed"


def test_workflow_capability_requires_the_entire_reviewed_workflow(monkeypatch):
    import base64

    reviewed = (Path(__file__).parent / "fixtures/acceptance-snapshot.yml").read_bytes()
    def provide(source):
        monkeypatch.setattr(snapshot.hzlib, "gh", lambda *a: (0, base64.b64encode(source).decode()))
    provide(reviewed)
    assert snapshot.workflow_supports_snapshot("c" * 40)
    disabled = reviewed.replace(b'--accept-wheel-sha256 "$EXPECTED_WHEEL_SHA256"', b'--accept-wheel-sha256 "" # --accept-wheel-sha256 "$EXPECTED_WHEEL_SHA256"')
    provide(disabled)
    assert not snapshot.workflow_supports_snapshot("c" * 40)
    provide(b"# " + b"\n# ".join(reviewed.splitlines()))
    assert not snapshot.workflow_supports_snapshot("c" * 40)


def test_scoped_hold_names_are_honored_by_queue_and_do_not_delete_other_holds(tool, home, monkeypatch):
    from steuermaschine_heinzel.native.queue_lib import hzlib

    queue = tool("hz-queue")
    root = hzlib.release_holds()
    root.mkdir()
    first, second = root / ("a" * 32), root / ("b" * 32)
    first.write_text("release one")
    second.write_text("release two")
    monkeypatch.setattr(queue, "main_state", lambda: "green")
    ready = [{"repo": hzlib.CORE}]
    assert queue.gate(ready, 1000, {"last_batch": 0}) == "merge freeze is on"
    first.unlink()
    assert queue.gate(ready, 1000, {"last_batch": 0}) == "merge freeze is on"
    second.unlink()
    assert queue.gate(ready, 1000, {"last_batch": 0}) == ""


@pytest.mark.parametrize("case", ["missing_os", "duplicate_os", "failed_workflow", "failed_job", "candidate_label"])
def test_a_partial_or_failed_workflow_cannot_be_accepted_from_green_log_lines(tool, home, monkeypatch, case):
    rel = tool("hz-release")
    ubuntu = 'Acceptance (ubuntu-24.04)\tstep\t2026Z {"verdict": "ACCEPTED"}\n'
    mac = 'Acceptance (macos-latest)\tstep\t2026Z {"verdict": "ACCEPTED_EXCEPT_UNPROVABLE"}\n'
    log = ubuntu + mac
    info = {"status": "completed", "conclusion": "success", "jobs": [
        {"name": "Acceptance (ubuntu-24.04)", "conclusion": "success"},
        {"name": "Acceptance (macos-latest)", "conclusion": "success"}]}
    if case == "missing_os":
        log = ubuntu
    elif case == "duplicate_os":
        log += ubuntu
    elif case == "failed_workflow":
        info["conclusion"] = "failure"
    elif case == "failed_job":
        info["jobs"][1]["conclusion"] = "cancelled"
    else:
        log = log.replace('"ACCEPTED"', '"CANDIDATE_ACCEPTED"')
    monkeypatch.setattr(rel.hzlib, "gh", lambda *a: (0, log))
    monkeypatch.setattr(rel.hzlib, "ghj", lambda *a, **kw: info)
    monkeypatch.setattr(rel.hzlib, "failing_tests", lambda *a: [])
    assert rel.verdict(42)[0] is False


def test_incident_freeze_created_immediately_before_own_unlink_is_preserved(tool, home, monkeypatch):
    rel = tool("hz-release")
    rel.acquire_snapshot_freeze("1.2.3")
    owned = rel.snapshot_hold_path()
    original = Path.unlink
    def incident_then_unlink(path, *args, **kwargs):
        if path == owned:
            rel.paths()[0].write_text("incident created at the unlink boundary")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", incident_then_unlink)
    rel.release_snapshot_freeze()
    assert rel.paths()[0].read_text() == "incident created at the unlink boundary"
    assert rel.hzlib.merge_frozen()


def test_merge_freezes_fail_closed_on_an_unreadable_hold_directory(tool, home, monkeypatch):
    from steuermaschine_heinzel.native.queue_lib import hzlib

    root = hzlib.release_holds()
    root.mkdir()
    original = Path.lstat
    def unreadable(path):
        if path == root:
            raise PermissionError("unreadable hold state")
        return original(path)
    monkeypatch.setattr(Path, "lstat", unreadable)
    assert hzlib.merge_frozen()


def test_snapshot_capability_is_checked_at_captured_commit_not_mutable_tag(tool, home, monkeypatch):
    rel = tool("hz-release")
    captured = {"version": "1.2.3", "tag": "v1.2.3", "commit": "c" * 40,
                "wheel_sha256": "a" * 64, "profile_sha256": "b" * 64}
    monkeypatch.setattr(snapshot, "capture", lambda version: captured)
    checked = []
    monkeypatch.setattr(snapshot, "workflow_supports_snapshot", lambda ref: checked.append(ref) or True)
    assert rel.pinned_snapshot("1.2.3") == captured
    assert checked == [captured["commit"]]


def test_workflow_fingerprint_download_is_bound_to_the_captured_commit(monkeypatch):
    import base64

    source = (Path(__file__).parent / "fixtures/acceptance-snapshot.yml").read_bytes()
    calls = []
    def fetch(*args):
        calls.append(args)
        return 0, base64.b64encode(source).decode()
    monkeypatch.setattr(snapshot.hzlib, "gh", fetch)
    assert snapshot.workflow_supports_snapshot("c" * 40)
    assert calls[0][1].endswith("?ref=" + "c" * 40)
    assert not snapshot.workflow_supports_snapshot("v1.2.3")
    assert len(calls) == 1


def test_superseded_direct_uv_workflow_is_not_the_reviewed_current_contract(monkeypatch):
    import base64

    current = (Path(__file__).parent / "fixtures/acceptance-snapshot.yml").read_bytes()
    assert current.count(b"uses: ./.github/actions/setup-uv") == 1
    previous = current.replace(
        b"uses: ./.github/actions/setup-uv",
        b"uses: astral-sh/setup-uv@07a047e608db84b3d8c06cd9a7c7dcc9c355c18e # v10.3.2",
    )
    monkeypatch.setattr(snapshot.hzlib, "gh", lambda *args: (0, base64.b64encode(previous).decode()))
    assert not snapshot.workflow_supports_snapshot("c" * 40)
