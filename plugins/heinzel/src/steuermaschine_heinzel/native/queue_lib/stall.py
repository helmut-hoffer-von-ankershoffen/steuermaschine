"""Why a queued PR does not merge (stall diagnosis) and the alarm state the queue raises and clears."""

from __future__ import annotations
import contextlib
import datetime
import fcntl
import glob
import json
import os
import subprocess
import time
from pathlib import Path
from . import edge  # moved: records, processes and what used to be separate scripts
from ... import state as statedir  # the plugin's state paths
REQUIRED = ("CI OK", "merge-gate", "coverage-floor")  # contexts main requires; the caller drops what the rules do not demand
GREEN = ("SUCCESS", "SKIPPED", "NEUTRAL")
STALL_MIN = 15
QUEUE_GATES = ("merge freeze", "main's latest run is not green", "main CI evidence is unknown", "batch cadence", "release")
TRAP = "see RCA trap: checks attached to another suite"


# ---------- pure rules ----------

def alarm(cls: str, key: str, text: str) -> dict:
    return {"cls": cls, "key": key, "text": " ".join(str(text).split())}


def stamp(value) -> float | None:
    """Epoch seconds of a GitHub ISO time; None when absent or unreadable."""
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def minutes(since: float | None, now: float) -> int:
    return max(int((now - since) / 60), 0) if since else 0


def check_name(check: dict) -> str:
    return check.get("name") or check.get("context") or "?"


def check_result(check: dict) -> str:
    """One word per rollup entry: check runs carry status/conclusion, commit statuses carry state."""
    if "state" in check and "status" not in check:
        return {"SUCCESS": "SUCCESS", "PENDING": "PENDING", "EXPECTED": "PENDING"}.get(check["state"], check["state"] or "PENDING")
    if check.get("status") != "COMPLETED":
        return "PENDING"
    return check.get("conclusion") or "PENDING"


def green(rollup: list[dict]) -> bool:
    """Every check complete and green; an empty rollup is not green."""
    return bool(rollup) and all(check_result(c) in GREEN for c in rollup)


def green_since(rollup: list[dict]) -> float | None:
    times = [t for t in (stamp(c.get("completedAt")) for c in rollup) if t]
    return max(times) if times else None


def required_missing(required, rollup: list[dict]) -> list[str]:
    """Required contexts with no entry at all in the PR rollup (posted to another check suite of the same SHA)."""
    present = {check_name(c) for c in rollup}
    return [name for name in required if name not in present]


def required_not_green(required, rollup: list[dict]) -> list[str]:
    results = {check_name(c): check_result(c) for c in rollup}
    return [f"{name}={results[name]}" for name in required if name in results and results[name] != "SUCCESS"]


def wait_reason(state: str | None, rollup: list[dict], required, since: float | None, now: float, everything: list[dict] | None = None) -> str:
    """Why GitHub will not merge a PR whose checks say nothing is wrong. '' when it is not that case. `rollup` holds
    the checks that gate (the caller dropped advisory ones); `everything` is the whole rollup, when it differs."""
    if state != "BLOCKED" or not green(rollup):
        return ""
    everything = rollup if everything is None else everything
    head = f"BLOCKED with green checks for {minutes(since, now)} min: "
    if missing := required_missing(required, everything):
        return head + f"required check contexts missing from the PR rollup: {', '.join(missing)}; {TRAP}"
    if weak := required_not_green(required, everything):
        return head + f"required checks present but not SUCCESS: {', '.join(weak)}"
    return head + "every required context is green in the rollup; look at reviews, conversations and branch rules"


def pending_reason(state: str | None, rollup: list[dict]) -> str:
    """The plain reasons: checks still running, or a merge state the queue waits on."""
    waiting = sorted({check_name(c) for c in rollup if check_result(c) == "PENDING"})
    if waiting:
        return "checks pending: " + ", ".join(waiting[:5]) + (f" (+{len(waiting) - 5})" if len(waiting) > 5 else "")
    if not rollup:
        return "no checks reported on the head yet"
    return f"GitHub mergeStateStatus {state or 'unknown'}"


