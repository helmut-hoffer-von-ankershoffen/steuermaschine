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
from steuermaschine.preflight import PreflightReport

from . import affected
from . import preflight as _preflight
from .affected_lib import select
from .check_lib import cheap
from .preflight_lib import head, scoped_cov

SUMMARY_KIND = "heinzel.check.summary"  # state store: the last report per head, green or not, keyed by the commit
COVERAGE_ENV = {"COVERAGE_CORE": "ctrace"}  # the C tracer: the default core of a new Python cannot record what the measurement needs


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


def bound(rt: Any, wt: Path, base: str) -> cheap.Ctx:
    """The context of one run: the preflight's edges, with the merge-base as the base (what the head itself changes)."""
    inner = _preflight.bound(rt, wt)
    c = cheap.Ctx(wt=wt, standards=inner.standards, sh=inner.sh, gate=inner.gate, meta=inner.meta, gh=inner.gh, repo=inner.repo)
    c.sha = c.git("rev-parse", "HEAD")[1].strip()
    rc, merge_base = c.git("merge-base", base, "HEAD")
    if rc != 0 or not c.sha or not merge_base.strip():
        raise Refuse(f"{wt} has no merge-base with {base}: is it a git worktree with that ref?", f"git -C {wt} fetch origin", code=USAGE)
    c.base_sha = merge_base.strip().split()[0]
    head.bind(c.base_sha)
    branch = c.git("rev-parse", "--abbrev-ref", "HEAD")[1].strip()
    c.branch = "" if branch == "HEAD" else branch
    c.scoped = lambda worktree, sel: scoped_cov.run(Path(worktree), Path(sel), c.sh, c.gate, c.base_sha, env=COVERAGE_ENV)
    return c


def extra_tests(sel: Path | None) -> list[str]:
    if sel is None:
        return []
    try:
        listed = json.loads(sel.expanduser().read_text()).get("python") or []
    except (OSError, ValueError, AttributeError) as why:
        raise Refuse(f"unreadable selection {sel}: {why}", "sm check --help", code=USAGE) from None
    return [str(test).split("::")[0] for test in listed]


@native("check", writes=True, files=("check_lib", "affected.py", "affected_lib", "preflight.py", "preflight_lib"), help=H(
    "The cheap checks of exactly what a Core head changes, in one pass: READY and a receipt for the commit, or every failure with its fix.",
    "Use before you ask for a review and before any push, on your committed head; again after every new commit.",
    "writes the check receipt of the commit on this machine when every check is green (written once per commit; "
    "`sm review verdict --approve` needs it); runs local checks through the run lock.",
    "sm check ~/Code/heinzel-chat-composer",
    "ask for the review; then sm head preflight WORKTREE --sel SEL.json",
    "Checks, in this order; every one runs, so one pass names every finding:\n"
    "  tree        clean tree, and the head has commits of its own over the base\n"
    "  ruff        format and lint of the changed Python files\n"
    "  types       the push hook's type checks (read from Core's scripts/scoped_checks.py), unless only web, desktop,\n"
    "              plugins or documents changed\n"
    "  web         when web files changed: the hook's own web steps (lint, generated client, types) and the Vitest files\n"
    "              the selector names\n"
    "  docs        when documents, ADRs or the site's tooling changed: the hook's own document steps\n"
    "  standards   the repository-wide standards tests (Core's scripts/preflight_standards.txt, else the setting)\n"
    "  tests       the hook's early guards (the contract tests of a changed path) and the tests `sm head select` names\n"
    "              for the changed paths, the docs style test for a document among them\n"
    "  coverage    when src/ changed: scoped coverage of the whole changed files by those tests (>= 90 %)\n"
    "  bound       HEAD and the tree did not change while the checks ran\n"
    "Which test belongs to which path is never listed here: Core's selector, hook and standards list at the commit say it.\n"
    "The head is compared with its merge-base with --base. When the selector selects the full set, Core's\n"
    "scripts/related_tests.py narrows the unit tests; when that cannot either, the suite is NOT run and the tests check warns:\n"
    "the push hook and CI run it. E2E lanes never run here. --sel FILE adds tests ({\"python\": [files]}).\n"
    "The receipt is bound to the commit: any new commit needs a new run. A run with --skip, a red run and a dirty tree write none.\n"
    "Exit 0 READY, 1 NOT READY, 2 usage."))
def check(worktree: Path | None = typer.Argument(None, metavar="WORKTREE", help="Core worktree whose HEAD is the head to check (default: the current directory)."),
          base: str | None = typer.Option(None, "--base", metavar="REV", help="The ref the head branched from (default: the setting preflight_base)."),
          sel: Path | None = typer.Option(None, "--sel", metavar="FILE", help='More tests to run and to measure coverage with: JSON {"python": [test files]}.'),
          skip: list[str] | None = typer.Option(None, "--skip", metavar="NAME", help="Leave one check out (repeatable); named in the verdict, and no receipt is written.")) -> HeadCheck:
    rt, usage = runtime(), "sm check --help"
    providers: Any = rt.providers
    wt = _preflight.core_worktree(worktree or Path.cwd(), usage)
    skipped = list(skip or [])
    registry = cheap.checks()
    if unknown := registry.unknown(skipped):
        raise Refuse(f"--skip takes one of {', '.join(cheap.STEPS)}; unknown: {', '.join(unknown)}", usage, code=USAGE)
    ref = base or str(_preflight.setting(rt, "preflight_base"))
    c = bound(rt, wt, ref)
    try:
        selection = affected.selection(select.Repo(wt), c.changed())
    except select.RepoError as why:
        raise Refuse(f"the selector of {wt} cannot be read: {why}", f"sm head select --repo {wt} --base {ref}", code=USAGE) from None
    c.plan = cheap.plan(c, selection, extra_tests(sel))
    steps, left_out, not_run = registry.run(c, set(skipped), say=rt.say)
    report = HeadCheck.of(f"sm check {c.sha[:9]} ({c.branch or 'detached'}) vs {ref} {c.base_sha[:9]}", steps, left_out, not_run, worktree=str(wt), head=c.sha,
                          branch=c.branch, base=c.base_sha, selector=c.plan.selector, changed=len(c.plan.changed),
                          generated_at=datetime.fromtimestamp(providers.clock.now()).astimezone().isoformat(timespec="seconds"))
    if not report.ready:
        report.receipt_note = "none: fix every numbered failure in one commit, then run sm check again"
    elif left_out:
        report.receipt_note = f"none: {', '.join(left_out)} left out by --skip; a receipt needs every check"
    else:
        new = verdicts.record_check(providers.store, c.sha, {"worktree": str(wt), "branch": c.branch, "base": c.base_sha, "at": providers.clock.now(), "command": report.command,
                                                             "checks": [{"name": s.name, "status": s.status, "detail": s.detail} for s in report.steps]})
        report.receipt = True
        report.receipt_note = f"{verdicts.CHECKS} {c.sha} ({'written' if new else 'this commit already had one'}): an approval of this exact commit can be recorded"
    providers.store.save(SUMMARY_KIND, c.sha, report.model_dump(mode="json"))
    return report
