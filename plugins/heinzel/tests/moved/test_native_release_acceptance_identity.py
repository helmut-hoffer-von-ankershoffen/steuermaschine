import json
import pytest

SNAP = {
    "version": "0.9.16",
    "tag": "v0.9.16",
    "commit": "c" * 40,
    "wheel_sha256": "a" * 64,
    "profile_sha256": "b" * 64,
}
IDENTITY = {
    "version": "0.9.16",
    "app_version": "0.9.16",
    "ref": "main",
    "commit": "d" * 40,
    "snapshot": SNAP,
}


def bound(rel):
    rel.remember(
        acceptance_identity=IDENTITY,
        snapshot=SNAP,
        acceptance="2026-01-01T00:00:00Z",
        acceptance_requested=True,
        acceptance_run_attempt=1,
        acceptance_marker="release-ours",
        acceptance_commit="d" * 40,
    )


def row(run=7, **kw):
    return (
        dict(
            databaseId=run,
            attempt=1,
            createdAt="2026-01-02T00:00:00Z",
            headSha="d" * 40,
            displayTitle="Acceptance 0.9.16 release-ours",
            status="completed",
            conclusion="success",
        )
        | kw
    )


def test_newer_unrelated_success_cannot_certify(tool, home, monkeypatch):
    rel = tool("hz-release")
    bound(rel)
    monkeypatch.setattr(
        rel,
        "runs",
        lambda *a: [row(8, displayTitle="Acceptance 0.9.17 unrelated"), row()],
    )
    assert rel.ours(rel.CORE, "acceptance.yml", "acceptance")["databaseId"] == 7


def test_legacy_timestamp_attempt_is_not_adopted(tool, home, monkeypatch):
    rel = tool("hz-release")
    rel.remember(acceptance="2026-01-01T00:00:00Z")
    with pytest.raises(rel.Stop, match="legacy"):
        rel.acceptance_step("0.9.16", 0)


def test_duplicate_marker_fails_closed(tool, home, monkeypatch):
    rel = tool("hz-release")
    bound(rel)
    monkeypatch.setattr(rel, "runs", lambda *a: [row(), row(8)])
    with pytest.raises(rel.Stop, match="ambiguous"):
        rel.ours(rel.CORE, "acceptance.yml", "acceptance")


def test_restart_uses_persisted_id_outside_window(tool, home, monkeypatch):
    rel = tool("hz-release")
    bound(rel)
    rel.remember(acceptance_run_id=7)
    monkeypatch.setattr(rel, "runs", lambda *a: pytest.fail("no moving window"))
    monkeypatch.setattr(
        rel.hzlib,
        "ghj",
        lambda *a, **kw: {
            "id": 7,
            "path": ".github/workflows/acceptance.yml",
            "head_sha": "d" * 40,
            "display_title": row()["displayTitle"],
            "created_at": row()["createdAt"],
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 1,
        },
    )
    assert rel.ours(rel.CORE, "acceptance.yml", "acceptance")["databaseId"] == 7


def test_ambiguous_dispatch_is_not_repeated(tool, home, monkeypatch):
    rel = tool("hz-release")
    bound(rel)
    monkeypatch.setattr(rel, "runs", lambda *a: [])
    monkeypatch.setattr(rel, "dispatch", lambda *a, **kw: pytest.fail("no redispatch"))
    clock = iter([0, 1])
    monkeypatch.setattr(rel.time, "time", lambda: next(clock))
    with pytest.raises(rel.Stop, match="timed out"):
        rel.acceptance_step("0.9.16", 0)


@pytest.mark.parametrize(
    "field,value",
    [
        ("expect_version", "0.9.17"),
        ("expect_app_version", "0.9.17"),
        ("core_wheel_sha256", "f" * 64),
        ("profile_sha256", "f" * 64),
        ("sha", "e" * 40),
        ("run_id", "8"),
        ("event", "push"),
        ("run_attempt", "2"),
        (None, None),
    ],
)
def test_receipts_require_versions_hashes_commit_and_run(
    tool, home, monkeypatch, field, value
):
    rel = tool("hz-release")
    rel.remember(acceptance_run_attempt=1)

    def download(*args):
        root = rel.Path(args[-1])
        for os_name in ["Linux", "macOS"]:
            p = root / f"acceptance-result-{os_name}" / "result.json"
            p.parent.mkdir()
            ci = {
                "repository": rel.CORE,
                "sha": "d" * 40,
                "run_id": "7",
                "run_attempt": "1",
                "runner_os": os_name,
                "event": "workflow_dispatch",
            }
            published = {
                "expect_version": "0.9.16",
                "expect_app_version": "0.9.16",
                "core_wheel_sha256": "a" * 64,
                "profile_sha256": "b" * 64,
            }
            if field is not None:
                (ci if field in ci else published)[field] = value
            p.write_text(
                json.dumps(
                    {
                        "verdict": "ACCEPTED",
                        "public_path_proof": True,
                        "ci": ci,
                        "published": published,
                    }
                )
            )
        return 0, ""

    monkeypatch.setattr(rel.hzlib, "gh", download)
    assert rel.acceptance_receipts(7, IDENTITY) is (field is None)


