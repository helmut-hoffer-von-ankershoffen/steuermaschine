"""`sm timing gate|hosted|stages`: the moved tests of the former wait tool's gate, hosted and stage timings, then the
commands through the whole command tree, on fakes.

Moved tests keep their assertions. Changed:
- `hw` is the moved library; `main(argv)` below stands for the former entry point's dispatch (the `--json` word and the
  subcommand); the option checks are the edge's now, so the two tests of invalid inputs go through the command tree
  (exit 2, no read) instead of catching the parser's exit;
- an assertion that named the old command: the refusal line starts `timing hosted:`;
- fakes of the edges: the fixed time is the clock's (it was a patched datetime class and a patched time function),
  the tracker and GitHub answers come from the `Commands` provider (it was a patched process call, same argv), and a
  timeout is an OSError (there is no process exception any more);
- the fixture no longer forbids the diagnosis function: it was not moved (parity/timing-gate.md)."""

import json
from datetime import UTC, datetime

import pytest
from steuermaschine_heinzel.native.tick_lib import timing as hw
from tick_support import REPO, build

from steuermaschine.github.testing import FakeGh
from steuermaschine.protocols import Result
from steuermaschine.testing import invoke
from steuermaschine.testing.fakes import FakeClock


@pytest.fixture(autouse=True)
def fresh_library(monkeypatch, tmp_path):
    """Every test starts with an unbound library: output goes to the process streams, as the former tool's did."""
    monkeypatch.setattr(hw, "EDGE", hw.Edge())
    monkeypatch.setattr(hw, "REPO", REPO)
    monkeypatch.setattr(hw, "BEAD_PREFIX", "tracker-")
    monkeypatch.setattr(hw, "BEAD_ROOT", str(tmp_path / "beads"))
    monkeypatch.setattr(hw, "HIST", tmp_path / "gate.jsonl")


def main(argv):
    """The former entry point's dispatch, for the moved tests."""
    as_json = "--json" in argv
    words = [a for a in argv if a != "--json"]
    if words[0] == "hosted":
        return hw.hosted(words[1:], as_json)
    options = dict(zip(words[1::2], words[2::2], strict=True))
    return hw.stages(hw.BEAD_PREFIX + options["--bead"].removeprefix(hw.BEAD_PREFIX), int(options["--pr"]), options.get("--tag"), as_json)


hw.main = main  # the moved tests call it by its former name

