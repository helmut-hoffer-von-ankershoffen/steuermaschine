"""Preflight: named checks of one piece of work, run in a fixed order before it is handed over. The answer is READY,
or NOT READY with a numbered list of the exact failures, each with its fix.

    checks = Preflight()

    @checks.check("tree")
    def tree(ctx) -> Step:
        step = Step("tree")
        if ctx.dirty:
            step.fail("working tree is not clean (3 paths)", "commit the change or remove the stray files")
        step.detail = "2 commit(s) ahead"                    # the one summary line of a check
        return step

    @checks.check("hosted", opt_in=True, after_green=True)   # only when asked for, and only when nothing before it failed
    def hosted(ctx) -> Step: ...

    steps, skipped, not_run = checks.run(ctx, skip={"lint"}, opted={"hosted"}, say=print)
    report = PreflightReport.of("preflight of abc123", steps, skipped, not_run)
    report.render()      the text;  report.exit_code is 0 for READY and 1 for NOT READY
    report.save(path)    the summary document: the report as JSON, the same document `--format json` prints

A plugin registers its checks on a `Preflight` and decides what `ctx` is; the checks get it unchanged.

Rules:
- A check returns a `Step`: PASS, WARN (it passed, with something to know), FAIL or SKIP; `detail` is its summary line.
  `fail(message, fix)` records one failure: the first line of the message says what is wrong, further lines are
  evidence, `fix` is the one thing to do. `warn(text)` never makes the verdict NOT READY.
- A check is left out only when it is named in `skip`, and the verdict names every skipped check.
- An opt-in check runs only when it is named in `opted`; the verdict names every opt-in check that did not run.
- A check with `after_green` runs only when no check before it failed; otherwise it is a SKIP row that says why.
- READY means: no check recorded a failure.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from .models import Model

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
BLOCKED = "not run: fix the failures above first"


@dataclass
class Step:
    """What one check found."""

    name: str
    status: str = PASS  # PASS, WARN, FAIL, SKIP
    detail: str = ""
    failures: list[tuple[str, str]] = field(default_factory=list)  # (message, fix)
    data: dict[str, Any] = field(default_factory=dict)

    def fail(self, message: str, fix: str) -> Step:
        self.status = FAIL
        self.failures.append((message, fix))
        return self

    def warn(self, text: str) -> Step:
        if self.status == PASS:
            self.status = WARN
        self.data.setdefault("warnings", []).append(text)
        return self


@dataclass(frozen=True)
class Check:
    name: str
    run: Callable[[Any], Step]
    opt_in: bool = False  # runs only when named in `opted`
    after_green: bool = False  # runs only when no check before it failed
    blocked: str = BLOCKED  # the detail of its SKIP row when a check before it failed


class Preflight:
    """The checks of one kind of work, in the order they run."""

    def __init__(self) -> None:
        self.checks: list[Check] = []

    def add(self, name: str, run: Callable[[Any], Step], *, opt_in: bool = False, after_green: bool = False, blocked: str = BLOCKED) -> None:
        if name in self.names:
            raise ValueError(f"a check named {name!r} is already registered")
        self.checks.append(Check(name, run, opt_in, after_green, blocked))

    def check(self, name: str, *, opt_in: bool = False, after_green: bool = False, blocked: str = BLOCKED) -> Callable[[Callable[[Any], Step]], Callable[[Any], Step]]:
        def register(run: Callable[[Any], Step]) -> Callable[[Any], Step]:
            self.add(name, run, opt_in=opt_in, after_green=after_green, blocked=blocked)
            return run

        return register

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks)

    def unknown(self, names: Iterable[str]) -> list[str]:
        """The given names that are no check: for refusing a mistyped `--skip` before anything runs."""
        return [name for name in names if name not in self.names]

    def run(self, ctx: Any, skip: Iterable[str] = (), opted: Iterable[str] = (), not_run: Iterable[str] = (),
            say: Callable[[str], None] = lambda text: None) -> tuple[list[Step], list[str], list[str]]:
        """Run the checks in order. -> (steps, skipped names, names of opt-in parts that did not run).
        `not_run`: opt-in parts the caller itself left out (an optional part inside a check), named in the verdict."""
        skip, opted = set(skip), set(opted)
        steps: list[Step] = []
        left = list(not_run)
        for check in self.checks:
            if check.name in skip:
                steps.append(Step(check.name, SKIP, "skipped by --skip"))
                continue
            if check.opt_in and check.name not in opted:
                left.append(check.name)
                continue
            if check.after_green and any(s.failures for s in steps):
                steps.append(Step(check.name, SKIP, check.blocked))
                left.append(check.name)
                continue
            say(f"... {check.name}")
            steps.append(check.run(ctx))
        return steps, [name for name in self.names if name in skip], left


def verdict(steps: list[Step], skipped: list[str], not_run: list[str]) -> tuple[str, list[tuple[str, str, str]]]:
    """-> (the verdict line, [(check, message, fix)])."""
    failures = [(s.name, m, fix) for s in steps for m, fix in s.failures]
    flags = (f" (SKIPPED by flag: {', '.join(skipped)})" if skipped else "") + (f" (opt-in not run: {', '.join(not_run)})" if not_run else "")
    return (f"NOT READY: {len(failures)} failure(s){flags}" if failures else f"READY{flags}"), failures


class CheckFailure(Model):
    number: int
    step: str
    message: str
    fix: str


class CheckRow(Model):
    name: str
    status: str  # PASS | WARN | FAIL | SKIP
    detail: str = ""
    data: dict[str, Any] = {}  # what the check measured; its warnings are data["warnings"]


class PreflightReport(Model):
    """The result of one preflight, and its summary document. A plugin subclasses it to add what its work is."""

    title: str
    ready: bool
    verdict: str
    skipped: list[str] = []
    not_run: list[str] = []
    failures: list[CheckFailure] = []
    steps: list[CheckRow] = []

    @classmethod
    def of(cls, title: str, steps: list[Step], skipped: list[str], not_run: list[str], **more: Any) -> Self:
        line, failures = verdict(steps, skipped, not_run)
        return cls(title=title, ready=not failures, verdict=line, skipped=list(skipped), not_run=list(not_run),
                   failures=[CheckFailure(number=n, step=name, message=m, fix=fix) for n, (name, m, fix) in enumerate(failures, 1)],
                   steps=[CheckRow(name=s.name, status=s.status, detail=s.detail, data=s.data) for s in steps], **more)

    @property
    def exit_code(self) -> int:
        return 0 if self.ready else 1

    def render(self) -> list[str]:
        out = [self.title]
        for s in self.steps:
            first = next(((f.message.splitlines() or [""])[0] for f in self.failures if f.step == s.name), "")
            out.append(f"  [{s.status}] {s.name}: {s.detail or first}")
            out += [f"         warning: {w}" for w in s.data.get("warnings", [])]
        out.append(self.verdict)
        for f in self.failures:
            out.append(f"  {f.number}. {f.step}: {f.message}")
            out.append(f"     fix: {f.fix}")
        return out

    def save(self, path: Path) -> Path:
        """Write the summary document (this report as JSON) and return its path."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.model_dump(mode="json"), indent=1) + "\n")
        return path
