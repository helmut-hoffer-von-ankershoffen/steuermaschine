"""Extra or stale `hz ui serve` processes on the Mini. The owned backend is the one process that listens on the live
port (supervised by launchd); every other `ui serve` is a leftover that holds a port, memory and an old version.
Read-only: it lists them with pid, age and version, it never signals one."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from steuermaschine.localtime import duration

SERVE = re.compile(r"(?:^|[\s/])(?:hz|heinzel)\s+ui\s+serve\b|\bheinzel\b.*\bui\s+serve\b")
VERSION_IN = re.compile(r"(?<![0-9.])v?(\d+\.\d+\.\d+)(?![0-9.])")
ETIME = re.compile(r"^(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)$")


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    age: int  # seconds
    command: str

    @property
    def version(self) -> str:
        found = VERSION_IN.search(self.command)
        return found.group(1) if found else ""


def etime(text: str) -> int | None:
    """`ps` elapsed time `[[dd-]hh:]mm:ss` in seconds."""
    found = ETIME.match(text.strip())
    if not found:
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in found.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def parse_ps(text: str) -> list[Proc]:
    """From `ps -axo pid=,ppid=,etime=,command=`."""
    out = []
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit() and (age := etime(parts[2])) is not None:
            out.append(Proc(int(parts[0]), int(parts[1]), age, parts[3]))
    return out


def listener_pids(lsof_out: str) -> list[int]:
    """From `lsof -Fp`: the process ids."""
    return [int(line[1:]) for line in lsof_out.splitlines() if line.startswith("p") and line[1:].isdigit()]


def related(procs: list[Proc], owned: set[int]) -> set[int]:
    """The owned pids, their ancestors (a launcher that execs the server) and their descendants (workers)."""
    by_pid = {p.pid: p for p in procs}
    keep = set(owned)
    for pid in owned:
        parent = by_pid[pid].ppid if pid in by_pid else 0
        while parent > 1 and parent in by_pid and parent not in keep:
            keep.add(parent)
            parent = by_pid[parent].ppid
    grew = True
    while grew:
        grew = False
        for p in procs:
            if p.ppid in keep and p.pid not in keep:
                keep.add(p.pid)
                grew = True
    return keep


def extra(procs: list[Proc], owned: set[int], me: int = 0) -> list[Proc]:
    mine = related(procs, owned)
    return [p for p in procs if SERVE.search(p.command) and p.pid not in mine and p.pid != me]


def warnings(commands: Any, port: int, installed: str, me: int = 0, env: dict[str, str] | None = None) -> list[str]:
    """One line per extra or stale serve process; a line that says so when the processes could not be read."""
    ps = commands.run(["ps", "-axo", "pid=,ppid=,etime=,command="], timeout=20, env=env)
    if ps.rc:
        return [f"serve processes unread (ps exited {ps.rc}): extra `hz ui serve` processes are unchecked"]
    lsof = commands.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"], timeout=20, env=env)
    owned = set(listener_pids(lsof.out)) if lsof.rc == 0 else set()
    procs = parse_ps(ps.out)
    found = extra(procs, owned, me)
    if not owned and found:
        return [f"serve processes: nobody listens on port {port}, so the owned backend is unknown; {len(found)} `ui serve` running: "
                + ", ".join(f"pid {p.pid}" for p in found)]
    out = []
    for p in sorted(found, key=lambda p: p.pid):
        version = p.version
        stale = bool(version and installed and version != installed)
        out.append(f"extra `ui serve` pid {p.pid} age {duration(p.age)} version {version or 'unknown'}"
                   + (f" (STALE: installed is {installed})" if stale else "") + f"; the owned backend is pid {', '.join(map(str, sorted(owned)))}: stop it by pid")
    return out
