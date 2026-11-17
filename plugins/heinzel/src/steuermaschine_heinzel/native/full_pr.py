"""Supported two-review producer for full-PR Makefile source authorizations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime

from . import admission, release_window
from .admission_lib import focused_admit
from .admission_lib import full_pr as ops
from .admission_lib.hold import admission_hold


class FullPrResult(Model):
    stage: str
    sha256: str
    path: str
    evidence: dict[str, Any] = {}

    def render(self) -> list[str]:
        return [f"{self.stage}: {self.path}", f"sha256 {self.sha256}"]


@native(
    "head full-pr prepare",
    writes=True,
    help=H(
        "Prepare exact reviewed Makefile inputs for ordinary complete PR CI.",
        "Use for a clean feature head after protected main deploys full-pr-reviewed support.",
        "Private immutable source/transaction files, review registration and window holds; no GitHub write.",
        "sm head full-pr prepare WORKTREE --out PACKET_DIR",
        "Independent source review, then prepare --source-approved DIGEST; independently review prepare.out before apply.",
        "First call emits source.json. Record an independent sm review verdict for its digest. Repeat with\n"
        "--source-approved to prepare the exact slot transaction. Neither approval is CI evidence; full PR CI remains required.",
    ),
)
def prepare(
    worktree: Path = typer.Argument(..., help="Owned Core feature worktree."),
    out: Path = typer.Option(..., "--out", help="New private review packet directory."),
    source_approved: str | None = typer.Option(None, "--source-approved", help="Independently approved source packet digest."),
    nonconsumers: Path | None = typer.Option(None, "--nonconsumers", help="Existing authenticated nonconsumer receipts."),
    review_root: Path | None = typer.Option(None, "--review-root", help="Root for nonconsumer review paths."),
) -> FullPrResult:
    rt = runtime()
    if source_approved is not None:
        admission.require_verdict(source_approved)
    try:
        with release_window.held(rt, "prepare", str(out), give_back_on_success=source_approved is None), admission_hold(rt, f"head full-pr prepare {out}"):
            result = ops.prepare(worktree, out, source_approved, nonconsumers, review_root)
            admission.register_prepared(result["sha256"], "head full-pr " + result["stage"])
            return FullPrResult(**result)
    except admission.ERRORS as error:
        raise Refuse(f"full PR authorization refused: {error}", "") from error


@native(
    "head full-pr apply",
    writes=True,
    help=H(
        "Apply one independently reviewed full-PR source authorization.",
        "Use after independent approval of both source.json and prepare.out digests.",
        "One append-only registry slot, private attempt/readback/receipt files; pauses and restores the queue under existing locks.",
        "sm head full-pr apply PACKET_DIR --approved SHA256",
        "Normal pinned push and full original PR CI; this receipt does not attest tests.",
        "Refuses until deployed protected verifier support, exact source/base/tree, capacity and readback all agree.\nAn existing attempt is never replayed or automatically restored.",
    ),
)
def apply(
    packet: Path = typer.Argument(..., help="Prepared private packet directory."),
    approved: str = typer.Option(..., "--approved", help="Independently approved prepare.out digest."),
) -> FullPrResult:
    rt = runtime()
    admission.require_verdict(approved)
    try:
        prepared = ops.read(packet / "prepare.out")
        ops.slots.require(ops.slots.digest(focused_admit.canonical(prepared)) == approved, "approved transaction digest differs")
        admission.require_verdict(prepared["source_sha256"])
        with release_window.held(rt, "apply", str(packet), give_back_on_success=True), admission_hold(rt, f"head full-pr apply {packet}"):
            receipt = ops.apply(packet, approved)
            return FullPrResult(stage="applied", sha256=approved, path=str(packet / "receipt.json"), evidence=receipt)
    except admission.ERRORS as error:
        raise Refuse(f"full PR authorization refused: {error}", "") from error
