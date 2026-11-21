"""`go`: where is the project, and what is the ONE next command. Read-only by construction: it gets a store that
refuses writes.

The contract (text and --json carry the same fields):
    PHASE    where the most urgent thing stands
    NEXT     one command that can be run now, or "nothing runnable now"
    WAITING  who or what the project waits on, when NEXT cannot move it
    TRAP     what goes wrong at exactly this point
    ALSO     the other things in flight, one line each
    STATE    counts
"""

from __future__ import annotations

from typing import Any

from . import GO_SCHEMA, PROG
from .defaults import ReadOnlyStore
from .plugins import Alarm, GoItem

RANK = {"refused": 20, "dry_run": 25, "review": 30, "waiting": 40}
JOURNALS = "journals"  # one JSON lines file per pipeline: the runs that are history, one line each
HISTORY_DAYS = 2.0  # a run not updated for this many days is history: never a NEXT


def updated_at(state: dict[str, Any]) -> float | None:
    """When a run state last changed (its `updated` stamp), as epoch seconds; None when it has none."""
    import time

    try:
        return time.mktime(time.strptime(str(state.get("updated") or ""), "%Y-%m-%dT%H:%M:%S%z"))
    except ValueError:
        return None


def is_history(state: dict[str, Any], now: float, days: float) -> bool:
    """A finished run, or one not updated for `days`: history, which never produces a NEXT."""
    at = updated_at(state)
    return state.get("status") == "finished" or (at is not None and now - at > days * 86400)


def journal_history(store: Any, folder: Any, now: float, days: float) -> list[tuple[str, str]]:
    """Move every run that is history out of the run states into `folder/<pipeline>.jsonl` (append first, then delete).
    Returns (state, journal) per moved run."""
    import json

    moved = []
    for kind in sorted(k for k in _kinds(store) if k.startswith("runs.")):
        for key in store.keys(kind):
            state = store.load(kind, key)
            if not state or not is_history(state, now, days):
                continue
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{kind.removeprefix('runs.')}.jsonl"
            with target.open("a", encoding="utf-8") as out:
                out.write(json.dumps({"key": key, "journaled_at": now, "state": state}, sort_keys=True, default=str) + "\n")
            store.delete(kind, key)
            moved.append((f"{kind}/{key}", str(target)))
    return moved


def run_states(store: Any) -> list[dict[str, Any]]:
    out = []
    for kind in sorted(k for k in _kinds(store) if k.startswith("runs.")):
        for key in store.keys(kind):
            if state := store.load(kind, key):
                out.append(state)
    return out


def _kinds(store: Any) -> list[str]:
    inner = getattr(store, "_inner", store)
    if hasattr(inner, "kinds"):
        return list(inner.kinds())
    root = getattr(inner, "root", None)
    return [p.name for p in (root / "state").iterdir() if p.is_dir()] if root and (root / "state").is_dir() else []


def alarms(store: Any, plugin: Any, providers: Any, settings: Any, now: float, stall_minutes: float) -> list[dict[str, Any]]:
    found: list[Alarm] = []
    for s in run_states(store):
        since = s.get("waiting_since")
        if s.get("status") == "waiting" and since and (now - since) / 60 >= stall_minutes:
            found.append(Alarm(f"stall:{s['pipeline']}:{s['key'][:12]}", "alert",
                               f"{s.get('label')} has waited {(now - since) / 60:.0f} min: {s.get('waiting_on') or s.get('phase')}", s.get("next", "")))
    if plugin is not None:
        found += plugin.alarms(providers, settings)
    acks = store.load("alarms", "acks") or {}
    return [{"key": a.key, "level": a.level, "text": a.text, "next": a.next, "acked": a.key in acks} for a in found]


def compute(project: Any, store: Any, plugin: Any, providers: Any, settings: Any) -> dict[str, Any]:
    ro = ReadOnlyStore(store)
    now = providers.clock.now()
    items: list[GoItem] = []
    alarm_rows = alarms(ro, plugin, providers, settings, now, project.stall_minutes)
    for a in alarm_rows:
        if a["level"] == "alert" and not a["acked"]:
            items.append(GoItem(0, f"ALERT {a['key']}: {a['text']}", a["next"] or f"{PROG} alarm list", trap=f"Acknowledge once you act on it: {PROG} alarm ack {a['key']}"))
    focus = ro.load("focus", "list") or {}
    states = run_states(ro)
    counts: dict[str, int] = {}
    for s in states:
        counts[s.get("status", "?")] = counts.get(s.get("status", "?"), 0) + 1
        if is_history(s, now, HISTORY_DAYS):  # finished, or untouched for days: never a NEXT
            continue
        rank = RANK.get(str(s.get("status")), 50) - (15 if s.get("key") in focus or s.get("arg") in focus else 0)
        nxt = s.get("next", "") if s.get("status") != "waiting" else ""
        if s.get("status") == "review":
            nxt = f"give the reviewer brief to a different agent, then: {s.get('next', '')}"
        items.append(GoItem(rank, f"{s.get('label')}: {s.get('phase')}", nxt, s.get("waiting_on", ""), s.get("trap", "")))
    if plugin is not None:
        items += plugin.go_items(providers, settings)
    for key, f in focus.items():
        if not any(s.get("key") == key or s.get("arg") == key for s in states):
            items.append(GoItem(35, f"focus {key}{' for ' + f['for'] if f.get('for') else ''}: no run yet", f.get("next", "")))
    items.sort(key=lambda i: (i.rank, not i.next))
    runnable = [i for i in items if i.next]
    top = (runnable or items or [GoItem(90, "idle: nothing in flight", f"{PROG} head status")])[0]
    if top.rank > 0 and items and items[0].rank == 0:
        top = items[0]
    rest = [i for i in items if i is not top]
    report = {"schema_version": GO_SCHEMA, "project": project.name, "plugin": project.plugin, "phase": top.phase, "next": top.next, "waiting_on": top.waiting_on,
            "trap": top.trap, "also": [{"phase": i.phase, "next": i.next, "waiting_on": i.waiting_on} for i in rest],
            "alarms": alarm_rows, "state": {"runs": counts, "focus": sorted(focus)}, "read_only": True}
    if plugin is not None:
        plugin.go_report(report, providers, settings)  # layers may add ALSO items or set the trap
    return report


def render(report: dict[str, Any]) -> list[str]:
    out = [f"PHASE    {report['phase']}", f"NEXT     {report['next'] or 'nothing runnable now'}"]
    if report.get("waiting_on"):
        out.append(f"WAITING  {report['waiting_on']}")
    if report.get("trap"):
        out.append(f"TRAP     {report['trap']}")
    for n, item in enumerate(report.get("also", [])):
        out.append(f"{'ALSO    ' if n == 0 else '        '} {item['phase']}" + (f" -> {item['next']}" if item["next"] else ""))
    runs = ", ".join(f"{v} {k}" for k, v in sorted(report["state"]["runs"].items())) or "none"
    out.append(f"STATE    project {report['project']} (plugin {report['plugin']}); runs: {runs}; focus: {', '.join(report['state']['focus']) or 'none'}")
    out.append("         (read-only)")
    return out
