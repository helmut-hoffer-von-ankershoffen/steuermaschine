"""The plugin's engine pipelines: one resumable command per job, composed of the existing native commands.

    sm head admit WORKTREE      preflight -> packet -> packet review STOP -> prepare -> apply review STOP -> apply* -> pr* -> queue*
    sm release cut VERSION      pins -> notes -> admit prepare -> admission review STOP -> admit apply* -> publish review STOP -> resume* -> verify

(* writes outside this machine: only with --live.) The core engine keeps the state per head or version, stops at each
review stop with the file, its canonical sha256 and the reviewer brief, resumes with `--approved SHA256`, and voids
what was done for an older head or main. Every step calls a native command through `native.call`, so the review gate
of that command holds inside the pipeline too; nothing here reimplements a step.
"""

from __future__ import annotations

from typing import Any


def pipelines(settings: Any) -> list[Any]:
    from . import head_admit, release_cut

    return [head_admit.pipeline(), release_cut.pipeline()]
