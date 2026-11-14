# ruff: noqa: F401, RUF007, UP031  (moved verbatim; see tests/ci_verbatim.py)
"""Watch of the long-running live hz on this machine (its server on its port, the launchd daemon, every child) for leaks.

READ-ONLY toward hz: it only runs ps, lsof, netstat and launchctl list and one GET of the local web; it never signals,
restarts or opens hz. A sample is one JSON line: tree pids with parents, per process fds by type, TCP states, listening
ports, children, orphans, zombies, RSS and virtual memory, CPU over 2 s, threads, uptime, the on-disk package version,
the load and the seconds the web took to answer. `table` is the report, `findings` the check.

The caller fills EDGE (`configure`): run (the process runner), state (directory of samples.jsonl, rotated at 20 MB),
config (an optional TOML file that overrides DEFAULTS), daemon_label (the launchd label of the daemon)."""

import http.client
import json
import os
import re
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

EDGE: dict = {}  # run, state, config, daemon_label: set by the caller

DEFAULTS = {  # thresholds of the check and the report; a TOML file (EDGE["config"]) overrides single values
    "port": 8737, "allowed_ports": [8737], "window_hours": 6,
    "fds_max": 500, "rss_max_mb": 800, "close_wait_max": 5, "orphans_max": 0, "zombies_max": 0, "cpu_idle_max": 25.0, "cpu_window_minutes": 10,
    "min_samples": 6, "rise_rel": 0.10, "fds_slope_per_hour": 10, "sockets_slope_per_hour": 10, "close_wait_slope_per_hour": 2,
    "children_slope_per_hour": 3, "rss_slope_mb_per_hour": 20, "restart_window_minutes": 60, "stale_minutes": 15,
}


def configure(**values):
    """Replace the edges with the caller's."""
    EDGE.clear()
    EDGE.update(values)


MAX_BYTES = 20 * 1024 * 1024
CPU_SECONDS = 2.0
FD_KINDS = {"REG": "files", "DIR": "files", "CHR": "files", "VREG": "files", "IPv4": "sockets", "IPv6": "sockets", "unix": "sockets",
            "PIPE": "pipes", "KQUEUE": "kqueue"}


def state_dir() -> Path:
    return Path(str(EDGE["state"])).expanduser()


def load_config() -> dict:
    override = tomllib.loads(Path(str(EDGE["config"])).expanduser().read_text()) if EDGE.get("config") else {}
    return {**DEFAULTS, "daemon_label": str(EDGE.get("daemon_label") or ""), **override}


def run(cmd: list[str]) -> str:
    """The only way this tool touches the system: a read-only command, '' when it fails."""
    return str(EDGE["run"](cmd, timeout=30).out or "")


# --- parsing (pure, tested with fixtures) -------------------------------------------------------------------------------

def parse_clock(text: str) -> float:
    """`[[dd-]hh:]mm:ss[.cc]` (etime and cputime) in seconds."""
    days = 0
    if "-" in text:
        d, text = text.split("-", 1)
        days = int(d)
    parts = [float(p) for p in text.split(":")]
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return days * 86400 + secs


def parse_ps(text: str) -> dict[int, dict]:
    """`ps -axo pid=,ppid=,stat=,etime=,time=,rss=,vsz=,command=` -> {pid: {...}}."""
    procs = {}
    for line in text.splitlines():
        f = line.split(None, 7)
        if len(f) < 8 or not f[0].isdigit():
            continue
        procs[int(f[0])] = {"pid": int(f[0]), "ppid": int(f[1]), "stat": f[2], "age_s": parse_clock(f[3]), "cpu_s": parse_clock(f[4]),
                            "rss_kb": int(f[5]), "vsz_kb": int(f[6]), "cmd": f[7].strip()}
    return procs


def parse_cpu(text: str) -> dict[int, float]:
    """`ps -o pid=,time= -p ...` -> cumulative CPU seconds."""
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) == 2 and f[0].isdigit():
            out[int(f[0])] = parse_clock(f[1])
    return out


def parse_threads(text: str) -> dict[int, int]:
    """`ps -M -p ...`: one line per thread, the first of a process starts with the user."""
    counts: dict[int, int] = {}
    for line in text.splitlines()[1:]:
        f = line.split()
        if not f:
            continue
        pid = f[0] if f[0].isdigit() else f[1] if len(f) > 1 and f[1].isdigit() else ""
        if pid:
            counts[int(pid)] = counts.get(int(pid), 0) + 1
    return counts


