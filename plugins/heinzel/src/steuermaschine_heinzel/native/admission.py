"""Native admission commands: `sm head hash|blockers|prepare|apply` (single-packet path).

Registered with the core's native registry (`steuermaschine.native`). `head prepare` reads GitHub and writes two
local files next to the packet; it sends no cancel probe (apply does, before the write). `head apply` performs the
one registry write and is unavailable until reviews/head-apply.md carries the verdict.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, NoReturn

import typer
from pydantic import BaseModel, Field

from steuermaschine import USAGE, verdicts
from steuermaschine import go as _go
from steuermaschine.engine import Refuse, Wait
from steuermaschine.helpdoc import H
from steuermaschine.native import native, runtime

from .admission_lib import focused_admit as _a
from .admission_lib import ops, wait
from .admission_lib.hold import admission_hold


class AdmissionSettings(BaseModel):
    """Settings of the admission area (mixin of the plugin's settings model)."""

    admission_wait_max_minutes: float = Field(45.0, description="How long `sm head prepare --wait` waits for a window without uncovered active runs before it exits 75.")
    admission_wait_poll_seconds: float = Field(90.0, description="Seconds between two looks at the active run inventory while `sm head prepare --wait` waits.")
    admission_nonconsumers_file: Path | None = Field(None, description="Reviewed nonconsumer receipts file that `sm head prepare` and `sm head blockers` "
                                                                       "use when --nonconsumers is not given; none: no receipts.")
    admission_nonconsumers_dir: Path | None = Field(None, description="Directory of reviewed receipts files (*.json): when prepare waits for or refuses "
                                                                      "active runs, it names the newest file there that covers every one of them; none: no search.")
    queue_service_label: str | None = Field(None, description="The launchd label of the service that runs `sm daemon queue` on this machine; "
                                                              "`sm head apply` and `sm release admit apply` pause it around the registry write. "
                                                              "Unset: both refuse before they pause anything, naming this key.")


def register_prepared(digest: str, source: str) -> None:
    """Make the digest of a prepare reviewable: `sm review verdict` accepts it even when no run is waiting."""
    verdicts.register(runtime().providers.store, digest, source)


def require_verdict(digest: str) -> None:
    """Apply refuses unless an independent reviewer recorded an approval for the digest (the store `sm review verdict` writes)."""
    store = runtime().providers.store
    verdicts.require_independent(store, digest, _go.run_states(store), f"sm review verdict {digest} --approve --reviewer NAME")


ERRORS = (_a.AdmissionError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError)
RECEIPTS_HINT = "reviewed receipts for such runs: --nonconsumers FILE or the setting admission_nonconsumers_file"


def _refuse(error: Exception, core_repo: Path | None = None, nonconsumers: Path | None = None) -> NoReturn:
    text = str(error)
    active = ("active Actions run" in text or "active run" in text) and not isinstance(error, wait.WrittenWithoutReceipt)
    hint = "sm head blockers --core-repo WORKTREE" if active else ""
    covering = _covering(core_repo, nonconsumers) if active and core_repo is not None else ""
    raise Refuse(f"focused admission refused: {text}" + (f" ({covering or RECEIPTS_HINT})" if active else ""), hint) from error


def _covering(core_repo: Path, nonconsumers: Path | None) -> str:
    """A reviewed receipts file that covers every run blocking now, named with the option that passes it; "" when the
    setting admission_nonconsumers_dir is not set, the blockers cannot be read, or no file there covers them all."""
    folder = runtime().settings.get("admission_nonconsumers_dir")
    if not folder:
        return ""
    try:
        blocking = {run.id for run in ops.blockers(core_repo, nonconsumers).blocking}
    except ERRORS:
        return ""
    if not blocking:
        return ""
    candidates = sorted(Path(str(folder)).expanduser().glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        if nonconsumers is not None and path.resolve() == Path(nonconsumers).resolve():
            continue
        try:
            named = {receipt.get("run") for receipt in ops.receipts_of(path)}
        except ERRORS:
            continue
        if blocking <= named:
            return (f"the reviewed receipts file {path} covers every blocking run ({', '.join(str(run) for run in sorted(blocking))}): "
                    f"pass it with --nonconsumers {path} (sm head admit: --set nonconsumers={path})")
    return ""


def _nonconsumers(given: Path | None) -> Path | None:
    """--nonconsumers wins; otherwise the setting admission_nonconsumers_file; otherwise none."""
    if given is not None:
        return given
    configured = runtime().settings.get("admission_nonconsumers_file")
    return Path(str(configured)).expanduser() if configured else None


@native("head hash", writes=False, replaces=("hz-focused-admit",), help=H(
    "The canonical sha256 of a prepared admission: the digest a review approves.",
    "Use after `sm head prepare`, and by the reviewer on the same file.",
    "nothing (read-only).",
    "sm head hash PACKET_DIR/prepare.out",
    "sm head apply PACKET --approved SHA256 (after an independent review)"))
def head_hash(prepared: Path = typer.Argument(..., metavar="PREPARED", help="The stored prepared transaction (prepare.out).")) -> ops.HashResult:
    try:
        return ops.hash_file(prepared)
    except ERRORS as error:
        _refuse(error)


@native("head blockers", writes=False, replaces=("hz-focused-admit",), help=H(
    "The exact active Actions runs that make apply refuse.",
    "Use before prepare and when apply refused with an unknown active run.",
    "nothing (read-only).",
    "sm head blockers --core-repo WORKTREE",
    "sm head prepare PACKET --core-repo WORKTREE --review-root ROOT --wait"))
def head_blockers(
    core_repo: Path = typer.Option(..., "--core-repo", help="The owned Core checkout."),
    nonconsumers: Path | None = typer.Option(None, "--nonconsumers", help="Reviewed nonconsumer receipts; their runs are omitted. Default: the setting admission_nonconsumers_file."),
) -> ops.BlockersResult:
    try:
        return ops.blockers(core_repo, _nonconsumers(nonconsumers))
    except ERRORS as error:
        _refuse(error)


@native("head prepare", writes=True, replaces=("hz-focused-admit",), help=H(
    "Prepare the exact registry transaction for one reviewed packet, or for a batch; stores it and its arguments.",
    "Use after the packet is built and reviewed, on current main.",
    "prepare.out and prepare.args.json next to the packet (batch: in --out); nothing outside this machine (no cancel probe, no variable).",
    "sm head prepare PACKET.json --core-repo WORKTREE --review-root ROOT --wait",
    "sm head hash PACKET_DIR/prepare.out, then an independent review of that file",
    "Batch: sm head prepare --packets A.json --packets B.json --core-repos WT_A --core-repos WT_B [--prs 745 --prs 0] "
    "--review-root ROOT --out DIR. Plans of a batch are valid for the first merge only. "
    "Repair: sm head prepare PACKET --repair-main --core-repo WT --review-root ROOT admits only the plan of main's exact "
    "tree, when main's push CI failed solely for a missing plan and main is the queue merge of the admitted head of PACKET; "
    "a repair ends in a fresh attempt-1 push run on main: after apply, merge one fix-forward PR "
    "(`sm queue add --fix-forward --for \"Changed paths=BEAD\" PR`); `sm queue main-rerun` refuses, Core's focused jobs "
    "refuse run attempt 2."))
def head_prepare(
    packet: Path | None = typer.Argument(None, metavar="[PACKET]", help="The reviewed packet.json (single packet)."),
    core_repo: Path | None = typer.Option(None, "--core-repo", help="Single packet: the owned Core checkout at the reviewed head."),
    review_root: Path = typer.Option(..., "--review-root", help="Root the packets' review paths are relative to."),
    pr: int | None = typer.Option(None, "--pr", help="Single packet: the open PR of this head; omit for a pre-open head."),
    reviewed_protected_producer: bool = typer.Option(False, "--reviewed-protected-producer", help="The packet changes protected producer sources and was reviewed for it."),
    nonconsumers: Path | None = typer.Option(None, "--nonconsumers", help="Reviewed nonconsumer receipts file. Default: the setting admission_nonconsumers_file."),
    expected_remote_head: str | None = typer.Option(None, "--expected-remote-head", help="Old full SHA of a reviewed fast-forward of an existing PR."),
    resulting_main_only: bool = typer.Option(False, "--resulting-main-only", help="Admit one reviewed resulting-main plan for an unchanged green PR."),
    repair_main: bool = typer.Option(False, "--repair-main", help="Admit the missing plan of main's exact tree after the queue merged an "
                                     "admitted head onto a later main (the batch plan trap); PACKET is that head's packet at main's parent."),
    packets: list[Path] = typer.Option([], "--packets", help="Batch: one reviewed packet per member (repeat the option), 2 to 20."),
    core_repos: list[Path] = typer.Option([], "--core-repos", help="Batch: one owned Core checkout per packet, in the same order."),
    prs: list[int] = typer.Option([], "--prs", help="Batch: the PR per packet in the same order, 0 = pre-open; omit when all are pre-open."),
    out: Path | None = typer.Option(None, "--out", help="Batch: directory for prepare.out and prepare.args.json."),
    wait_: bool = typer.Option(False, "--wait", help="Wait while an uncovered Actions run is active (it is named); retry a truncated inventory."),
) -> ops.PrepareResult | ops.BatchPrepareResult:
    with admission_hold(runtime(), f"head prepare {packet or ' '.join(str(p) for p in packets)}"):
        return _prepare(packet, core_repo, review_root, pr, reviewed_protected_producer, nonconsumers, expected_remote_head, resulting_main_only,
                        repair_main, packets, core_repos, prs, out, wait_)


def _prepare(packet: Path | None, core_repo: Path | None, review_root: Path, pr: int | None, reviewed_protected_producer: bool,
             nonconsumers: Path | None, expected_remote_head: str | None, resulting_main_only: bool, repair_main: bool, packets: list[Path],
             core_repos: list[Path], prs: list[int], out: Path | None, wait_: bool) -> ops.PrepareResult | ops.BatchPrepareResult:
    rt = runtime()
    nonconsumers = _nonconsumers(nonconsumers)
    limits = wait.limits(rt)
    try:
        if packets:
            if (packet is not None or core_repo is not None or pr is not None or expected_remote_head is not None or resulting_main_only
                    or reviewed_protected_producer or repair_main):
                raise _a.AdmissionError("batch takes --packets/--core-repos/--prs; update, resulting-main, repair-main and protected producer stay single-packet")
            if out is None or not core_repos:
                raise _a.AdmissionError("batch needs --core-repos (one per packet) and --out DIR")
            batch: dict[str, Any] = {"prs": prs or None, "nonconsumers": nonconsumers}
            if not wait_:
                done = ops.prepare_batch(packets, core_repos, review_root, out, **batch)
                register_prepared(done.sha256, "head prepare")
                return done
            waited: ops.BatchPrepareResult = wait.prepare_when_clear(
                lambda: ops.prepare_batch(packets, core_repos, review_root, out, **batch),
                lambda: ops.blockers(core_repos[0], nonconsumers).blocking, **limits)
            register_prepared(waited.sha256, "head prepare")
            return waited
        if packet is None or core_repo is None or core_repos or prs or out is not None:
            raise _a.AdmissionError("single-packet mode needs PACKET and --core-repo (and no --core-repos, --prs, --out)")
        options: dict[str, Any] = {"pr": pr, "protected": reviewed_protected_producer, "nonconsumers": nonconsumers,
                                   "expected_remote_head": expected_remote_head, "resulting_main_only": resulting_main_only,
                                   "repair_main": repair_main}
        single = ops.prepare(packet, core_repo, review_root, **options) if not wait_ else ops.prepare_waiting(packet, core_repo, review_root, **limits, **options)
        register_prepared(single.sha256, "head prepare")
        return single
    except wait.StillWaiting as waiting:
        covering = _covering(core_repo or core_repos[0], nonconsumers) if "uncovered active run" in waiting.message else ""
        hint = f"; {covering or RECEIPTS_HINT}" if "uncovered active run" in waiting.message else ""
        raise Wait(waiting.message + hint, waiting.waiting_on) from waiting
    except ERRORS as error:
        _refuse(error, core_repo or (core_repos[0] if core_repos else None), nonconsumers)


@native("head apply", writes=True, replaces=("hz-focused-admit",), help=H(
    "Apply one approved prepared transaction: the single registry slot write, with receipt.",
    "Use after an independent reviewer approved the sha256 of prepare.out.",
    "one repository variable (a focused plan slot); pauses and restores the merge queue; writes a receipt.",
    "sm head apply PACKET.json --approved SHA256 [--wait]",
    "sm head admit WORKTREE (push, PR, queue)",
    "While prepare or apply runs it holds the validation window: no new validation starts (`sm ci window status`), the runs going finish; "
    "--wait waits for them (exit 75 after admission_wait_max_minutes: the same command again)."))
def head_apply(
    packet: Path = typer.Argument(..., metavar="PACKET", help="The packet.json that was prepared (single packet; a batch directory is refused)."),
    approved: str = typer.Option(..., "--approved", help="The sha256 of prepare.out the reviewer approved."),
    wait_: bool = typer.Option(False, "--wait", help="Wait while a validation run (or another uncovered run) is active; the hold keeps new validations from starting."),
) -> ops.ApplyResult:
    if packet.is_dir():
        raise Refuse(f"{packet} is a directory: batch apply is not available yet (first-merge-only is not enforced, chained "
                     "plans are not built); `sm head apply` applies one prepared packet.json only. Nothing was written.", code=USAGE)
    rt = runtime()
    try:
        require_verdict(approved)  # before the hold: a missing verdict never holds the window
        with admission_hold(rt, f"head apply {packet}"):
            if not wait_:
                return ops.apply(packet, approved)
            applied: ops.ApplyResult = wait.retry_when_clear(lambda: ops.apply(packet, approved), **wait.limits(rt))
            return applied
    except wait.StillWaiting as waiting:
        raise Wait(waiting.message, waiting.waiting_on) from waiting
    except ERRORS as error:
        _refuse(error)
