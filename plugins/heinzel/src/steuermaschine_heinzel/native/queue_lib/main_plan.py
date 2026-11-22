"""The merge rule for a Core PR, computed by sm alone (no plan registry, no reader of Core's).

A Core PR merges only when all of these hold for the exact PINNED head:
1. the status checks main's ruleset requires (read from GitHub, `rules/branches/main`) are green on it; an unreadable or empty ruleset refuses;
2. main's tip is an ancestor of the head: the tree CI tested is the tree the squash produces, so a head BEHIND main is
   updated and tested again, never merged;
3. when the head changes a protected path against main (`native/protected.py`: `.github/`, the release, publish, PyPI and
   acceptance scripts, the gate scripts, Makefile, uv.lock, the coverage and pytest config, quarantine.toml, conftest.py,
   Core's own execution inputs), a protected-review verdict exists, recorded by a different agent
   of exactly those files' content (the digest `sm head admit` stops at, `sm review verdict`): an update of the branch that leaves them alone keeps it.

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


def _raw(path: str, ref: str) -> tuple[str | None, bool]:
    """(text, absent) of one file of Core at REF: the text when it is there, absent=True on GitHub's not-found, else (None, False)."""
    code, out = hzlib.gh("api", "-H", "Accept: application/vnd.github.raw", f"repos/{hzlib.CORE}/contents/{path}?ref={ref}")
    if code == 0:
        return out, False
    return None, '"status":"404"' in out.replace(" ", "") or "Not Found" in out


def _changed(pr: str) -> dict[str, str] | None:
    """The files the PR changes, each with the id of its content at the head (`removed` when deleted): GitHub's own list, every
    page. None when it cannot be read."""
    code, out = hzlib.gh("api", "--paginate", f"repos/{hzlib.CORE}/pulls/{pr}/files?per_page=100", "--jq",
                         '.[] | [.filename, .sha, .status] | @tsv')
    if code != 0:
        return None
    found = {}
    for line in out.splitlines():
        name, _, rest = line.partition("\t")
        blob, _, status = rest.partition("\t")
        if name:
            found[name] = "removed" if status == "removed" else blob
    return found


def required_checks() -> set[str] | None:
    """The status checks main's ruleset requires (what GitHub enforces), or None
    when the ruleset cannot be read or requires none: the rule then refuses."""
    rules = hzlib.ghj("api", f"repos/{hzlib.CORE}/rules/branches/main")
    if not isinstance(rules, list):
        return None
    try:
        found = {c["context"] for rule in rules if rule.get("type") == "required_status_checks" for c in rule["parameters"]["required_status_checks"]}
    except (KeyError, TypeError, AttributeError):
        return None
    return found or None


def _verdict(head: str, blobs: dict[str, str]) -> str:
    """'' when a different agent approved exactly the content of these protected files, else the refusal text."""
    from steuermaschine.engine import Refuse

    protected = importlib.import_module("steuermaschine_heinzel.native.protected")
    digest, _record = protected.record(blobs)
    paths = sorted(blobs)
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


GATE_REVIEW = "gate review"


def gap(pr: str, head: str = "", gate_in_ruleset: bool = False) -> str:
    """'' when the merge rule holds for the pinned head of PR, else the refusal reason (always starting with PREFIX).
    GATE_IN_RULESET: a ruleset that requires the Gate review check carries the protected-path review itself, so the
    recorded-verdict rule (3) applies only where the ruleset does not require it (the arm-time rule of auto-merge)."""
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
        required = required_checks()
        if required is None:
            return f"{PREFIX}: cannot read the required status checks of main's ruleset (unreadable or none): nothing is merged on a guess"
        rollup = hzlib.latest_checks(info.get("statusCheckRollup") or [])
        green = {c.get("name") or c.get("context") for c in rollup if c.get("conclusion") == "SUCCESS" or c.get("state") == "SUCCESS"}
        if missing := sorted(required - green):
            return f"{PREFIX}: #{pr} ({head[:8]}): required checks not green: {', '.join(missing)}"
        compare = hzlib.ghj("api", f"repos/{hzlib.CORE}/compare/{main}...{head}", default={}) or {}
        if compare.get("status") not in ("ahead", "identical"):
            return (f"{PREFIX}: #{pr} ({head[:8]}) is {compare.get('status') or 'unreadable'} against main {main[:8]}: "
                    "update the branch and let CI test it again")
        if gate_in_ruleset and any(GATE_REVIEW in str(name).lower() for name in required):
            return ""
        changed = _changed(pr)
        if changed is None:
            return f"{PREFIX}: cannot read the files #{pr} changes"
        protected = importlib.import_module("steuermaschine_heinzel.native.protected")
        plan, absent = _raw("scripts/focused_plan.py", main)
        if plan is None and not absent:
            return f"{PREFIX}: cannot read Core's scripts/focused_plan.py at main {main[:8]}: its protected files are unknown"
        found = protected.matches(list(changed), protected.execution_inputs(plan))
        if "pyproject.toml" in changed and protected.pyproject_weakens(_raw("pyproject.toml", main)[0], _raw("pyproject.toml", head)[0]):
            found.add("pyproject.toml")
    except Exception as error:  # noqa: BLE001 - fail closed: every error is a named refusal
        return f"{PREFIX}: cannot check #{pr}: {error}"
    if found and (reason := _verdict(head, {name: changed[name] for name in found})):
        return f"{PREFIX}: #{pr} ({head[:8]}): {reason}"
    return ""
