"""One-screen release status: the pure parts (which train is active, check counts, summary, text)."""

import json, os, subprocess, sys, time
from ... import state as statedir  # the plugin's state paths
TRAIN_DIR = str(statedir.path("train"))


def active_train(train_dir=None):
    """The active train: legacy state.json if still present, else the lowest vX.Y.Z.json not done or cleared."""
    import glob
    import re
    train_dir = train_dir or TRAIN_DIR
    try:
        return json.load(open(os.path.join(train_dir, "state.json")))
    except (OSError, ValueError):
        pass
    found = []
    for path in glob.glob(os.path.join(train_dir, "v*.json")):
        m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)\.json", os.path.basename(path))
        if not m:
            continue
        try:
            data = json.load(open(path))
        except (OSError, ValueError):
            continue
        if data.get("phase") not in ("done", "cleared"):
            found.append((tuple(map(int, m.groups())), data))
    return min(found, key=lambda item: item[0])[1] if found else None


def counts(items):
    """items: dicts with status/conclusion -> {'ok','fail','run','skip'} counts."""
    c = {"ok": 0, "fail": 0, "run": 0, "skip": 0}
    for i in items:
        st, co = (i.get("status") or "").lower(), (i.get("conclusion") or "").lower()
        if st not in ("completed", ""):
            c["run"] += 1
        elif co == "success":
            c["ok"] += 1
        elif co in ("skipped", "neutral", "cancelled"):
            c["skip"] += 1
        elif co:
            c["fail"] += 1
        else:
            c["run"] += 1
    return c


def summarize(train, pr, runs, jobs, frozen, now):
    """train: state.json dict or None; pr: dict or None; runs: release-branch workflow runs; jobs: failed job names of the CI run."""
    if not train or train.get("phase") in (None, "idle") and not pr:
        return {"active": False}
    by = {}
    for r in runs:
        by.setdefault(r["name"].split()[0] if r["name"].split() else r["name"], []).append(r)  # "CI <sha>", "Acceptance  "
    ci = by.get("CI") or [{}]
    acc = by.get("Acceptance") or [{}]
    blockers = []
    if ci[0].get("conclusion") == "failure":
        blockers.append("candidate CI failed: " + (", ".join(jobs) or "unknown job"))
    if acc[0].get("status") not in (None, "completed"):
        blockers.append(f"Acceptance attempt {acc[0].get('attempt', 1)} in progress")
    elif acc[0].get("conclusion") == "failure":
        blockers.append("Acceptance failed")
    if pr and pr.get("mergeable") not in (None, "MERGEABLE"):
        blockers.append(f"PR {pr.get('mergeable')}")
    if blockers and not any("in progress" in b for b in blockers):
        nxt = "classify failed job against known-failures.toml; KNOWN -> sm ci one rerun, else fix forward"
    elif any("in progress" in b for b in blockers):
        nxt = "wait for Acceptance; then the train merges the release PR and tags (no new pushes, freeze on)"
    else:
        nxt = "train merges when checks are green; verify tag, publish, sm release verify --mini"
    started = train.get("start")
    return {"active": True, "version": train.get("version"), "pr": (pr or {}).get("number"), "phase": train.get("phase"),
            "age_min": int((now - started) / 60) if started else None, "ci": ci[0].get("conclusion") or ci[0].get("status"),
            "acc": acc[0].get("conclusion") or acc[0].get("status"), "acc_attempt": acc[0].get("attempt"),
            "checks": pr.get("checks") if pr else None, "frozen": frozen, "blockers": blockers, "next": nxt}


def render(s):
    if not s.get("active"):
        return "no release in flight"
    ck = s["checks"] or {}
    return "\n".join([
        f"RELEASE v{s['version']}  PR #{s['pr']}  train phase {s['phase']}  age {s['age_min']} min",
        f"candidate PR checks: ok {ck.get('ok', '?')}  fail {ck.get('fail', '?')}  running {ck.get('run', '?')}  skipped {ck.get('skip', '?')}",
        f"candidate CI: {s['ci']}   acceptance: {s['acc']} (attempt {s['acc_attempt']})",
        f"freeze: {'ON (gate hold)' if s['frozen'] else 'off'}",
        "blockers: " + ("; ".join(s["blockers"]) or "none"),
        "next: " + s["next"]])
