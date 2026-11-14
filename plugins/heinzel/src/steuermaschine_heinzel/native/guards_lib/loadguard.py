"""The load guard: above STOP_LOAD it pauses (SIGSTOP, never kill) the newest pytest/coverage process tree that runs from a
worktree under ~/Code outside the run lock, one tree per tick; below RESUME_LOAD it continues (SIGCONT) the trees it
paused, one per tick, the earliest first.

Origin: P0 2026-11-07, load 94 and 149, live web down, from preflights run outside the gate in several worktrees at once.
It never signals the live service (no pytest under ~/Code), a run that has the run lock or a release tool among its
ancestors, another user's process, or a process it did not pause.

Moved from the former loadguard with its decisions unchanged (`plan`, `ungated_roots`, the tick). What changed is the
edge: the process table, the signals, the clock and the paused list come in through `Edge` (providers and the store), and
an ancestor is also gated when it is an sm process (`smprocs.Shapes`, the real process shapes, not the spelling `sm ...`) that
runs one of `GATED_PATHS`. Without established shapes (`shapes=None`) it pauses nothing; continuing what it paused goes on.

Review of daemon-loadguard.md: a paused tree whose root has exited (or whose root pid now runs something else) is not
just forgotten: its recorded children that still exist, belong to this user and are stopped get SIGCONT first (finding
2). A tree held paused longer than HELD_ALARM_SECONDS raises an ALARM line on every tick (finding 3).
"""

from __future__ import annotations

import re
import signal
from collections.abc import Callable
from dataclasses import dataclass, field

from steuermaschine.smprocs import ENTRY_SHAPES, Shapes

STOP_LOAD = 100.0
RESUME_LOAD = 40.0
TEST = re.compile(r"(^|[\s/])(pytest|py\.test|coverage)(\s|$)")
# old name kept: processes of the retired tools can still run on a machine; they keep their protection
GATED = re.compile(r"(^|[\s/])(hz-gate|hz-release|hz-train|hz-merge-train)(\s|$)|release_candidate")
GATED_PATHS = (("gate", "run"), ("release",), ("train",), ("daemon", "train"))  # sm command paths that hold the run lock or release
PS_FIELDS = "pid=,ppid=,uid=,stat=,etime=,command="
HELD_ALARM_SECONDS = 1800  # a tree paused this long means the load never fell: say so loudly


def parse_clock(text: str) -> float:
    """`[[dd-]hh:]mm:ss` (ps etime) in seconds."""
    days, _, text = text.rpartition("-")
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    return (int(days) if days else 0) * 86400 + seconds


def parse_ps(text: str) -> dict[int, dict]:
    procs = {}
    for line in text.splitlines():
        f = line.split(None, 5)
        if len(f) == 6 and f[0].isdigit():
            procs[int(f[0])] = {"pid": int(f[0]), "ppid": int(f[1]), "uid": int(f[2]), "stat": f[3], "age_s": parse_clock(f[4]), "cmd": f[5].strip()}
    return procs


def ancestors(procs: dict[int, dict], pid: int):
    seen = set()
    pid = procs[pid]["ppid"] if pid in procs else 0
    while pid in procs and pid not in seen and pid > 1:
        seen.add(pid)
        yield procs[pid]
        pid = procs[pid]["ppid"]


