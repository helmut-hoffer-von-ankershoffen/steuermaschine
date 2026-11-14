"""`sm head select`: changed files of a core pull request -> the minimal tests, lanes and CI jobs. Read-only.

The selection is the checkout's own: its scripts/affected.py `classify` (the moved copy in affected_lib only when the
checkout has none), and with Core's merge tier (ADR 0149) its `focused_lanes`, the lanes CI runs or None for the full set.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, Field

from steuermaschine import USAGE
from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime

from .affected_lib import select
from .coord_lib.base import child_env


class SelectSettings(BaseModel):
    """Settings of the test selection (mixin of the plugin's settings table)."""

    select_repo_dir: Path | None = Field(None, description="The core checkout the test map is read from when --repo is not given; none: the setting core_repo_dir.")


class Selection(Model):
    """The minimal tests of a change. `full` means: run everything (the change is not safely narrowable)."""

    files: list[str] = []
    full: bool = False
    pytest: list[str] = []
    vitest: list[str] = []
    desktop: list[str] = []  # merge tier: the desktop unit tests, ["ALL"] for the whole suite
    e2e_lanes: list[str] = []
    jobs: list[str] = []
    reasons: list[str] = []
    repository: str = ""
    self_test: list[str] = []  # with --self-test: one line per built-in case
    failures: int = 0
    selector: str = ""  # core: the checkout's scripts/affected.py; port: the moved copy (the checkout has none)
    lanes: dict[str, list[str]] | None = None  # merge tier: the focused CI lanes; None with lanes_reason: the full set
    lanes_reason: str = ""  # "focused", or why CI runs the full set; empty: the checkout has no focused_lanes

    model_config = {**Model.model_config, "extra": "allow"}

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0

    def render(self) -> list[str]:
        if self.self_test:
            return self.self_test
        lines = select.render(self.model_dump()).splitlines()
        if self.desktop:
            lines.insert(5, f"desktop ({len(self.desktop)}): " + " ".join(self.desktop[:12]) + (f" ... +{len(self.desktop) - 12}" if len(self.desktop) > 12 else ""))
        if self.lanes_reason:
            lines.append("focused CI lanes: " + ("; ".join(f"{lane} ({', '.join(t[:3])}{' ...' if len(t) > 3 else ''})" for lane, t in self.lanes.items())
                                                 if self.lanes is not None else f"none, CI runs the FULL set: {self.lanes_reason}"))
        return lines + ([f"selector: {self.selector}"] if self.selector else [])


def core_selector(checkout: select.Repo) -> tuple[Callable[[list[str]], dict[str, Any]], Callable[[dict[str, Any]], tuple[Any, str]] | None] | None:
    """(classify, focused_lanes or None) from the checkout's own scripts/affected.py, or None when it has none.

    Loaded under a name of its own (two checkouts never share a module); its import graph is the one the moved Repo
    loads the same way, so the checkout's related_tests.py runs, as before, and nothing else of it."""
    script = checkout.root / "scripts/affected.py"
    if not script.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"core_affected_{abs(hash(str(checkout.root)))}", script)
    if spec is None or spec.loader is None:
        raise select.RepoError(f"{script} cannot be loaded")
    module: Any = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        repo = module.Repo(checkout.root)
    except Exception as exc:  # the checkout's own code: any failure is a refusal, never a traceback
        raise select.RepoError(f"{script} failed: {exc}") from None
    repo.__dict__["related"] = checkout.related  # not Repo.related: it imports `related_tests` by a global name
    lanes = getattr(module, "focused_lanes", None)
    return (lambda changed: dict(module.classify(repo, changed)),
            (lambda result: lanes(result, {} if result["full"] else repo.lanes)) if lanes else None)


def selection(checkout: select.Repo, changed: list[str]) -> dict[str, Any]:
    core = core_selector(checkout)
    if core is None:
        return {**select.classify(checkout, changed), "selector": "port"}
    classify, lanes = core
    result = classify(changed)
    if lanes is not None:
        found, reason = lanes(result)
        result |= {"lanes": found, "lanes_reason": reason}
    return {**result, "selector": "core"}


# The merge tier's desktop and lane cases (Core ADR 0149), checked when the checkout's selector has a desktop key.
DESKTOP_CASES: list[tuple[str, list[str], dict[str, Any]]] = [
    ("desktop app file", ["desktop/src/main.js"], {"full": False, "jobs_has": ["electron"], "desktop_some": True, "lanes_has": ["desktop"]}),
    ("desktop test file", ["desktop/test/window.test.js"], {"full": False, "desktop_some": True, "jobs_not": ["test-linux"]}),
    ("desktop Markdown", ["desktop/README.md"], {"full": False, "desktop": []}),
    ("desktop managed = full", ["desktop/managed/src/main.ts"], {"full": True, "desktop": ["ALL"], "lanes": None}),
    ("desktop dependencies = full", ["desktop/package.json"], {"full": True}),
]