def stall_alarm(key: str, state: str | None, rollup: list[dict], required, since: float | None, now: float,
                queue_block: str = "", limit: int = STALL_MIN, everything: list[dict] | None = None) -> dict | None:
    """A queued PR whose checks are all complete and green but which is unmerged for more than `limit` minutes.
    ALERT unless the queue itself recorded a gate (freeze, red main, cadence): then WARN, the gate has its own alarm."""
    if not green(rollup):
        return None
    age = minutes(since, now)
    if age <= limit:
        return None
    shown = key if not key.isdigit() else f"#{key}"
    reason = wait_reason(state, rollup, required, since, now, everything)
    if reason:
        return alarm("ALERT", f"stall:{key}", f"STALL {shown} {reason}; sm queue why")
    if queue_block and any(gate in queue_block for gate in QUEUE_GATES):
        return alarm("WARN", f"stall:{key}", f"STALL {shown} green and unmerged for {age} min, queue waits: {queue_block}")
    return alarm("ALERT", f"stall:{key}", f"STALL {shown} checks complete and green, {state or 'state unknown'}, unmerged for {age} min"
                 f" and the queue records {'`' + queue_block + '`' if queue_block else 'no block'}; sm queue why")


# ---------- de-duplication and the owner push ----------

def state_path() -> Path:
    return Path(edge.setting("alarm_state_file") or statedir.path("alarm.json"))


def load() -> dict:
    try:
        data = json.loads(state_path().read_text())
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def save(data: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    tmp.replace(path)


@contextlib.contextmanager
def locked():
    """One writer at a time: hz-queue and the watch both rewrite the state, and a lost update would push twice."""
    path = state_path().with_suffix(".lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a")
    except OSError:
        yield
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        handle.close()


def quiet_hours(now: float) -> bool:
    try:
        from zoneinfo import ZoneInfo
        hour = datetime.datetime.fromtimestamp(now, ZoneInfo("Europe/Berlin")).hour
    except Exception:  # no tz database: local time
        hour = time.localtime(now).tm_hour
    return hour >= 23 or hour < 7


def notifier() -> str:
    """The owner push command, the same route as hz-train; '' when none is configured."""
    chosen = edge.setting("alarm_notify")
    if chosen == "off":
        return ""
    return chosen or edge.setting("release_train_notify") or next(iter(sorted(glob.glob(str(
        Path.home() / ".claude/plugins/synced/*/telegram-bridge/skills/telegram-notify/scripts/send.sh")))), "")


def push(text: str) -> bool:
    command = notifier()
    if not command:
        return False
    try:
        return edge.run([command, text], capture_output=True, check=False, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def renotify_seconds() -> float:
    return float(edge.setting("alarm_renotify_minutes") or 120) * 60


def process(alarms: list[dict], source: str, now: float | None = None, notify=push, complete: bool = True) -> list[dict]:
    """Record `alarms` for `source`, drop this source's alarms that no longer hold (when `complete`), push each new
    ALERT to the owner once. Returns every alarm of this call with `first`, `acked` and `pushed` filled in."""
    now = time.time() if now is None else now
    with locked():
        return _process(alarms, source, now, notify, complete)


def _process(alarms: list[dict], source: str, now: float, notify, complete: bool) -> list[dict]:
    data = load()
    seen = set()
    out = []
    for item in alarms:
        key = item["key"]
        seen.add(key)
        entry = data.get(key) or {"first": now, "sources": [], "notified": 0, "acked": False}
        entry.update(cls=item["cls"], text=item["text"], last=now, sources=sorted(set(entry.get("sources", [])) | {source}))
        pushed = False
        due = not entry.get("notified") or now - entry["notified"] >= renotify_seconds()
        if item["cls"] == "ALERT" and not entry.get("acked") and due and not quiet_hours(now):
            pushed = bool(notify(f"ALERT {item['text']}"))
            if pushed:
                entry["notified"] = now
        data[key] = entry
        out.append({**item, "first": entry["first"], "acked": bool(entry.get("acked")), "pushed": pushed, "new": entry["first"] == now})
    if complete:
        for key in [k for k, e in data.items() if source in e.get("sources", []) and k not in seen]:
            data[key]["sources"] = [s for s in data[key]["sources"] if s != source]
            if not data[key]["sources"]:
                del data[key]
    save(data)
    return out


def clear(key: str, source: str | None = None) -> bool:
    """Drop one alarm (for `source` only when given). True when something changed."""
    with locked():
        return _clear(key, source)


def _clear(key: str, source: str | None) -> bool:
    data = load()
    if key not in data:
        return False
    if source is None:
        del data[key]
    else:
        left = [s for s in data[key].get("sources", []) if s != source]
        if left == data[key].get("sources", []):
            return False
        if left:
            data[key]["sources"] = left
        else:
            del data[key]
    save(data)
    return True
