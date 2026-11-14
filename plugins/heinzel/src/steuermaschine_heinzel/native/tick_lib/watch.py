"""The deadline watch: what a tick must look at beyond `go` (a crash report only if new, UNREACHABLE owners, no
re-listed reviewed PRs, the noise rules).

Moved from the former watch module; the functions are pure or read files they are given. What changed:
- the acknowledgement times come from the caller (the state store, and the former marker files read-only), so
  `scan_crashes` takes a time and `live_uv_warning` an extra `known` time;
- `starved_age` reads GitHub through the one `Gh` client and takes the clock;
- no function here raises alarms, notifies, or starts the anomaly digest: the caller hands the digest lines in.

Duplicate until its slice lands: `queue_age_min` (the push gate's, slice heinzel-native-queue-release).
"""

from __future__ import annotations

import glob
import os
import re
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from steuermaschine.github import Gh


def new_crashes(files: list[tuple[str, float]], ack_ts: float) -> list[str]:
    """Only .ips/.crash files newer than the last ack count; other diagnostics (.diag etc.) never."""
    return sorted(f for f, m in files if f.endswith((".ips", ".crash")) and m > ack_ts)


def scan_crashes(diag: str, ack_ts: float) -> list[str]:
    ts = ack_ts
    fl = [(os.path.basename(p), os.path.getmtime(p)) for p in glob.glob(os.path.join(diag, "*"))]
    return new_crashes(fl, ts)


def live_ids(text: str) -> set[str]:
    return set(re.findall(r"\ba[0-9a-f]{16}\b", text))


def reachable(owner: str | None, ids_text: str) -> bool:
    """Owner is reachable if it is a live agent id listed in agent-ids.md; topic labels are not addressable."""
    return bool(re.fullmatch(r"a[0-9a-f]{16}", owner or "")) and owner in live_ids(ids_text)


def reviewed(n: str | int, review_files: list[str], queued: set[int]) -> bool:
    pat = re.compile(rf"(?<!\d){n}(?!\d)")
    return int(n) in queued or any(pat.search(os.path.basename(f)) for f in review_files)


def filter_lines(lines: list[str], review_files: list[str], queued: set[int], ids_text: str) -> list[str]:
    out = []
    for l in lines:  # noqa: E741
        m = re.match(r"(?:\w+(?:\([^)]*\))?\s+)?#(\d+)", l)
        if m and ("NEEDS_REVIEW" in l or "FLAG" in l) and reviewed(m.group(1), review_files, queued):
            continue
        o = re.search(r"\[owner: ([^\],]+)", l)
        if o and not reachable(o.group(1).strip(), ids_text):
            l += "  UNREACHABLE (do not SendMessage; route via orchestrator)"  # noqa: E741
        out.append(l)
    return out


def load_cfg(path: str | None) -> dict[str, Any]:
    d = {"action_prefixes": [], "ignore_prefixes": [], "starved_min": 15, "overdue_beads": [], "owned_prs": []}
    if tomllib and path and os.path.exists(path):
        d.update(tomllib.load(open(path, "rb")))  # noqa: SIM115
    return d


def pr_actions_holds(path: str | None) -> set[int]:
    """PR numbers marked `hold = true` in the PR action table: known, parked, never an ACTION."""
    if not (tomllib and path and os.path.exists(path)):
        return set()
    return {int(k) for k, v in tomllib.load(open(path, "rb")).items() if k.isdigit() and isinstance(v, dict) and v.get("hold")}


