"""--replace-acceptance-run over a same-run rerun: archive attempt N's exact rejection, credit no later attempt."""

import pytest

from test_native_release_acceptance_identity import IDENTITY, SNAP, bound
from steuermaschine_heinzel import state as statedir

TITLE = "Acceptance 0.9.16 release-ours"


def run_json(attempt, status="completed", conclusion="failure", **kw):
    return {
        "id": 7,
        "path": ".github/workflows/acceptance.yml",
        "head_sha": "d" * 40,
        "display_title": TITLE,
        "run_attempt": attempt,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://example.invalid/runs/7/attempts/{attempt}",
        "created_at": "2026-01-02T00:00:00Z",
    } | kw


JOBS = [
    {"id": 1, "name": "Acceptance (ubuntu-24.04)", "status": "completed", "conclusion": "success",
     "html_url": "https://example.invalid/job/1", "run_attempt": 1},
    {"id": 2, "name": "Acceptance (macos-latest)", "status": "completed", "conclusion": "failure",
     "html_url": "https://example.invalid/job/2", "run_attempt": 1},
]


@pytest.fixture
def rel(tool, home, monkeypatch):
    module = tool("hz-release")
    monkeypatch.setattr(module, "RECEIPT_OVERRIDE", None)
    target = module.paths()[1]
    assert target == statedir.root() / "release.json"
    bound(module)
    module.remember(acceptance_run_id=7, snapshot=SNAP)
    return module


def fake(monkeypatch, rel, latest, original=None, jobs=JOBS, calls=None):
    core = rel.CORE

    def ghj(*args, **kw):
        path = args[1] if args[:1] == ("api",) else None
        if calls is not None:
            calls.append(args)
        if path == f"repos/{core}/actions/runs/7":
            return latest
        if path == f"repos/{core}/actions/runs/7/attempts/1":
            return original
        if path == f"repos/{core}/actions/runs/7/attempts/1/jobs?per_page=100":
            return {"jobs": jobs}
        raise AssertionError(f"unexpected gh call {args}")

    monkeypatch.setattr(rel.hzlib, "ghj", ghj)
    monkeypatch.setattr(rel.hzlib, "gh", lambda *a: (0, "e" * 40))
    monkeypatch.setattr(rel.release_snapshot, "workflow_supports_snapshot", lambda commit: True)
    monkeypatch.setattr(rel, "verdict", lambda run: pytest.fail("rerun replacement must not read the latest attempt"))
    dispatched = []
    monkeypatch.setattr(rel, "step", lambda *a: dispatched.append(a))
    monkeypatch.setattr(rel, "dispatch", lambda *a, **kw: pytest.fail("no dispatch in tests"))
    return dispatched


def test_rerun_rejection_is_archived_and_later_attempt_not_credited(rel, monkeypatch):
    dispatched = fake(monkeypatch, rel, run_json(2, conclusion="success"), run_json(1))
    monkeypatch.setattr(rel, "ours", lambda *a: {"databaseId": 99})  # the fresh dispatch, never run 7
    assert rel.acceptance_step("0.9.16", 0, harness_ref="main", replace_run=7, replace_reason="runner image outage") == 99
    data = rel.state()
    archive = data["acceptance_attempts"][0]
    assert archive["acceptance_run_id"] == 7 and archive["acceptance_run_attempt"] == 1
    assert archive["acceptance_identity"] == IDENTITY
    rerun = archive["rerun_rejection"]
    assert rerun["run_attempt"] == 1 and rerun["conclusion"] == "failure"
    assert rerun["html_url"].endswith("/attempts/1")
    assert [job["conclusion"] for job in rerun["jobs"]] == ["success", "failure"]
    assert rerun["jobs"][1]["html_url"] == "https://example.invalid/job/2"
    assert rerun["latest_attempt_seen"] == 2 and rerun["later_attempts_credited"] is False
    assert rerun["reason"] == "runner image outage"
    assert archive["verdict"][0] == "attempt 1: failure"
    # Fresh dispatch path: binding cleared, new harness on a different main commit, same snapshot.
    assert data["acceptance_run_id"] is None and data["acceptance_marker"] is None
    assert data["acceptance_run_attempt"] is None and data["acceptance_requested"] is False
    assert data["acceptance_identity"]["commit"] == "e" * 40
    assert data["acceptance_identity"]["snapshot"] == SNAP
    assert data["finished"] is False and data["acceptance_run"] is None
    assert dispatched and dispatched[0][0] == "acceptance"


def stop_without_mutation(rel, match, **kw):
    before = rel.state()
    with pytest.raises(rel.Stop, match=match):
        rel.acceptance_step("0.9.16", 0, **({"harness_ref": "main", "replace_run": 7} | kw))
    assert rel.state() == before


def test_non_replace_invocation_still_stops(rel, monkeypatch):
    fake(monkeypatch, rel, run_json(2, conclusion="success"), run_json(1))
    with pytest.raises(rel.Stop, match="attempt changed"):
        rel.ours(rel.CORE, "acceptance.yml", "acceptance")
    before = rel.state()
    monkeypatch.setattr(rel, "step", lambda label, done, act, timeout: done())
    with pytest.raises(rel.Stop, match="attempt changed"):
        rel.acceptance_step("0.9.16", 0)
    assert rel.state() == before


