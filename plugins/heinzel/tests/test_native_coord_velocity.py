"""`sm doc velocity` and `sm doc velocity-record` on the fake process runner; the measurement itself is tested by the
moved tests in test_native_coord_velocity_moved.py, whose fake GitHub this file reuses."""

import json

from coord_kit import app_runtime
from test_native_coord_velocity_moved import FakeGh

from steuermaschine.protocols import Result
from steuermaschine.testing import invoke
from steuermaschine_heinzel.native import velocity


class Commands:
    """The process runner's shape around the moved tests' fake GitHub."""

    def __init__(self, fail=False):
        self.gh, self.fail, self.calls = FakeGh(), fail, []

    def run(self, argv, cwd=None, timeout=None, env=None):
        self.calls.append(list(argv))
        if self.fail:
            return Result(1, "", "gh: Server Error (HTTP 502)")
        return Result(0, self.gh(list(argv)).stdout, "")


def test_velocity_in_text_and_json_within_the_call_budget_and_cached(tmp_path):
    c = Commands()
    rt = app_runtime(tmp_path, c, velocity_cache_dir=str(tmp_path / "cache"))
    done = invoke(rt, ["doc", "velocity", "--sample", "466:100:1"])
    lines = done.stdout.splitlines()
    assert done.exit_code == 0 and lines[0].startswith("velocity: 1 explicit samples; ") and "/15 API calls, " in lines[0]
    assert lines[1].startswith("#466 run 100 attempt 1: attempt elapsed proxy ") and lines[-1].startswith("limit: ") and "hz-" not in done.stdout
    assert all(call[:2] == ["gh", "api"] for call in c.calls) and len(c.calls) <= 15
    doc = velocity.Velocity.model_validate(json.loads(invoke(rt, ["--format", "json", "doc", "velocity", "--sample", "466:100:1"]).stdout))
    assert doc.cache_hits >= 1 and doc.samples[0]["pr"] == 466 and doc.repo == "helmut-hoffer-von-ankershoffen/heinzel"
    assert (tmp_path / "cache").is_dir()


def test_velocity_usage_errors_and_an_unavailable_forge(tmp_path):
    rt = app_runtime(tmp_path, Commands(), velocity_cache_dir=str(tmp_path / "cache"))
    assert invoke(rt, ["doc", "velocity"]).exit_code == 2
    assert invoke(rt, ["doc", "velocity", "--sample", "1:2:3", "--sample", "1:2:3", "--sample", "1:2:3", "--sample", "1:2:3"]).exit_code == 2
    done = invoke(rt, ["doc", "velocity", "--sample", "466:x:1"])
    assert done.exit_code == 2 and "use PR:RUN:ATTEMPT" in done.stderr
    assert invoke(rt, ["doc", "velocity", "--sample", "466:100:1", "--repo", "../x"]).exit_code == 2
    done = invoke(app_runtime(tmp_path, Commands(fail=True), velocity_cache_dir=str(tmp_path / "c2")), ["doc", "velocity", "--sample", "466:100:1", "--release", "467:101:1"])
    assert done.exit_code == 1 and "GitHub request failed" in done.stderr


def test_record_keeps_the_first_observation_in_the_state_home_by_default(tmp_path):
    rt = app_runtime(tmp_path, Commands())
    head = "a" * 40
    first = invoke(rt, ["--format", "json", "doc", "velocity-record", f"466:{head}"])
    doc = velocity.EligibleRecorded.model_validate(json.loads(first.stdout))
    again = invoke(rt, ["doc", "velocity-record", f"466:{head}"])
    assert again.stdout == f"#466 aaaaaaaa: eligible observed {doc.eligible_observed_at} ({doc.source})\n"
    journal = rt.project.home / "velocity" / "eligibility.jsonl"
    assert len(journal.read_text().splitlines()) == 1


def test_record_refuses_a_short_head_and_an_unusable_journal(tmp_path):
    rt = app_runtime(tmp_path, Commands(), velocity_cache_dir=str(tmp_path / "cache"))
    assert invoke(rt, ["doc", "velocity-record", "466:abc"]).exit_code == 2
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "eligibility.jsonl").mkdir()
    done = invoke(rt, ["doc", "velocity-record", "466:" + "a" * 40])
    assert done.exit_code == 1 and "the journal is unusable" in done.stderr
