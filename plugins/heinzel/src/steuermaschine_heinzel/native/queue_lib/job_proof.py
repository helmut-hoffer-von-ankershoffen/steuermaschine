"""Proof that a successful candidate job ran, when GitHub returns no steps for it.

GitHub sometimes returns `steps: []` for a finished success job (v0.9.31, run 42440095997, jobs 128607543550,
128607543598, 128615853505), and the pinned candidate reader then refuses "never acquired a runner". The reader
itself is the reviewed Core file (byte-exact, pinned) and stays unchanged; the release gate around it accepts such a
job only when every one of these holds, and otherwise leaves the job as it is so the reader refuses it:

- the job is completed with conclusion success, in the same run, on attempt 1;
- it has a real runner (`runner_id` is an integer above 0);
- the log of that exact job (read by its ID) is non-empty.

The job's steps are then one synthetic step that names the proof, so the reader's own check passes and the evidence
says why. A job with recorded steps, a job without a runner, or one whose log is empty or unreadable is untouched.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

LOG_PROVEN = "steps missing in the GitHub API; the job log of this job is non-empty"


def needs_log(job: object, run_id: int) -> bool:
    """A success job of this run's first attempt with a real runner and no steps: only its log can prove it ran."""
    return (
        isinstance(job, dict)
        and type(job.get("id")) is int
        and job["id"] > 0
        and job.get("run_id") == run_id
        and job.get("run_attempt") == 1
        and job.get("status") == "completed"
        and job.get("conclusion") == "success"
        and type(job.get("runner_id")) is int
        and job["runner_id"] > 0
        and job.get("steps") == []
    )


def log_proven(job: dict[str, Any], run_id: int, log: Callable[[int], bytes]) -> dict[str, Any]:
    """The job with one synthetic step when its non-empty log proves it ran; else the job unchanged."""
    if not needs_log(job, run_id):
        return job
    try:
        size = len(log(job["id"]).strip())
    except (ValueError, OSError):  # the candidate reader's refusal is a ValueError: unreadable stays unproven
        return job
    if not size:
        return job
    return job | {"steps": [{"name": LOG_PROVEN, "status": "completed", "conclusion": "success", "log_bytes": size}]}
