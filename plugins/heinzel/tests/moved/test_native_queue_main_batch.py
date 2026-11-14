"""Batch admissions preserve exact final pending/failure evidence and never multiply greens."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from steuermaschine_heinzel.native.queue_lib import hzlib
from steuermaschine_heinzel.native.queue_lib import main_batch

BASE, FIRST, FINAL = "a" * 40, "b" * 40, "c" * 40


@pytest.fixture(autouse=True)
def isolated_batch_queue(tmp_path, monkeypatch):
    """Each case owns its queue even when the test runner inherits an explicit path."""
    state = tmp_path / "queue.json"
    monkeypatch.setenv("HZ_QUEUE_STATE", str(state))
    assert state.parent == tmp_path and not state.exists()


@pytest.mark.parametrize("markers,expected", [
    ([f"Main batch full suite ({BASE})"], True),
    ([f"Main batch focused scope ({BASE})"], True),
    ([f"Main batch focused scope ({FIRST})"], False),
    ([f"Main batch full suite ({BASE})", f"Main batch focused scope ({BASE})"], False),
])
def test_vendored_reader_binds_one_integration_marker_to_the_aggregate_base(monkeypatch, markers, expected):
    proof = {"run": 99, "base": BASE, "prs": [[12, FIRST], [13, FINAL]]}
    monkeypatch.setattr(main_batch, "_identity_for", lambda sha, get: proof)
    def get(endpoint):
        assert endpoint == "actions/runs/99/attempts/1/jobs?per_page=100"
        return {"total_count": len(markers), "jobs": [
            {"name": name, "status": "completed", "conclusion": "success"} for name in markers
        ]}
    assert main_batch.proof_for(FIRST, get) == (proof if expected else None)


def row(run_id, sha):
    return {"databaseId": run_id, "headSha": sha, "status": "completed", "conclusion": "success",
            "displayTitle": main_batch.PREFIX + sha, "attempt": 1, "createdAt": "2026-11-03T10:00:00Z"}


@pytest.mark.parametrize(("status", "conclusion"), [("in_progress", None), ("completed", "failure"), ("completed", "success")])
def test_reader_follows_exact_final_and_deduplicates(monkeypatch, status, conclusion):
    proof = {"run": 99, "base": BASE, "prs": [[12, FIRST], [13, FINAL]]}
    final = {"id": 99, "run_attempt": 1, "event": "push", "head_branch": "main", "head_sha": FINAL,
             "path": ".github/workflows/ci.yml", "repository": {"full_name": hzlib.CORE}, "status": status,
             "conclusion": conclusion, "created_at": "2026-11-03T10:01:00Z", "display_title": main_batch.PREFIX + FINAL}
    jobs = {"total_count": 1, "jobs": [{"name": main_batch.DELEGATED + "99", "status": "completed", "conclusion": "success"}]}
    monkeypatch.setattr(main_batch, "proof_for", lambda sha, get: proof)
    monkeypatch.setattr(hzlib, "ghj", lambda *args, **kwargs: (jobs if "/98/attempts/" in args[1] else {"total_count": 1, "jobs": [{"name": "CI OK", "status": "completed", "conclusion": "success"}]}) if "/jobs?" in args[1] else final)
    resolved = hzlib.resolve_main_runs([row(98, FIRST), {**row(99, FINAL), "status": status, "conclusion": conclusion}])
    assert len(resolved) == 1
    assert (resolved[0]["databaseId"], resolved[0]["status"], resolved[0]["conclusion"]) == (99, status, conclusion)


def test_unproven_admission_does_not_count_as_green(monkeypatch):
    monkeypatch.setattr(hzlib, "ghj", lambda *args, **kwargs: {"total_count": 0, "jobs": []})
    result = hzlib.resolve_main_runs([row(98, FIRST)])[0]
    assert result["status"] == "waiting" and result["conclusion"] is None and result["batch_unknown"]


def test_legacy_full_run_needs_no_extra_api_reads(monkeypatch):
    monkeypatch.setattr(hzlib, "ghj", lambda *args, **kwargs: pytest.fail("legacy API read"))
    original = {**row(1, FIRST), "displayTitle": "An earlier CI run"}
    assert hzlib.resolve_main_runs([original]) == [original]


def test_publisher_writes_final_proof_before_intermediates(tool, monkeypatch, home):
    queue = tool("hz-queue")
    writes = []
    final_run = {"id": 99, "run_attempt": 1, "event": "push", "head_branch": "main", "head_sha": FINAL,
                 "path": ".github/workflows/ci.yml", "repository": {"full_name": hzlib.CORE}, "status": "queued",
                 "display_title": main_batch.PREFIX + FINAL}

    def read(*args, **kwargs):
        if args == ("api", "user"):
            return {"id": main_batch.PUBLISHER_ID}
        if args[0] == "pr":
            return {"mergeCommit": {"oid": FIRST if args[2] == "12" else FINAL}}
        if "/compare/" in args[1]:
            return {"status": "ahead", "total_commits": 2, "commits": [
                {"sha": FIRST, "parents": [{"sha": BASE}]}, {"sha": FINAL, "parents": [{"sha": FIRST}]}]}
        if "/pulls/" in args[1]:
            return {"merged": True, "merge_commit_sha": FIRST if args[1].endswith("/12") else FINAL,
                    "base": {"ref": "main", "repo": {"full_name": hzlib.CORE}}}
        return {"workflow_runs": [final_run]}

    monkeypatch.setattr(queue.hzlib, "ghj", read)
    monkeypatch.setattr(queue.hzlib, "gh", lambda *args: (writes.append(args) or (0, "")))
    assert queue.publish_batch(BASE, ["12", "13"])
    assert [args[3] for args in writes] == [f"repos/{hzlib.CORE}/statuses/{FINAL}", f"repos/{hzlib.CORE}/statuses/{FIRST}"]
    assert queue.load()["batch_proof"]["final_run"] == 99


def test_wrong_publisher_never_writes_proof(tool, monkeypatch):
    queue = tool("hz-queue")
    monkeypatch.setattr(queue.hzlib, "ghj", lambda *args, **kwargs: {"id": 7})
    monkeypatch.setattr(queue.hzlib, "gh", lambda *args: pytest.fail("unauthorized proof write"))
    assert not queue.publish_batch(BASE, ["12", "13"])


def test_rerun_failure_is_not_hidden_by_first_attempt_delegation(monkeypatch):
    paths = []
    def read(*args, **kwargs):
        paths.append(args[1])
        return {"total_count": 1, "jobs": [{"name": "CI OK", "conclusion": "failure"}]}
    monkeypatch.setattr(hzlib, "ghj", read)
    monkeypatch.setattr(main_batch, "proof_for", lambda *args: pytest.fail("rerun delegated"))
    failed = {**row(98, FIRST), "attempt": 2, "conclusion": "failure"}
    assert hzlib.resolve_main_runs([failed]) == [failed]
    assert paths == []


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "startup_failure"])
def test_failed_source_is_never_replaced_by_a_green_final(monkeypatch, conclusion):
    failed = {**row(98, FIRST), "conclusion": conclusion}
    monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: pytest.fail("failed source looked up delegation"))
    assert hzlib.resolve_main_runs([failed]) == [failed]


@pytest.mark.parametrize(("key", "value"), [("displayTitle", None), ("displayTitle", ""), ("displayTitle", 7), ("attempt", None), ("attempt", True), ("attempt", 0)])
def test_missing_requested_metadata_is_unknown(monkeypatch, key, value):
    original = {**row(98, FIRST), key: value}
    if value is None:
        original.pop(key)
    monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: pytest.fail("metadata must fail before API reads"))
    result = hzlib.resolve_main_runs([original])[0]
    assert result["batch_unknown"] and result["conclusion"] is None


@pytest.mark.parametrize("payload", [
    {"total_count": True, "jobs": [{"name": "CI OK", "status": "completed", "conclusion": "success"}]},
    {"total_count": 1, "jobs": {}},
    {"total_count": 1, "jobs": [None]},
    {"total_count": 1, "jobs": [{"name": "CI OK", "conclusion": "success"}]},
    {"total_count": 1, "jobs": [{"name": "CI OK", "status": "in_progress", "conclusion": "success"}]},
    {"total_count": 1, "jobs": [{"name": 7, "status": "completed", "conclusion": "success"}]},
])
def test_malformed_admission_jobs_are_unknown(monkeypatch, payload):
    monkeypatch.setattr(hzlib, "ghj", lambda *a, **k: payload)
    assert hzlib.resolve_main_runs([row(98, FIRST)])[0]["batch_unknown"]


def test_unknown_protocol_withholds_old_full_suite_streak(tool, monkeypatch):
    flakes = tool("hz-flakes")
    unknown = {**row(98, FIRST), "batch_unknown": True, "status": "waiting", "conclusion": None}
    old = [{**row(n, FINAL), "displayTitle": "Legacy full CI", "event": "push", "headBranch": "main"} for n in (3, 2, 1)]
    jobs = [{"name": "Test (macos-latest)", "conclusion": "success"}]
    data = flakes.group([unknown, *old], jobs_of=lambda repo, rid: jobs, attempt_of=lambda *a: {})
    assert data["streak"] == 0 and data["unknown"] == 1
    assert "qualification withheld" in flakes.group_report(data) and "bar met" not in flakes.group_report(data)
    assert any("qualification withheld" in line for line in flakes.group_compact(data))
    assert "qualification withheld" in flakes.report([unknown, *old], {}, 3)
    assert "bar met" not in flakes.report([unknown, *old], {}, 3)


def test_intent_publisher_uses_pending_with_bound_public_compare_url(tool, monkeypatch):
    queue = tool("hz-queue")
    writes = []
    monkeypatch.setattr(queue.hzlib, "ghj", lambda *args, **kwargs: {"id": main_batch.PUBLISHER_ID} if args == ("api", "user") else {"mergeCommit": {"oid": FIRST}})
    monkeypatch.setattr(queue.hzlib, "gh", lambda *args: writes.append(args) or (0, ""))
    assert queue.publish_intent(BASE, {"pr": "12"}) == FIRST
    assert len(writes) == 1
    assert "state=pending" in writes[0] and f"target_url={main_batch.intent_url(BASE, FIRST)}" in writes[0]


def test_closure_does_not_overwrite_an_existing_final_proof(tool, monkeypatch):
    queue = tool("hz-queue")
    writes = []
    def read(*args, **kwargs):
        if args == ("api", "user"):
            return {"id": main_batch.PUBLISHER_ID}
        commit = FIRST if FIRST in args[1] else FINAL
        return [{"context": main_batch.CONTEXT, "state": "pending" if commit == FIRST else "success",
                 "creator": {"id": main_batch.PUBLISHER_ID}, "target_url": main_batch.intent_url(BASE, commit)}]
    monkeypatch.setattr(queue.hzlib, "ghj", read)
    monkeypatch.setattr(queue.hzlib, "gh", lambda *args: writes.append(args) or (0, ""))
    queue.close_intents(BASE, [FIRST, FINAL])
    assert len(writes) == 1 and FIRST in writes[0][3]
    assert "state=success" in writes[0]
    assert f"target_url={main_batch.intent_url(BASE, FIRST)}" in writes[0]


def test_orphan_intent_does_not_block_a_verified_full_ci_result(monkeypatch):
    calls = []
    def read(*args, **kwargs):
        calls.append(args[1])
        return {"total_count": 1, "jobs": [{"name": "CI OK", "status": "completed", "conclusion": "success"}]}
    monkeypatch.setattr(hzlib, "ghj", read)
    monkeypatch.setattr(main_batch, "proof_for", lambda *args: pytest.fail("full CI depended on assembly intent"))
    complete = row(98, FIRST)
    assert hzlib.resolve_main_runs([complete]) == [complete]
    assert all("/jobs?" in endpoint for endpoint in calls)


def test_full_ci_ignores_its_skipped_delegation_branch(monkeypatch):
    jobs = [
        {"name": main_batch.DELEGATED + "${{ needs.admission.outputs.run }}", "status": "completed", "conclusion": "skipped"},
        {"name": "CI OK", "status": "completed", "conclusion": "success"},
    ]
    monkeypatch.setattr(hzlib, "ghj", lambda *args, **kwargs: {"total_count": len(jobs), "jobs": jobs})
    monkeypatch.setattr(main_batch, "proof_for", lambda *args: pytest.fail("a skipped branch is not a delegation"))
    complete = row(98, FIRST)
    assert hzlib.resolve_main_runs([complete]) == [complete]


@pytest.mark.parametrize("ci_result", [None, "skipped", "failure"])
def test_skipped_delegation_still_requires_successful_full_ci(monkeypatch, ci_result):
    jobs = [{"name": main_batch.DELEGATED + "unused", "status": "completed", "conclusion": "skipped"}]
    if ci_result is not None:
        jobs.append({"name": "CI OK", "status": "completed", "conclusion": ci_result})
    monkeypatch.setattr(hzlib, "ghj", lambda *args, **kwargs: {"total_count": len(jobs), "jobs": jobs})
    monkeypatch.setattr(main_batch, "proof_for", lambda *args: None)
    assert hzlib.resolve_main_runs([row(98, FIRST)])[0]["batch_unknown"]


@pytest.mark.parametrize("results", [("skipped", "skipped"), ("skipped", "success")])
def test_multiple_delegation_branches_remain_ambiguous(monkeypatch, results):
    jobs = [
        {"name": main_batch.DELEGATED + str(number), "status": "completed", "conclusion": result}
        for number, result in enumerate(results)
    ]
    jobs.append({"name": "CI OK", "status": "completed", "conclusion": "success"})
    monkeypatch.setattr(hzlib, "ghj", lambda *args, **kwargs: {"total_count": len(jobs), "jobs": jobs})
    monkeypatch.setattr(main_batch, "proof_for", lambda *args: None)
    assert hzlib.resolve_main_runs([row(98, FIRST)])[0]["batch_unknown"]


def test_event_reporting_and_batch_unknown_withholding_compose(tool, monkeypatch):
    import json
    flakes = tool("hz-flakes")
    def legacy(run_id, event, conclusion="success"):
        return {**row(run_id, FINAL), "displayTitle": "Legacy full CI", "event": event, "headBranch": "main",
                "conclusion": conclusion, "createdAt": f"2026-11-03T{run_id:02}:00:00Z"}
    listed = [legacy(4, "push"), legacy(3, "schedule", "failure"), legacy(2, "workflow_dispatch"), legacy(1, "push")]
    calls = []
    monkeypatch.setattr(flakes, "gh", lambda *args: calls.append(args) or json.dumps(listed))
    runs = flakes.main_runs(hzlib.CORE, 10)
    assert [run["databaseId"] for run in runs] == [4, 3, 2, 1]
    assert "--event" not in calls[0]
    green = [{"name": "Test (macos-latest)", "conclusion": "success"}]
    failed = [{"name": "Test (ubuntu-24.04)", "conclusion": "failure"}]
    data = flakes.group(runs, jobs_of=lambda repo, rid: failed if rid == 3 else green, attempt_of=lambda *a: {})
    assert data["streak"] == 1
    unknown = {**legacy(5, "push"), "displayTitle": None}
    listed.insert(0, unknown)
    unresolved = flakes.main_runs(hzlib.CORE, 10)
    assert unresolved[0]["batch_unknown"]
    data = flakes.group(unresolved, jobs_of=lambda repo, rid: failed if rid == 3 else green, attempt_of=lambda *a: {})
    assert data["streak"] == 0 and "qualification withheld" in flakes.group_report(data)


def publisher_fixture(queue, monkeypatch, *, altered=None, write_codes=()):
    """Actual public identities and contiguous squash ancestry for publisher regressions."""
    final = {"id": 99, "run_attempt": 1, "event": "push", "head_branch": "main", "head_sha": FINAL,
             "path": ".github/workflows/ci.yml", "repository": {"full_name": hzlib.CORE}, "status": "queued",
             "display_title": main_batch.PREFIX + FINAL}
    responses = {
        "user": {"id": main_batch.PUBLISHER_ID},
        f"repos/{hzlib.CORE}/compare/{BASE}...{FINAL}": {"status": "ahead", "total_commits": 2, "commits": [
            {"sha": FIRST, "parents": [{"sha": BASE}]}, {"sha": FINAL, "parents": [{"sha": FIRST}]}]},
    }
    for number, sha in ((12, FIRST), (13, FINAL)):
        responses[f"repos/{hzlib.CORE}/pulls/{number}"] = {"merged": True, "merge_commit_sha": sha,
            "base": {"ref": "main", "repo": {"full_name": hzlib.CORE}}}
    if altered is not None:
        altered(responses)
    def read(*args, **kw):
        if args[0] == "pr":
            return {"mergeCommit": {"oid": FIRST if args[2] == "12" else FINAL}}
        return responses.get(args[1], {"workflow_runs": [final]})
    writes = []
    codes = iter(write_codes)
    monkeypatch.setattr(queue.hzlib, "ghj", read)
    monkeypatch.setattr(queue.hzlib, "gh", lambda *args: writes.append(args) or (next(codes, 0), ""))
    return writes


@pytest.mark.parametrize("mutation", [
    lambda data: data.update(user=[]),
    lambda data: data.update(user={"id": float(main_batch.PUBLISHER_ID)}),
    lambda data: data.update({f"repos/{hzlib.CORE}/compare/{BASE}...{FINAL}": []}),
    lambda data: data[f"repos/{hzlib.CORE}/compare/{BASE}...{FINAL}"].update(total_commits=3),
    lambda data: data[f"repos/{hzlib.CORE}/compare/{BASE}...{FINAL}"]["commits"][1].update(parents=[{"sha": BASE}]),
    lambda data: data[f"repos/{hzlib.CORE}/pulls/12"].update(merged=False),
    lambda data: data[f"repos/{hzlib.CORE}/pulls/13"].update(merge_commit_sha=FIRST),
])
def test_publisher_refuses_unknown_or_noncontiguous_actual_history(tool, monkeypatch, home, mutation):
    queue = tool("hz-queue")
    writes = publisher_fixture(queue, monkeypatch, altered=mutation)
    assert queue.publish_batch(BASE, ["12", "13"]) is False
    assert not writes and not queue.load()["batch_proof"]


@pytest.mark.parametrize("write_codes", [(0, 0), (0, 1), (1,)])
def test_proof_publication_never_replaces_progress_or_incident_history(tool, monkeypatch, home, write_codes):
    queue = tool("hz-queue")
    pinned = {"databaseId": 7, "headSha": "d" * 40, "status": "completed", "conclusion": "failure"}
    old = {"prs": ["1"], "shas": ["d" * 8]}
    history = [{"pr": "12", "main": FIRST[:8], "anchor": {"completed": [pinned], "pending": []}}]
    queue.update(batch=old, red_batch=old, main_failure=pinned, progress_history=history)
    writes = publisher_fixture(queue, monkeypatch, write_codes=write_codes)
    succeeded = queue.publish_batch(BASE, ["12", "13"])
    assert succeeded is (write_codes == (0, 0))
    state = queue.load()
    assert state["batch"] == state["red_batch"] == old
    assert state["main_failure"] == pinned and state["progress_history"] == history
    assert bool(state["batch_proof"]) is succeeded
    assert all("state=success" in args for args in writes)  # identity only, never a full CI/check result


@pytest.mark.parametrize("conclusion", [None, "failure", "success"])
def test_release_reader_uses_exact_final_full_run(tool, monkeypatch, conclusion):
    rel = tool("hz-release")
    proof = {"run": 99, "base": BASE, "prs": [[12, FIRST], [13, FINAL]]}
    final = {"id": 99, "run_attempt": 1, "event": "push", "head_branch": "main", "head_sha": FINAL,
             "path": ".github/workflows/ci.yml", "repository": {"full_name": hzlib.CORE},
             "status": "in_progress" if conclusion is None else "completed", "conclusion": conclusion,
             "created_at": "2026-11-03T10:01:00Z", "display_title": main_batch.PREFIX + FINAL}
    def read(*args, **kw):
        if "/98/attempts/" in args[1]:
            return {"total_count": 1, "jobs": [{"name": main_batch.DELEGATED + "99", "status": "completed", "conclusion": "success"}]}
        return final
    monkeypatch.setattr(main_batch, "proof_for", lambda *a: proof)
    monkeypatch.setattr(hzlib, "ghj", read)
    monkeypatch.setattr(hzlib, "gh", lambda *a: (0, FINAL))
    monkeypatch.setattr(hzlib, "only_never_started_macos_jobs_failed", lambda *a: False)
    monkeypatch.setattr(rel, "runs", lambda *a, **kw: [row(98, FIRST)])
    if conclusion == "failure":
        with pytest.raises(rel.Stop, match="main tip.*red"):
            rel.main_tip_green()
    else:
        assert rel.main_tip_green() is (conclusion == "success")


@pytest.mark.parametrize("members", ["12", None, [True, "13"], ["0", "13"], ["12", "12"]])
def test_publisher_rejects_malformed_members_before_status_writes(tool, monkeypatch, home, members):
    queue = tool("hz-queue")
    writes = publisher_fixture(queue, monkeypatch)
    assert not queue.publish_batch(BASE, members)
    assert not writes


def test_shared_validator_updates_invalidate_the_running_queue(tool, monkeypatch, tmp_path):
    import os
    queue = tool("hz-queue")
    for name in ("hz-queue", "hzlib.py", "hz-merge-train", "hz-merge-gate", "main_batch.py", "known_main_infra.py"):
        path = tmp_path / name
        path.touch()
        os.utime(path, (100, 100))
    monkeypatch.setattr(queue, "__file__", str(tmp_path / "hz-queue"))
    assert queue.tools_changed_at() == 100
    os.utime(tmp_path / "main_batch.py", (200, 200))
    assert queue.tools_changed_at() == 200
    os.utime(tmp_path / "known_main_infra.py", (300, 300))
    assert queue.tools_changed_at() == 300
