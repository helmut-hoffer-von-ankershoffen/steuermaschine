"""The project's part of reading a failing log: which failing checks are jobs of the CI run that a rerun can name.
The log cutting itself is the core's (`steuermaschine.github.logs`)."""

from __future__ import annotations

NOT_JOBS = ("CodeQL", "SonarCloud", "merge-gate", "coverage-floor", "review", "attempt")  # checks that are not jobs of the CI run


def rerun_job(job_names: list[str]) -> str | None:
    """The first failing name that is a job of the CI run (the aggregate and the gate checks are not), or None."""
    names = [n for n in job_names if n and n not in ("CI OK", "run") and not n.startswith(NOT_JOBS)]
    return names[0] if names else None
