"""Collecting the focused plan registry: which entries of the slots may go, and the exact bytes of the result.

Pure functions over strings and dicts; no process, no network, no clock. The registry is five repository variables:
the legacy slot (plain JSON) and four compressed slots (an envelope with the codec `gzip+base64`). An entry is
`pull_request:<head sha>` (a plan for one PR head) or `push-tree:<tree sha>` (the plan for main after that tree merged).

An entry may go when its PR is merged, or closed longer ago than `CLOSED_GRACE_SECONDS` (close and reopen is a standard loop
and must find its plan), for every PR of that head, and its tree is no longer wanted: no open PR and no PR of unknown state
has a plan with that tree, no unreleased release reservation names it, and it is not the tree of main's head (a rerun of
main's CI, or a push just now, reads `push-tree:<that tree>`). A `push-tree`
entry goes with its pull request entries and only then; one without a pull request entry stays (it may be main's own).
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import zlib
from collections.abc import Mapping
from typing import Any

from steuermaschine.hashing import canonical_sha256

NAMES = ("FOCUSED_CI_PLANS", *(f"FOCUSED_CI_PLANS_V2_{i}" for i in range(4)))
LEGACY = NAMES[0]
LIMITS = {LEGACY: 48 * 1024, **{name: 40000 for name in NAMES[1:]}}  # bytes of one variable, as the alarm and `sm status` count
MAX_DECODED = 1024 * 1024
ALARM_PERCENT = 85
SCHEMA = 1
CLOSED_GRACE_SECONDS = 7 * 24 * 3600


class Unreadable(ValueError):
    """The registry (or a slot of it) cannot be decoded; nothing may be collected from it."""


def decode(name: str, value: str) -> dict[str, Any]:
    """The entries of one slot. An empty slot is empty; anything undecodable raises `Unreadable`."""
    if not value:
        return {}
    try:
        text = value
        if name != LEGACY:
            envelope = json.loads(value)
            if not (isinstance(envelope, dict) and envelope.get("codec") == "gzip+base64" and type(envelope.get("bytes")) is int
                    and 0 < envelope["bytes"] <= MAX_DECODED and isinstance(envelope.get("data"), str)):
                raise Unreadable(f"{name}: not a gzip+base64 envelope")
            decoder = zlib.decompressobj(wbits=31)
            raw = decoder.decompress(base64.b64decode(envelope["data"], validate=True), envelope["bytes"] + 1)
            if len(raw) != envelope["bytes"] or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise Unreadable(f"{name}: the envelope does not match its length")
            text = raw.decode()
        parsed = json.loads(text)
    except (ValueError, zlib.error) as error:
        raise Unreadable(f"{name}: undecodable ({type(error).__name__})") from error
    if not isinstance(parsed, dict):
        raise Unreadable(f"{name}: not a JSON object")
    return parsed


def compact(entries: Mapping[str, Any]) -> bytes:
    return json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()


def encode(name: str, entries: Mapping[str, Any]) -> str:
    """The value of one slot for `entries`; deterministic (gzip without a time stamp). An empty slot is the envelope of `{}`
    (or `{}` for the legacy slot), never an empty value: a variable may not be set to nothing."""
    raw = compact(entries)
    if name == LEGACY:
        return raw.decode()
    envelope = {"schema": SCHEMA, "codec": "gzip+base64", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                "data": base64.b64encode(gzip.compress(raw, mtime=0)).decode()}
    return json.dumps(envelope)


def fill_percent(size: int, name: str) -> int:
    return size * 100 // LIMITS[name]


def over_threshold(sizes: Mapping[str, int], percent: int = ALARM_PERCENT) -> list[tuple[str, int]]:
    """The slots whose variable passed `percent` of its limit, fullest first."""
    rows = [(name, fill_percent(int(sizes.get(name, 0)), name)) for name in NAMES]
    return sorted(((name, used) for name, used in rows if used > percent), key=lambda row: -row[1])


def entry_kind(key: str) -> str:
    return "pull_request" if key.startswith("pull_request:") else "push-tree" if key.startswith("push-tree:") else "other"


def classify(slots: Mapping[str, Mapping[str, Any]], prs: Mapping[str, list[tuple[int, str, float | None]]], reserved_trees: set[str],
             main_tree: str = "", now: float = 0.0) -> list[dict[str, Any]]:
    """One row per entry: slot, key, kind, tree, prs (numbers), state, action (collect | keep) and why.

    `prs` maps a head sha to its pull requests as (number, state, closed epoch or None), state in open | merged | closed.
    `main_tree` is the tree of main's head now; `now` the epoch the grace period of a closed pull request runs from.
    """
    rows: list[dict[str, Any]] = []
    for slot in NAMES:
        for key, plan in sorted(slots.get(slot, {}).items()):
            tree = str(plan.get("tree", "")) if isinstance(plan, dict) else ""
            kind = entry_kind(key)
            if kind == "push-tree":
                tree = key.split(":", 1)[1]
            row: dict[str, Any] = {"slot": slot, "key": key, "kind": kind, "tree": tree,
                                   "prs": [], "state": "", "action": "keep", "why": ""}
            if kind == "pull_request":
                found = prs.get(key.split(":", 1)[1], [])
                row["prs"] = sorted(number for number, _, _ in found)
                states = {state for _, state, _ in found}
                row["state"] = "unmatched" if not found else "open" if "open" in states else "merged" if "merged" in states else "closed"
                recent = [c for _, state, c in found if state == "closed" and (c is None or now - c < CLOSED_GRACE_SECONDS)]
                if row["state"] == "closed" and recent:
                    row["state"] = "recent"  # closed within the grace period (or when unknown): it may be reopened
                row["why"] = {"unmatched": "no pull request has this head (updated or unknown)", "open": "its pull request is open",
                                    "recent": "its pull request was closed less than 7 days ago (close and reopen keeps its plan)"}.get(row["state"], "")
            else:
                row["state"] = "main" if row["kind"] == "push-tree" else "unknown"
            rows.append(row)
    wanted = {r["tree"] for r in rows if r["kind"] == "pull_request" and r["state"] in ("open", "unmatched", "recent") and r["tree"]} | set(reserved_trees) | ({main_tree} if main_tree else set())
    wanted_why = {t: "its tree is the tree of main's head" for t in ([main_tree] if main_tree else [])}
    for row in rows:
        if row["kind"] == "pull_request" and row["state"] in ("merged", "closed"):
            if row["tree"] in wanted:
                row["why"] = wanted_why.get(row["tree"], "its tree is still wanted by an open pull request or a release reservation")
            elif not row["tree"]:
                row["why"] = "its plan names no tree"
            else:
                row["action"], row["why"] = "collect", f"pull request {', '.join(map(str, row['prs']))} {row['state']}"
    going = {r["tree"] for r in rows if r["action"] == "collect"}
    for row in rows:
        if row["kind"] == "push-tree":
            if row["tree"] in wanted:
                row["why"] = wanted_why.get(row["tree"], "its tree is still wanted by an open pull request or a release reservation")
            elif row["tree"] in going:
                row["action"], row["why"] = "collect", "the main plan of a collected pull request"
            else:
                row["why"] = "no collected pull request has this tree (may be main's own)"
    return rows


def transaction(repository: str, values: Mapping[str, str], rows: list[dict[str, Any]], main: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The exact change: per slot the keys removed and the sha256 of the value before and after.

    Raises `Unreadable` when a slot cannot be decoded. The digest of this document is what a reviewer approves; it binds main's sha and tree."""
    removed: dict[str, list[str]] = {}
    for row in rows:
        if row["action"] == "collect":
            removed.setdefault(row["slot"], []).append(row["key"])
    before = {name: hashlib.sha256(values.get(name, "").encode()).hexdigest() for name in NAMES}
    after = dict(before)
    for slot, keys in removed.items():
        left = {k: v for k, v in decode(slot, values.get(slot, "")).items() if k not in keys}
        after[slot] = hashlib.sha256(encode(slot, left).encode()).hexdigest()
    return {"schema": SCHEMA, "repository": repository, "before": before, "remove": {s: sorted(k) for s, k in sorted(removed.items())}, "after": after,
            "main": dict(main or {})}


def digest(tx: Mapping[str, Any]) -> str:
    return canonical_sha256(json.dumps(tx))


def new_values(values: Mapping[str, str], tx: Mapping[str, Any]) -> dict[str, str]:
    """The values to write: only the slots with removals, each rebuilt from its current entries."""
    out: dict[str, str] = {}
    for slot, keys in tx["remove"].items():
        entries = decode(slot, values.get(slot, ""))
        missing = [key for key in keys if key not in entries]
        if missing:
            raise Unreadable(f"{slot}: {len(missing)} entry/entries to remove are not there any more")
        out[slot] = encode(slot, {k: v for k, v in entries.items() if k not in keys})
    return out
