"""`sm self stats`: what the invocation log (usagelog.py) and the run lock's history say about the tool's own use.

Pure and deterministic: rows in, one report out; no model inside. The caller reads the two files and passes the
rows. Merges per day are left out: the merge receipts are a plugin's format, not something the core can read.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import Model

WINDOWS = {"1d": 1, "7d": 7, "30d": 30}
REPEAT = 2  # the same command refused for the same reason MORE than this many times is a repeat
STAMP = "%Y-%m-%dT%H:%M:%S%z"


class CommandStat(Model):
    command: str
    calls: int
    p50_ms: int
    p95_ms: int
    refused: int


class ReasonStat(Model):
    command: str
    reason: str
    count: int


class RerunStat(Model):
    command: str
    reruns: int  # calls on a tree that an earlier call of the same command had already failed on


class FlakyStat(Model):
    command: str
    tree: str
    exits: list[int]  # the distinct exit codes, one tree, one command


class GateStat(Model):
    runs: int
    wait_hours: float
    run_hours: float
    failure_rate: float


class Friction(Model):
    item: str
    minutes: float
    bead: str


class StatsReport(Model):
    """What `sm self stats` prints."""

    since: str
    calls: int
    commands: list[CommandStat]
    refusals: list[ReasonStat]
    repeated: list[ReasonStat]
    unchanged_reruns: list[RerunStat]
    flaky: list[FlakyStat]
    gate: GateStat
    merges_per_day: str
    friction: list[Friction]

    def render(self) -> list[str]:
        out = [f"window {self.since}: {self.calls} calls"]
        out += ["", "Calls per command (p50 / p95 ms, refused):"]
        out += [f"  {c.calls:>5}  {c.command:<28} {c.p50_ms:>6} / {c.p95_ms:<6} {c.refused or ''}" for c in self.commands] or ["  none"]
        out += ["", "Refusals by reason:"] + ([f"  {r.count:>5}  {r.command}: {r.reason}" for r in self.refusals] or ["  none"])
        out += ["", f"Repeated refusals (same command and reason, more than {REPEAT} times):"]
        out += [f"  {r.count:>5}  {r.command}: {r.reason}" for r in self.repeated] or ["  none"]
        out += ["", "Unchanged reruns (same command, same tree, after a failure):"]
        out += [f"  {r.reruns:>5}  {r.command}" for r in self.unchanged_reruns] or ["  none"]
        out += ["", "Flaky candidates (same command and tree, different exits):"]
        out += [f"  {f.command} on {f.tree[:12]}: exits {f.exits}" for f in self.flaky] or ["  none"]
        g = self.gate
        out += ["", f"Gate: {g.runs} runs, waited {g.wait_hours} h, ran {g.run_hours} h, failure rate {g.failure_rate:.0%}"]
        out += [f"Merges per day: {self.merges_per_day}", "", "Top friction (estimated minutes, suggested bead):"]
        out += [f"  {i}. {f.item} (~{f.minutes:g} min)\n     bead: {f.bead}" for i, f in enumerate(self.friction, 1)] or ["  none"]
        return out


def read_lines(path: Path) -> list[dict[str, Any]]:
    """The JSON objects of a JSONL file, the rotated generation first; a missing file or a broken line is skipped."""
    rows: list[dict[str, Any]] = []
    for name in (path.with_name(path.name + ".1"), path):
        try:
            text = name.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def stamp(row: dict[str, Any], key: str) -> datetime | None:
    try:
        return datetime.strptime(str(row[key]), STAMP)
    except (KeyError, ValueError):
        return None


def percentile(values: list[int], pct: int) -> int:
    """Nearest rank."""
    ordered = sorted(values)
    return ordered[max(math.ceil(pct / 100 * len(ordered)) - 1, 0)] if ordered else 0


def number(value: Any) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def build(invocations: list[dict[str, Any]], gate_rows: list[dict[str, Any]], since: str, now: datetime | None = None) -> StatsReport:
    cut = (now or datetime.now(UTC)) - timedelta(days=WINDOWS[since])
    calls = [r for r in invocations if (at := stamp(r, "started_at")) is not None and at >= cut]
    by_command: dict[str, list[dict[str, Any]]] = {}
    for row in calls:
        by_command.setdefault(str(row.get("command", "?")), []).append(row)
    refused = Counter((str(r.get("command", "?")), str(r.get("reason") or "unspecified")) for r in calls if r.get("exit") in (1, 75))
    commands = sorted((CommandStat(command=name, calls=len(rows), p50_ms=percentile(d := [int(number(r.get("duration_ms"))) for r in rows], 50),
                                   p95_ms=percentile(d, 95), refused=sum(1 for r in rows if r.get("exit") in (1, 75)))
                       for name, rows in by_command.items()), key=lambda c: (-c.calls, c.command))
    refusals = [ReasonStat(command=c, reason=why, count=n) for (c, why), n in sorted(refused.items(), key=lambda kv: (-kv[1], kv[0]))]
    repeated = [r for r in refusals if r.count > REPEAT]
    reruns, flaky = same_tree(calls)
    window = [r for r in gate_rows if (at := stamp(r, "at")) is not None and at >= cut]
    failed = sum(1 for r in window if str(r.get("exit")) != "0")
    gate = GateStat(runs=len(window), wait_hours=round(sum(number(r.get("waited_s")) for r in window) / 3600, 2),
                    run_hours=round(sum(number(r.get("ran_s")) for r in window) / 3600, 2), failure_rate=round(failed / len(window), 3) if window else 0.0)
    return StatsReport(since=since, calls=len(calls), commands=commands, refusals=refusals, repeated=repeated, unchanged_reruns=reruns, flaky=flaky, gate=gate,
                       merges_per_day="not available: the merge receipts are a plugin's format and the core cannot read them", friction=top_friction(commands, repeated, gate, reruns, flaky))


def same_tree(calls: list[dict[str, Any]]) -> tuple[list[RerunStat], list[FlakyStat]]:
    """Group the calls by (command, tree SHA); calls without a tree are left out."""
    groups: dict[tuple[str, str], list[int]] = {}
    for row in sorted(calls, key=lambda r: str(r.get("started_at"))):
        if row.get("tree") and isinstance(row.get("exit"), int):
            groups.setdefault((str(row.get("command", "?")), str(row["tree"])), []).append(row["exit"])
    reruns: Counter[str] = Counter()
    flaky = []
    for (command, tree), exits in groups.items():
        reruns[command] += sum(1 for i, code in enumerate(exits) if i and any(exits[:i]))
        if len(set(exits)) > 1:
            flaky.append(FlakyStat(command=command, tree=tree, exits=sorted(set(exits))))
    return ([RerunStat(command=c, reruns=n) for c, n in sorted(reruns.items(), key=lambda kv: (-kv[1], kv[0])) if n],
            sorted(flaky, key=lambda f: (f.command, f.tree)))


def top_friction(commands: list[CommandStat], repeated: list[ReasonStat], gate: GateStat, reruns: list[RerunStat], flaky: list[FlakyStat]) -> list[Friction]:
    """The three biggest items by estimated minutes lost: a repeated refusal or an unchanged rerun counts one minute each, a flaky candidate three, a command
    its calls times its p95, the gate its waiting time."""
    found = [Friction(item=f"`sm {r.command}` refused {r.count} times: {r.reason}", minutes=float(r.count),
                      bead=f"sm: stop the repeated refusal '{r.reason}' of `sm {r.command}` (fix the cause or route it in NEXT)") for r in repeated]
    found += [Friction(item=f"`sm {c.command}` is slow: p95 {c.p95_ms} ms over {c.calls} calls", minutes=round(c.calls * c.p95_ms / 60000, 1),
                       bead=f"sm: make `sm {c.command}` faster (p95 {c.p95_ms} ms)") for c in commands if c.p95_ms >= 1000]
    found += [Friction(item=f"`sm {r.command}` rerun {r.reruns} times on an unchanged tree after a failure", minutes=float(r.reruns),
                       bead=f"sm: `sm {r.command}` is rerun unchanged: say why it failed, or make the failure final") for r in reruns]
    found += [Friction(item=f"`sm {f.command}` gave exits {f.exits} on one tree ({f.tree[:12]})", minutes=3.0,
                       bead=f"sm: `sm {f.command}` is flaky: same tree, different exits") for f in flaky]
    if gate.wait_hours > 0:
        found.append(Friction(item=f"the gate made commands wait {gate.wait_hours} h in {gate.runs} runs", minutes=round(gate.wait_hours * 60, 1),
                              bead="sm: cut the gate wait (fewer heavy commands in parallel or a shorter hold)"))
    return sorted(found, key=lambda f: (-f.minutes, f.item))[:3]


# ----------------------------------------------------------------------------- trusted call counts (the deletion clock)
class CallCount(Model):
    command: str
    calls: int
    last: str = ""  # the last day with a call, YYYY-MM-DD
    hidden: bool = False


class CallsReport(Model):
    """`sm self stats --calls`: calls per command over the window, every command of the tree listed (zero too).
    Trusted only when the counts cover the whole window: a command with 0 calls in a trusted report is unused."""

    days: int
    since: str  # the first day of the window
    covered_from: str  # the first day the counts hold; "" when there are none
    trusted: bool
    calls: int
    commands: list[CallCount]

    def render(self) -> list[str]:
        head = (f"calls in the last {self.days} days (since {self.since}): {self.calls}; "
                + ("TRUSTED: the counts cover the whole window" if self.trusted else
                   f"NOT TRUSTED: the counts start {self.covered_from or 'nowhere'}, after the window starts"))
        return [head, *[f"  {c.calls:>6}  {c.command}{'  (hidden)' if c.hidden else ''}{'  last ' + c.last if c.last else ''}" for c in self.commands]]


def read_calls(folder: Path) -> list[tuple[str, str]]:
    """(day, command) of every counted call in the calls files of `folder`; broken lines are skipped."""
    out: list[tuple[str, str]] = []
    for path in sorted(folder.glob("*.tsv")) if folder.is_dir() else []:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and len(parts[0]) == 10:
                out.append((parts[0], parts[1]))
    return out


def calls(rows: list[tuple[str, str]], tree: dict[str, bool], days: int = 30, now: datetime | None = None) -> CallsReport:
    """`tree`: command path (without the program name) -> hidden; every one of them is listed."""
    since = ((now or datetime.now(UTC)) - timedelta(days=days)).strftime("%Y-%m-%d")
    covered = min((day for day, _ in rows), default="")
    window = [(day, command) for day, command in rows if day >= since]
    count = Counter(command for _, command in window)
    last: dict[str, str] = {}
    for day, command in window:
        last[command] = max(day, last.get(command, ""))
    names = sorted(set(tree) | set(count))
    found = [CallCount(command=n, calls=count.get(n, 0), last=last.get(n, ""), hidden=tree.get(n, False)) for n in names]
    return CallsReport(days=days, since=since, covered_from=covered, trusted=bool(covered) and covered <= since, calls=len(window),
                       commands=sorted(found, key=lambda c: (-c.calls, c.command)))
