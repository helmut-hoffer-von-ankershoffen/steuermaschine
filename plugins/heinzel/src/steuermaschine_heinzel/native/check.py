"""`sm check`: the cheap checks of exactly what one Core head changes, before any review or push, and the receipt.

The checks are `check_lib/cheap.py`; what belongs to a changed path comes from Core at the commit (its selector, as
`sm head select` reads it, its hook and its standards list). This module is the edge: the options, the result model,
the binding to this call's providers, and the one write: a green run on a clean tree writes the check receipt of the
commit (`steuermaschine.verdicts.record_check`), which `sm review verdict --approve` asks for when a digest binds
that commit.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import typer

from steuermaschine import USAGE, verdicts
from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.native import native, runtime
from steuermaschine.preflight import SKIP, PreflightReport

from . import affected, lock
from . import preflight as _preflight
from .affected_lib import select
from .check_lib import cheap, queue
from .preflight_lib import head

SUMMARY_KIND = "heinzel.check.summary"  # state store: the last report per head, green or not, keyed by the commit


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

    def render(self) -> list[str]:
        return [*super().render(), f"receipt: {self.receipt_note}"]


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


def extra_tests(sel: Path | None) -> list[str]:
    if sel is None:
        return []
    try:
        listed = json.loads(sel.expanduser().read_text()).get("python") or []
    except (OSError, ValueError, AttributeError) as why:
        raise Refuse(f"unreadable selection {sel}: {why}", "sm check --help", code=USAGE) from None
    return [str(test).split("::")[0] for test in listed]


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
    "files only. Every step runs or fails: no slot (75) or a timeout (124) in any step, types included, fails, never skips.\n"
    "A src/ change with no selected test fails. The receipt lists the steps that ran and says coverage and whole-project\n"
    "types are CI-enforced.\n"
    "Runs queue: one sm check at a time per machine, the others wait (exit 75 after 20 minutes: run it again).\n"
    "E2E lanes never run here. --sel FILE adds tests ({\"python\": [files]}).\n"
    "The receipt is bound to the commit and names the base it was measured from: any new commit needs a new run, and\n"
    "an approval needs the receipt's base to be the base the reviewed packet builds the head on. A run with --skip or\n"
    "with another --base than the setting preflight_base, a red run and a dirty tree write none.\n"
    "Exit 0 READY, 1 NOT READY, 2 usage."))
def check(worktree: Path | None = typer.Argument(None, metavar="WORKTREE", help="Core worktree whose HEAD is the head to check (default: the current directory)."),
          base: str | None = typer.Option(None, "--base", metavar="REV", help="The ref the head branched from (default: the setting preflight_base)."),
          sel: Path | None = typer.Option(None, "--sel", metavar="FILE", help='More tests to run: JSON {"python": [test files]}.'),
          skip: list[str] | None = typer.Option(None, "--skip", metavar="NAME", help="Leave one check out (repeatable); named in the verdict, and no receipt is written."),
          allow_full: bool = typer.Option(False, "--allow-full", help="Let the run lock run the whole unit suite when nothing narrows the change (the coordinator's call; logged)."),
          full: bool = typer.Option(False, "--full", hidden=True, help="A full local run: the whole unit suite when nothing narrows the change, "
                                                                      "through the heavy lane (load bar, exclusive push lane); logged.")) -> HeadCheck:
    rt, usage = runtime(), "sm check --help"
    providers: Any = rt.providers
    wt = _preflight.core_worktree(worktree or Path.cwd(), usage)
    skipped = list(skip or [])
    registry = cheap.checks()
    if unknown := registry.unknown(skipped):
        raise Refuse(f"--skip takes one of {', '.join(cheap.STEPS)}; unknown: {', '.join(unknown)}", usage, code=USAGE)
    default = str(_preflight.setting(rt, "preflight_base"))
    ref = base or default
    extra = extra_tests(sel)
    with queue.queued(lock.policy(rt.settings, providers.commands, providers.clock).directory, rt.say):  # one run at a time: the rest wait
        c = bound(rt, wt, ref, allow_full, full or allow_full)
        try:
            selection = affected.selection(select.Repo(wt), c.changed())
        except select.RepoError as why:
            raise Refuse(f"the selector of {wt} cannot be read: {why}", f"sm head select --repo {wt} --base {ref}", code=USAGE) from None
        c.plan = cheap.plan(c, selection, extra)
        steps, left_out, not_run = registry.run(c, set(skipped), say=rt.say)
    report = HeadCheck.of(f"sm check {c.sha[:9]} ({c.branch or 'detached'}) vs {ref} {c.base_sha[:9]}", steps, left_out, not_run, worktree=str(wt), head=c.sha,
                          branch=c.branch, base=c.base_sha, selector=c.plan.selector, changed=len(c.plan.changed),
                          generated_at=datetime.fromtimestamp(providers.clock.now()).astimezone().isoformat(timespec="seconds"))
    if not report.ready:
        report.receipt_note = "none: fix every numbered failure in one commit, then run sm check again"
    elif left_out:
        report.receipt_note = f"none: {', '.join(left_out)} left out by --skip; a receipt needs every check"
    elif ref != default:  # checks measured from another base cover a part of the head: no proof of the head
        report.receipt_note = f"none: measured from --base {ref}, not from {default} (the setting preflight_base); run sm check without --base for the receipt"
    else:
        state = verdicts.record_check(providers.store, c.sha, {"worktree": str(wt), "branch": c.branch, "base": c.base_sha, "at": providers.clock.now(), "command": report.command,
                                                               "checks": [{"name": s.name, "status": s.status, "detail": s.detail} for s in report.steps]})
        report.receipt = state in ("written", "kept")
        report.receipt_note = (f"{verdicts.CHECKS} {c.sha} ({'written' if state == 'written' else 'this commit already had one'}), measured from {c.base_sha[:12]}: "
                               "an approval of this exact commit on that base can be recorded. Steps run: " + ", ".join(s.name for s in report.steps if s.status != SKIP)
                               + "; coverage and whole-project types did not run here: PR CI enforces them" if report.receipt else f"none that counts: {state}")
    providers.store.save(SUMMARY_KIND, c.sha, report.model_dump(mode="json"))
    return report
