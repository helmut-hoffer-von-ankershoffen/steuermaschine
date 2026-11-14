"""State files of the scripted era, adopted READ-ONLY: the launchd loops still write them. One function per file.
Nothing here writes. `root` is the setting `legacy_state_dir`.

Also here: `sweep`, which renames the old tool names inside a sentence taken from such a file (an alarm text written
by a loop) to the commands of the tool map, so nothing a coordinator reads names a script.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from pathlib import Path

ENDED = ("done", "cleared")
VERSION_FILE = re.compile(r"v\d+\.\d+\.\d+\.json")


def _json(path: Path):
    return json.loads(path.read_text())


def _json_or(path: Path, default=None):
    try:
        return _json(path)
    except (OSError, ValueError):
        return default


def version_key(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text)[:3])


# ------------------------------------------------------------------ the merge queue
def queue_state(root: Path) -> dict:
    """hz-queue.json with every key the queue loop writes, defaults filled in. Unreadable: an empty queue."""
    data = _json_or(root / "queue.json", {})
    data = data if isinstance(data, dict) else {}
    return {"items": data.get("items", []), "last_batch": data.get("last_batch", 0), "blocked": data.get("blocked", ""),
            "batch": data.get("batch", {}), "red_batch": data.get("red_batch", {}), "main_failure": data.get("main_failure", {}),
            "main_reruns": data.get("main_reruns", {}), "main_restored": data.get("main_restored", {}),
            "main_rerun_last": data.get("main_rerun_last", ""), "readable": bool(data) or (root / "queue.json").exists()}


def queue_log_tail(root: Path, lines: int = 40) -> list[str]:
    """The last lines of hz-queue.log (what the loop printed)."""
    try:
        with (root / "queue.log").open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(handle.tell() - 16384, 0))
            return handle.read().decode(errors="replace").splitlines()[-lines:]
    except OSError:
        return []


def deferred_runs(root: Path) -> set[str]:
    try:
        return {line.split()[0] for line in (root / "ci-deferred.txt").read_text().splitlines() if line.split()}
    except OSError:
        return set()


# ------------------------------------------------------------------ holds and the freeze
def release_holds(root: Path) -> list[str]:
    try:
        return sorted(p.name for p in (root / "release-holds").iterdir())
    except OSError:
        return []


def release_holds_present(root: Path) -> bool:
    """Unknown, unreadable or malformed hold directories block merging just like an owned hold."""
    holds = root / "release-holds"
    try:
        mode = holds.lstat().st_mode
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if not stat.S_ISDIR(mode):
        return True
    try:
        return any(holds.iterdir())
    except OSError:
        return True


def incident_frozen(root: Path) -> bool:
    """The shared incident sentinel, including dangling links and unreadable state."""
    try:
        (root / "merge-freeze").lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def merge_frozen(root: Path) -> bool:
    return incident_frozen(root) or release_holds_present(root)


def incident_since(root: Path) -> float | None:
    try:
        return (root / "merge-freeze").lstat().st_mtime
    except OSError:
        return None


def hold_files(root: Path) -> list[str]:
    """Names of every hold file: the incident freeze, the gate hold and the release holds."""
    return [p.name for p in [*root.glob("merge-freeze"), *root.glob("gate.hold"), *(root / "release-holds").glob("*")]]


def gate_hold_since(root: Path) -> float | None:
    try:
        return (root / "gate.hold").stat().st_mtime
    except OSError:
        return None


def pr_actions(path: Path | None) -> dict:
    """The reviewed PR action table (TOML): {key: {action, owner, hold}}. Raises OSError/ValueError when unreadable."""
    if path is None:
        return {}
    return tomllib.loads(path.read_text())


# ------------------------------------------------------------------ releases and the train
def release_receipt(root: Path) -> dict | None:
    """hz-release.json: the receipt of the release in flight, before its handoff. Raises ValueError when malformed."""
    path = root / "release.json"
    return _json(path) if path.exists() else None


def release_receipt_changed(root: Path) -> float | None:
    try:
        return (root / "release.json").stat().st_mtime
    except OSError:
        return None


def release_tails(root: Path) -> list[tuple[str, dict]]:
    """hz-release/vX.Y.Z.json, oldest version first. An unreadable one raises (nothing is guessed)."""
    tails = root / "release"
    out = [(p.stem[1:], _json(p)) for p in (tails.glob("v*.json") if tails.is_dir() else []) if VERSION_FILE.fullmatch(p.name)]
    return sorted(out, key=lambda item: version_key(item[0]))


def accepted_versions(root: Path) -> list[str]:
    out = []
    for path in (root / "release").glob("v*.json"):
        data = _json_or(path, {})
        if isinstance(data, dict) and data.get("finished") and data.get("accepted"):
            out.append(path.stem.removeprefix("v"))
    return out


def dropped_candidates(root: Path) -> list[str]:
    return sorted(p.name.split("dropped-", 1)[1] for p in root.glob("release.json.dropped-*"))


def train_states(root: Path) -> tuple[dict | None, list[dict]]:
    """(unmigrated state.json or None, the per-version train states oldest first) from hz-train/."""
    train = root / "train"
    legacy = _json(train / "state.json") if (train / "state.json").exists() else None
    out = []
    for path in sorted(train.glob("v*.json"), key=lambda p: version_key(p.stem) or (1 << 30,)):
        if VERSION_FILE.fullmatch(path.name):
            out.append(_json(path))
    return legacy, out


def active_trains(root: Path) -> list[dict]:
    legacy, trains = train_states(root)
    if legacy is not None:
        return [legacy] if legacy.get("version") and legacy.get("phase", "idle") not in (*ENDED, "idle") else []
    return [t for t in trains if t.get("phase") not in ENDED]


def rehearsal_state(root: Path) -> dict | None:
    """hz-rehearse.json: `current` (an open rehearsal) and `last` (verdict, failures). None when there is none."""
    path = root / "rehearse.json"
    if not path.exists():
        return None
    data = _json(path)
    if not isinstance(data, dict):
        raise ValueError("the rehearsal state is not an object")
    return data


# ------------------------------------------------------------------ alarms, focus, admission
def alarm_state(root: Path) -> dict:
    """hz-alarm.json: key -> {cls, text, acked, first, ...}. Missing: no alarms. Raises ValueError when malformed."""
    path = root / "alarm.json"
    if not path.exists():
        return {}
    data = _json(path)
    if not isinstance(data, dict):
        raise ValueError("the alarm state is not an object")
    return data


def focus_entries(root: Path) -> list[dict]:
    """hz-go-focus.json: the declared focus heads. Raises ValueError when malformed."""
    path = root / "go-focus.json"
    if not path.exists():
        return []
    data = _json(path)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError("the focus file is not {entries: [...]}")
    return [e for e in data["entries"] if isinstance(e, dict) and e.get("worktree")]


def admission_receipt_heads(root: Path) -> list[str]:
    """Heads with a focused admission receipt (hz-focused-admission-receipts/head-<sha>-*.json). Raises OSError when
    the directory is missing."""
    names = os.listdir(root / "focused-admission-receipts")
    return sorted({m[1] for n in names if (m := re.match(r"head-([0-9a-f]{40})-", n))})


def slot_sizes_cache(root: Path) -> dict | None:
    """hz-status-slots.json: {at, sizes} as the hourly table cached it."""
    data = _json_or(root / "status-slots.json")
    return data if isinstance(data, dict) and isinstance(data.get("sizes"), dict) else None


def next_claims(root: Path, now: float) -> dict:
    """hz-next-claims.json: {key: {owner, until, at}}, only the live claims (the next-actions tool wrote them)."""
    data = _json_or(root / "next-claims.json", {})
    data = data if isinstance(data, dict) else {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get("until"), int | float) and v["until"] > now}


def next_last_update(root: Path) -> float:
    """hz-next.json: when the last hourly owner update was recorded (0: never, or unreadable)."""
    data = _json_or(root / "next.json", {})
    value = data.get("last_update", 0) if isinstance(data, dict) else 0
    return float(value) if isinstance(value, int | float) else 0.0


# ------------------------------------------------------------------ sentences from these files
def sweep(text: str, rows: list) -> str:
    """Rename every old tool in a sentence to the command the tool map gives it (`rows`: its ToolMapRows).
    A tool followed by a word that ends one of its commands takes that command; a file name is left alone."""
    by_tool = {r.tool: r for r in rows}
    if not text or not by_tool:
        return text
    prefix = re.escape(next(iter(by_tool)).split("-")[0])
    token = re.compile(rf"(?<![\w./-])(?:[~/][^\s'\"`]*/)?(?P<tool>{prefix}-[a-z0-9]+(?:-[a-z0-9]+)*)(?![\w.-])(?P<word> [a-z][a-z-]*)?")

    def swap(m: re.Match) -> str:
        row, word = by_tool.get(m["tool"]), (m["word"] or "").strip()
        if row is None or not row.path:
            return m[0] if row is None else f"the {m['tool'].split('-', 1)[1]} step" + (f" {word}" if word else "")
        paths = [row.path, *row.also]
        hit = next((p for p in paths if word and p.split()[-1] == word), None)
        return f"sm {hit}" if hit else f"sm {row.path}" + (f" {word}" if word else "")

    return token.sub(swap, text)
