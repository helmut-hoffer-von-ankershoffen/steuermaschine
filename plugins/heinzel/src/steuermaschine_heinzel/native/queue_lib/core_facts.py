"""Core's reviewed CI facts for the readers that judge Core's main or its pull requests (and, by the same convention,
the other Heinzel repositories): the release policy of a given commit, or of the tip of Core's main.

One accessor so no reader types a workflow file, a job or a check name. A commit's policy never changes, so it is kept
for the life of the process; the tip of main is read again after a minute. Anything unreadable raises CandidateError:
each caller fails closed in its own way (a hold, a NO-GO, "unknown").
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from . import core_policy
from .release_candidate import REPOSITORY, CandidateError, require

MAIN_TTL = 60.0  # seconds a read of main's tip is reused
_BY_COMMIT: dict[str, core_policy.Policy] = {}
_MAIN: list[tuple[float, str]] = []


def _gh_json(path: str) -> Any:
    """One read-only `gh api` call on the Core repository; None when it fails (the readers fail closed)."""
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        done = subprocess.run([gh, "api", f"repos/{REPOSITORY}/{path}"], capture_output=True, text=True, timeout=60,  # noqa: S603
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return json.loads(done.stdout) if done.returncode == 0 else None
    except ValueError:
        return None


def _get(path: str) -> Any:
    value = _gh_json(path)
    require(value is not None, f"{path} unreadable")
    return value


def at(commit: str) -> core_policy.Policy:
    """The release policy of Core commit `commit` (exact SHA)."""
    if commit not in _BY_COMMIT:
        _BY_COMMIT[commit] = core_policy.at_commit_api(_get, commit)
    return _BY_COMMIT[commit]


def main_sha() -> str:
    now = time.monotonic()
    if _MAIN and now - _MAIN[0][0] < MAIN_TTL:
        return _MAIN[0][1]
    found = _get("commits/main")
    sha = found.get("sha") if isinstance(found, dict) else None
    require(isinstance(sha, str) and core_policy.SHA.fullmatch(sha), "Core main has no exact commit")
    assert isinstance(sha, str)
    _MAIN[:] = [(now, sha)]
    return sha


NO_POLICY = f"Core main has no {core_policy.PATH}; merge the Core policy head first"


def at_main() -> core_policy.Policy:
    """The release policy at the tip of Core's main: the base every queue, triage and gate decision is judged by.
    Main without the manifest raises one clear line (NO_POLICY), never a trace."""
    sha = main_sha()
    try:
        return at(sha)
    except CandidateError as exc:
        if core_policy.PATH in str(exc) and "JSON" not in str(exc) and "keys" not in str(exc):  # absent, not garbled
            raise CandidateError(NO_POLICY) from None
        raise


def safe(commit: str | None = None) -> core_policy.Policy | None:
    """The policy at `commit` (default: main's tip), or None when it cannot be read (the caller fails closed)."""
    try:
        return at(commit) if commit else at_main()
    except CandidateError:
        return None


def forget() -> None:
    """Drop the cached reads (tests; a long-running process never needs it: a commit's policy is immutable)."""
    _BY_COMMIT.clear()
    _MAIN.clear()


# The facts the readers use, at main's tip (each raises CandidateError when the policy is unreadable).
def aggregate() -> str:
    """The aggregate job of Core's CI run (red whenever another job is; it repeats the real failures)."""
    return at_main().aggregate


def ci_file() -> str:
    """The file name of Core's CI workflow (`gh run list --workflow`, the Actions API)."""
    return at_main().workflow_file


def ci_path() -> str:
    return at_main().workflow


def judge_file() -> str:
    """The file name of the merge-gate workflow (it posts the API checks)."""
    return at_main().judge_file


def judge_path() -> str:
    return at_main().judge_workflow


def gate_check(role: str) -> str:
    """The name of the merge-gate workflow's check of `role` (coverage or sonar)."""
    return at_main().gate_checks[role]


def api_checks() -> tuple[str, ...]:
    """The checks the merge-gate workflow posts (all required contexts but the aggregate): the Sonar gate, then the
    coverage floor."""
    gates = at_main().gate_checks
    return (gates["sonar"], gates["coverage"])


def contexts() -> tuple[str, ...]:
    """Every required status context: the aggregate, then the merge-gate workflow's checks."""
    return (aggregate(), *api_checks())


class Names:
    """A tuple of names read from the policy when it is used, never at import (a module constant that stays one)."""

    def __init__(self, read: Callable[[], Iterable[str]]) -> None:
        self._read = read

    def _names(self) -> tuple[str, ...]:
        return tuple(self._read())

    def __iter__(self) -> Iterator[str]:
        return iter(self._names())

    def __contains__(self, name: object) -> bool:
        return name in self._names()

    def __len__(self) -> int:
        return len(self._names())

    def __getitem__(self, index: Any) -> Any:
        return self._names()[index]

    def __add__(self, other: Iterable[str]) -> tuple[str, ...]:
        return self._names() + tuple(other)

    def __radd__(self, other: Iterable[str]) -> tuple[str, ...]:
        return tuple(other) + self._names()

    def __eq__(self, other: object) -> bool:
        return tuple(other) == self._names() if isinstance(other, (tuple, list, Names)) else NotImplemented

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"Names({self._read.__name__})"


API_CHECKS = Names(api_checks)  # the merge-gate workflow's checks
CONTEXTS = Names(contexts)  # every required status context
