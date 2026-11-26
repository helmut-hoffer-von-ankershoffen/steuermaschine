"""`sm check`: the cheap checks of exactly what one Core head changes, before any review or push, and the receipt.

The checks are `check_lib/cheap.py`; what belongs to a changed path comes from Core at the commit (its selector, as
`sm head select` reads it, its hook and its standards list). This module is the edge: the options, the result model,
the binding to this call's providers, and the one write: a green run on a clean tree writes the check receipt of the
commit (`steuermaschine.verdicts.record_check`), which `sm review verdict --approve` asks for when a digest binds
that commit.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from shlex import join
from typing import Any

import typer

from steuermaschine import USAGE, verdicts
from steuermaschine.config import child_environment
from steuermaschine.engine import Refuse, Wait
from steuermaschine.helpdoc import H
from steuermaschine.native import native, runtime
from steuermaschine.preflight import SKIP, PreflightReport, Step

from . import affected, gallery_sync, lock
from . import preflight as _preflight
from .affected_lib import select
from .check_lib import budget, cheap, slots
from .preflight_lib import head

SUMMARY_KIND = "heinzel.check.summary"  # state store: the last report per head, green or not, keyed by the commit


def tests_counts(steps: list[Any]) -> dict[str, int]:
    """How many of the selected test files ran here (the receipt says it, `sm review verdict --approve` prints it)."""
    data = next((s.data for s in steps if s.name == "tests" and "tests_selected" in s.data), {})
    return {"tests_ran": int(data["tests_ran"]), "tests_selected": int(data["tests_selected"])} if data else {}


class HeadCheck(PreflightReport):
    """The cheap checks of one head. `receipt` is True when this commit now has its check receipt."""

    command: str = "sm check"
    generated_at: str = ""
    worktree: str = ""
    head: str = ""
    branch: str = ""
    base: str = ""  # the commit the head is compared with: its merge-base with the base ref
    selector: str = ""
    changed: int = 0
    receipt: bool = False
    receipt_note: str = ""
    deferred: str = ""  # why part of the run, or all of it, is left to CI ("": nothing is)
    ci_must_run: list[str] = []  # what PR CI must run because it did not run here

    def render(self) -> list[str]:
        out = [*super().render()]
        if self.deferred or self.ci_must_run:
            out.append(f"deferred to CI: {self.deferred or 'part of the run'}; CI must run: {', '.join(self.ci_must_run) or 'nothing more'}")
        return [*out, f"receipt: {self.receipt_note}"]


def command_of(worktree: object) -> str:
    """The command that writes the check receipt of the head of `worktree`, as other commands name it."""
    return f"sm check {worktree}"


def bound(rt: Any, wt: Path, base: str, allow_full: bool = False, full: bool = False) -> cheap.Ctx:
    """The context of one run: the preflight's edges, with the merge-base as the base (what the head itself changes)."""
    inner = _preflight.bound(rt, wt, allow_full or full, full, None if full else cheap.FAST_SECONDS)
    c = cheap.Ctx(full=full, wt=wt, standards=inner.standards, sh=inner.sh, gate=inner.gate, meta=inner.meta, gh=inner.gh, repo=inner.repo)
    c.sha = c.git("rev-parse", "HEAD")[1].strip()
    rc, merge_base = c.git("merge-base", base, "HEAD")
    if rc != 0 or not c.sha or not merge_base.strip():
        raise Refuse(f"{wt} has no merge-base with {base}: is it a git worktree with that ref?", f"git -C {wt} fetch origin", code=USAGE)
    c.base_sha = merge_base.strip().split()[0]
    head.bind(c.base_sha)
    branch = c.git("rev-parse", "--abbrev-ref", "HEAD")[1].strip()
    c.branch = "" if branch == "HEAD" else branch
    return c


@contextmanager
def isolated_gate(gate: _preflight.Shell) -> Iterator[_preflight.Shell]:
    """Give this check and all its gated children a private pytest cleanup namespace."""
    parent = child_environment().get("PYTEST_DEBUG_TEMPROOT") or None
    with tempfile.TemporaryDirectory(prefix="sm-check-", dir=parent, ignore_cleanup_errors=True) as root:
        def run(cmd: Sequence[object], cwd: object = None, env: dict[str, str] | None = None) -> tuple[int, str]:
            return gate(cmd, cwd=cwd, env={**(env or {}), "PYTEST_DEBUG_TEMPROOT": root})

        yield run


