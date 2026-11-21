"""Typed entry of the release-main route: what `sm release admit prepare|apply` call.

The checks are those of the release branch of the operator copy's `main` (kept as `release_slots.main`, the argv form
for the moved tests). Added here, as for the PR admission: prepare stores its output and arguments next to the packet;
apply takes only the approved digest and verifies the stored arguments against the reviewed transaction first.
"""
# mypy: disable-error-code="no-untyped-call,attr-defined"
from __future__ import annotations

import json
import os
import shutil
import site
import sys
from pathlib import Path
from typing import Any

from steuermaschine.models import Model

from ... import environment  # moved: the process context is a typed model
from ..admission_lib import ops as admission
from ..admission_lib import wait
from . import release_slots as rs

a = rs.a
HOST = "github.com"
# what the former entry turned into a refusal (exit 1) instead of a traceback
ERRORS = (a.AdmissionError, OSError, ValueError, KeyError, TypeError, rs.subprocess.SubprocessError)


class ReleasePrepareResult(Model):
    sha256: str
    version: str
    head: str
    main: str
    slot: str
    recovery: bool
    prepared_path: str
    args_path: str
    prepared: dict[str, Any]

    def render(self) -> list[str]:
        kind = "paused recovery" if self.recovery else "fresh release"
        return [f"prepared {self.prepared_path}", f"sha256 {self.sha256}",
                f"release v{self.version} ({kind}) head {self.head[:8]} main {self.main[:8]} slot {self.slot}",
                "a reviewer who is not the author approves exactly this sha256; then: sm release admit apply PACKET --approved SHA256"]


def isolated_startup() -> bool:
    """The operator copy required `python3 -I` on its reviewed entry point: no code from PYTHONPATH, the user site or the
    working directory. A command of an installed `sm` is not started with -I; it is accepted when the same holds."""
    if sys.flags.isolated:
        return True
    cwd = os.getcwd()
    return (not environment.interpreter().loads_foreign_code()
            and not site.ENABLE_USER_SITE and "" not in sys.path and cwd not in sys.path)


def _runtime(core_repo: Path, runtime: Any) -> Any:
    rs.require(isolated_startup(), "release-main-only requires an isolated start: no PYTHONPATH, user site or working directory on the import path")
    return runtime or rs.Runtime(core_repo, github_host=HOST)


def _cleanup(source: Path | None, workspace: Path | None) -> None:
    for path in (source, workspace):
        if path is not None:
            shutil.rmtree(path, ignore_errors=True)


def prepare(packet: Path, core_repo: Path, review_root: Path, *, nonconsumers: Path | None = None, runtime: Any = None) -> ReleasePrepareResult:
    """Prepare the exact release-main transaction; stores it and its arguments next to the packet. Writes no variable."""
    packet, core_repo, review_root = Path(packet), Path(core_repo), Path(review_root)
    source = workspace = None
    try:
        receipts = admission._receipts(nonconsumers)
        rt = _runtime(core_repo, runtime)
        prepared, _packet, source, workspace = rs.prepare_release(rt, packet, review_root, nonconsumers=receipts)
    finally:
        _cleanup(source, workspace)
    sha = admission.canonical_sha256(prepared)
    target, args_path = packet.parent / admission.PREPARED_FILE, packet.parent / admission.ARGS_FILE
    admission._atomic(target, a.canonical(prepared).decode() + "\n")
    admission._atomic(args_path, json.dumps({
        "schema": 1, "route": rs.release.MODE, "sha256": sha, "packet": str(packet.resolve()), "core_repo": str(core_repo.resolve()),
        "review_root": str(review_root.resolve()), "nonconsumers": None if nonconsumers is None else str(Path(nonconsumers).resolve())},
        indent=1, sort_keys=True) + "\n")
    proof = prepared["release_main"]
    return ReleasePrepareResult(sha256=sha, version=proof["version"], head=prepared["head"], main=prepared["main"], slot=prepared["slot"],
                                recovery=proof["guard"]["recovery"] is not None, prepared_path=str(target), args_path=str(args_path), prepared=prepared)


def check_stored_args(args: dict[str, Any], approved_doc: dict[str, Any], approved: str) -> None:
    """The stored arguments are not trusted: each must agree with the reviewed transaction before anything runs."""
    rs.require(args.get("route") == rs.release.MODE and (approved_doc.get("release_main") or {}).get("mode") == rs.release.MODE,
               "this is not a release-main transaction; there is no PR admission any more")
    rs.require(args.get("sha256") == approved, "stored prepare arguments belong to another prepared transaction")
    rs.require(args.get("core_repo") == approved_doc.get("core_repo"), "stored prepare arguments differ from the reviewed transaction: core checkout")
    rs.require(rs.digest(a._stable_file(Path(args["packet"]), a.MAX_PACKET_BYTES)) == approved_doc.get("packet_sha256"),
               "stored prepare arguments differ from the reviewed transaction: packet bytes")
    nonconsumers = Path(args["nonconsumers"]) if args.get("nonconsumers") else None
    rs.require(admission._receipts(nonconsumers) == approved_doc.get("nonconsumers", []),
               "stored prepare arguments differ from the reviewed transaction: nonconsumer receipts")


def apply(packet: Path, approved: str, *, runtime: Any = None) -> admission.ApplyResult:
    """The one registry write of the release route: recompute, require byte equality with the reviewed transaction, apply."""
    packet = Path(packet)
    rs.require(isinstance(approved, str) and a.HEX.fullmatch(approved) is not None, "apply needs --approved with the sha256 the reviewer approved")
    args = admission.stored_args(packet)
    approved_doc = a._json_bytes(a._stable_file(packet.parent / admission.PREPARED_FILE, 1024 * 1024), "prepared transaction")
    rs.require(admission.canonical_sha256(approved_doc) == approved,
               "the approved sha256 is not the digest of the stored prepared transaction; reprepare and review")
    check_stored_args(args, approved_doc, approved)
    core_repo, review_root = Path(args["core_repo"]), Path(args["review_root"])
    source = workspace = None
    try:
        receipts = admission._receipts(Path(args["nonconsumers"]) if args.get("nonconsumers") else None)
        rt = _runtime(core_repo, runtime)
        prepared, packet_doc, source, workspace = rs.prepare_release(rt, packet, review_root, nonconsumers=receipts)
        rs.require(a.canonical(approved_doc) == a.canonical(prepared), "prepared transaction changed; reprepare and review")
        receipt = wait.tracked(rt, lambda: rs.apply_release(rt, prepared, packet_doc, source, approved, review_root))
    finally:
        _cleanup(source, workspace)
    return admission.ApplyResult(receipt_path=str(receipt), sha256=approved, head=prepared["head"])
