"""`sm timing gate`, `sm timing hosted`, `sm timing stages` through the whole command tree, on fakes: text and json,
validated against the result models; refusals; no GitHub call where none is promised; nothing written."""

import json
from datetime import UTC, datetime

from steuermaschine_heinzel.native import tick
from tick_support import REPO, build, contract_gaps

from steuermaschine.github.testing import FakeGh
from steuermaschine.testing import invoke
from steuermaschine.testing.fakes import FakeClock

CREATED, STARTED, COMPLETED = "2026-11-10T03:00:00Z", "2026-11-10T04:00:07+01:00", "2026-11-10T03:01:10Z"
BEAD, TAG = "tracker-8um1", "v0.9.24"
MERGE_SHA, TAG_SHA = "a" * 40, "b" * 40
ONE_O_CLOCK = datetime(2026, 11, 10, 1, tzinfo=UTC).timestamp()


def test_gate_waits_in_text_and_json_without_any_command(tmp_path, monkeypatch):
    rows = [{"at": "1970-01-01T00:00:00+0000", "waited_s": 99, "ran_s": 99, "exit": 0},
            {"at": "1970-01-01T00:30:00+0000", "waited_s": 0, "ran_s": 7, "exit": 0},
            {"at": "1970-01-01T00:59:00+0000", "waited_s": 2, "ran_s": 17, "exit": 1}, ]
    (tmp_path / "gate.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\nnot json\n")
    rt = build(tmp_path, monkeypatch, clock=FakeClock(3601))
    done = invoke(rt, ["timing", "gate"])
    assert (done.exit_code, done.stdout) == (0, "gate 60 min: 2 commands; waited median 1s, p90 2s, total 2s; ran median 12s, p90 17s, total 24s; 1 with a non-zero exit\n")
    doc = json.loads(invoke(rt, ["--format", "json", "timing", "gate"]).stdout)
    stats = tick.GateWaits.model_validate(doc)
    assert (stats.runs, stats.wait_total_seconds, stats.ran_total_seconds, stats.ran_p90_seconds, stats.nonzero_exit) == (2, 2, 24, 17, 1)
    assert rt.providers.commands.calls == [] and rt.providers.store.writes == 0  # the local summary asks nobody
    (tmp_path / "other").mkdir()
    empty = invoke(build(tmp_path / "other", monkeypatch), ["timing", "gate"])
    assert empty.stdout == "gate 60 min: 0 commands; waited median -, p90 -, total 0s; ran median -, p90 -, total 0s; 0 with a non-zero exit\n"


def hosted_github(**job_changes):
    run = {"id": 41, "head_sha": "a" * 40, "run_attempt": 1, "path": ".github/workflows/focused-job.yml", "event": "workflow_dispatch",
           "status": "completed", "conclusion": "success", "created_at": CREATED}
    job = {"id": 701, "run_id": 41, "head_sha": "b" * 40, "name": "Focused Linux", "status": "completed", "conclusion": "success",
           "started_at": STARTED, "completed_at": COMPLETED, "runner_id": 20, **job_changes}
    gh = FakeGh()
    gh.on("actions/runs/41", run)
    gh.on("actions/runs/41/attempts/1/jobs", [{"total_count": 1, "jobs": [job]}])
    return gh


def test_hosted_timing_in_text_and_json_makes_two_reads_per_run(tmp_path, monkeypatch):
    gh = hosted_github()
    done = invoke(build(tmp_path, monkeypatch, gh), ["timing", "hosted", "41"])
    assert done.exit_code == 0 and done.stdout.splitlines() == [
        f"Run 41 attempt 1 {'a' * 40} '.github/workflows/focused-job.yml' (completed)",
        f"  Job 701 {'b' * 40} 'Focused Linux' (success, measured) | dispatch to start 7s | job wall 63s"]
    assert gh.lines() == [f"api repos/{REPO}/actions/runs/41", f"api --paginate --slurp repos/{REPO}/actions/runs/41/attempts/1/jobs?per_page=100"]
    assert not gh.writes()
    doc = json.loads(invoke(build(tmp_path, monkeypatch, hosted_github()), ["--format", "json", "timing", "hosted", "41"]).stdout)
    report = tick.HostedTiming.model_validate(doc)
    run, = report.runs
    job, = run.jobs
    assert (report.repository, run.id, run.attempt, run.head_sha, run.workflow, run.created_at) == (REPO, 41, 1, "a" * 40, ".github/workflows/focused-job.yml", CREATED)
    assert (job.id, job.timing, job.dispatch_to_start_seconds, job.job_wall_seconds, job.runner_id) == (701, "measured", 7, 63, 20)
    assert "lines" not in doc  # the text form is not part of the document


def test_hosted_timing_refuses_an_inventory_it_cannot_trust(tmp_path, monkeypatch):
    bad = invoke(build(tmp_path, monkeypatch, hosted_github(run_id=42)), ["timing", "hosted", "41"])
    assert bad.exit_code == 1 and bad.stdout == "" and "timing hosted: run 41: incomplete or mismatched attempt job inventory" in bad.stderr
    as_json = invoke(build(tmp_path, monkeypatch, FakeGh()), ["--format", "json", "timing", "hosted", "41"])
    error = json.loads(as_json.stdout)
    assert as_json.exit_code == 1 and error["code"] == 1 and error["error"] == "timing hosted: run 41: GitHub API request failed"