def test_corrected_harness_retains_original_artifacts_and_rejection(
    tool, home, monkeypatch
):
    rel = tool("hz-release")
    bound(rel)
    rel.remember(finished=True, acceptance_run=7)
    monkeypatch.setattr(rel, "ours", lambda *a: row())
    monkeypatch.setattr(rel, "verdict", lambda run: (False, ["original rejection"]))
    monkeypatch.setattr(rel.hzlib, "gh", lambda *a: (0, "e" * 40))
    monkeypatch.setattr(
        rel.release_snapshot, "workflow_supports_snapshot", lambda commit: True
    )
    monkeypatch.setattr(rel, "step", lambda *a: None)
    assert rel.acceptance_step("0.9.16", 0, harness_ref="main", replace_run=7) == 7
    data = rel.state()
    assert data["finished"] is False
    assert data["acceptance_run"] is None
    assert data["acceptance_attempts"][0]["acceptance_run"] == 7
    assert data["acceptance_identity"]["snapshot"] == SNAP
    assert data["acceptance_identity"]["commit"] == "e" * 40
    assert data["acceptance_attempts"][0]["acceptance_identity"] == IDENTITY
    assert data["acceptance_attempts"][0]["verdict"] == ["original rejection"]


def test_dispatch_uses_supported_ref_with_resolved_sha_and_both_hashes(
    tool, home, monkeypatch
):
    rel = tool("hz-release")
    monkeypatch.setattr(rel.release_snapshot, "capture", lambda version: SNAP)
    monkeypatch.setattr(
        rel.release_snapshot, "workflow_supports_snapshot", lambda commit: True
    )
    monkeypatch.setattr(rel.hzlib, "gh", lambda *a: (0, "d" * 40))
    monkeypatch.setattr(
        rel, "ours", lambda *a: row() if rel.state().get("sent") else None
    )

    def dispatch(repo, workflow, ref, key, **inputs):
        assert ref == "main"
        assert rel.state()["acceptance_identity"]["commit"] == "d" * 40
        assert inputs["expected_version"] == inputs["expected_app_version"] == "0.9.16"
        assert inputs["expected_wheel_sha256"] == "a" * 64
        assert inputs["expected_profile_sha256"] == "b" * 64
        assert inputs["release_attempt"] == rel.state()["acceptance_marker"]
        rel.remember(sent=True)

    monkeypatch.setattr(rel, "dispatch", dispatch)
    assert rel.acceptance_step("0.9.16", 1) == 7


def test_same_run_rerun_cannot_replace_original_rejection(tool, home, monkeypatch):
    rel = tool("hz-release")
    bound(rel)
    rel.remember(acceptance_run_id=7)
    monkeypatch.setattr(
        rel.hzlib,
        "ghj",
        lambda *a, **kw: {
            "id": 7,
            "path": ".github/workflows/acceptance.yml",
            "head_sha": "d" * 40,
            "display_title": row()["displayTitle"],
            "run_attempt": 2,
        },
    )
    with pytest.raises(rel.Stop, match="attempt changed"):
        rel.ours(rel.CORE, "acceptance.yml", "acceptance")


def test_discovery_cannot_adopt_already_rerun_success(tool, home, monkeypatch):
    rel = tool("hz-release")
    bound(rel)
    monkeypatch.setattr(rel, "runs", lambda *a: [row(attempt=2)])
    with pytest.raises(rel.Stop, match="original run attempt"):
        rel.ours(rel.CORE, "acceptance.yml", "acceptance")


def test_corrected_ref_must_be_protected_main(tool, home):
    rel = tool("hz-release")
    with pytest.raises(rel.Stop, match="protected main"):
        rel.acceptance_identity("0.9.16", SNAP, "unreviewed-branch")


