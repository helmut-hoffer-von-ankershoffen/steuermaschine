#!/usr/bin/env python3
"""hz_plan: release candidate list as one table (`hz-train plan`) and merges since a LOCAL clock time (`hz-tick --merged-since HH:MM`).

Velocity bead (tonight's by-hand repeats): the release-A candidate list was rebuilt by hand 3x, and the watch agent read UTC merge
times as local. Both are pure functions over gh data so they are tested without the network.
"""
import json, os, subprocess, sys
from datetime import datetime, timedelta, timezone
from . import digest as d  # moved: the definitions of the digest module this table needs
from . import edge  # moved: records, processes and what used to be separate scripts
from ... import state as statedir  # the plugin's state paths
from ..queue_lib import core_facts
R = "helmut-hoffer-von-ankershoffen/heinzel"
LIST = "research/release-a.txt"  # moved: in the records checkout (edge.records_dir()); one PR number per word; '#' starts a comment line


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if s else None


def local_to_utc(hhmm, now=None):
    """'22:30' LOCAL clock time -> the most recent such instant at or before now, as aware UTC (yesterday if it is still ahead)."""
    now = (now or datetime.now(timezone.utc)).astimezone()
    h, m = (int(x) for x in hhmm.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if t > now:
        t -= timedelta(days=1)
    return t.astimezone(timezone.utc)


def merged_lines(prs, since_utc):
    """prs: gh rows with number,title,mergedAt. -> header + one line per PR merged at or after since, local and UTC time."""
    rows = sorted((parse_ts(p["mergedAt"]), p) for p in prs if p.get("mergedAt"))
    rows = [(t, p) for t, p in rows if t >= since_utc]
    out = [f"{len(rows)} merged since {since_utc.astimezone():%H:%M} local ({since_utc:%H:%MZ})"]
    out += [f"  #{p['number']} {t.astimezone():%H:%M} local / {t:%H:%MZ}  {p['title'][:60]}" for t, p in rows]
    return out


def ci_span(roll, now):
    """(minutes from first check start to last completion or now, finished) over the rollup."""
    st = [parse_ts(c.get("startedAt")) for c in roll if c.get("startedAt") and not c["startedAt"].startswith("0001")]
    if not st:
        return None, False
    done = all(c.get("status") == "COMPLETED" for c in roll)
    en = [parse_ts(c.get("completedAt")) for c in roll if c.get("completedAt") and not c["completedAt"].startswith("0001")]
    end = max(en) if done and en else now
    return int((end - min(st)).total_seconds() // 60), done


def plan_row(info, queue, now, known=()):
    """info: gh pr view json. -> (pr, state, mergeable, ci, minutes, action)."""
    n = info["number"]
    if info.get("state") == "MERGED":
        return (n, "MERGED", "-", "-", None, "none")
    roll = info.get("statusCheckRollup") or []
    cls, real = d.classify_pr(roll)
    mins, _ = ci_span(roll, now)
    ms = info.get("mergeStateStatus", "?")
    q = queue.get(str(n))
    state = "DRAFT" if info.get("isDraft") else ("QUEUED" if q == "queued" else ("NEEDS-WORK" if q == "needs work" else "OPEN"))
    red = sorted({c["name"] for c in roll if c.get("conclusion") in d.REAL_BAD and not d.is_focused(c.get("name")) and c["name"] not in (core_facts.aggregate(), *core_facts.api_checks())})
    ci = {"ready": "green", "pending": "pending", "deferred": "deferred"}.get(cls, "FAIL " + ",".join(real)[:40])
    if cls == "pending" and red:  # red while still running: act now, do not wait for the rest
        ci = f"pending, RED {len(red)}: " + ",".join(red)[:30]
    if state == "DRAFT":
        act = "ready for review first"
    elif ms == "DIRTY":
        act = "rebase (conflicts)"
    elif cls == "failing":
        bead = d.known_bead(real, list(known))
        act = f"KNOWN({bead}): sm ci one rerun --job once" if bead else f"sm ci faillog {n}"
    elif cls == "deferred":
        act = "sm ci rerun"
    elif cls == "pending" and red:
        known_bead = d.known_bead(red, list(known))
        act = f"RED while running: KNOWN({known_bead}) rerun --job after run ends" if known_bead else f"sm ci faillog {n} now (red while running)"
    elif cls == "pending":
        act = "wait (no push)"
    elif state == "QUEUED":
        act = "queue merges when main is green"
    else:
        act = f"sm head ship {n}"
    return (n, state, ms, ci, mins, act)


def table(rows):
    out = [f"{'PR':>5} {'state':<10} {'merge':<9} {'CI':<44} {'min':>4}  action"]
    for n, s, ms, ci, mins, act in rows:
        out.append(f"{n:>5} {s:<10} {ms:<9} {ci:<44} {'' if mins is None else mins:>4}  {act}")
    ok = sum(1 for r in rows if r[1] == "MERGED")
    ready = sum(1 for r in rows if r[1] != "MERGED" and r[3] == "green" and r[2] not in ("DIRTY", "DRAFT"))
    dirty = [str(r[0]) for r in rows if r[2] == "DIRTY"]
    out.append(f"merged {ok}/{len(rows)}, green-open {ready}, DIRTY {','.join(dirty) or '-'}, not green {len(rows) - ok - ready}")
    return out


def _gh(*a):
    r = edge.run(["gh", *a], capture_output=True, text=True, timeout=60)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None


def intended(words):
    if words:
        return [int(w.lstrip("#")) for w in words]
    try:
        text = open(edge.records_dir() / LIST, encoding="utf-8").read()
    except OSError:
        return []
    return [int(w.lstrip("#")) for line in text.splitlines() if not line.startswith("#") for w in line.split() if w.lstrip("#").isdigit()]


def queue_status():
    try:
        items = json.load(open(str(statedir.path("queue.json"))))["items"]
    except Exception:
        return {}
    return {i["key"].split()[-1]: i["status"] for i in items}


def plan(words):
    prs = intended(words)
    if not prs:
        print(f"no PRs: pass numbers or list them in {edge.records_dir() / LIST}")
        return 2
    known = d.load_known(str(edge.records_dir() / "research" / "known-failures.toml"))
    q, now = queue_status(), datetime.now(timezone.utc)
    rows = []
    for n in prs:
        info = _gh("pr", "view", str(n), "-R", R, "--json", "number,state,isDraft,mergeStateStatus,statusCheckRollup")
        rows.append(plan_row(info, q, now, known) if info else (n, "?", "?", "gh failed", None, "retry"))
    print("\n".join(table(rows)))
    return 0


def merged_since(hhmm):
    since = local_to_utc(hhmm)
    prs = _gh("pr", "list", "-R", R, "--state", "merged", "--limit", "100", "--json", "number,title,mergedAt") or []
    print("\n".join(merged_lines(prs, since)))
    return 0

