"""`sm gate run -- COMMAND...` and `sm gate status`: the command around the core run lock (`steuermaschine.runlock`).

Generic: the lock's slots, load bar, holds and worker caps are the core's. What a project adds is its policy: where the
lock files live, its limits, the commands it refuses and the ones that are light by themselves. A plugin hands it in
through `sm_provider_extras` under the key `POLICY`, as a callable

    policy(settings, commands, clock, allow_full, cwd) -> runlock.Policy

Without one the lock uses `default_policy`: files below the project's state directory, the core defaults of `Policy`,
no refusals.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import typer

from ..config import child_environment
from ..engine import Refuse
from ..helpdoc import H
from ..models import Model
from ..native import Group, native, runtime
from ..runlock import Outcome, Policy, Request, RunLock, Runner, Status, bad_reason, git_tree_state

POLICY = "gate.policy"  # key in `Providers.extra`: the project's policy factory


class GateRun(Model):
    """What `sm gate run` did. The command's own output went to the terminal."""

    status: str  # ran | refused | gave_up
    command: list[str]
    code: int
    lane: str = ""
    waited_seconds: float = 0.0
    ran_seconds: float = 0.0
    reason: str = ""

    @property
    def exit_code(self) -> int:
        return self.code

    def render(self) -> list[str]:
        return []  # the command's own output is the output; why nothing ran is said on stderr


def default_policy(home: Path) -> Policy:
    """The lock of a project whose plugin hands in no policy: files in `<state>/gate`, the core limits, no refusals."""
    return Policy(directory=home / "gate", prefix="sm-gate", history=home / "gate" / "history.jsonl")


def with_tree_state(chosen: Policy, commands: Runner) -> Policy:
    """The policy with the rerun refusal on (a policy that brings its own `tree_state` keeps it)."""
    return chosen if chosen.tree_state is not None else replace(chosen, tree_state=git_tree_state(commands))


def model(outcome: Outcome, argv: list[str]) -> GateRun:
    return GateRun(status=outcome.status, command=list(argv), code=outcome.exit_code, lane=outcome.lane, waited_seconds=round(outcome.waited_seconds, 1),
                   ran_seconds=round(outcome.ran_seconds, 1), reason=outcome.reason)


def execute(lock: RunLock, request: Request, say: Callable[[str], None]) -> GateRun:
    """Run one request under the lock and say why nothing ran, when nothing ran."""
    result = model(lock.run(request), list(request.argv))
    if result.reason:
        say(f"sm gate: {'refusing: ' if result.status == 'refused' else ''}{result.reason}")
    return result


def lock_of(allow_full: bool = False) -> RunLock:
    """The run lock of the current project: the plugin's policy (`POLICY`), else `default_policy`."""
    rt = runtime()
    commands, clock = rt.providers.commands, rt.providers.clock
    factory: Callable[..., Policy] | None = (rt.providers.extra or {}).get(POLICY)
    settings: Mapping[str, Any] = rt.settings or {}
    chosen = factory(settings, commands, clock, allow_full, os.getcwd()) if factory else default_policy(Path(rt.project.home))
    return RunLock(with_tree_state(chosen, commands), commands, clock, say=rt.say)


GATE = Group("gate", H(
    "gate: the run lock for heavy local commands (tests, type checks, builds, pushes) on this machine.",
    "Use for every heavy local command; never start one without it.",
    "run takes a lock slot while the command runs; status only reads.",
    "sm gate run -- pytest tests/unit/test_x.py",
    "sm gate status"))


@native("gate run", writes=True, help=H(
    "Run one heavy local command when a slot is free and the load allows; capped test workers; exclusive.",
    "Use for every pytest, vitest, tsc, mypy, playwright, make and git push on this machine.",
    "takes a lock slot while the command runs and appends one line to the gate history; the command does the rest.",
    "sm gate run -- pytest tests/unit/test_x.py",
    "sm gate status   (who holds the lock, when this one waits)",
    "Options come before the `--`, the command after it. The command's output is its own; its exit code is returned.\n"
    "Refuses (exit 2) what the project's policy refuses (with the reason and the allowed route); --allow-full admits what\n"
    "the policy lets it admit, and logs it. Exit 75: gave up after --max-wait, nothing ran. Exit 124: stopped by --timeout.\n"
    "Short commands run in the light lane (ruff, lint target, read-only git, and any command whose p50 over its green runs is\n"
    "30 s or less) and do not queue behind heavy runs. A command that failed on this very tree (HEAD tree plus dirty state) is\n"
    "refused until the tree changes or --force-rerun REASON is given; the history line records tree, caller (SM_CALLER) and failure class.\n"
    "The project's policy may count some commands as light by themselves. A gated command started inside a gated command runs at once.\n"
    "--exclusive (critical-path pushes, whose pre-push hook runs a timed test session): it queues ahead\n"
    "of everything, starts only when no heavy command runs, and no other heavy command starts until it ends; never light."))
def gate_run(command: list[str] = typer.Argument(..., metavar="COMMAND...", help="The command and its arguments, after `--`."),
             priority: int = typer.Option(0, "--priority", min=0, max=2, help="1: ahead of the normal queue (small fixes); 2: ahead of 1 (release pushes)."),
             max_wait: int = typer.Option(0, "--max-wait", metavar="SECONDS", min=0, help="Give up after this long with exit 75 and nothing run; 0 waits."),
             timeout: int | None = typer.Option(None, "--timeout", metavar="SECONDS", min=1, help="Stop the command this long after it was admitted."),
             light: bool = typer.Option(False, "--light", help="The command is light: no heavy slot, at most four at a time."),
             hold_exempt: bool = typer.Option(False, "--hold-exempt", help="Start although a hold is set."),
             allow_full: bool = typer.Option(False, "--allow-full", help="Admit what the project's policy refuses unless it never admits it; logged."),
             exclusive: bool = typer.Option(False, "--exclusive", help="Wait for an idle machine and keep it: no other heavy command starts meanwhile."),
             force_rerun: str = typer.Option("", "--force-rerun", metavar="REASON", help="Rerun a command that already failed on this very tree; the reason goes to the history.")) -> GateRun:
    if force_rerun and (bad := bad_reason(force_rerun)):
        raise Refuse(bad, "sm gate run --force-rerun REASON -- COMMAND", code=2)
    request = Request(argv=command, environ=child_environment(), timeout_seconds=timeout, priority=priority, max_wait_seconds=max_wait, light=light,
                      hold_exempt=hold_exempt, cwd=os.getcwd(), exclusive=exclusive, force_rerun=force_rerun.strip())
    return execute(lock_of(allow_full), request, runtime().say)


@native("gate status", writes=False, help=H(
    "Heavy slots in use, the workers each gate gets, the load, and who holds the run lock.",
    "Use when a gated command waits.",
    "nothing (read-only).",
    "sm gate status",
    "sm gate run -- COMMAND"))
def gate_status() -> Status:
    return lock_of().status()