def test_default_mode_remains_default_on_resume(tool, home, monkeypatch):
    rel = tool("hz-release")
    rel.remember(version="0.9.16", snapshot=SNAP, acceptance_snapshot_mode=False)
    monkeypatch.setattr(rel, "next_version", lambda request: ("0.9.16", "patch"))
    monkeypatch.setattr(rel, "has_tag", lambda repo, tag: False)  # no tag: a clear Not Found
    monkeypatch.setattr(rel, "step", lambda *a: None)
    monkeypatch.setattr(rel, "compose_beside_pypi", lambda *a: None)
    monkeypatch.setattr(rel, "pypi_step", lambda *a: None)
    monkeypatch.setattr(rel, "compose_step", lambda *a: None)
    monkeypatch.setattr(rel, "acceptance_step", lambda *a: 7)
    monkeypatch.setattr(rel, "verdict", lambda run: (False, ["rejected"]))
    rel.paths()[0].touch()
    assert rel.release_core("0.9.16", "", 0) == 1
    assert not rel.paths()[0].exists()


def test_interrupted_replacement_blocks_next_release(tool, home, monkeypatch):
    rel = tool("hz-release")
    bound(rel)
    rel.remember(version="0.9.16", finished=True, acceptance_run=7, acceptance_run_id=7)
    monkeypatch.setattr(rel, "ours", lambda *a: row())
    monkeypatch.setattr(rel, "verdict", lambda run: (False, ["original rejection"]))
    monkeypatch.setattr(rel.hzlib, "gh", lambda *a: (0, "e" * 40))
    monkeypatch.setattr(
        rel.release_snapshot, "workflow_supports_snapshot", lambda commit: True
    )

    def interrupt(*a):
        raise rel.Stop("interrupted after durable replacement")

    monkeypatch.setattr(rel, "step", interrupt)
    with pytest.raises(rel.Stop, match="interrupted"):
        rel.acceptance_step("0.9.16", 0, harness_ref="main", replace_run=7)
    retained = rel.state()
    monkeypatch.setattr(rel, "next_version", lambda request: ("0.9.17", "patch"))
    with pytest.raises(rel.Stop, match="unfinished"):
        rel.release_core("0.9.17", "", 0)
    assert rel.state() == retained


def test_dispatch_ref_race_stops_instead_of_adopting_changed_main(
    tool, home, monkeypatch
):
    rel = tool("hz-release")
    bound(rel)
    monkeypatch.setattr(rel, "runs", lambda *args: [row(headSha="e" * 40)])
    with pytest.raises(rel.Stop, match="ref changed"):
        rel.acceptance_step("0.9.16", 0)
    assert rel.state()["acceptance_identity"]["commit"] == "d" * 40
    assert "acceptance_run_id" not in rel.state()


def test_default_resume_cannot_switch_hold_mode(tool, home, monkeypatch):
    rel = tool("hz-release")
    bound(rel)
    rel.remember(version="0.9.16", finished=False, acceptance_snapshot_mode=False)
    rel.paths()[0].write_text("original freeze")
    monkeypatch.setattr(rel, "next_version", lambda request: ("0.9.16", "patch"))
    with pytest.raises(rel.Stop, match="original snapshot hold mode"):
        rel.release_core("0.9.16", "", 0, True)
    assert rel.paths()[0].read_text() == "original freeze"
    assert not rel.hzlib.release_holds().exists()


def test_corrected_main_resume_keeps_release_snapshot(tool, home, monkeypatch):
    rel = tool("hz-release")
    bound(rel)
    rel.remember(
        acceptance_identity=IDENTITY | {"commit": "e" * 40},
        acceptance_commit="e" * 40,
        acceptance_snapshot_mode=True,
        acceptance_attempts=[
            {
                "acceptance_run_id": 6,
                "acceptance_identity": IDENTITY,
                "verdict": ["original rejection"],
            }
        ],
    )
    retained = rel.state()["acceptance_attempts"]
    monkeypatch.setattr(rel, "ours", lambda *args: row(headSha="e" * 40))
    monkeypatch.setattr(rel, "step", lambda *args: None)
    assert rel.acceptance_step("0.9.16", 0, SNAP, "main", 6) == 7
    assert rel.state()["snapshot"] == SNAP
    assert rel.state()["acceptance_identity"]["snapshot"] == SNAP
    assert rel.state()["acceptance_identity"]["commit"] == "e" * 40
    assert rel.state()["acceptance_attempts"] == retained