def stage_github(**pull):
    gh = FakeGh()
    gh.on(f"show {BEAD} --json", [{"id": BEAD, "created_at": "2026-11-10T00:00:00Z"}])
    gh.on("pulls/41", {"number": 41, "base": {"repo": {"full_name": REPO}}, "state": "closed", "merged": True, "created_at": "2026-11-10T00:10:00Z",
                       "merged_at": "2026-11-10T00:30:00Z", "merge_commit_sha": MERGE_SHA, **pull})
    gh.on(f"releases/tags/{TAG}", {"tag_name": TAG, "draft": False, "prerelease": False, "published_at": "2026-11-10T00:45:00Z"})
    gh.on(f"git/ref/tags/{TAG}", {"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": TAG_SHA}})
    gh.on(f"compare/{MERGE_SHA}...{TAG_SHA}", {"status": "ahead", "merge_base_commit": {"sha": MERGE_SHA}})
    return gh


def test_stage_timing_in_text_and_json_with_the_exact_calls(tmp_path, monkeypatch):
    gh = stage_github()
    rt = build(tmp_path, monkeypatch, gh, FakeClock(ONE_O_CLOCK))
    done = invoke(rt, ["timing", "stages", "--bead", "8um1", "--pr", "41", "--tag", TAG])
    assert done.exit_code == 0 and done.stdout.splitlines() == [
        f"Bead {BEAD} and Core PR #41 and release {TAG}", "  Bead creation to PR (ask time proxy)  600s", "  PR to merge  1200s", "  Merge to GitHub publication  900s"]
    assert [call[:1] + call[-1:] if call[0] == "gh" else call for call in gh.calls] == [
        ["bd", "--directory", str(tmp_path / "beads"), "--readonly", "--sandbox", "show", BEAD, "--json"],
        ["gh", f"repos/{REPO}/pulls/41"], ["gh", f"repos/{REPO}/releases/tags/{TAG}"], ["gh", f"repos/{REPO}/git/ref/tags/{TAG}"],
        ["gh", f"repos/{REPO}/compare/{MERGE_SHA}...{TAG_SHA}?per_page=1"]]
    assert not gh.writes() and rt.providers.store.writes == 0
    start, outcome = map(json.loads, done.stderr.splitlines())  # the two progress events, on stderr
    assert (start["event"], outcome["event"], outcome["outcome"]) == ("wait.stages.start", "wait.stages.outcome", "ok")
    doc = json.loads(invoke(build(tmp_path, monkeypatch, stage_github(), FakeClock(ONE_O_CLOCK)), ["--format", "json", "timing", "stages", "--bead", BEAD, "--pr", "41", "--tag", TAG]).stdout)
    report = tick.StageTiming.model_validate(doc)
    assert (report.repository, report.bead, report.pr, report.release_tag, report.tag_commit, report.observed_at) == (REPO, BEAD, 41, TAG, TAG_SHA, "2026-11-10T01:00:00+00:00")
    assert {key: (row.state, row.seconds) for key, row in report.intervals.items()} == {
        "bead_to_pr": ("measured", 600), "pr_to_merge": ("measured", 1200), "merge_to_publication": ("measured", 900)}
    assert "lines" not in doc and "incomplete" not in doc


def test_stage_timing_without_a_tag_is_pending_and_an_impossible_interval_exits_1(tmp_path, monkeypatch):
    pending = invoke(build(tmp_path, monkeypatch, stage_github(), FakeClock(ONE_O_CLOCK)), ["timing", "stages", "--bead", "8um1", "--pr", "41"])
    assert pending.exit_code == 0
    assert "  Merge to GitHub publication  1800s elapsed (pending verification) (no exact release tag supplied)" in pending.stdout
    partial = invoke(build(tmp_path, monkeypatch, stage_github(merged_at="2026-11-09T00:00:00Z"), FakeClock(ONE_O_CLOCK)),
                     ["--format", "json", "timing", "stages", "--bead", "8um1", "--pr", "41", "--tag", TAG])
    report = tick.StageTiming.model_validate(json.loads(partial.stdout))
    row = report.intervals["pr_to_merge"]
    assert partial.exit_code == 1 and (row.state, row.seconds, row.reason) == ("unavailable", None, "end timestamp precedes start timestamp")


def test_stage_timing_fails_closed_when_a_record_is_not_the_one_asked_for(tmp_path, monkeypatch):
    gh = stage_github(number=42)
    wrong = invoke(build(tmp_path, monkeypatch, gh, FakeClock(ONE_O_CLOCK)), ["timing", "stages", "--bead", "8um1", "--pr", "41"])
    assert wrong.exit_code == 1 and wrong.stdout == "" and "invalid or mismatched Core PR record" in wrong.stderr
    nothing = invoke(build(tmp_path, monkeypatch, FakeGh(), FakeClock(ONE_O_CLOCK)), ["--format", "json", "timing", "stages", "--bead", "8um1", "--pr", "41"])
    assert nothing.exit_code == 1 and json.loads(nothing.stdout)["error"] == "exact bead record unavailable"


def test_the_commands_are_read_only_and_their_help_meets_the_contract():
    for function, path in ((tick.timing_gate, "timing gate"), (tick.timing_hosted, "timing hosted"), (tick.timing_stages, "timing stages")):
        command = function.__sm_native__
        assert command.writes is False and command.replaces == ("hz-wait",) and command.path == path and "hz-" not in command.help
    assert contract_gaps(tick.timing_gate, tick.timing_hosted, tick.timing_stages) == {}
    assert tick.TIMING.path == "timing" and "hz-" not in tick.TIMING.help


def test_every_setting_of_the_area_has_a_description_and_its_prefix():
    for name, field in tick.TickSettings.model_fields.items():
        assert name.startswith("tick_") and field.description, name
