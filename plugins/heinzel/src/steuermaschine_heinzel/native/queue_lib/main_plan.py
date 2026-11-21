"""The merge rule for a Core PR, computed by sm alone (no plan registry, no reader of Core's).

A Core PR merges only when all of these hold for the exact PINNED head:
1. the required checks (the CI aggregate and the merge-gate checks, read from Core's release policy at main) are green on it;
2. main's tip is an ancestor of the head: the tree CI tested is the tree the squash produces, so a head BEHIND main is
   updated and tested again, never merged;
3. when the head changes a protected path against main (`native/protected.py`: `.github/`, the release, publish, PyPI and
   acceptance scripts, the gate scripts, Makefile, uv.lock, the coverage and pytest config, quarantine.toml, conftest.py,
   Core's own execution inputs), a protected-review verdict for exactly this head exists, recorded by a different agent
   (`sm review verdict`, the digest `sm head admit` stops at).

`queue add` does not evaluate the rule: it queues, and the merge-time guard decides. Fail closed: any unreadable fact is a
refusal that names it.
"""
# mypy: disable-error-code="no-untyped-call"
from __future__ import annotations

import importlib
from pathlib import Path

from . import hzlib

SETTING = "queue_core_repo"  # kept: the setting still exists, nothing reads a Core checkout for this rule any more
PREFIX = "merge rule"
_core: list[Path] = []


def configure(core_repo: Path | str | None) -> None:
    """Remember the configured Core checkout (setting `queue_core_repo`); the rule itself reads GitHub, not the checkout."""
    _core.clear()
    if core_repo:
        _core.append(Path(core_repo).expanduser())


def core_repo() -> Path | None:
    return _core[0] if _core else None


def _raw(path: str, ref: str) -> str | None:
    """One file of Core at REF as text, or None when it is not there."""
    code, out = hzlib.gh("api", "-H", "Accept: application/vnd.github.raw", f"repos/{hzlib.CORE}/contents/{path}?ref={ref}")
    return out if code == 0 else None


def _changed(pr: str) -> list[str] | None:
    """The file names the PR changes (GitHub's own list, every page); None when it cannot be read."""
    code, out = hzlib.gh("api", "--paginate", f"repos/{hzlib.CORE}/pulls/{pr}/files?per_page=100", "--jq", ".[].filename")
    return [line for line in out.splitlines() if line] if code == 0 else None


def _verdict(head: str, paths: list[str]) -> str:
    """'' when a different agent approved exactly this head's protected paths, else the refusal text."""
    from steuermaschine.engine import Refuse

    protected = importlib.import_module("steuermaschine_heinzel.native.protected")
    digest, _record = protected.record(head, paths)
    try:
        native = importlib.import_module("steuermaschine.native")
        verdicts = importlib.import_module("steuermaschine.verdicts")
        go = importlib.import_module("steuermaschine.go")
        store = native.runtime().providers.store
        verdicts.require_independent(store, digest, go.run_states(store), f"sm review verdict {digest} --approve --reviewer NAME --model M")
    except Refuse as refused:
        return f"{refused.message}: changed protected paths {', '.join(paths[:6])}"
    except Exception as error:  # noqa: BLE001 - fail closed: no readable store, no verdict
        return f"cannot read the protected-review verdicts ({error}): changed protected paths {', '.join(paths[:6])}"
    return ""


def gap(pr: str, head: str = "") -> str:
    """'' when the merge rule holds for the pinned head of PR, else the refusal reason (always starting with PREFIX)."""
    try:
        info = hzlib.ghj("pr", "view", pr, "-R", hzlib.CORE, "--json", "headRefOid,statusCheckRollup", default={}) or {}
        current = info.get("headRefOid", "") if isinstance(info, dict) else ""
        head = head or current
        ref = hzlib.ghj("api", f"repos/{hzlib.CORE}/git/ref/heads/main", default={}) or {}
        main = (ref.get("object") or {}).get("sha", "") if isinstance(ref, dict) else ""
        if len(head) != 40 or len(main) != 40:
            return f"{PREFIX}: cannot check #{pr}: its head or main is unreadable"
        if current != head:
            return f"{PREFIX}: #{pr} is at {current[:8] or '?'}, not the pinned head {head[:8]}: ship it again"
        facts = importlib.import_module("steuermaschine_heinzel.native.queue_lib.core_facts")
        rollup = hzlib.latest_checks(info.get("statusCheckRollup") or [])
        green = {c.get("name") or c.get("context") for c in rollup if c.get("conclusion") == "SUCCESS" or c.get("state") == "SUCCESS"}
        if missing := [name for name in facts.contexts() if name not in green]:
            return f"{PREFIX}: #{pr} ({head[:8]}): required checks not green: {', '.join(missing)}"
        compare = hzlib.ghj("api", f"repos/{hzlib.CORE}/compare/{main}...{head}", default={}) or {}
        if compare.get("status") not in ("ahead", "identical"):
            return (f"{PREFIX}: #{pr} ({head[:8]}) is {compare.get('status') or 'unreadable'} against main {main[:8]}: "
                    "update the branch and let CI test it again")
        names = _changed(pr)
        if names is None:
            return f"{PREFIX}: cannot read the files #{pr} changes"
        protected = importlib.import_module("steuermaschine_heinzel.native.protected")
        found = protected.matches(names, protected.execution_inputs(_raw("scripts/focused_plan.py", main)))
        if "pyproject.toml" in names and protected.pyproject_weakens(_raw("pyproject.toml", main), _raw("pyproject.toml", head)):
            found.add("pyproject.toml")
    except Exception as error:  # noqa: BLE001 - fail closed: every error is a named refusal
        return f"{PREFIX}: cannot check #{pr}: {error}"
    if found and (reason := _verdict(head, sorted(found))):
        return f"{PREFIX}: #{pr} ({head[:8]}): {reason}"
    return ""
