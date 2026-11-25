"""The queue and a red main: rerun, checkpoint and macOS capacity recovery.

Both are the moved queue code (`queue_lib/queue_daemon.py`: `main_rerun`, and `main_checkpoint.establish`), reached
through the queue's own argument parser; this module only gives them typed options and a result model. A separate
module so that a change here does not touch the reviewed queue commands; its review unit names what it executes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import typer
from pydantic import Field

from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime

from . import queue as area
from . import queue_hold
from .queue_lib import hzlib, main_capacity, main_capacity_recovery, main_checkpoint


class QueueMain(Model):
    """What `main-rerun` or `checkpoint` printed, and for a checkpoint its proof."""

    action: str
    lines: list[str] = []
    errors: list[str] = Field([], description="What the command wrote to stderr (a refusal names its reason here).")
    proof: dict | None = Field(None, description="checkpoint: the verified continuity proof (head, anchor, segments, incident).")
    exit: int = 0

    def render(self) -> list[str]:
        return self.lines

    @property
    def exit_code(self) -> int:
        return self.exit


def _call(action: str, argv: list[str]) -> QueueMain:
    module = area._queue()
    try:
        code, lines, errors = area._run(module, argv)
    except (ValueError, OSError, KeyError) as error:  # the moved code raises on evidence it cannot accept
        raise Refuse(f"{action} refused: {error}", "sm queue why") from None
    errors = area._translate(errors)
    for line in errors:
        runtime().say(line)
    proof = None
    if action == "checkpoint" and code == 0 and lines:
        try:
            proof = json.loads(lines[-1])
        except ValueError:
            proof = None
    return QueueMain(action=action, lines=area._translate(lines), errors=errors, proof=proof, exit=code)


@native("queue main-rerun", writes=True, replaces=("hz-queue",), help=H(
    "Rerun main's failed jobs once when every failing job is KNOWN; a fully green second attempt lets merges proceed.",
    "Use when main is red by KNOWN non-focused failures only and the loop has not rerun it yet; --dry-run shows the plan.",
    "nothing with --dry-run. Otherwise: one `gh run rerun --failed` of main's run, queue state, log and one note.",
    "sm queue main-rerun --dry-run", "sm queue why",
    "Refused when a focused job would run again (failed, or a repaired main): Core's focused jobs accept run attempt 1 only,\n"
    "so such a run cannot pass on attempt 2. A repair ends in a fresh attempt-1 push run on main: merge one fix-forward PR\n"
    "(sm queue add --fix-forward --for \"JOB=BEAD\" PR).\n"
    "Never twice per run, never while a release run, a merge freeze or an unfinished release holds. It restores merging only,\n"
    "never a release: a release still needs a first-attempt full green main run. Exit 1: refused, the reason is printed.\n"
    "While the loop runs it owns the lock and does the same check each tick; this command then says so and exits 1."))
def queue_main_rerun(dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be rerun, start nothing.")) -> QueueMain:
    return _call("main-rerun", ["main-rerun", *(["--dry-run"] if dry_run else [])])


@native("queue checkpoint", writes=True, replaces=("hz-queue",), help=H(
    "Verify merged-release and focused-main continuity past a pinned main failure; with --apply record the evidence.",
    "Use when main's failure is pinned and a merged release plus focused main proofs cover everything since.",
    "nothing without --apply (it downloads evidence into temporary files). With --apply: the checkpoint file beside the queue state.",
    "sm queue checkpoint --pr 795 --run 43195668437 --core-repo ~/Code/heinzel", "sm queue why",
    "Needs the idle queue lock: stop the loop first (exit 1 otherwise). Merges only; pins are unchanged, and every gate of\n"
    "the loop revalidates the recorded identities. A refusal names the evidence it could not accept."))
def queue_checkpoint(
    pr: int = typer.Option(..., "--pr", metavar="PR", help="The merged release PR."),
    run: int = typer.Option(..., "--run", metavar="RUN", help="The CI run that proved it."),
    core_repo: Path = typer.Option(..., "--core-repo", metavar="DIR", help="A checkout of the core repository to read commits from."),
    apply: bool = typer.Option(False, "--apply", help="Record the evidence identities after verification."),
) -> QueueMain:
    return _call("checkpoint", ["checkpoint", "--pr", str(pr), "--run", str(run), "--core-repo", str(core_repo), *(["--apply"] if apply else [])])

@native("queue main-capacity", writes=True, help=H(
    "Verify a macOS runner capacity failure and optionally dispatch the full macOS main lane.",
    "Use for the exact original split-main pair before a replacement exists.",
    "Without --dispatch, reads evidence only. With --dispatch, records one intent and requests lane=macos on main.",
    "sm queue main-capacity --head SHA --push-run RUN --macos-run RUN --dispatch", "sm queue why",
    "A replay only reads back the recorded run. An ambiguous response never sends another request. Queue and release "
    "admission still use their existing rules."))
def queue_main_capacity(
    head: str = typer.Option(..., "--head", help="Exact current main commit."),
    push_run: int = typer.Option(..., "--push-run", min=1, help="Original Linux push run."),
    macos_run: int = typer.Option(..., "--macos-run", min=1, help="Original coalesced macOS dispatch."),
    dispatch: bool = typer.Option(False, "--dispatch", help="Send one official full macOS dispatch."),
) -> QueueMain:
    rt = runtime()

    def authorize() -> None:
        if hzlib.merge_frozen() or queue_hold.current(rt):
            raise Refuse("a release, incident or operator hold blocks the dispatch", "sm queue why")

    def post(workflow: str):
        return hzlib.ghj(
            "api", "--method", "POST", "-H", "X-GitHub-Api-Version: 2026-03-10",
            f"repos/{hzlib.CORE}/actions/workflows/{workflow}/dispatches", "-f", "ref=main",
            "-f", "inputs[lane]=macos", "-F", "return_run_details=true", default=None,
        )

    try:
        if not dispatch:
            proof = main_capacity_recovery.assess(head, push_run, macos_run, api=main_checkpoint.get,
                                                  read_log=main_capacity.read_aggregate_log)
            return QueueMain(action="main-capacity", lines=[f"Capacity-only original macOS run {macos_run}."],
                             proof={"head": head, "jobs": list(proof.jobs), "inventory_sha256": proof.inventory_sha256})
        found = main_capacity_recovery.dispatch(
            rt.providers.store, head, push_run, macos_run, api=main_checkpoint.get,
            read_log=main_capacity.read_aggregate_log, now=lambda: datetime.fromtimestamp(rt.providers.clock.now(), UTC),
            authorize=authorize, post=post,
        )
        return QueueMain(action="main-capacity", proof=found, lines=[f"Bound full macOS replacement run {found['run']} ({found['status']}): {found['url']}"])
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise Refuse(f"main capacity recovery refused: {error}", "sm queue why") from None