def summarize(lines: list[str], cfg: dict[str, Any], starved_age: int | None = None) -> tuple[list[str], int]:
    """Digest lines -> (ACTION lines or ['OK'], info count). ACTION only for NEEDS_REVIEW/FLAG (filter_lines already removed reviewed and
    queued PRs), STALE with a live agent, READY to queue, P0, OWNER_MSG. STALE/FLAG whose owner is UNREACHABLE is INFO (counted, not printed).
    Ignored prefixes, owned/held PRs and 'starved' lines (unless a main/release run waits >= starved_min NOW) are dropped.
    Legacy cancellation hints become one INFO line; only exact triage can establish their cause."""
    out, info = [], 0
    cancelled, cancellation_hint = set(), False
    owned = set(cfg["owned_prs"])
    for l in lines:  # noqa: E741
        cancellation = re.fullmatch(r"RERUN #(\d+) \(cancelled, not failed\)", l)
        if cancellation or l.startswith("RERUN command for all of them:"):
            cancellation_hint = True
            if cancellation:
                cancelled.add(int(cancellation.group(1)))
            continue
        if any(l.startswith(p) for p in cfg["ignore_prefixes"]):
            continue
        if "starved" in l and (starved_age is None or starved_age < cfg["starved_min"]):
            continue
        m = re.match(r"(?:\w+(?:\([^)]*\))?\s+)?#(\d+)", l)
        if m and int(m.group(1)) in owned:
            continue
        if not (l.startswith("P0") or any(l.startswith(p) for p in cfg["action_prefixes"])):
            continue
        if "UNREACHABLE" in l and l.startswith(("STALE", "FLAG")):
            info += 1
            continue
        out.append("ACTION: " + l)
    if cancellation_hint:
        prs = " for " + " ".join(f"#{number}" for number in sorted(cancelled)) if cancelled else ""
        out.append(f"INFO: legacy cancellation hints{prs}; use sm ci triage deadline for exact-head status and cancellation evidence")
    return out or ["OK"], info


def queue_age_min(created_at_list: list[str], now: datetime | None = None) -> int | None:
    """Minutes the oldest queued main CI run has waited (None when nothing is queued). Timestamps are gh ISO strings."""
    if not created_at_list:
        return None
    now = now or datetime.now(UTC)
    oldest = min(datetime.strptime(c, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) for c in created_at_list)
    return int((now - oldest).total_seconds() // 60)


def starved_age(commands: Any, clock: Any, repo: str) -> int | None:
    try:
        r = Gh(commands, timeout=60, clock=clock).run(["run", "list", "-R", repo, "--status", "queued", "--limit", "50", "--json", "createdAt,headBranch"])
        import json
        runs = [x["createdAt"] for x in json.loads(r.out or "[]") if x["headBranch"] == "main" or x["headBranch"].startswith("release/")]
        return queue_age_min(runs, datetime.fromtimestamp(clock.now(), UTC))
    except Exception:
        return None


def live_uv_warning(tool: str, ack: str, allow_log: str, slack: float = 60, known: float = 0.0) -> str | None:
    """Bead ppww: WARN when the live heinzel tool venv changed after the last acknowledgement (`sm alarm ack tick:venv`, or
    the former marker file `ack`) and the last allowed use (`allow_log`). The product writes no update record of its
    own, so the acknowledgement is the evidence."""
    try:
        changed = os.path.getmtime(tool)
    except OSError:
        return None
    for f in (ack, allow_log):
        try:  # noqa: SIM105
            known = max(known, os.path.getmtime(f))
        except OSError:
            pass
    if changed > known + slack:
        return (f"WARN: live heinzel tool venv changed {time.strftime('%m-%d %H:%M', time.localtime(changed))}, not acknowledged "
                "(unisolated uv tool install? bead ppww; verify web and plugins, then sm alarm ack tick:venv)")
    return None


def crash_line(crashes: list[str]) -> str:
    return f"P0 NEW CRASH {', '.join(crashes[:3])} (investigate, then sm alarm ack tick:crash)"


def review_files(research: Path) -> list[str]:
    return glob.glob(os.path.join(str(research), "review-*.md"))


def agent_ids(research: Path) -> str:
    path = os.path.join(str(research), "agent-ids.md")
    return open(path).read() if os.path.exists(path) else ""
