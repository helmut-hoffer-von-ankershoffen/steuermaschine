"""The list of deferred runs: PR CI runs that were cancelled to free hosted runners and wait for their re-run.

One document in the store (kind `heinzel.ci_ops.deferred`, key `runs`): `{"runs": [{"id", "branch", "workflow"}]}`,
in the order they were cancelled. Until this area has written it once, the former list file is read instead
(`legacy.deferred_runs`). Every cancel attempt is also one line in the audit log (kind `heinzel.ci_ops.cancel_log`,
one document per day).

Other areas (the merge queue, the release train) read the list through `ids` and `runs`, and replace it with `save`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from steuermaschine.protocols import Clock, Store

from . import legacy

KIND = "heinzel.ci_ops.deferred"
KEY = "runs"
AUDIT = "heinzel.ci_ops.cancel_log"
LOCK = "heinzel.ci_ops.deferred"


def runs(store: Store, legacy_file: Path | None = None) -> list[dict[str, Any]]:
    """The deferred runs, oldest first."""
    kept = store.load(KIND, KEY)
    if kept is None:
        return legacy.deferred_runs(legacy_file)
    return [dict(row) for row in kept.get("runs", [])]


def ids(store: Store, legacy_file: Path | None = None) -> set[str]:
    """The run ids on the list: a cancelled check of one of them is a deferral, not a failure."""
    return {str(row["id"]) for row in runs(store, legacy_file)}


def save(store: Store, rows: list[dict[str, Any]]) -> None:
    store.save(KIND, KEY, {"runs": [{"id": str(r["id"]), "branch": r["branch"], "workflow": r.get("workflow", "")} for r in rows]})


def add(store: Store, legacy_file: Path | None, run_id: str, branch: str, workflow: str) -> None:
    """Record one cancelled run at once, so a deferral that stops half way has recorded what it cancelled."""
    save(store, [*runs(store, legacy_file), {"id": str(run_id), "branch": branch, "workflow": workflow}])


def text(rows: list[dict[str, Any]]) -> str:
    """The list as lines `id branch workflow name`: the form the re-run plan parses and `sm ci deferred` prints."""
    return "".join(f"{r['id']} {r['branch']} {r.get('workflow', '')}".rstrip() + "\n" for r in rows)


def stamp(clock: Clock) -> str:
    return datetime.fromtimestamp(clock.now(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit(store: Store, clock: Clock, line: str) -> None:
    """One dated line in the audit log of cancel attempts."""
    when = stamp(clock)
    page = store.load(AUDIT, when[:10]) or {"lines": []}
    page["lines"].append(f"{when} {line}")
    store.save(AUDIT, when[:10], page)


def audit_lines(store: Store) -> list[str]:
    return [line for day in store.keys(AUDIT) for line in (store.load(AUDIT, day) or {}).get("lines", [])]