def _problems(got: dict[str, Any], want: dict[str, Any]) -> list[str]:
    problems = []
    for key, value in want.items():
        if key == "desktop_some":
            problems += [] if got.get("desktop") else ["desktop is empty"]
        elif key == "lanes_has":
            problems += [f"lanes lack {lane}" for lane in value if "lanes" in got and lane not in (got["lanes"] or {})]
        elif key.endswith("_has"):
            problems += [f"{key[:-4]} lacks {[v for v in value if v not in got[key[:-4]]]}"] if any(v not in got[key[:-4]] for v in value) else []
        elif key.endswith("_not"):
            problems += [f"{key[:-4]} should not have {[v for v in value if v in got[key[:-4]]]}"] if any(v in got[key[:-4]] for v in value) else []
        elif key in got and got[key] != value:
            problems.append(f"{key} = {got[key]!r}, want {value!r}")
    if got["full"] is False and got["pytest"] == ["ALL"]:
        problems.append("pytest ALL without full")
    return problems


def run_self_test(checkout: select.Repo) -> tuple[int, list[str]]:
    """The built-in cases against the checkout's own selector; the desktop cases when it selects desktop tests."""
    probe = selection(checkout, [])
    if probe["selector"] == "port":
        return select.self_test(checkout)
    cases = [*select.CASES, *(DESKTOP_CASES if "desktop" in probe else [])]
    failures, lines = 0, []
    for name, files, want in cases:
        got = selection(checkout, files)
        problems = _problems(got, want)
        lanes = "" if "lanes" not in got else f", CI lanes {sorted(got['lanes']) if got['lanes'] is not None else 'FULL'}"
        lines.append(f"{'ok ' if not problems else 'FAIL'} {name}: {len(got['pytest'])} pytest, {len(got['vitest'])} vitest, "
                     f"{len(got.get('desktop') or [])} desktop, lanes {got['e2e_lanes']}{lanes}")
        lines += [f"     {p}" for p in problems]
        failures += bool(problems)
    lines.append(f"{len(cases) - failures}/{len(cases)} cases pass" if not failures else f"{failures} failing")
    return failures, lines


@native("head select", writes=False, replaces=("hz-affected",), help=H(
    "Changed files of a core pull request -> the minimal pytest paths, Vitest files, E2E lanes and CI jobs.",
    "Use before running tests for a head, and to see which CI jobs a change needs.",
    "nothing (read-only: the checkout's Makefile, tests and import graph; git diff and status with --base).",
    "sm head select --repo ~/Code/heinzel-logo --base origin/main",
    "sm gate run -- pytest <the printed paths>",
    "Give FILE paths relative to the repository root, or --base REV for the files the checkout differs in (three-dot\n"
    "diff plus the working tree), or --stdin (one path per line). Conservative on purpose: workflows, Makefile,\n"
    "pyproject, the lockfile, scripts, the kernel, installers, the updater, the daemon, credentials, release code,\n"
    "shared fixtures and every path the map does not know select the FULL set. The checkout's own scripts/affected.py selects;\n"
    "with Core's merge tier it also prints the focused CI lanes (focused_lanes) or why CI runs the full set. --self-test runs\n"
    "the built-in cases (and the desktop cases of the merge tier) against the checkout and exits 1 on a mismatch. Exit 2 when\n"
    "the directory is not a core checkout."))
def head_select(files: list[str] | None = typer.Argument(None, metavar="[FILE]...", help="Changed paths, relative to the repository root."),
                repo: Path | None = typer.Option(None, "--repo", metavar="DIR", help="The core checkout or worktree to read the map from."),
                base: str | None = typer.Option(None, "--base", metavar="REV", help="Add the files the checkout differs from REV in."),
                stdin: bool = typer.Option(False, "--stdin", help="Read more paths from standard input, one per line."),
                self_test: bool = typer.Option(False, "--self-test", help="Run the built-in cases against the checkout.")) -> Selection:
    rt: Any = runtime()
    s = rt.settings or {}
    root = Path(os.path.expanduser(str(repo or s.get("select_repo_dir") or s.get("core_repo_dir") or "")))
    try:
        if not str(repo or s.get("select_repo_dir") or s.get("core_repo_dir") or ""):
            raise select.RepoError("no core checkout is configured; pass --repo DIR")
        checkout = select.Repo(root)
        if self_test:
            failures, lines = run_self_test(checkout)
            return Selection(repository=str(root), self_test=lines, failures=failures)
        changed = list(files or [])
        if stdin:
            changed += list(sys.stdin.read().splitlines())
        if base:
            changed += select.changed_from_git(rt.providers.commands, root, base, child_env(s))
        return Selection(repository=str(root), **selection(checkout, changed))
    except select.RepoError as exc:
        raise Refuse(str(exc), "sm head select --repo DIR FILE...", code=USAGE) from None
