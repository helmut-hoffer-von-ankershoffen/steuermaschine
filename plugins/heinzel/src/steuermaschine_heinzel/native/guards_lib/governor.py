"""The agent load governor: above HIGH (1-minute load) stop builder processes in descending CPU order (SIGSTOP); below LOW
continue them, 6 per 20 s (SIGCONT).

Origin: bead 'Mini overload x3'. Only processes whose args/cwd sit under builder worktrees are eligible; the live
service, Claude.app, Chrome and the governor itself are never touched.

Moved from the former governor with its decisions unchanged (`eligible`, `pick`, `step`, `resume`). What changed is the
edge: the process table and cwd come from `ps` and `lsof` through the Commands provider, signals are the caller's `kill`,
the state is a dict the caller keeps in the Store (`legacy_state` reads the former file, read-only), and the thresholds
are bound from settings. Never eligible either: any sm process (`smprocs.Shapes`: the real process shapes, the stable install
root), whatever it runs. `pick` with `shapes=None` (the shapes could not be established) stops nothing (fail closed).

Allowlist (review of daemon-governor.md, finding 1): only TEST processes agents started are eligible: a test runner
(`TEST`: pytest, vitest, playwright, jest, coverage run) whose args or cwd sit under a builder worktree (`BUILDER`),
and the descendants of such a runner (xdist workers, vitest forks). The coordinator's checkout
(~/Code/heinzel-management outside its `.worktrees/`) is never a builder worktree, and a build tool that is not a
test runner (git, uv, make, node, python) is never stopped by itself: the governor once matched the coordinator's
own commands by their cwd.

Findings 3 and 4 of the same review: a recorded pid whose process now runs something else (its args differ from the
recorded ones) is dropped; a pid held stopped longer than HELD_ALARM_SECONDS raises an ALARM line on every tick and in
the digest line.

Bead ltui: the args alone cannot tell a reused pid from the process the governor stopped (`ps` may show it otherwise),
and dropping a pid it stopped without SIGCONT could leave it stopped for good. So before a changed pid is dropped:
when it still exists with the recorded uid (the governor only ever stops processes of its own uid), it is continued
(SIGCONT is a no-op for a process that runs); with another uid it is not signalled and an ALARM line names it.
"""

from __future__ import annotations

import os
import re
import signal
from collections.abc import Callable
from typing import Any

from steuermaschine.smprocs import ENTRY_SHAPES, Shapes

HIGH = 45.0
LOW = 25.0
RESUME_BATCH, RESUME_EVERY, STOP_BATCH, INTERVAL = 6, 20, 6, 15
HELD_ALARM_SECONDS = 1800
ARGS_KEPT = 120  # the recorded args: the first characters, compared against the live process
PS_ARGS = ["ps", "-axo", "pid=,ppid=,pcpu=,uid=,args="]


def bind(high: float, low: float) -> None:
    """Set the thresholds (settings `guards_governor_high` / `guards_governor_low`)."""
    global HIGH, LOW
    HIGH, LOW = high, low


BUILDER = re.compile(r"(/Code/hz-[^\s/]*|/Code/heinzel-(?!management(?:[\s/]|$))[^\s/]*|pytest-of-[^/\s]+|/Code/heinzel-management/\.worktrees/)")
TEST = re.compile(r"(^|[\s/])(pytest|py\.test|vitest|playwright|jest)([\s./-]|$)|(^|\s)-m\s+pytest(\s|$)|(^|[\s/])coverage\s+run(\s|$)")
NEVER = re.compile(
    # old name kept: processes of the retired tools can still run on a machine; they keep their protection
    r"(hz-queue|hz-train|hz-release|hz-tick|hz-ask|uv/tools/heinzel/|\.local/share/uv/tools/heinzel|8737|"
    # old name kept: processes of the retired tools can still run on a machine; they keep their protection
    r"io\.github\.helmut-hoffer-von-ankershoffen\.heinzel|Claude\.app|/Claude|Google Chrome|Chrome Helper|hz-governor|hz_governor|hz serve|hz daemon|"
    r"(^|[\s/])sm\s+(daemon|queue|release|train|go|gate|mini)(\s|$))")


