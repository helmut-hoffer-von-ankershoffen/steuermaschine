#!/usr/bin/env python3
"""hz_next: `hz-train next [--start] [X.Y.Z]`, the N+1 batch view at the cut (research/continuous-release-2026-11-08.md section 3, change 1).

cut(N) ends nothing: when train N has been cut (its state file exists, phase release or later) the same tick opens N+1's batch.
The view prints (1) PRs merged since the cut, (2) queued PRs that are green with CI already run on the current main, (3) blockers,
(4) builders to nudge (DEAD owners and START lines of hz-refill). `--start` also sends the GO lines (hz-announce-merge), queues the
green PRs (hz-queue add) and records the batch in ~/.cache/hz-train/next-vN+1.json (one file per version, written once, then updated).
Read-only except for those commands and that record; it never touches the train state files (v*.json) or hz-release.
"""
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
from . import plan as hz_plan
from . import edge  # moved: records, processes and what used to be separate scripts
from steuermaschine.config import child_environment, search_path
from ... import state as statedir  # the plugin's state paths
CORE = "helmut-hoffer-von-ankershoffen/heinzel"
ENV = child_environment(PATH=os.path.expanduser("~/.local/bin") + ":/opt/homebrew/bin:" + search_path())
BLOCKER = re.compile(r"MAIN RED|main failure pinned|STALE LOOPS|starved|UNKNOWN", re.I)
NUDGE = re.compile(r"^(DEAD|START)\b")


def next_version(version):
    major, minor, patch = (int(p) for p in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def sh(cmd, timeout=90):
    try:
        done = edge.run(edge.argv(cmd), capture_output=True, text=True, timeout=timeout, env=ENV)  # moved: an argument list, no shell
        return (done.stdout or "") + ("" if done.returncode == 0 else done.stderr or "")
    except subprocess.TimeoutExpired:
        return ""


def queued_prs(text):
    """hz-queue list rows `<pr> <status>` -> PR numbers (core, numeric) whose status is exactly `queued`."""
    return [int(m[1]) for l in text.splitlines() if (m := re.match(r"^(\d+)\s+queued\s*$", l.strip()))]


def pr_ready(view):
    """gh pr view json -> (state, why). green-current: every check finished successfully and the branch is not BEHIND main."""
    rollup = view.get("statusCheckRollup") or []
    done = [c for c in rollup if (c.get("status") or "COMPLETED") == "COMPLETED"]
    if not rollup or len(done) < len(rollup):
        return "running", "CI not finished"
    bad = [c.get("name") or c.get("context") for c in rollup if (c.get("conclusion") or c.get("state")) not in ("SUCCESS", "NEUTRAL", "SKIPPED")]
    if bad:
        return "red", "failing: " + ", ".join(str(b) for b in bad[:3])
    if view.get("mergeStateStatus") == "BEHIND":
        return "behind", "green but BEHIND main (an update costs a CI run)"
    return "green-current", "green, CI ran on current main"


def blockers(digest, held):
    lines = [l.strip() for l in digest.splitlines() if l.strip() and l.strip() != "quiet" and BLOCKER.search(l)]
    return [f"hold/freeze file: {h}" for h in held] + [l[:200] for l in lines]


def nudges(refill):
    return [l.strip() for l in refill.splitlines() if NUDGE.match(l.strip())]


def render(version, nxt, since_label, merged, queued, blocked, nudge):
    out = [f"sm release train next: v{version} is cut; v{nxt} batch is OPEN (since {since_label})"]
    out += merged or [f"0 merged since {since_label}"]
    out.append(f"{sum(1 for _, s, _ in queued if s == 'green-current')} of {len(queued)} queued PRs are green with CI on current main")
    out += [f"  #{n} {s}: {why}" for n, s, why in queued]
    out.append("blockers: " + ("none" if not blocked else str(len(blocked))))
    out += [f"  {b}" for b in blocked]
    out.append(f"builders to nudge: {len(nudge)}" + (" (sm beads refill)" if nudge else ""))
    out += [f"  {n}" for n in nudge[:20]]
    return "\n".join(out)


def record_path(state_dir, nxt):
    return Path(state_dir) / f"next-v{nxt}.json"


def record(state_dir, nxt, data):
    """One file per version, atomic; the first write keeps `opened_at`. Not named v*.json: hz-train's trains() never reads it."""
    path = record_path(state_dir, nxt)
    old = {}
    try:
        old = json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    data = old | data | {"opened_at": old.get("opened_at", data["updated_at"])}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    os.replace(tmp, path)
    return path


def bump_time(prs, version):
    """mergedAt (aware UTC) of the merged `chore(release): vX.Y.Z` PR, else None."""
    for p in prs:
        if p.get("mergedAt") and re.match(rf"chore\(release\):\s*v?{re.escape(version)}\b", p.get("title", "")):
            return hz_plan.parse_ts(p["mergedAt"])
    return None


def cut_version(trains):
    """The newest train that has been cut (state file with a phase other than idle/cleared)."""
    cut = [t for t in trains if t.get("version") and t.get("phase") not in (None, "idle", "cleared")]
    return cut[-1] if cut else None


def run(state_dir, trains, start=False, version=None, sh=sh, now=None):
    now = now or time.time()
    train = next((t for t in trains if t.get("version") == version), None) if version else cut_version(trains)
    if not train:
        print("sm release train next: no train has been cut" + (f" (v{version} unknown)" if version else "") + "; nothing to open")
        return 0
    v, nxt = train["version"], next_version(train["version"])
    since = datetime.fromtimestamp(train.get("start") or now, timezone.utc)
    prs = json.loads(sh(f"gh pr list -R {CORE} --state merged --limit 80 --json number,title,mergedAt") or "[]")
    bump = bump_time(prs, v)
    if bump:  # the batch cursor is N's bump commit; before it merges the train start is the cursor
        since = bump
    merged = hz_plan.merged_lines([p for p in prs if not p.get("title", "").startswith("chore(release)")], since)
    queued = []
    for n in queued_prs(sh("sm queue list --active")):  # moved: the queue's listing
        view = json.loads(sh(f"gh pr view {n} -R {CORE} --json statusCheckRollup,mergeStateStatus") or "{}")
        queued.append((n, *pr_ready(view)))
    held = [str(p) for p in (statedir.path("merge-freeze"), statedir.path("gate.hold")) if p.exists()]
    blocked = blockers(edge.digest(sh), held)  # moved: `sm go --view tick`; unreadable is an UNKNOWN blocker
    nudge = nudges(sh("sm beads refill"))  # moved: the refill view
    label = f"{since.astimezone():%H:%M} local"
    print(render(v, nxt, label, merged, queued, blocked, nudge))
    green = [n for n, s, _ in queued if s == "green-current"]
    data = {"cut": v, "next": nxt, "since": since.isoformat(), "merged": len(merged) - 1, "queued": [q[0] for q in queued],
            "green": green, "blockers": blocked, "nudge": len(nudge), "started": bool(start),
            "updated_at": datetime.fromtimestamp(now, timezone.utc).isoformat()}
    if start:
        go = ""  # moved: the GO message tool is retired (tool map); its callers fold into go and tick
        if go:
            print("GO lines:\n" + go)
        if green:
            print(sh("sm queue add " + " ".join(str(n) for n in green)).strip())  # moved: the queue's add
        print(f"next: v{nxt} batch open, {len(merged) - 1} merged, {len(green)} queued, briefs: {len(nudge)}")
        print("recorded " + str(record(state_dir, nxt, data)))
    else:
        print("(view only; `sm release train next --start` sends the GOs, queues the green PRs and records the batch)")
    return 0