def parse_lsof(text: str) -> dict[int, dict]:
    """`lsof -nP -p ... -F pftTn` -> {pid: {"fds": {...}, "tcp": {state: n}, "listen": [ports]}}. Only numeric fds count
    (cwd, txt, mem, rtd are not descriptors)."""
    out: dict[int, dict] = {}
    pid = None
    fd = kind = name = None
    state = None

    def close() -> None:
        nonlocal fd, kind, name, state
        if pid is not None and fd is not None and fd.isdigit():
            rec = out[pid]
            rec["fds"][FD_KINDS.get(kind or "", "other")] += 1
            if kind in ("IPv4", "IPv6") and state:
                rec["tcp"][state] = rec["tcp"].get(state, 0) + 1
                if state == "LISTEN":
                    match = re.search(r":(\d+)$", name or "")
                    if match:
                        rec["listen"].append(int(match.group(1)))
        fd = kind = name = state = None

    for line in text.splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            close()
            pid = int(val)
            out[pid] = {"fds": dict.fromkeys(("files", "sockets", "pipes", "kqueue", "other"), 0), "tcp": {}, "listen": []}
        elif tag == "f":
            close()
            fd = val
        elif tag == "t":
            kind = val
        elif tag == "n":
            name = val
        elif tag == "T" and val.startswith("ST="):
            state = val[3:]
    close()
    return out


def time_wait_count(netstat: str, ports: list[int]) -> int:
    """TIME_WAIT sockets have no owner in lsof; count them by local port in `netstat -an -p tcp`."""
    n = 0
    for line in netstat.splitlines():
        f = line.split()
        if len(f) >= 6 and f[0].startswith("tcp") and f[-1] == "TIME_WAIT":
            match = re.search(r"\.(\d+)$", f[3])
            if match and int(match.group(1)) in ports:
                n += 1
    return n


def find_tree(procs: dict[int, dict], server: int | None, daemon: int | None) -> dict[int, dict]:
    """Roots plus every descendant; each process gets a role."""
    roots = [p for p in (server, daemon) if p in procs]
    tree = {}
    stack = list(roots)
    while stack:
        pid = stack.pop()
        if pid in tree:
            continue
        rec = dict(procs[pid])
        rec["role"] = "server" if pid == server else "daemon" if pid == daemon else "supervisor" if "supervis" in rec["cmd"].lower() else "child"
        tree[pid] = rec
        stack += [p for p, r in procs.items() if r["ppid"] == pid]
    return tree


def find_orphans(procs: dict[int, dict], tree: dict[int, dict]) -> list[int]:
    """hz processes reparented to launchd that are not the server or daemon."""
    return sorted(p for p, r in procs.items()
                  if p not in tree and r["ppid"] == 1 and re.search(r"-m heinzel\b|/tools/heinzel/|\bheinzel-(daemon|supervisor)\b", r["cmd"]))


def hz_version(cmd: str) -> str | None:
    """Read the package on disk, which may differ from the running server."""
    match = re.match(r"(\S+)/bin/python", cmd)
    if not match:
        return None
    for info in sorted(Path(match.group(1)).glob("lib/python*/site-packages/heinzel-*.dist-info")):
        return info.name[len("heinzel-"):-len(".dist-info")]
    return None


def build_sample(procs, tree_ids, lsof, threads, cpu_pct, netstat, orphans, ports, version, now) -> dict:
    rows = []
    for pid in sorted(tree_ids):
        r = procs[pid]
        fdinfo = lsof.get(pid, {"fds": {}, "tcp": {}, "listen": []})
        rows.append({"pid": pid, "ppid": r["ppid"], "role": r["role"], "stat": r["stat"], "age_s": int(r["age_s"]), "rss_kb": r["rss_kb"],
                     "vsz_kb": r["vsz_kb"], "cpu": round(cpu_pct.get(pid, 0.0), 1), "threads": threads.get(pid, 0),
                     "fds": sum(fdinfo["fds"].values()), "fd_types": fdinfo["fds"]})
    tcp: dict[str, int] = {}
    listen: list[int] = []
    kinds = dict.fromkeys(("files", "sockets", "pipes", "kqueue", "other"), 0)
    for pid in tree_ids:
        info = lsof.get(pid)
        if not info:
            continue
        for k, v in info["tcp"].items():
            tcp[k] = tcp.get(k, 0) + v
        listen += info["listen"]
        for k, v in info["fds"].items():
            kinds[k] += v
    tcp["TIME_WAIT"] = time_wait_count(netstat, ports)
    server = next((r for r in rows if r["role"] == "server"), None) or (rows[0] if rows else None)
    return {"ts": now, "running": bool(rows), "version": version, "server_pid": server["pid"] if server else None,
            "uptime_s": server["age_s"] if server else None, "procs": rows, "orphans": orphans, "listen": sorted(set(listen)), "tcp": tcp,
            "totals": {"procs": len(rows), "children": sum(1 for r in rows if r["role"] == "child"), "fds": sum(r["fds"] for r in rows),
                       "fd_types": kinds, "sockets": kinds["sockets"], "close_wait": tcp.get("CLOSE_WAIT", 0),
                       "zombies": sum(1 for r in rows if "Z" in r["stat"]), "orphans": len(orphans),
                       "rss_kb": sum(r["rss_kb"] for r in rows), "vsz_kb": sum(r["vsz_kb"] for r in rows),
                       "cpu": round(sum(r["cpu"] for r in rows), 1), "threads": sum(r["threads"] for r in rows)}}


