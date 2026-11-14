"""The candidate gate: GitHub sometimes returns `steps: []` for a finished success job (v0.9.31, run 42440095997).
A real runner plus a non-empty log of that exact job proves it ran; the pinned reader and its other checks are
unchanged, and a job that never acquired a runner is still refused."""

import pytest

from steuermaschine_heinzel.native.queue_lib import job_proof
from steuermaschine_heinzel.native.queue_lib import release_candidate as rc
from steuermaschine_heinzel.native.release_lib import candidate_policy

RUN = 42440095997
JOB = {"id": 128607543598, "name": "Unit tests", "runner_id": 7, "steps": [], "run_id": RUN, "run_attempt": 1,
       "status": "completed", "conclusion": "success", "head_sha": "a" * 40}


def logs(text):
    asked = []

    def read(job_id):
        asked.append(job_id)
        return text

    return read, asked


def test_empty_steps_with_a_runner_and_a_non_empty_log_of_that_job_get_one_proof_step():
    read, asked = logs(b"2026-11-08T10:00:00Z 1680 passed\n")
    proven = job_proof.log_proven(JOB, RUN, read)
    assert asked == [JOB["id"]]
    assert proven["steps"] == [{"name": job_proof.LOG_PROVEN, "status": "completed", "conclusion": "success", "log_bytes": 32}]
    assert {k: v for k, v in proven.items() if k != "steps"} == {k: v for k, v in JOB.items() if k != "steps"}


def test_recorded_steps_need_no_log():
    read, asked = logs(b"log")
    job = JOB | {"steps": [{"name": "Run"}]}
    assert job_proof.log_proven(job, RUN, read) is job and asked == []


@pytest.mark.parametrize("change", [
    {"runner_id": 0}, {"runner_id": None}, {"runner_id": "7"},   # never acquired a runner
    {"conclusion": "failure"}, {"status": "in_progress"},          # not a finished success
    {"run_attempt": 2}, {"run_id": RUN + 1},                       # another attempt or run
    {"id": None}, {"steps": None},                                 # no identity to read, or not the empty-steps shape
])
def test_any_other_shape_is_left_for_the_reader_to_refuse_without_reading_a_log(change):
    read, asked = logs(b"log")
    job = JOB | change
    assert job_proof.log_proven(job, RUN, read) is job and asked == []


@pytest.mark.parametrize("text", [b"", b"  \n"])
def test_an_empty_log_proves_nothing(text):
    read, _ = logs(text)
    assert job_proof.log_proven(JOB, RUN, read) is JOB


def test_an_unreadable_log_proves_nothing():
    def broken(job_id):
        raise rc.CandidateError("candidate job log unavailable")

    assert job_proof.log_proven(JOB, RUN, broken) is JOB


def test_the_release_gate_reader_proves_empty_steps_jobs_by_their_log(monkeypatch):
    other = JOB | {"id": 2, "name": "Lint", "steps": [{"name": "Run"}]}
    never = JOB | {"id": 3, "name": "Coverage", "runner_id": 0}
    page = {"total_count": 3, "jobs": [JOB, other, never]}
    monkeypatch.setattr(candidate_policy, "job_log", lambda job_id: b"ok\n" if job_id == JOB["id"] else pytest.fail("log of a job with steps or without a runner"))
    reader = candidate_policy.without_api_check_runs(lambda path: page)
    jobs = reader(f"actions/runs/{RUN}/attempts/1/jobs?per_page=100&page=1")["jobs"]
    assert [bool(job["steps"]) for job in jobs] == [True, True, False]
    assert jobs[0]["steps"][0]["name"] == job_proof.LOG_PROVEN and jobs[1] is other and jobs[2] is never


def test_the_pinned_reader_and_the_checkpoint_keep_the_runner_and_steps_check():
    import inspect

    from steuermaschine_heinzel.native.queue_lib import main_checkpoint

    assert 'job["runner_id"] > 0 and job.get("steps")' in inspect.getsource(rc.validate)  # the pinned reader is unchanged
    source = inspect.getsource(main_checkpoint.merged_candidate)
    assert "job_proof.log_proven(job, run_id," in source and 'f"actions/jobs/{job_id}/logs"' in source and "and job.get(\"steps\")" in source
