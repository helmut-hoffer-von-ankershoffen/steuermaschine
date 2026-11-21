"""Typed entry of the focused admission: what the native commands call.

Single-packet path of the former `prepare` / `apply` / `blockers` operations plus the canonical digest. The checks
and their order are those of `focused_slots.main` (kept there as the argv form for the moved tests and for parity);
this module adds only: prepare stores its output and its own arguments next to the packet, apply needs only the
approved digest, and apply refuses with a fix when the worktree is not at the reviewed head.

Nothing here writes outside this machine except `apply`, through `focused_slots.apply_prepared`.
"""

# mypy: disable-error-code="no-untyped-call"
# (the moved focused_* modules are untyped, as at their source; this module is the typed layer over them)
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from steuermaschine.models import Model as _Model

from . import focused_admit as a
from . import focused_slots as s

FIRST_MERGE_ONLY = ("RULE: these plans bind main as it is now and are valid for the FIRST merge only. After one member merged, every other "
                    "member needs a new head (`sm ship`) and its own admission at the new main; a second merge on these plans turns main red.")
ARGS_FILE = "prepare.args.json"
PREPARED_FILE = "prepare.out"


class HashResult(_Model):
    path: str
    sha256: str

    def render(self) -> list[str]:
        return [self.sha256]






class BlockingRun(_Model):
    """One active uncovered run, as the wait names it."""

    id: int
    status: str | None = None
    event: str | None = None
    head_branch: str | None = None
    path: str | None = None
    head_sha: str | None = None








class NonconsumerResult(_Model):
    receipts: list[dict[str, Any]]
    excluded: list[dict[str, Any]]

    def render(self) -> list[str]:
        return [json.dumps(self.model_dump(), indent=1, sort_keys=True),
                "Independent review required; prepare and apply revalidate these receipts."]


class ApplyResult(_Model):
    receipt_path: str
    sha256: str
    head: str

    def render(self) -> list[str]:
        return [self.receipt_path]


def canonical_sha256(value: Any) -> str:
    """THE digest a reviewer approves: sha256 of canonical JSON (sorted keys, compact separators, ASCII-escaped, no newline)."""
    return hashlib.sha256(a.canonical(value)).hexdigest()  # = focused_slots.digest(focused_admit.canonical(value))


def hash_file(path: Path) -> HashResult:
    parsed = a._json_bytes(a._stable_file(Path(path), 1024 * 1024), "prepared transaction")
    return HashResult(path=str(path), sha256=canonical_sha256(parsed))


def _receipts(nonconsumers: Path | None) -> list[dict[str, Any]]:
    if nonconsumers is None:
        return []
    receipts: list[dict[str, Any]] = a._json_bytes(a._stable_file(Path(nonconsumers), a.MAX_REVIEW_BYTES), "nonconsumers")["receipts"]
    return receipts
















def _atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def stored_args(packet: Path) -> dict[str, Any]:
    path = Path(packet).parent / ARGS_FILE
    s.require(path.is_file(), f"no {ARGS_FILE} next to the packet: this packet was never prepared")
    args = a._json_bytes(a._stable_file(path, 1024 * 1024), "stored prepare arguments")
    s.require(args.get("schema") == 1 and args.get("packet") == str(Path(packet).resolve()), "stored prepare arguments belong to another packet")
    return args








# ----------------------------------------------------------------------------- batch: many reviewed pairs, one transaction


def nonconsumer_receipts(core_repo: Path, *, runtime: Any = None) -> NonconsumerResult:
    rt = runtime or s.Runtime(core_repo)
    output, excluded = s.stuck_queued_receipts(rt, set())
    return NonconsumerResult(receipts=output["receipts"], excluded=excluded)