# --- sampling and storage -----------------------------------------------------------------------------------------------

PS_FIELDS = "pid=,ppid=,stat=,etime=,time=,rss=,vsz=,command="


def daemon_pid(label: str) -> int | None:
    match = re.search(r'"PID"\s*=\s*(\d+)', run(["launchctl", "list", label]))
    return int(match.group(1)) if match else None


LOAD_MAX = 40  # P0 2026-11-07: above this the live web on 8737 stopped answering
WEB_TIMEOUT_S = 5


def probe_web(port: int) -> float | None:
    """Seconds the live web took to answer one read-only GET of /, or None when it did not answer within WEB_TIMEOUT_S."""
    began = time.monotonic()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=WEB_TIMEOUT_S) as response:
            response.read(1)
    except urllib.error.HTTPError:
        pass  # it answered
    except (OSError, ValueError, http.client.HTTPException):
        return None
    return round(time.monotonic() - began, 2)


def take_sample(cfg: dict, sleep=time.sleep, clock=time.time, probe=probe_web, loadavg=os.getloadavg) -> dict:
    sample = _take_sample(cfg, sleep, clock)
    sample["load1"] = round(loadavg()[0], 1)
    sample["web_s"] = probe(cfg["port"]) if sample.get("running") else None
    return sample


def _take_sample(cfg: dict, sleep=time.sleep, clock=time.time) -> dict:
    ports = sorted(set(cfg["allowed_ports"]) | {cfg["port"]})
    listener = run(["lsof", "-nP", f"-iTCP:{cfg['port']}", "-sTCP:LISTEN", "-t"]).split()
    server = int(listener[0]) if listener else None
    daemon = daemon_pid(cfg["daemon_label"])
    first = time.monotonic()
    procs = parse_ps(run(["ps", "-axo", PS_FIELDS]))
    tree = find_tree(procs, server, daemon)
    if not tree:
        return build_sample(procs, [], {}, {}, {}, "", [], ports, None, clock())
    ids = ",".join(map(str, tree))
    sleep(CPU_SECONDS)
    after = parse_cpu(run(["ps", "-o", "pid=,time=", "-p", ids]))
    elapsed = max(time.monotonic() - first, 0.001)
    cpu_pct = {pid: max(after[pid] - tree[pid]["cpu_s"], 0.0) / elapsed * 100 for pid in after if pid in tree}
    lsof = parse_lsof(run(["lsof", "-nP", "-p", ids, "-F", "pftTn"]))
    threads = parse_threads(run(["ps", "-M", "-p", ids]))
    srv = tree.get(server) or next(iter(tree.values()))
    return build_sample(tree, list(tree), lsof, threads, cpu_pct, run(["netstat", "-an", "-p", "tcp"]), find_orphans(procs, tree), ports,
                        hz_version(srv["cmd"]), clock())


def samples_path() -> Path:
    return state_dir() / "samples.jsonl"


def append_sample(sample: dict) -> None:
    path = samples_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size >= MAX_BYTES:
        path.replace(path.with_suffix(".jsonl.1"))
    with path.open("a") as out:
        out.write(json.dumps(sample, separators=(",", ":")) + "\n")


def read_samples(since: float = 0.0) -> list[dict]:
    path = samples_path()
    rows = []
    for file in (path.with_suffix(".jsonl.1"), path):
        try:
            lines = file.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("ts", 0) >= since:
                rows.append(row)
    return sorted(rows, key=lambda r: r["ts"])