def ps_table(text: str) -> list[dict]:
    """`ps -axo pid=,ppid=,pcpu=,uid=,args=` as rows."""
    rows = []
    for l in text.splitlines():  # noqa: E741
        p = l.split(None, 4)
        if len(p) == 5:
            try:  # noqa: SIM105
                rows.append({"pid": int(p[0]), "ppid": int(p[1]), "cpu": float(p[2]), "uid": int(p[3]), "args": p[4]})
            except ValueError:
                pass
    return rows


def cwd_of(text: str) -> str:
    """The working directory in `lsof -a -d cwd -Fn -p PID` output."""
    for l in text.splitlines():  # noqa: E741
        if l.startswith("n"):
            return l[1:]
    return ""


def protected(row, me, shapes: Shapes = ENTRY_SHAPES) -> bool:
    """Never stopped: this process and its parent, another user's, the NEVER list, any sm process."""
    return bool(row["pid"] in (me, os.getppid()) or row["uid"] != os.getuid() or NEVER.search(row["args"]) or shapes.is_sm(row["args"]))


def eligible(row, me, cwd=lambda p: "", shapes: Shapes = ENTRY_SHAPES, child_of_test: bool = False):
    """A test runner of a builder worktree (args or cwd), or (`child_of_test`) a descendant of one."""
    if protected(row, me, shapes):
        return False
    if child_of_test:
        return True
    if not TEST.search(row["args"]):
        return False
    if BUILDER.search(row["args"]):
        return True
    c = cwd(row["pid"])
    return bool(c and BUILDER.search(c) and not NEVER.search(c))


def descendants(rows, roots: set[int]) -> set[int]:
    """Every pid below one of `roots` in the table (by ppid)."""
    kids: dict[int, list[int]] = {}
    for r in rows:
        kids.setdefault(r["ppid"], []).append(r["pid"])
    found: set[int] = set()
    todo = list(roots)
    while todo:
        for pid in kids.get(todo.pop(), []):
            if pid not in found and pid not in roots:
                found.add(pid)
                todo.append(pid)
    return found


def pick(rows, already, n=STOP_BATCH, me=None, cwd=lambda p: "", shapes: Shapes | None = ENTRY_SHAPES):
    if shapes is None:
        return []
    me = me or os.getpid()
    roots = {r["pid"] for r in rows if eligible(r, me, cwd, shapes)}
    below = descendants(rows, roots)
    cand = [r for r in sorted(rows, key=lambda r: -r["cpu"]) if r["pid"] not in already and r["cpu"] > 0.5
            and (r["pid"] in roots or (r["pid"] in below and eligible(r, me, cwd, shapes, child_of_test=True)))]
    return cand[:n]


def fresh_state() -> dict:
    return {"stopped": {}, "last_resume": 0}


def alive(pid, rows):
    return any(r["pid"] == pid for r in rows)


def same_process(pid, entry, rows):
    """The pid still runs the process that was stopped: alive with the recorded args (a reused pid has other args)."""
    return any(r["pid"] == pid and r["args"][:ARGS_KEPT] == entry.get("args") for r in rows)