# ----------------------------------------------------------------------------- moved: the gate history
def test_percentile_and_gate_stats_missing_file(tmp_path):
    assert hw.pctl([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90) == 9
    assert hw.gate_stats(str(tmp_path / "none.jsonl"))["runs"] == 0


def test_gate_stats_separate_wait_from_command_wall_time(tmp_path, monkeypatch):
    monkeypatch.setattr(hw.EDGE, "clock", FakeClock(3601))
    rows = [
        {"at": "1970-01-01T00:00:00+0000", "waited_s": 99, "ran_s": 99, "exit": 0},
        {"at": "1970-01-01T00:30:00+0000", "waited_s": 0, "ran_s": 7, "exit": 0},
        {"at": "1970-01-01T00:59:00+0000", "waited_s": 2, "ran_s": 17, "exit": 1},
    ]
    history = tmp_path / "gate.jsonl"
    history.write_text("\n".join(map(json.dumps, rows)))
    stats = hw.gate_stats(history)
    assert stats["runs"] == 2
    assert stats["wait_total_s"] == 2
    assert stats["ran_total_s"] == 24
    assert stats["ran_p90_s"] == 17


# ----------------------------------------------------------------------------- moved: hosted timing
CREATED = "2026-11-10T03:00:00Z"
STARTED = "2026-11-10T04:00:07+01:00"
COMPLETED = "2026-11-10T03:01:10Z"


def job(run_id=41, **changes):
    return {"id": 701, "run_id": run_id, "head_sha": "b" * 40, "name": "Focused Linux", "status": "completed",
            "conclusion": "success", "started_at": STARTED, "completed_at": COMPLETED, "runner_id": 20, **changes}


@pytest.fixture
def source(monkeypatch):
    def disallowed():
        pytest.fail("hosted timing must not read local gates or write the velocity log")

    monkeypatch.setattr(hw, "gate_stats", disallowed)
    run = {"id": 41, "head_sha": "a" * 40, "run_attempt": 1, "path": ".github/workflows/focused-job.yml",
           "event": "workflow_dispatch", "status": "completed", "conclusion": "success", "created_at": CREATED}
    pages = [{"total_count": 1, "jobs": [job()]}]
    calls = []
    root = f"repos/{hw.REPO}/actions/runs/41"

    def gh(*args):
        calls.append(args)
        if args == ("api", root):
            return run
        if args == ("api", "--paginate", "--slurp", f"{root}/attempts/{run['run_attempt']}/jobs?per_page=100"):
            return pages
        pytest.fail(f"unexpected GitHub call: {args}")

    monkeypatch.setattr(hw, "gh", gh)
    return run, pages, calls


def test_exact_run_attempt_preserves_job_provenance_and_separates_durations(source, capsys):
    _, _, calls = source
    assert hw.main(["hosted", "--json", "41"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["repository"] == hw.REPO
    run, = report["runs"]
    assert (run["id"], run["attempt"], run["head_sha"], run["created_at"]) == (41, 1, "a" * 40, CREATED)
    assert run["jobs"] == [dict(id=701, head_sha="b" * 40, name="Focused Linux", status="completed",
                                conclusion="success", runner_id=20, started_at=STARTED, completed_at=COMPLETED,
                                timing="measured", dispatch_to_start_s=7, job_wall_s=63)]
    assert len(calls) == 2


@pytest.mark.parametrize(("fields", "timing", "dispatch", "wall"), [
    ({"status": "queued", "conclusion": None}, "queued", None, None),
    ({"status": "in_progress", "conclusion": None, "completed_at": None}, "running", 7, None),
    ({"conclusion": "cancelled", "runner_id": 0, "steps": []}, "not_started", None, None),
    ({"runner_id": 0}, "unverified_start", None, None),
    ({"runner_id": None, "steps": [{"started_at": STARTED}]}, "measured", 7, 63),
    ({"conclusion": "skipped"}, "not_started", None, None),
    ({"started_at": None}, "missing_or_invalid_start", None, None),
    ({"started_at": "2026-11-10T03:00:00"}, "missing_or_invalid_start", None, None),
    ({"completed_at": None}, "missing_or_invalid_completion", 7, None),
    ({"completed_at": CREATED}, "missing_or_invalid_completion", 7, None),
    ({"started_at": CREATED, "completed_at": CREATED}, "measured", 0, 0),
])
def test_timing_uses_only_observed_intervals(fields, timing, dispatch, wall, source):
    run, _, _ = source
    row = hw.hosted_job(run, job(**fields))
    assert (row["timing"], row["dispatch_to_start_s"], row["job_wall_s"]) == (timing, dispatch, wall)


@pytest.mark.parametrize("run_fields", [{"run_attempt": 2}, {"event": "pull_request"}, {"created_at": None}])
def test_run_creation_is_not_a_dispatch_clock_for_a_rerun_or_other_event(run_fields, source, capsys):
    run, _, _ = source
    run.update(run_fields)
    assert hw.main(["--json", "hosted", "41"]) == 0
    result, = json.loads(capsys.readouterr().out)["runs"]
    row, = result["jobs"]
    assert result["attempt"] == run["run_attempt"]
    assert row["dispatch_to_start_s"] is None
    assert row["job_wall_s"] == 63


def test_paginated_jobs_are_checked_and_included_in_text(source, capsys):
    _, pages, _ = source
    pages[:] = [{"total_count": 2, "jobs": [job()]}, {"total_count": 2, "jobs": [job(id=702)]}]
    assert hw.main(["hosted", "41"]) == 0
    report = capsys.readouterr().out
    assert "Run 41 attempt 1" in report
    assert "Job 701" in report and "Job 702" in report
    assert "dispatch to start 7s | job wall 63s" in report


def test_queued_run_can_have_no_job_records(source, capsys):
    run, pages, _ = source
    run.update(status="queued", conclusion=None)
    pages[:] = [{"total_count": 0, "jobs": []}]
    assert hw.main(["hosted", "41"]) == 0
    report = capsys.readouterr().out
    assert "(queued)" in report and "No job records" in report


@pytest.mark.parametrize("case", ["partial", "foreign_run", "wrong_attempt", "duplicate"])
def test_invalid_inventory_returns_no_timing_report(case, source, capsys):
    _, pages, _ = source
    rows = {"partial": [job()], "foreign_run": [job(42), job(id=702)],
            "wrong_attempt": [job(run_attempt=2), job(id=702)], "duplicate": [job(), job()]}
    pages[:] = [{"total_count": 2, "jobs": rows[case]}]
    assert hw.main(["hosted", "41", "--json"]) == 1
    out = capsys.readouterr()
    assert not out.out
    assert "run 41: incomplete or mismatched attempt job inventory" in out.err


@pytest.mark.parametrize("fields", [
    {"conclusion": None}, {"status": "queued"}, {"conclusion": "unknown"}, {"created_at": 42},
])
def test_inconsistent_run_metadata_stops_before_job_lookup(fields, source, capsys):
    run, _, calls = source
    run.update(fields)
    assert hw.main(["hosted", "41", "--json"]) == 1
    out = capsys.readouterr()
    assert not out.out
    assert out.err == "timing hosted: run 41: invalid run identity or metadata\n"
    assert len(calls) == 1


@pytest.mark.parametrize("fields", [
    {"conclusion": None}, {"status": "queued"}, {"conclusion": "unknown"}, {"started_at": 42},
    {"completed_at": {}}, {"runner_id": True}, {"run_attempt": True}, {"steps": "invalid"},
    {"steps": [42]}, {"steps": [{"started_at": 42}]},
])
def test_inconsistent_job_metadata_cannot_produce_timing(fields, source, capsys):
    _, pages, _ = source
    pages[0]["jobs"] = [job(**fields)]
    assert hw.main(["hosted", "41"]) == 1
    out = capsys.readouterr()
    assert not out.out
    assert out.err == "timing hosted: run 41: incomplete or mismatched attempt job inventory\n"


def test_text_escapes_untrusted_workflow_and_job_controls(source, capsys):
    run, pages, _ = source
    run["path"] = "focused\n\r\x1b[31m\u202e.yml"
    pages[0]["jobs"] = [job(name="Focused\n\t\x07\u2028job")]
    assert hw.main(["hosted", "41"]) == 0
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 2
    assert ascii(run["path"]) in out and ascii(pages[0]["jobs"][0]["name"]) in out
    assert all(character.isprintable() or character == "\n" for character in out)


@pytest.mark.parametrize("failure", [OSError("unexpected\nsecret\x1b"),
                                      TimeoutError("gh secret\x1b"),
                                      ValueError("unexpected\nsecret\x1b"), None])
def test_api_failures_emit_only_a_fixed_message(failure, monkeypatch, capsys):
    def gh(*args):
        if failure is not None:
            raise failure
        return []

    monkeypatch.setattr(hw, "gh", gh)
    assert hw.main(["hosted", "41", "--json"]) == 1
    out = capsys.readouterr()
    assert not out.out
    assert out.err == "timing hosted: run 41: GitHub API request failed\n"


@pytest.mark.parametrize("args", [[], ["0"], ["-1"], ["41/attempts"], ["41", "41"], [str(i) for i in range(1, 10)]])
def test_invalid_id_inputs_fail_before_github(args, source, tmp_path, monkeypatch):
    _, _, calls = source
    gh = FakeGh()
    error = invoke(build(tmp_path, monkeypatch, gh), ["timing", "hosted", *args])
    assert error.exit_code == 2
    assert not calls and not gh.calls


def test_accepts_eight_ids_and_preserves_order(monkeypatch, capsys):
    ids = [str(i) for i in range(40, 32, -1)]
    monkeypatch.setattr(hw, "hosted_run", lambda number: {"id": number})
    assert hw.main(["hosted", *ids, "--json"]) == 0
    assert [run["id"] for run in json.loads(capsys.readouterr().out)["runs"]] == [int(i) for i in ids]


# ----------------------------------------------------------------------------- moved: stage timing
BEAD, TAG = "tracker-8um1", "v0.9.24"
BEAD_AT, OPENED = "2026-11-10T00:00:00Z", "2026-11-10T00:10:00Z"
MERGED, PUBLISHED = "2026-11-10T00:30:00Z", "2026-11-10T00:45:00Z"
MERGE_SHA, TAG_SHA, ANNOTATED_SHA = "a" * 40, "b" * 40, "c" * 40
PR, RELEASE, REF = "pulls/41", f"releases/tags/{TAG}", f"git/ref/tags/{TAG}"
COMPARE = f"compare/{MERGE_SHA}...{TAG_SHA}?per_page=1"


@pytest.fixture
def evidence(monkeypatch):
    monkeypatch.setattr(hw.EDGE, "clock", FakeClock(datetime(2026, 11, 10, 1, tzinfo=UTC).timestamp()))
    replies = {
        "bead": [{"id": BEAD, "created_at": BEAD_AT}],
        PR: {"number": 41, "base": {"repo": {"full_name": hw.REPO}}, "state": "closed", "merged": True,
             "created_at": OPENED, "merged_at": MERGED, "merge_commit_sha": MERGE_SHA},
        RELEASE: {"tag_name": TAG, "draft": False, "prerelease": False, "published_at": PUBLISHED},
        REF: {"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": TAG_SHA}},
        COMPARE: {"status": "ahead", "merge_base_commit": {"sha": MERGE_SHA}},
    }
    calls = []

    class Commands:
        def run(self, argv, cwd=None, timeout=None, env=None):
            args = tuple(argv)
            if args[0] == "bd":
                assert args == ("bd", "--directory", hw.BEAD_ROOT, "--readonly", "--sandbox", "show", BEAD, "--json")
                endpoint = "bead"
            else:
                assert args[:4] == ("gh", "api", "--hostname", "github.com")
                assert len(args) == 5 and args[4].startswith(f"repos/{hw.REPO}/")
                endpoint = args[4].removeprefix(f"repos/{hw.REPO}/")
            calls.append(endpoint)
            reply = replies[endpoint]
            return Result(int(reply is None), json.dumps(reply), "")

    monkeypatch.setattr(hw.EDGE, "commands", Commands())
    return replies, calls


def stage_report(capsys, *options, code=0):
    assert hw.main(["stages", "--pr", "41", "--bead", "8um1", "--json", *options]) == code
    result = capsys.readouterr()
    start, outcome = map(json.loads, result.err.splitlines())
    assert start["event"] == "wait.stages.start"
    assert outcome["event"] == "wait.stages.outcome"
    assert start["bead"] == outcome["bead"] == BEAD
    return json.loads(result.out) if result.out else None, outcome


@pytest.mark.parametrize("annotated", [False, True])
def test_publication_requires_exact_core_release_and_ancestry(evidence, capsys, annotated):
    replies, calls = evidence
    annotation = f"git/tags/{ANNOTATED_SHA}"
    if annotated:
        replies[REF]["object"] = {"type": "tag", "sha": ANNOTATED_SHA}
        replies[annotation] = {"sha": ANNOTATED_SHA, "object": {"type": "commit", "sha": TAG_SHA}}
    snapshot, outcome = stage_report(capsys, "--tag", TAG)
    assert calls == ["bead", PR, RELEASE, REF, *([annotation] if annotated else []), COMPARE]
    assert snapshot["bead_time_basis"] == "creation proxy for owner ask"
    assert snapshot["release_tag"] == TAG and snapshot["tag_commit"] == TAG_SHA
    assert snapshot["intervals"] == {
        "bead_to_pr": {"state": "measured", "seconds": 600, "start_at": BEAD_AT, "end_at": OPENED},
        "pr_to_merge": {"state": "measured", "seconds": 1200, "start_at": OPENED, "end_at": MERGED},
        "merge_to_publication": {"state": "measured", "seconds": 900, "start_at": MERGED, "end_at": PUBLISHED},
    }
    assert outcome["outcome"] == "ok"


def test_exact_tag_can_point_to_the_merge_itself(evidence, capsys):
    replies, _ = evidence
    replies[REF]["object"]["sha"] = MERGE_SHA
    replies[f"compare/{MERGE_SHA}...{MERGE_SHA}?per_page=1"] = {
        "status": "identical", "merge_base_commit": {"sha": MERGE_SHA},
    }
    snapshot, _ = stage_report(capsys, "--tag", TAG)
    assert snapshot["tag_commit"] == MERGE_SHA
    assert snapshot["intervals"]["merge_to_publication"]["seconds"] == 900


@pytest.mark.parametrize("endpoint,change,reason", [
    (RELEASE, {"draft": True}, "exact final GitHub release unverified"),
    (RELEASE, {"tag_name": "v0.9.25"}, "exact final GitHub release unverified"),
    (REF, {"ref": "refs/heads/v0.9.24"}, "exact GitHub tag commit unavailable"),
    (COMPARE, {"status": "diverged"}, "GitHub comparison does not prove the release tag contains the merge"),
    (COMPARE, {"merge_base_commit": {"sha": ANNOTATED_SHA}},
     "GitHub comparison does not prove the release tag contains the merge"),
])
def test_unproven_tag_reports_only_elapsed_since_merge(evidence, capsys, endpoint, change, reason):
    replies, _ = evidence
    replies[endpoint].update(change)
    snapshot, outcome = stage_report(capsys, "--tag", TAG)
    assert snapshot["tag_commit"] is None
    row = snapshot["intervals"]["merge_to_publication"]
    assert (row["state"], row["seconds"], row["reason"]) == ("pending", 1800, reason)
    assert row["end_at"] == snapshot["observed_at"]
    assert outcome["intervals"]["merge_to_publication"] == "pending"


@pytest.mark.parametrize("state,merged,interval,expected", [
    ("closed", True, "merge_to_publication", ("pending", 1800, "no exact release tag supplied")),
    ("open", False, "pr_open_age", ("pending", 3000, "PR is open")),
    ("closed", False, "pr_to_merge", ("not_applicable", None, "PR closed without a merge")),
])
def test_without_release_proof(evidence, capsys, state, merged, interval, expected):
    replies, calls = evidence
    if not merged:
        replies[PR].update(state=state, merged=False, merged_at=None)
    snapshot, _ = stage_report(capsys, *([] if merged else ["--tag", TAG]))
    row = snapshot["intervals"][interval]
    assert (row["state"], row["seconds"], row["reason"]) == expected
    assert calls == ["bead", PR]


@pytest.mark.parametrize("endpoint,field,value,interval,reason", [
    ("bead", "created_at", MERGED, "bead_to_pr", "end timestamp precedes start timestamp"),
    (PR, "merged_at", BEAD_AT, "pr_to_merge", "end timestamp precedes start timestamp"),
    (RELEASE, "published_at", OPENED, "merge_to_publication", "end timestamp precedes start timestamp"),
    (RELEASE, "published_at", "2026-11-10T00:45:00", "merge_to_publication", "missing or invalid timestamp"),
])
def test_invalid_intervals_never_become_zero_or_pending(evidence, capsys, endpoint, field, value, interval, reason):
    replies, _ = evidence
    target = replies[endpoint][0] if endpoint == "bead" else replies[endpoint]
    target[field] = value
    snapshot, outcome = stage_report(capsys, "--tag", TAG, code=1)
    assert snapshot["intervals"][interval] == {"state": "unavailable", "seconds": None, "reason": reason}
    assert outcome["outcome"] == "partial"


@pytest.mark.parametrize("endpoint,reply,reason", [
    ("bead", [{"id": "tracker-abcd"}], "exact bead record unavailable"),
    (PR, {"number": 42}, "invalid or mismatched Core PR record"),
    (PR, None, "Core PR record unavailable"),
])
def test_wrong_or_unavailable_source_fails_closed(evidence, capsys, endpoint, reply, reason):
    replies, _ = evidence
    replies[endpoint] = reply
    snapshot, outcome = stage_report(capsys, code=1)
    assert snapshot is None
    assert outcome["outcome"] == "unavailable" and outcome["reason"] == reason


def test_human_output_labels_pending_publication(evidence, capsys):
    assert hw.main(["stages", "--bead", BEAD, "--pr", "41"]) == 0
    output = capsys.readouterr().out
    assert "Bead creation to PR (ask time proxy)  600s" in output
    assert "Merge to GitHub publication  1800s elapsed (pending verification) (no exact release tag supplied)" in output


@pytest.mark.parametrize("args", [
    ["stages", "--pr", "41"], ["stages", "--bead", "8um1"],
    ["stages", "--bead", "repo/8um1", "--pr", "41"], ["stages", "--bead", "8um1", "--pr", "#41"],
    ["stages", "--bead", "8um1", "--pr", "0"], ["stages", "--bead", "8um1", "--pr", "41", "--tag", "latest"],
    ["stages", "--bead", "8um1", "--pr", "41", "--tag", "v0.09.24"],
    ["stages", "--bead", "8um1", "--pr", "41", "--tag", "v0.9.24/other"],
    ["stages", "--bead", "8um1", "--pr", "41", "42"], ["gate", "--bead", "8um1"],
])
def test_invalid_inputs_make_no_reads(evidence, args, tmp_path, monkeypatch):
    _, calls = evidence
    gh = FakeGh()
    exc = invoke(build(tmp_path, monkeypatch, gh), ["timing", *args])
    assert exc.exit_code == 2
    assert calls == [] and gh.calls == []