# --- trends and findings (pure) -----------------------------------------------------------------------------------------

METRICS = {  # name: (extractor, unit divisor, slope key in config, label)
    "fds": (lambda s: s["totals"]["fds"], 1, "fds_slope_per_hour", "open fds"),
    "sockets": (lambda s: s["totals"]["sockets"], 1, "sockets_slope_per_hour", "sockets"),
    "close_wait": (lambda s: s["totals"]["close_wait"], 1, "close_wait_slope_per_hour", "CLOSE_WAIT"),
    "children": (lambda s: s["totals"]["children"], 1, "children_slope_per_hour", "children"),
    "rss_mb": (lambda s: s["totals"]["rss_kb"], 1024, "rss_slope_mb_per_hour", "RSS MB"),
}


def series(samples: list[dict], name: str) -> list[tuple[float, float]]:
    get, div, _, _ = METRICS[name]
    return [(s["ts"], get(s) / div) for s in samples if s.get("running")]


def trend(points: list[tuple[float, float]], samples: list[dict], cfg: dict, name: str) -> dict:
    """Slope per hour over the whole window, and whether the series is a leak: non-decreasing over its last
    `min_samples`+ samples, rising faster than the limit and by more than rise_rel of the baseline."""
    out = {"slope": 0.0, "run_slope": 0.0, "leak": False, "n": len(points), "run": 0, "rise": 0.0}
    if len(points) < 2:
        return out
    xs = [(t - points[0][0]) / 3600 for t, _ in points]
    ys = [v for _, v in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    var = sum((x - mx) ** 2 for x in xs)
    out["slope"] = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / var if var else 0.0
    start = len(ys) - 1
    while start > 0 and ys[start - 1] <= ys[start]:
        start -= 1
    run_pts = points[start:]
    out["run"] = len(run_pts)
    out["rise"] = run_pts[-1][1] - run_pts[0][1]
    hours = (run_pts[-1][0] - run_pts[0][0]) / 3600
    slope = out["rise"] / hours if hours > 0 else 0.0
    out["run_slope"] = slope
    first_hour = [v for (t, v), s in zip(points, [x for x in samples if x.get("running")], strict=True)
                  if (s.get("uptime_s") or 1e9) <= 3600]
    baseline = sum(first_hour) / len(first_hour) if first_hour else points[0][1]
    out["leak"] = (out["run"] >= cfg["min_samples"] and slope > cfg[METRICS[name][2]] and out["rise"] > cfg["rise_rel"] * max(baseline, 1e-9))
    return out


def rss_context(samples: list[dict], run: int) -> str:
    """Compare aggregate tree RSS within one server uptime, without assuming unchanged tree membership."""
    epoch = []
    for sample in reversed(samples):
        if not sample.get("running"):
            break
        if epoch and (sample["server_pid"] != epoch[-1]["server_pid"] or sample["version"] != epoch[-1]["version"]
                      or (sample["uptime_s"] or 0) > (epoch[-1]["uptime_s"] or 0)):
            break
        epoch.append(sample)
    epoch.reverse()
    prior = epoch[:-run] if run else []
    if not prior:
        return ""
    peak = max(s["totals"]["rss_kb"] for s in prior) / 1024
    current = epoch[-1]["totals"]["rss_kb"] / 1024
    if current > peak:
        return ""
    return (f"; current tree RSS {current:.1f} MB is at/below earlier {peak:.1f} MB tree peak during this server uptime; "
            "residency or tree membership changes can also trigger this warning")


def findings(samples: list[dict], cfg: dict, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    pressure = []
    if samples and (samples[-1].get("load1") or 0) > LOAD_MAX:
        pressure.append(f"Mini load {samples[-1]['load1']:.0f} above {LOAD_MAX}: stop or gate local heavy work")
    if not samples or not samples[-1].get("running"):
        return ["hz not running (no server on port %d)" % cfg["port"], *pressure]
    last = samples[-1]
    t = last["totals"]
    out = []
    if "web_s" in last and last["web_s"] is None:
        pressure.append(f"live web on port {cfg['port']} did not answer within {WEB_TIMEOUT_S} s")
    window = [s for s in samples if s["ts"] >= now - cfg["window_hours"] * 3600]
    pairs = [(a, b) for a, b in zip(samples, samples[1:], strict=False) if b["ts"] >= now - cfg["restart_window_minutes"] * 60]
    for a, b in pairs:
        if not a.get("running"):
            out.append("hz was down and is back (restart)")
        elif b["server_pid"] != a["server_pid"] or (b["uptime_s"] or 0) < (a["uptime_s"] or 0):
            out.append(f"hz restarted: pid {a['server_pid']}->{b['server_pid']}, uptime {a['uptime_s']}->{b['uptime_s']} s")
        elif b["version"] != a["version"]:
            out.append(f"hz package on disk changed: {a['version']}->{b['version']}; server restart not observed")
    out = out[-1:]
    if t["fds"] > cfg["fds_max"]:
        out.append(f"{t['fds']} open fds, limit {cfg['fds_max']}")
    if t["rss_kb"] / 1024 > cfg["rss_max_mb"]:
        out.append(f"RSS {t['rss_kb'] // 1024} MB, limit {cfg['rss_max_mb']}")
    if t["close_wait"] > cfg["close_wait_max"]:
        out.append(f"{t['close_wait']} sockets in CLOSE_WAIT, limit {cfg['close_wait_max']}")
    if t["orphans"] > cfg["orphans_max"]:
        out.append(f"{t['orphans']} orphan hz process(es) under launchd: pids {last['orphans']}")
    if t["zombies"] > cfg["zombies_max"]:
        out.append(f"{t['zombies']} zombie process(es) in the hz tree")
    stray = [p for p in last["listen"] if p not in cfg["allowed_ports"]]
    if stray:
        out.append(f"unexpected listening port(s) {stray}")
    recent = [s for s in samples if s["ts"] >= now - cfg["cpu_window_minutes"] * 60 and s.get("running")]
    if len(recent) >= 2 and all(s["totals"]["children"] == 0 for s in recent):
        avg = sum(s["totals"]["cpu"] for s in recent) / len(recent)
        limit = cfg["cpu_idle_max"]
        if avg > limit:
            rounded = round(avg, 1)
            displayed = f"{rounded:g}" if rounded > limit else f">{limit:g}"
            detail = ""
            if all("procs" in sample for sample in recent):
                means = {
                    role: sum(p["cpu"] for sample in recent for p in sample["procs"] if p["role"] == role) / len(recent)
                    for role in ("server", "daemon")
                }
                daemon_samples = sum(any(p["role"] == "daemon" for p in sample["procs"]) for sample in recent)
                detail = (f" (mean contributions: server {means['server']:.1f}%, daemon {means['daemon']:.1f}%; "
                          f"daemon observed in {daemon_samples}/{len(recent)} samples)")
            out.append(
                f"CPU {displayed}% sample mean from {len(recent)} intermittent samples in a {cfg['cpu_window_minutes']} min lookback "
                f"({CPU_SECONDS:g}s target per sample) with no observed children, "
                f"limit {limit:g}%{detail}"
            )
    for name in METRICS:
        tr = trend(series(window, name), window, cfg, name)
        if tr["leak"]:
            context = rss_context(window, tr["run"]) if name == "rss_mb" else ""
            out.append(f"{METRICS[name][3]} grows monotonically: +{tr['rise']:.0f} over {tr['run']} samples "
                       f"(sample-window tail {tr['run_slope']:+.1f}/h, whole window {tr['slope']:+.1f}/h; may span restarts){context}")
    return pressure + out


def table(samples: list[dict], cfg: dict) -> list[str]:
    if not samples or not samples[-1].get("running"):
        return ["hz not running"]
    last = samples[-1]
    t = last["totals"]
    up = last["uptime_s"] or 0
    head = (f"hz (on disk {last['version'] or '?'}) pid {last['server_pid']} up {int(up // 86400)}d{int(up % 86400 // 3600)}h  procs {t['procs']} "
            f"(children {t['children']}, orphans {t['orphans']}, zombies {t['zombies']})  threads {t['threads']}  cpu {t['cpu']}%  "
            f"listen {last['listen']}  tcp {last['tcp']}")
    lines = [head, f"{'metric':<11}{'now':>8}{'slope/h':>10}{'samples':>9}  verdict"]
    for name, (get, div, _, label) in METRICS.items():
        tr = trend(series(samples, name), samples, cfg, name)
        now_val = get(last) / div
        lines.append(f"{label:<11}{now_val:>8.0f}{tr['slope']:>+10.1f}{tr['n']:>9}  {'LEAK? monotonic over %d samples' % tr['run'] if tr['leak'] else 'ok'}")
    return lines