def extra_tests(sel: Path | None) -> tuple[list[str], list[str]]:
    if sel is None:
        return [], []
    try:
        data = json.loads(sel.expanduser().read_text())
        python = data.get("python", [])
        vitest = data.get("vitest", [])
        if not all(isinstance(paths, list) and all(isinstance(path, str) and path for path in paths)
                   for paths in (python, vitest)):
            raise ValueError("python and vitest must be lists of test paths")
        if any("::" in path for path in python):
            raise ValueError("the Python selection takes test files, not individual pytest node IDs")
        if any(any(character.isspace() for character in path) for path in vitest):
            raise ValueError("Vitest paths cannot contain whitespace")
    except (OSError, ValueError, AttributeError) as why:
        raise Refuse(f"unreadable selection {sel}: {why}", "sm check --help", code=USAGE) from None
    return list(dict.fromkeys(python)), list(dict.fromkeys(vitest))


def plugins_check(rt: Any, wt: Path) -> HeadCheck:
    """A heinzel-plugins worktree: regenerate every stale screenshot gallery (gallery_sync.py); its hooks run the rest."""
    commands = rt.providers.commands
    step = Step("gallery")
    items = gallery_sync.stale(commands, wt)
    if not items:
        step.detail = "every screenshot gallery is fresh (its UI inputs did not change)"
    else:
        lines, failures = gallery_sync.regenerate(rt, wt, items)
        for message, command in failures:
            step.fail(message, command)
        step.detail = "; ".join(lines) + ("; sm ship takes the regenerated gallery into the head commit" if lines else "")
    sha = gallery_sync.head(commands, wt)
    branch = str(commands.run(["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"], timeout=30).out).strip()
    report = HeadCheck.of(f"sm check {sha[:9]} ({branch or 'detached'}) in the plugins repository", [step], [], [],
                          worktree=str(wt), head=sha, branch=branch)
    report.receipt_note = "none: a plugins head has no check receipt; its commit and push hooks run the owning checks"
    return report


@native("check", writes=True, help=H(
    "The fast checks of exactly what a Core head changes, in one pass: READY and a receipt for the commit, or every failure with its fix.",
    "Use before you ask for a review and before any push, on your committed head; again after every new commit.",
    "writes the check receipt of the commit on this machine when every check is green (written once per commit; "
    "`sm review verdict --approve` needs it); runs local checks through the run lock.",
    "sm check ~/Code/heinzel-chat-composer",
    "ask for the review; then sm head preflight WORKTREE --sel SEL.json",
    "Checks, in this order; every one runs, so one pass names every finding:\n"
    "  tree        clean tree, and the head has commits of its own over the base\n"
    "  ruff        format and lint of the changed Python files\n"
    "  types       the push hook's type checks (read from Core's scripts/scoped_checks.py) over the changed Python files\n"
    "              only, unless only web, desktop, plugins or documents changed; not gating here: a timeout reports\n"
    "              \"skipped: busy\" (PR CI's lint job runs them over the project)\n"
    "  hook        Core's scripts/scoped_checks.py answered for the changed paths: which are web files and documents, their\n"
    "              steps and the early guards. A hook that is missing or fails is a failure, never a pass\n"
    "  parity      what PR CI fails on that the hook leaves out, each when a path it reads changed: the lockfile\n"
    "              (uv lock --check), the template (scripts/sync_template.py --check), the import contracts (lint-imports)\n"
    "  web         when web files changed: the hook's own web steps (lint, generated client, types) and the Vitest files\n"
    "              the selector names\n"
    "  docs        when documents, ADRs or the site's tooling changed: the hook's own document steps\n"
    "  standards   the repository-wide standards tests (Core's scripts/preflight_standards.txt, else the setting)\n"
    "  tests       the hook's early guards (the contract tests of a changed path) and the tests `sm head select` names\n"
    "              for the changed paths, the docs style test for a document among them\n"
    "  bound       HEAD and the tree did not change while the checks ran\n"
    "Which test belongs to which path is never listed here: Core's selector, hook and standards list at the commit say it.\n"
    "The head is compared with its merge-base with the base. When the selector selects the full set, Core's\n"
    "scripts/related_tests.py narrows the unit tests; when it says everything too, the whole unit suite is left to PR CI (a\n"
    "warning says so; --allow-full, the coordinator's call, runs it here through the heavy lane); when it fails, the run fails.\n"
    "Everything here is fast: each command runs in a fast slot of the run lock (up to three at once, any load, never exclusive)\n"
    "and is stopped after 150 s. The whole unit suite, integration and e2e never run here; PR CI runs them.\n"
    "Coverage and the whole-project type checks never run here: owner decision 2026-11-21, fast local checks and CI is the\n"
    "gate (PR CI enforces 90 % coverage and whole-project mypy/pyright before merge). Types run over the changed\n"
    "files only. Every step runs, fails or is deferred to CI by name: no slot (75) or a timeout (124) of a command, types included, fails.\n"
    "A src/ change with no selected test fails. The receipt lists the steps that ran and says coverage and whole-project\n"
    "types are CI-enforced.\n"
    "Slot pool: at most 4 checks run at once on a machine (a shared pool of file locks), at background priority; a run waits up\n"
    "60 s for a slot. No slot in that time, a 1-minute load over 30, or the 90 s wall budget of the whole run spent with nothing\n"
    "failed: exit 75, never READY, never a receipt: run the same command again. A failure found before the budget ended still\n"
    "fails (exit 1). Only a selection of more than 40 test files (and a change nothing narrows) is left to CI: the receipt then\n"
    "stores deferred: true and ci_must_run, and `sm check`, `sm ship` and `sm review verdict --approve` print \"deferred to CI: ...\".\n"
    "A type check stopped at its own limit (124) or without a slot (75) still fails.\n"
    "--extended gives the same focused work a 600 s wall budget. It keeps each command limit and leaves whole-suite\n"
    "selection to the existing rules. It cannot be combined with --allow-full.\n"
    "E2E lanes never run here. --sel FILE adds Python and Vitest files. When Core selects all of Vitest, "
    "named Vitest files run locally and hosted CI must run the complete suite.\n"
    "The receipt is bound to the commit and names the base it was measured from: any new commit needs a new run, and\n"
    "an approval needs the receipt's base to be the base the reviewed packet builds the head on. A run with --skip or\n"
    "with another --base than the setting preflight_base, a red run and a dirty tree write none.\n"
    "A worktree of the plugins repository: the gallery check only. Every screenshot gallery whose UI inputs changed (the\n"
    "repository's own `screenshots-check --all`) is regenerated with the frozen capture of `sm plugin gallery`;\n"
    "deterministic, no agent. `sm ship` then takes exactly the regenerated files into the head commit.\n"
    "Exit 0 READY, 1 NOT READY, 2 usage, 75 wait."))