def test_lower_attempt_still_stops(rel, monkeypatch):
    rel.remember(acceptance_run_attempt=2)
    fake(monkeypatch, rel, run_json(1), run_json(1))
    stop_without_mutation(rel, "attempt changed", replace_reason="x")


@pytest.mark.parametrize("field", [{"head_sha": "f" * 40}, {"display_title": "Acceptance 0.9.16 release-other"},
                                   {"path": ".github/workflows/other.yml"}, {"id": 8}])
def test_latest_identity_mismatch_still_stops(rel, monkeypatch, field):
    fake(monkeypatch, rel, run_json(2, **field), run_json(1))
    stop_without_mutation(rel, "identity changed", replace_reason="x")


@pytest.mark.parametrize("field", [{"head_sha": "f" * 40}, {"display_title": "Acceptance 0.9.16 release-other"},
                                   {"run_attempt": 2}, {"id": 8}])
def test_original_attempt_mismatch_still_stops(rel, monkeypatch, field):
    fake(monkeypatch, rel, run_json(2), run_json(1, **field))
    stop_without_mutation(rel, "exact bound original attempt", replace_reason="x")


def test_succeeded_original_attempt_still_stops(rel, monkeypatch):
    fake(monkeypatch, rel, run_json(2), run_json(1, conclusion="success"))
    stop_without_mutation(rel, "completed non-success", replace_reason="x")


@pytest.mark.parametrize("status,conclusion", [("in_progress", None), ("queued", None), ("completed", None)])
def test_running_original_attempt_still_stops(rel, monkeypatch, status, conclusion):
    fake(monkeypatch, rel, run_json(2), run_json(1, status=status, conclusion=conclusion))
    stop_without_mutation(rel, "completed non-success", replace_reason="x")


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_missing_reason_stops(rel, monkeypatch, reason):
    fake(monkeypatch, rel, run_json(2), run_json(1))
    stop_without_mutation(rel, "--replace-reason", replace_reason=reason)


@pytest.mark.parametrize("jobs", [[], None, [JOBS[0] | {"run_attempt": 2}]])
def test_unreadable_original_jobs_stop(rel, monkeypatch, jobs):
    fake(monkeypatch, rel, run_json(2), run_json(1), jobs=jobs)
    stop_without_mutation(rel, "original attempt's jobs", replace_reason="x")


def test_replacement_still_requires_different_main_commit(rel, monkeypatch):
    fake(monkeypatch, rel, run_json(2), run_json(1))
    monkeypatch.setattr(rel.hzlib, "gh", lambda *a: (0, "d" * 40))
    stop_without_mutation(rel, "different corrected harness commit", replace_reason="x")


def test_replacement_still_requires_protected_main(rel, monkeypatch):
    fake(monkeypatch, rel, run_json(2), run_json(1))
    stop_without_mutation(rel, "protected main", harness_ref="feature", replace_reason="x")


def test_other_run_id_is_not_treated_as_rerun(rel, monkeypatch):
    calls = []
    fake(monkeypatch, rel, run_json(2), run_json(1), calls=calls)
    before = rel.state()
    with pytest.raises(rel.Stop, match="attempt changed"):
        rel.acceptance_step("0.9.16", 0, harness_ref="main", replace_run=9, replace_reason="x")
    assert rel.state() == before
    assert not any("attempts" in str(call) for call in calls)


def test_resume_after_archive_does_not_rearchive(rel, monkeypatch):
    fake(monkeypatch, rel, run_json(2, conclusion="success"), run_json(1))
    monkeypatch.setattr(rel, "ours", lambda *a: {"databaseId": 99})
    rel.acceptance_step("0.9.16", 0, harness_ref="main", replace_run=7, replace_reason="outage")
    archived = rel.state()["acceptance_attempts"]
    rel.acceptance_step("0.9.16", 0, harness_ref="main", replace_run=7, replace_reason="outage")
    assert rel.state()["acceptance_attempts"] == archived


def test_replace_reason_requires_replace_flag(rel, monkeypatch):
    with pytest.raises(SystemExit):
        rel.main(["0.9.16", "--replace-reason", "outage"])


def test_running_attempt_with_early_conclusion_still_stops(rel, monkeypatch):
    fake(monkeypatch, rel, run_json(2), run_json(1, status="in_progress", conclusion="failure"))
    stop_without_mutation(rel, "completed non-success", replace_reason="x")


def test_unchanged_attempt_keeps_existing_replace_route(rel, monkeypatch):
    calls = []
    fake(monkeypatch, rel, run_json(1), run_json(1), calls=calls)
    seen = []
    monkeypatch.setattr(rel, "verdict", lambda run: seen.append(run) or (True, []))
    stop_without_mutation(rel, "accepted run cannot be replaced", replace_reason="x")
    assert seen == [7]
    assert not any("attempts" in str(call) for call in calls)
