"""Exact, reviewed download incidents for the main merge queue only."""

import hashlib
import re
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

GREEN_JOB = ("success", "skipped", "neutral")
# These steps can fail before lint/test execution or during pinned tool validation.
# The reviewer must still confirm the logs show only pinned-tool download failures.
SETUP_STEPS = frozenset((
    "Run make lint",
    "Run make setup",
    "Install browser runtimes and host dependencies",
    "Every pinned tool, for every platform, still downloads and matches its checksum",
))
AGGREGATE_STEP = "Fail if any required job failed"
MAX_VALIDITY = timedelta(hours=24)


def failure_inventory(jobs: list[dict], run: int, sha: str) -> list[dict] | None:
    """Read a complete first-attempt failure inventory, including CI OK."""
    seen = set()
    failed = []
    for job in jobs:
        job_id = job.get("id")
        if (type(job_id) is not int or job_id <= 0 or job_id in seen or job.get("run_id") != run
                or job.get("head_sha") != sha or type(job.get("run_attempt")) is not int or job["run_attempt"] != 1
                or job.get("status") != "completed"):
            return None
        seen.add(job_id)
        if job.get("conclusion") in GREEN_JOB:
            continue
        if job.get("conclusion") != "failure" or not isinstance(job.get("steps"), list):
            return None
        steps = job["steps"]
        if any(not isinstance(step, dict) or step.get("status") != "completed"
               or step.get("conclusion") not in (*GREEN_JOB, "failure") for step in steps):
            return None
        bad_steps = [step for step in steps if step["conclusion"] == "failure"]
        if len(bad_steps) != 1:
            return None
        step = bad_steps[0]
        number, name = step.get("number"), step.get("name")
        permitted = {AGGREGATE_STEP} if job["name"] == "CI OK" else SETUP_STEPS
        if type(number) is not int or number <= 0 or not isinstance(name, str) or name not in permitted:
            return None
        failed.append({"id": job_id, "name": job["name"], "step_number": number, "step_name": name})
    names = {job["name"] for job in failed}
    if len(names) != len(failed) or len(failed) < 2 or "CI OK" not in names:
        return None
    return sorted(failed, key=lambda job: job["id"])


def reviewed_incident(path: Path, run: int, sha: str, jobs: list[dict], *, now: datetime | None = None) -> dict | None:
    """Return evidence only for the exact, still-in-date independently reviewed attempt."""
    try:
        raw = path.read_bytes()
        receipt = tomllib.loads(raw.decode())
    except (OSError, ValueError):
        return None
    if (receipt.get("run") != run or type(receipt.get("run")) is not int
            or receipt.get("sha") != sha or not re.fullmatch("[0-9a-f]{40}", sha)
            or receipt.get("attempt") != 1 or type(receipt.get("attempt")) is not int
            or receipt.get("total_jobs") != len(jobs) or type(receipt.get("total_jobs")) is not int
            or receipt.get("cause") != "pinned-tool-http-504"):
        return None
    reviewed, until = receipt.get("reviewed_at"), receipt.get("until")
    if not all(isinstance(t, datetime) and t.utcoffset() is not None for t in (reviewed, until)):
        return None
    now = now or datetime.now(timezone.utc)
    if not reviewed <= now < until or until - reviewed > MAX_VALIDITY:
        return None
    bead, review = receipt.get("bead"), receipt.get("review")
    if (not isinstance(bead, str) or not re.fullmatch(r"(?:heinzel-management-)?[a-z0-9]{2,6}", bead)
            or not isinstance(review, str) or not review.strip()):
        return None
    failures = failure_inventory(jobs, run, sha)
    entries = receipt.get("failures")
    if (failures is None or not isinstance(entries, list)
            or any(not isinstance(row, dict) or type(row.get("id")) is not int
                   or type(row.get("step_number")) is not int for row in entries)
            or sorted(entries, key=lambda job: job["id"]) != failures):
        return None
    return {"bead": bead, "receipt_sha256": hashlib.sha256(raw).hexdigest()}
