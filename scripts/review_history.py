# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""A spend-limit exception cannot override a prior same-head Gate rejection."""

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable

WORKFLOW = ".github/workflows/gate-review.yml"
REJECT_STEP = "Record explicit Gate review rejection"
LEGACY_JUDGE = "Judge the review and comment"


def _rows(document: object, key: str) -> tuple[int, list[dict]]:
    if isinstance(document, dict):
        total, records = document.get("total_count"), document.get(key)
        if (
            type(total) is int
            and total >= 0
            and isinstance(records, list)
            and all(isinstance(row, dict) for row in records)
        ):
            return total, records
    msg = "GitHub returned an unreadable Gate review inventory"
    raise ValueError(msg)


def _workflow_identity(run: dict, head: str) -> tuple[int, int]:
    identity, attempt = run.get("id"), run.get("run_attempt")
    if (
        (run.get("path"), run.get("event"), run.get("head_sha")) != (WORKFLOW, "pull_request_target", head)
        or run.get("status") not in ("queued", "in_progress", "completed", "waiting", "requested", "pending")
        or (run.get("status") != "completed" and run.get("conclusion") is not None)
        or (run.get("status") == "completed" and run.get("conclusion") not in
            ("success", "failure", "skipped", "cancelled", "neutral", "timed_out", "action_required", "stale"))
        or type(identity) is not int
        or identity < 1
        or type(attempt) is not int
        or attempt < 1
    ):
        msg = "GitHub returned inconsistent exact-head Gate workflow identity"
        raise ValueError(msg)
    return identity, attempt


def inventory(get: Callable[[str], object], repo: str, head: str) -> list[dict]:
    """Read only the complete exact-head protected-workflow inventory."""
    if not isinstance(head, str) or not re.fullmatch(r"[a-f0-9]{40}", head):
        raise ValueError("the Gate head is invalid")
    start = (
        f"repos/{repo}/actions/workflows/gate-review.yml/runs?event=pull_request_target&head_sha={head}&per_page=100"
    )
    known: dict[int, dict] = {}
    count = None
    for page in range(1, 11):
        total, runs = _rows(get(f"{start}&page={page}"), "workflow_runs")
        if count is None:
            count = total
        if total != count or len(runs) != min(100, max(0, total - len(known))):
            raise ValueError("the same-head Gate workflow inventory changed or is incomplete")
        for run in runs:
            identity, _ = _workflow_identity(run, head)
            if identity in known:
                raise ValueError("GitHub returned duplicate Gate workflow identity")
            known[identity] = run
        if len(known) == total:
            return list(known.values())
    raise ValueError("the same-head Gate workflow inventory is incomplete")


def verify_history(
    get: Callable[[str], object], repo: str, head: str, current: tuple[int, int] | None = None
) -> list[tuple[dict, dict]]:
    """Verify every attempt, excluding only the executing workflow's own attempt.

    The release reader supplies no current attempt: latest attempts are evidence too.
    """
    runs = inventory(get, repo, head)
    if current is not None and not any((r["id"], r["run_attempt"]) == current for r in runs):
        raise ValueError("the current Gate run and attempt are not in a complete same-head inventory")
    evidence = []
    for run in runs:
        for attempt in range(1, run["run_attempt"] + 1):
            if (run["id"], attempt) == current:
                continue
            if attempt == run["run_attempt"] and (
                run.get("status") != "completed" or run.get("conclusion") not in ("success", "failure")
            ):
                raise ValueError("a same-head Gate workflow is incomplete or ambiguous")
            job = _verify_attempt(get, repo, head, run["id"], attempt)
            if attempt == run["run_attempt"]:
                evidence.append((run, job))
    return evidence