def check(worktree: Path | None = typer.Argument(None, metavar="WORKTREE", help="Core worktree whose HEAD is the head to check (default: the current directory)."),
          base: str | None = typer.Option(None, "--base", metavar="REV", help="The ref the head branched from (default: the setting preflight_base)."),
          sel: Path | None = typer.Option(None, "--sel", metavar="FILE", help='Named local tests: JSON {"python": [files], "vitest": [files]}.'),
          skip: list[str] | None = typer.Option(None, "--skip", metavar="NAME", help="Leave one check out (repeatable); named in the verdict, and no receipt is written."),
          extended: bool = typer.Option(False, "--extended", help="Give the focused checks up to 600 seconds in total, with their usual command limits."),
          allow_full: bool = typer.Option(False, "--allow-full", help="Let the run lock run the whole unit suite when nothing narrows the change (the coordinator's call; logged)."),
          full: bool = typer.Option(False, "--full", hidden=True, help="A full local run: the whole unit suite when nothing narrows the change, "
                                                                      "through the heavy lane (load bar, exclusive push lane); logged.")) -> HeadCheck:
    rt, usage = runtime(), "sm check --help"
    providers: Any = rt.providers
    if extended and (allow_full or full):
        raise Refuse("--extended applies to focused checks; use it separately from --allow-full or --full", usage, code=USAGE)
    wt = _preflight.core_worktree(worktree or Path.cwd(), usage)
    skipped = list(skip or [])
    registry = cheap.checks()
    if unknown := registry.unknown(skipped):
        raise Refuse(f"--skip takes one of {', '.join(cheap.STEPS)}; unknown: {', '.join(unknown)}", usage, code=USAGE)
    target = (worktree or Path.cwd()).expanduser().resolve()
    if (target / ".git").exists() and gallery_sync.is_plugins(gallery_sync.origin_repo(providers.commands, target)):
        if extended:
            raise Refuse("--extended applies to Core checks", usage, code=USAGE)
        return plugins_check(rt, target)
    default = str(_preflight.setting(rt, "preflight_base"))
    ref = base or default
    extra, web_tests = extra_tests(sel)
    c = bound(rt, wt, ref, allow_full, full or allow_full)
    clock = providers.clock
    with slots.pool(lock.policy(rt.settings, providers.commands, clock).directory, "check", rt.say, sleep=clock.sleep, clock=clock.now,
                    demote=lambda: slots.lower_priority(providers.commands)) as grant:
        if grant.granted:
            if not (full or allow_full):  # the local run is a signal with a wall budget (check_lib/budget.py); --full is the coordinator's call
                seconds = budget.EXTENDED_SECONDS if extended else budget.BUDGET_SECONDS
                c.budget = budget.Budget(clock.now, seconds=seconds)
                c.gate = budget.gated(c.budget, lambda seconds: _preflight.gated(False, False, seconds), cheap.FAST_SECONDS)
            try:
                selection = affected.selection(select.Repo(wt), c.changed())
            except select.RepoError as why:
                raise Refuse(f"the selector of {wt} cannot be read: {why}", f"sm head select --repo {wt} --base {ref}", code=USAGE) from None
            c.plan = cheap.plan(c, selection, extra, web_tests=web_tests)
            with isolated_gate(c.gate) as c.gate:
                steps, left_out, not_run = registry.run(c, set(skipped), say=rt.say)
        else:  # no slot, or a loaded machine: nothing ran, so nothing is READY: exit 75, the same command again later
            raise Wait(f"sm check ran nothing: {grant.deferred}", "a free check slot and a quieter machine; run the same command again")
    if c.deferred and not [s for s in steps if s.failures]:  # the budget ran out and nothing failed: part of the run did not happen, so it is not READY
        retry = join(["sm", "check", str(wt), "--extended", *(["--base", base] if base else []),
                      *(["--sel", str(sel.expanduser().resolve())] if sel else []),
                      *(arg for name in skipped for arg in ("--skip", name))])
        recovery = (
            "Retry on a quieter machine; the extended budget is already in use."
            if extended
            else "The suggested command gives the same focused checks more time. Otherwise, retry on a quieter machine."
        )
        if not skipped and ref == default:
            recovery += (" Once READY with a receipt, rerun your original sm head admit command "
                         "to adopt that receipt if admission was in progress")
        else:
            recovery += " An admission receipt requires the default base and every check."
        raise Wait(
            f"sm check ran out of its local budget with nothing failed: {c.deferred}; not run here: {', '.join(c.ci)}. {recovery}",
            "a quieter machine for the ordinary retry", next_cmd=retry,
        )
    if c.unrun and not [s for s in steps if s.failures]:  # zero tests ran: never a receipt
        raise Wait(f"sm check ran no test: {c.unrun}", "run the owning tests by name (sm check --sel FILE), or sm check --full when the coordinator allows it")
    report = HeadCheck.of(f"sm check {c.sha[:9]} ({c.branch or 'detached'}) vs {ref} {c.base_sha[:9]}", steps, left_out, not_run, worktree=str(wt), head=c.sha,
                          branch=c.branch, base=c.base_sha, selector=c.plan.selector, changed=len(c.plan.changed), deferred=c.deferred, ci_must_run=c.ci,
                          generated_at=datetime.fromtimestamp(providers.clock.now()).astimezone().isoformat(timespec="seconds"))
    if not report.ready:
        report.receipt_note = "none: fix every numbered failure in one commit, then run sm check again"
    elif left_out:
        report.receipt_note = f"none: {', '.join(left_out)} left out by --skip; a receipt needs every check"
    elif ref != default:  # checks measured from another base cover a part of the head: no proof of the head
        report.receipt_note = f"none: measured from --base {ref}, not from {default} (the setting preflight_base); run sm check without --base for the receipt"
    else:
        state = verdicts.record_check(providers.store, c.sha, {"worktree": str(wt), "branch": c.branch, "base": c.base_sha, "at": providers.clock.now(), "command": report.command,
                                                               "deferred": bool(c.ci), "ci_must_run": list(c.ci), **tests_counts(steps),
                                                               "checks": [{"name": s.name, "status": s.status, "detail": s.detail} for s in report.steps]})
        report.receipt = state in ("written", "kept")
        report.receipt_note = (f"{verdicts.CHECKS} {c.sha} ({'written' if state == 'written' else 'this commit already had one'}), measured from {c.base_sha[:12]}: "
                               "an approval of this exact commit on that base can be recorded. Steps run: " + ", ".join(s.name for s in report.steps if s.status != SKIP)
                               + "; coverage and whole-project types did not run here: PR CI enforces them"
                               + (f"; deferred to CI: {', '.join(c.ci)}" if c.ci else "") if report.receipt else f"none that counts: {state}")
    providers.store.save(SUMMARY_KIND, c.sha, report.model_dump(mode="json"))
    return report
