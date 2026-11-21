"""What the moved ship code reaches outside its own directory, as functions: settings, the run lock, the queue."""

from __future__ import annotations

import contextlib
import io
import sys
from dataclasses import dataclass

from ..queue_lib import edge


@dataclass
class Done:
    """What a step printed and its exit code: the shape of the finished process each step used to be."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


def settings() -> dict:
    """The plugin's settings of the running native command ({} outside one)."""
    from steuermaschine.native import runtime

    try:
        values = runtime().settings
    except RuntimeError:
        return {}
    values = values.of("heinzel") if hasattr(values, "of") else values
    return values if isinstance(values, dict) else dict(values)


def configure() -> dict:
    """Point the moved modules at the configured records checkout before one is loaded (they compute paths at load)."""
    conf = settings()
    edge.configure(conf.get("queue_records_dir"))
    return conf


def gate(max_wait: int = 0, exclusive: bool = False, priority: int = 0) -> list[str]:
    """The argv prefix that runs a command under the run lock: this program's own `gate run`. `max_wait` (seconds,
    0: as long as it takes): give up with exit 75 and nothing run. `exclusive`: a release push, a full local run
    (`--full`): it waits for an idle machine below the load bars and keeps it. A normal push is a fast check: it runs beside others
    at any load; `priority` (1, 2) puts it ahead of the fast checks that wait."""
    return [sys.executable, "-m", "steuermaschine.app", "gate", "run", *(["--max-wait", str(max_wait)] if max_wait else []),
            *(["--priority", "2", "--full", "--exclusive"] if exclusive else ["--priority", str(priority)] if priority else []), "--"]


def captured(fn, *args, **kwargs) -> Done:
    """Call a moved `main`-style function and return what it printed and its exit code, as the process it used to be."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = fn(*args, **kwargs)
        except SystemExit as stop:
            code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
            if not isinstance(stop.code, int) and stop.code is not None:
                err.write(f"{stop.code}\n")
    return Done(int(code or 0), out.getvalue(), err.getvalue())


def ship(number: str, reason: str = "", reviewed_head: str | None = None) -> Done:
    """The ship step of the PR route: guard, then queue (with a reason: queue as reviewed, pinned to `reviewed_head`)."""
    from . import ship as shipping

    return shipping.ship(str(number), reason or None, reviewed_head if reason else None)


def commit_meta(worktree, pr: str | None = None, ref: str = "") -> tuple[list[str], int]:
    """(problems, commits checked) of the CI meta preflight of the preflight area (`native.preflight.meta_of`) for
    the head in `worktree` (`ref`: a branch other than the checked-out one), or for an open PR: commit subjects,
    branch name, confidential terms; [] when clean. A line names where a text is ("commit 2 subject", "branch name"),
    never the text, so no subject, branch name or term is printed by this unit. It raises the preflight's refusal
    when the PR cannot be read or the terms could not be checked: an unchecked text is not a clean one."""
    from pathlib import Path

    from steuermaschine.native import runtime

    from .. import preflight

    number = int(pr) if pr is not None and str(pr).isdigit() else None
    found = preflight.meta_of(runtime(), Path(worktree), ref, pr=number, quote=False)
    problems = [str(p) for p in found.problems or []]
    if problems:
        return problems, int(found.commits or 0)
    from .public_identity import check

    return check(runtime(), Path(worktree), ref or str(found.branch))


def ci_meta(worktree, pr: str | None = None, ref: str = "") -> list[str]:
    """The problems of `commit_meta`: [] when clean."""
    return commit_meta(worktree, pr, ref)[0]


def texts_problems(worktree, title: str, body: str) -> list[str]:
    """Problems of a title and a body that are about to be published in a PR (`native.preflight.texts_check`): the
    commit-message rule for the title, the confidential terms for both. [] when both may be published. The lines
    name the rule and the fix, never a term. Raises the preflight's refusal when the texts could not be checked: an
    unchecked text is not a clean one."""
    from pathlib import Path

    from steuermaschine.native import runtime

    from .. import preflight

    return [str(p) for p in preflight.texts_check(runtime(), Path(worktree), title=title, body=body)]