def release_changed(pid: int, entry: dict[str, Any], rows: list[dict[str, Any]], kill: Callable[[int, int], object],
                    acts: list[tuple[str, int]], dry: bool, logf: Callable[[str], object]) -> None:
    """A recorded pid that is alive but runs other args: continue it when its uid is the recorded one (an entry from
    before the uid was recorded: the governor's own uid, the only one it stops); otherwise only an ALARM line."""
    row = next((r for r in rows if r["pid"] == pid), None)
    if row is None:
        return  # gone: nothing is left stopped
    if row["uid"] != entry.get("uid", os.getuid()):
        logf(f"ALARM: pid {pid} was stopped by the governor and now runs another uid's process; dropped, not continued")
        return
    acts.append(("CONT", pid))
    logf(f"{'DRY ' if dry else ''}CONT {pid} args changed (pid reused or retitled): continued before dropping")
    if not dry:
        try:
            kill(pid, signal.SIGCONT)
        except (ProcessLookupError, PermissionError):
            logf(f"ALARM: pid {pid} could not be continued; check it with ps and kill -CONT {pid}")


def held_too_long(st: dict, now: float) -> list[tuple[int, float]]:
    """(pid, seconds) of every pid held stopped longer than HELD_ALARM_SECONDS, longest first."""
    held = [(int(k), now - float(v.get("t") or now)) for k, v in st.get("stopped", {}).items()]
    return sorted(((pid, s) for pid, s in held if s > HELD_ALARM_SECONDS), key=lambda x: -x[1])


def step(load: float, st: dict[str, Any], rows: list[dict[str, Any]], kill: Callable[[int, int], object], now: float, dry: bool = False,
         cwd: Callable[[int], str] = lambda p: "", off: bool = False, logf: Callable[[str], object] = lambda msg: None,
         shapes: Shapes | None = ENTRY_SHAPES) -> list[tuple[str, int]]:
    """One governor decision. Returns the list of actions. Mutates st."""
    acts: list[tuple[str, int]] = []
    for k, v in list(st["stopped"].items()):  # drop dead and changed pids; a changed one of the recorded uid is continued first
        if not same_process(int(k), v, rows):
            del st["stopped"][k]
            release_changed(int(k), v, rows, kill, acts, dry, logf)
    for pid, seconds in held_too_long(st, now):
        logf(f"ALARM: pid {pid} held stopped for {seconds / 60:.0f} min (load never fell below {LOW:g}); continue it with the off file or kill -CONT {pid}")
    if off:
        if st["stopped"]:
            return resume(st, kill, 10**6, acts, dry, logf, why="kill-switch")
        return acts
    if load > HIGH:
        for r in pick(rows, {int(k) for k in st["stopped"]}, cwd=cwd, shapes=shapes):
            acts.append(("STOP", r["pid"]))
            logf(f"{'DRY ' if dry else ''}STOP {r['pid']} cpu={r['cpu']} load={load:.0f} {r['args'][:80]}")
            if not dry:
                kill(r["pid"], signal.SIGSTOP)
                st["stopped"][str(r["pid"])] = {"t": now, "args": r["args"][:ARGS_KEPT], "uid": r["uid"]}
    elif load < LOW and st["stopped"] and now - st.get("last_resume", 0) >= RESUME_EVERY:
        resume(st, kill, RESUME_BATCH, acts, dry, logf, why=f"load={load:.0f}")
        if not dry:
            st["last_resume"] = now
    return acts


def resume(st, kill, n, acts, dry, logf, why=""):
    for k in sorted(st["stopped"], key=lambda k: st["stopped"][k]["t"])[:n]:
        acts.append(("CONT", int(k)))
        logf(f"{'DRY ' if dry else ''}CONT {k} {why}")
        if not dry:
            try:  # noqa: SIM105
                kill(int(k), signal.SIGCONT)
            except ProcessLookupError:
                pass
            del st["stopped"][k]
    return acts


def digest_line(st: dict, load: float, now: float | None = None) -> str:
    n = len(st.get("stopped", {}))
    long = held_too_long(st, now) if now is not None else []
    alarm = f"; ALARM {len(long)} held > {HELD_ALARM_SECONDS // 60} min (longest pid {long[0][0]}, {long[0][1] / 60:.0f} min)" if long else ""
    return f"GOV: stopped {n} pids, load {load:.0f}{alarm}" if (n or load > HIGH) else ""