def descendants(procs: dict[int, dict], root: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for p in procs.values():
        children.setdefault(p["ppid"], []).append(p["pid"])
    found, todo = [], [root]
    while todo:
        for child in children.get(todo.pop(), []):
            if child not in found and child != root:
                found.append(child)
                todo.append(child)
    return found


def gated(cmd: str, shapes: Shapes = ENTRY_SHAPES) -> bool:
    return bool(GATED.search(cmd)) or shapes.runs(cmd, GATED_PATHS)


def ungated_roots(procs: dict[int, dict], home: str, uid: int, shapes: Shapes = ENTRY_SHAPES) -> list[dict]:
    """The topmost pytest/coverage processes of this user that run from a worktree under ~/Code with no hz-gate (or
    release tool) above them, newest first."""
    worktree = re.compile(re.escape(home.rstrip("/")) + r"/Code/[^/\s]+/")

    def test(p: dict) -> bool:
        return p["uid"] == uid and bool(TEST.search(p["cmd"])) and bool(worktree.search(p["cmd"]))

    roots = [p for p in procs.values() if test(p) and not gated(p["cmd"], shapes) and not any(test(a) for a in ancestors(procs, p["pid"]))
             and not any(gated(a["cmd"], shapes) for a in ancestors(procs, p["pid"]))]
    return sorted(roots, key=lambda p: p["age_s"])


def plan(procs: dict[int, dict], stopped: list[dict], load: float, home: str, uid: int,
         shapes: Shapes | None = ENTRY_SHAPES) -> tuple[str, dict | None, list[int]]:
    """("stop", root, pids) | ("resume", entry, pids) | ("none", None, []): at most one tree per tick. `shapes` None: the sm
    process shapes could not be established, so nothing is paused (fail closed)."""
    if load > STOP_LOAD and shapes is not None:
        paused = {entry["pid"] for entry in stopped}
        for root in ungated_roots(procs, home, uid, shapes):
            if root["pid"] not in paused and "T" not in root["stat"]:
                return "stop", root, [root["pid"], *descendants(procs, root["pid"])]
    if load < RESUME_LOAD and stopped:
        entry = stopped[0]
        alive = [pid for pid in entry["pids"] if pid in procs and procs[pid]["uid"] == uid]
        return "resume", entry, alive
    return "none", None, []


def orphaned_children(procs: dict[int, dict], entry: dict, uid: int) -> list[int]:
    """The recorded children (not the root) of a paused tree that still exist, are this user's and are stopped (`T`):
    the ones that would stay SIGSTOPped forever once the root is gone. A reused pid that runs is not stopped: left alone."""
    return [pid for pid in entry.get("pids", [])[1:] if pid in procs and procs[pid]["uid"] == uid and "T" in procs[pid]["stat"]]


@dataclass
class Edge:
    """How `tick` reaches the world: a recorder in a test, the providers in the command."""

    kill: Callable[[int, int], object]
    now: Callable[[], float]
    load_state: Callable[[], list[dict]]
    save_state: Callable[[list[dict]], None]
    log: Callable[[str], None] = field(default=lambda line: None)


def tick(ps_text: str, load: float, edge: Edge, home: str, uid: int, shapes: Shapes | None = ENTRY_SHAPES) -> str:
    procs = parse_ps(ps_text)
    loaded = edge.load_state()
    # A paused root that is gone, or whose pid now runs something else, needs no continue; its children might.
    stopped = [e for e in loaded if e["pid"] in procs and procs[e["pid"]]["cmd"] == e["cmd"]]
    for entry in (e for e in loaded if e not in stopped):
        orphans = orphaned_children(procs, entry, uid)
        for pid in reversed(orphans):
            edge.kill(pid, signal.SIGCONT)
        if orphans:
            edge.log(f"SIGCONT {len(orphans)} paused child process(es) of test tree {entry['pid']}, whose root is gone: {orphans}")
    for entry in stopped:
        held = edge.now() - float(entry.get("at") or edge.now())
        if held > HELD_ALARM_SECONDS:
            edge.log(f"ALARM: test tree {entry['pid']} paused for {held / 60:.0f} min (load never fell below {RESUME_LOAD:.0f}); "
                     f"continue it by hand with kill -CONT {' '.join(str(p) for p in entry['pids'])}")
    action, subject, pids = plan(procs, stopped, load, home, uid, shapes)
    if action == "stop":
        for pid in pids:  # the root first, so it cannot start new workers while its children are being paused
            edge.kill(pid, signal.SIGSTOP)
        stopped.append({"pid": subject["pid"], "cmd": subject["cmd"], "pids": pids, "at": edge.now()})
        edge.log(f"load {load:.0f} > {STOP_LOAD:.0f}: SIGSTOP ungated test tree {subject['pid']} ({len(pids)} processes): {subject['cmd'][:200]}")
    elif action == "resume":
        for pid in reversed(pids):
            edge.kill(pid, signal.SIGCONT)
        stopped = [e for e in stopped if e["pid"] != subject["pid"]]
        edge.log(f"load {load:.0f} < {RESUME_LOAD:.0f}: SIGCONT test tree {subject['pid']} ({len(pids)} processes) after {edge.now() - subject['at']:.0f}s")
    edge.save_state(stopped)
    return f"{action} {subject['pid'] if subject else ''}".strip()