def _verify_attempt(get: Callable[[str], object], repo: str, head: str, run_id: int, attempt: int) -> dict:
    count, jobs = _rows(get(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"), "jobs")
    gate = [job for job in jobs if job.get("name") == "Gate review"]
    if count != len(jobs) or count > 100 or len(gate) != 1:
        msg = "a prior same-head Gate attempt has no complete job evidence"
        raise ValueError(msg)
    names = [row.get("name") for row in jobs]
    ids = [row.get("id") for row in jobs]
    conclusions = ("success", "failure", "skipped", "cancelled", "neutral", "timed_out", "action_required", "stale")
    if (not all(isinstance(name, str) and name for name in names)
        or len(set(names)) != len(names)
        or not all(type(identity) is int and identity > 0 for identity in ids)
        or len(set(ids)) != len(ids)
        or any((row.get("run_id"), row.get("run_attempt"), row.get("head_sha"), row.get("status")) !=
               (run_id, attempt, head, "completed") or row.get("conclusion") not in conclusions for row in jobs)):
        raise ValueError("a prior same-head Gate attempt has ambiguous job evidence")
    job = gate[0]
    if type(job.get("id")) is not int or job["id"] < 1:
        raise ValueError("a prior same-head Gate job has invalid identity")
    if (job.get("run_id"), job.get("run_attempt"), job.get("head_sha"), job.get("status")) != (
        run_id,
        attempt,
        head,
        "completed",
    ):
        msg = "a prior same-head Gate attempt is incomplete or has mismatched identity"
        raise ValueError(msg)
    steps = job.get("steps")
    if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
        msg = "a prior same-head Gate attempt has unreadable steps"
        raise ValueError(msg)
    names = [step.get("name") for step in steps]
    if (not all(isinstance(name, str) and name for name in names)
        or len(set(names)) != len(names)
        or any(step.get("status") != "completed" or step.get("conclusion") not in
               ("success", "failure", "skipped", "cancelled", "neutral", "timed_out", "action_required")
               for step in steps)):
        raise ValueError("a prior same-head Gate attempt has ambiguous steps")
    rejected = [(step.get("status"), step.get("conclusion")) for step in steps if step.get("name") == REJECT_STEP]
    if rejected == [("completed", "success")]:
        msg = "the trusted model explicitly rejected this exact head; the spending-limit exception is unavailable"
        raise ValueError(msg)
    if rejected == [("completed", "skipped")] and job.get("conclusion") in ("success", "failure"):
        return job
    # The original workflow only succeeded when the reviewer approved. A failed legacy judge may have rejected the head.
    legacy = [(step.get("status"), step.get("conclusion")) for step in steps if step.get("name") == LEGACY_JUDGE]
    if not rejected and job.get("conclusion") == "success" and legacy == [("completed", "success")]:
        return job
    msg = "a prior same-head Gate result cannot rule out an explicit rejection"
    raise ValueError(msg)


def check(get: Callable[[str], object], repo: str, head: str, run_id: int, attempt: int) -> None:
    """Verify complete same-head history for the current protected workflow attempt.

    Raises:
        ValueError: A rejection was found or GitHub could not prove the review history.

    """
    if (not isinstance(head, str) or not re.fullmatch(r"[a-f0-9]{40}", head)
        or type(run_id) is not int or run_id < 1 or type(attempt) is not int or attempt < 1):
        msg = "the current Gate head, run or attempt is invalid"
        raise ValueError(msg)
    verify_history(get, repo, head, (run_id, attempt))


def _github(path: str) -> object:
    result = subprocess.run(["gh", "api", path], text=True, capture_output=True, check=False, timeout=30)
    if result.returncode:
        msg = "GitHub could not supply prior Gate review attempts"
        raise ValueError(msg)
    return json.loads(result.stdout)


def main() -> int:
    """Run the history check and surface only its fixed diagnostic messages."""
    try:
        # Only the protected workflow checkout may supply this executable checker.
        base = os.environ["TRUSTED_BASE"]
        with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as stream:
            event = json.load(stream)
        if (
            os.environ["GITHUB_EVENT_NAME"] != "pull_request_target"
            or not re.fullmatch(r"[a-f0-9]{40}", base)
            or os.environ["GITHUB_WORKFLOW_SHA"] != base
            or os.environ["GITHUB_SHA"] != base
            or event["repository"]["full_name"] != os.environ["GITHUB_REPOSITORY"]
            or event["pull_request"]["head"]["sha"] != os.environ["PR_HEAD"]
            or event["pull_request"]["base"]["sha"] != base
            or event["pull_request"]["number"] != int(os.environ["PR_NUMBER"])
        ):
            raise ValueError("Gate runner identities differ from the protected event")
        check(
            _github,
            os.environ["GITHUB_REPOSITORY"],
            os.environ["PR_HEAD"],
            int(os.environ["GITHUB_RUN_ID"]),
            int(os.environ["GITHUB_RUN_ATTEMPT"]),
        )
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError, KeyError, TypeError):
        print("::error::GitHub Gate review history could not be verified", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
