"""The run lock policy of the Mini, for the core commands `sm gate run` and `sm gate status` (`steuermaschine.commands.gate`)
and for the plugin's own gated runs (`lock_of`), on `steuermaschine.runlock`. The plugin hands `policy` to the core under
`steuermaschine.commands.gate.POLICY` (plugin.py, `sm_provider_extras`).

The policy is the former lock's: at most three heavy runs, none at or above load 40, a second one only below load 20,
nine workers shared by the running gates, four light runs, and the refusal of full local suites. On top of it, the
scheduling of 2026-11-14 (all three are settings): no heavy start at or above load 12 (`lock_start_load`; the load
that made the timed pre-push session fail), the last heavy slot reserved for the focus head (`lock_reserved_slots`),
and the push lane (`lock_push_lane`): a push that is not documentation only is exclusive by itself.
Since 0.9.54 all of that is the lane of a full local run (`sm gate run --full`, hidden): what `sm check` and a push run
is fast (`lock_fast_slots`, three at once, fewer under load, no load bar, never exclusive), because hosted PR CI runs the
full suites anyway. It adopts the
former lock's files (settings `lock_directory`, `lock_prefix`), so a run started the old way and one started here
exclude each other for as long as both exist.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from steuermaschine import USAGE
from steuermaschine.native import runtime
from steuermaschine.runlock import PUSH_COMMANDS, Policy, Refusal, RunLock, git_tree_state, with_focus

from .. import state as statedir  # the plugin's state paths
from .lock_lib import refuse

WORKER_CAPS = {"VITEST_MAX_WORKERS": 8, "VITEST_MAX_THREADS": 8, "VITEST_MAX_FORKS": 8, "PYTEST_XDIST_AUTO_NUM_WORKERS": 8}
MAX_LOAD, HEAVY_SLOTS = 40.0, 3  # settings may lower these, never raise them: above them the live service starved
DOCS_ONLY = re.compile(r"(^|/)[^/]+\.(md|txt|rst)$|^(docs|research)/")
DEFAULT_FILES = {"lock_directory": ".", "lock_history": "gate-history.jsonl", "lock_allow_full_log": "gate-allow-full.log"}  # in the state directory


class LockSettings(BaseModel):
    """Settings of the run lock, a mixin of the plugin's table."""

    lock_directory: Path | None = Field(None, description="Directory of the run lock's files; none: the project's state directory.")
    lock_prefix: str = Field("gate", description="File name prefix of the run lock (`gate.lock`, `gate.heavy.1.lock` ...).")
    lock_max_load: float = Field(40.0, description="No gated command starts at or above this 1-minute load.")
    lock_heavy_slots: int = Field(3, description="Heavy commands that may run at once.")
    lock_extra_slot_load: float = Field(20.0, description="A second heavy command starts only below this load; with lock_start_load set it is the start bar of a focus head.")
    lock_start_load: float = Field(12.0, ge=0, description="No full local run (`--full`) starts at or above this 1-minute load (0: off); fast checks have no load bar. A focus head's full run goes by lock_extra_slot_load.")
    lock_reserved_slots: int = Field(1, ge=0, description="Heavy slots only a focus head (`sm focus add WORKTREE`) takes; one slot always stays open to everyone.")
    lock_push_lane: bool = Field(True, description="A full push (`--full`) that is not documentation only is exclusive: it waits for an idle machine and no heavy command starts beside it. A normal push is a fast check.")
    lock_fast_slots: int = Field(3, ge=0, description="Fast checks (everything without `--full`, pushes included) that may run at once, no load bar and never exclusive; one fewer from lock_extra_slot_load, one from lock_max_load (0: no fast lane).")
    lock_cpu_budget: int = Field(9, description="Test workers shared by all running heavy commands.")
    lock_light_slots: int = Field(4, description="Light commands that may run at once.")
    lock_history: Path | None = Field(None, description="One JSON line per gated command, either lane (times, exit, tree, caller, failure class); none: gate-history.jsonl in the state directory.")
    lock_allow_full_log: Path | None = Field(None, description="One line per full suite that ran with --allow-full; none: gate-allow-full.log in the state directory.")


def setting(settings: Mapping, name: str):
    value = settings.get(name) if settings else None
    return LockSettings.model_fields[name].default if value is None else value


def docs_only_push(commands, command: Sequence[str]) -> bool:
    """Whether the command pushes a branch that, against origin/main, changes only documentation."""
    command = list(command)
    words = command[2:] if command[:1] == ["env"] and command[1:2] and "=" in command[1] else command
    if words[:2] != ["git", "push"] and words[:2] != ["make", "pr"]:
        return False
    done = commands.run(["git", "diff", "--name-only", "origin/main...HEAD"])
    files = done.out.split()
    return done.rc == 0 and bool(files) and all(DOCS_ONLY.search(f) for f in files)


def log_allow_full(path: Path, argv: Sequence[str], clock, cwd: str) -> None:
    from datetime import datetime

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(f"{datetime.fromtimestamp(clock.now()).astimezone().strftime('%Y-%m-%dT%H:%M:%S%z')}\t{cwd}\t{' '.join(argv)}\n")
    except OSError:
        pass


def policy(settings: Mapping, commands, clock, allow_full: bool = False, cwd: str = "") -> Policy:
    def expand(name: str) -> Path:
        value = setting(settings, name)
        return Path(os.path.expanduser(str(value))) if value is not None else statedir.path(DEFAULT_FILES[name])

    def full_suite(argv: Sequence[str], environ: Mapping[str, str]) -> Refusal | None:
        why = refuse.refusal(list(argv), environ)
        if why and allow_full and why != refuse.NEVER_MAKE_PR:  # --allow-full never admits make pr
            log_allow_full(expand("lock_allow_full_log"), argv, clock, cwd)
            return None
        return Refusal(USAGE, f"{why}. {refuse.ROUTE}") if why else None

    return Policy(directory=expand("lock_directory"), prefix=str(setting(settings, "lock_prefix")), label="sm gate",
                  max_load=min(MAX_LOAD, float(setting(settings, "lock_max_load"))), heavy_slots=max(1, min(HEAVY_SLOTS, int(setting(settings, "lock_heavy_slots")))),
                  heavy_slot_limit=HEAVY_SLOTS, extra_slot_load=float(setting(settings, "lock_extra_slot_load")),
                  cpu_budget=int(setting(settings, "lock_cpu_budget")), worker_caps=WORKER_CAPS, light_slots=int(setting(settings, "lock_light_slots")),
                  start_load=min(MAX_LOAD, max(0.0, float(setting(settings, "lock_start_load")))), reserved_slots=max(0, int(setting(settings, "lock_reserved_slots"))),
                  fast_slots=max(0, min(HEAVY_SLOTS, int(setting(settings, "lock_fast_slots")))),
                  exclusive_commands=PUSH_COMMANDS if setting(settings, "lock_push_lane") else (),
                  held_env="HZ_GATE_HELD", history=expand("lock_history"), refusals=(full_suite,),
                  is_light=lambda argv: docs_only_push(commands, argv), tree_state=git_tree_state(commands))


def lock_of(allow_full: bool = False) -> RunLock:
    rt = runtime()
    table, commands, clock = rt.settings, rt.providers.commands, rt.providers.clock
    return RunLock(with_focus(policy(table, commands, clock, allow_full, os.getcwd()), rt.providers.store), commands, clock, say=rt.say)
