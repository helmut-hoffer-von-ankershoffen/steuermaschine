"""Native admission helpers: `sm head hash` (the digest an admission review approves) and the settings of `sm release admit`."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import NoReturn

import typer
from pydantic import BaseModel, Field

from steuermaschine import verdicts
from steuermaschine import go as _go
from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.native import native, runtime

from .admission_lib import focused_admit as _a
from .admission_lib import ops


class AdmissionSettings(BaseModel):
    """Settings of the admission area (mixin of the plugin's settings model)."""

    admission_wait_max_minutes: float = Field(45.0, description="How long `sm release admit prepare` waits for a window without uncovered active runs before it exits 75.")
    admission_wait_poll_seconds: float = Field(90.0, description="Seconds between two looks at the active run inventory while `sm release admit prepare` waits.")
    admission_nonconsumers_file: Path | None = Field(None, description="Reviewed nonconsumer receipts file that `sm release admit prepare` and "
                                                                       "use when --nonconsumers is not given; none: no receipts.")
    admission_nonconsumers_dir: Path | None = Field(None, description="Directory of reviewed receipts files (*.json): when prepare waits for or refuses "
                                                                      "active runs, it names the newest file there that covers every one of them; none: no search.")
    queue_service_label: str | None = Field(None, description="The launchd label of the service that runs `sm daemon queue` on this machine; "
                                                              "`sm release admit apply` and `sm release admit apply` pause it around the registry write. "
                                                              "Unset: both refuse before they pause anything, naming this key.")


def register_prepared(digest: str, source: str) -> None:
    """Make the digest of a prepare reviewable: `sm review verdict` accepts it even when no run is waiting."""
    verdicts.register(runtime().providers.store, digest, source)


def require_verdict(digest: str) -> None:
    """Apply refuses unless an independent reviewer recorded an approval for the digest (the store `sm review verdict` writes)."""
    store = runtime().providers.store
    verdicts.require_independent(store, digest, _go.run_states(store), f"sm review verdict {digest} --approve --reviewer NAME")


ERRORS = (_a.AdmissionError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError)
def _refuse(error: Exception) -> NoReturn:
    raise Refuse(f"focused admission refused: {error}", "") from error


@native("head hash", writes=False, replaces=("hz-focused-admit",), help=H(
    "The canonical sha256 of a prepared admission: the digest a review approves.",
    "Use after `sm release admit prepare`, and by the reviewer on the same file.",
    "nothing (read-only).",
    "sm head hash PACKET_DIR/prepare.out",
    "sm release admit apply PACKET --approved SHA256 (after an independent review)"))
def head_hash(prepared: Path = typer.Argument(..., metavar="PREPARED", help="The stored prepared transaction (prepare.out).")) -> ops.HashResult:
    try:
        return ops.hash_file(prepared)
    except ERRORS as error:
        _refuse(error)


@native("head nonconsumers", writes=False, help=H(
    "Generate candidate stuck-queued receipts for the current active run inventory.",
    "Use when active runs block admission; independently review the JSON before using it.",
    "nothing (read-only); no cancel probe is sent.",
    "sm head nonconsumers --core-repo WORKTREE --format json",
    "Review the receipts, then pass the JSON file to sm release admit prepare --nonconsumers FILE (or release cut --set nonconsumers=FILE)."))
def head_nonconsumers(
    core_repo: Path = typer.Option(..., "--core-repo", help="The owned Core checkout."),
) -> ops.NonconsumerResult:
    try:
        return ops.nonconsumer_receipts(core_repo)
    except ERRORS as error:
        _refuse(error)
