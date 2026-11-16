"""`sm head preflight`, `sm head meta` and `sm head coverage`: what a builder runs on a Core head before the packet
goes to the coordinator.

The mechanism (ordered checks, `--skip`, the verdict, the report) is the core's (`steuermaschine.preflight`). The
checks are `preflight_lib/head.py` (moved), the commit-meta rules `preflight_lib/ci_meta.py` (moved) and scoped
coverage `preflight_lib/scoped_cov.py` (moved). This module is the edge: typed options, the result models, and the
binding of the libraries to the providers and settings of this call. Heavy local commands go through the run lock.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, Field

from steuermaschine import USAGE
from steuermaschine.config import child_environment
from steuermaschine.engine import Refuse
from steuermaschine.github import Gh, GhError
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import NativeRuntime, native, runtime
from steuermaschine.preflight import PreflightReport
from steuermaschine.protocols import Result
from steuermaschine.runlock import Request

from .lock import lock_of
from .preflight_lib import ci_meta, head, scoped_cov, texts

TARGETS = ("check", "check-fast")
SUMMARY_KIND = "heinzel.preflight.summary"  # state store: the last report per head, keyed by the first eight characters of its SHA
Shell = Callable[..., tuple[int, str]]


class PreflightSettings(BaseModel):
    """Settings of the head preflight, a mixin of the plugin's table."""

    preflight_base: str = Field("origin/main", description="The ref a head is compared with: its changes, its commits and its coverage are measured against it.")
    preflight_plugins_repo: Path | None = Field(None, description="A local clone of the plugins repository, for the consumer check of `sm head preflight --consumers`; none: --plugins-repo is needed.")
    preflight_output_dir: Path | None = Field(None, description="Where the summary file and the logs of a preflight go when neither --sel nor --out is given; none: no files, "
                                                                "the summary is kept in the state store only.")
    preflight_standards: list[str] = Field(list(head.DEFAULT_STANDARDS), description="Globs of the repository-wide standards tests every preflight runs, whatever the selection; "
                                                                                      "the Core file scripts/preflight_standards.txt, when it exists, takes their place.")
    preflight_terms_file: Path | None = Field(None, description="The confidential terms, one per line, in a file outside every repository. The commit-meta check hands this path to "
                                                                 "the core's own checker; none: that checker runs with what its environment supplies.")


# ----------------------------------------------------------------------------- result models
class HeadPreflight(PreflightReport):
    """The preflight of one Core head. The same document is written next to the selection as the summary."""

    command: str = "sm head preflight"
    generated_at: str = ""
    arguments: list[str] = []
    worktree: str = ""
    head: str = ""
    branch: str = ""
    base: str = ""
    selection: str | None = None
    selection_sha256: str = ""  # the bytes of the selection this preflight ran with: `sm head admit` adopts it only for the same bytes
    github_calls: int = 0
    hosted_lanes: list[str] = []  # the hosted e2e lanes the head still needs (web or e2e files changed), run by `sm head validate`
    summary_file: str = ""
    next: str = ""

    def render(self) -> list[str]:
        where = self.summary_file or f"state store, {SUMMARY_KIND} {self.head[:8]} (no file: give --sel or --out, or set preflight_output_dir)"
        return [*super().render(), f"summary: {where}", *([f"next: {self.next}"] if self.next else [])]


class CommitMeta(Model):
    """Commit subjects, PR title, branch name and confidential terms of one head, checked as CI checks them."""

    worktree: str
    branch: str = ""
    pull_request: int | None = None
    title: str | None = None
    commits: int = 0
    problems: list[str] = []
    notes: list[str] = []

    @property
    def exit_code(self) -> int:
        return 1 if self.problems else 0

    def render(self) -> list[str]:
        if self.problems:
            return [*self.notes, f"commit meta FAIL ({len(self.problems)}), fix BEFORE pushing:", *("  - " + p for p in self.problems),
                    "  commit subjects cannot change after a push (no force-push): reword BEFORE the first push, else rebuild the branch from origin/main."]
        return [*self.notes, f"commit meta PASS ({self.commits} commits" + (", title" if self.title else "") + ")"]


class ScopedCoverage(Model):
    """Scoped coverage of one head: lines and branch arcs of the whole changed files under src/, by the selected tests."""

    worktree: str
    selection: str
    applies: bool = True  # False: no Python file under src/ changed
    percent: float | None = None  # None: no result (a selected test failed, or the worktree has no environment)
    threshold: float = scoped_cov.THRESHOLD
    files: list[str] = []
    code: int = 0  # exit code of the test run
    output: list[str] = []  # the end of the run's output when there is no result

    @property
    def exit_code(self) -> int:
        if not self.applies:
            return 0
        return self.code or (1 if self.percent is None or self.percent < self.threshold else 0)

    def render(self) -> list[str]:
        if not self.applies:
            return ["no Python under src/ changed: scoped coverage does not apply"]
        if self.percent is None:
            return [*self.output, f"scoped coverage gave no result (exit {self.code}): a selected test failed, or the worktree has no .venv (`uv sync --locked` there)"]
        return self.files


# ----------------------------------------------------------------------------- the edges of one call
def setting(rt: NativeRuntime, name: str) -> Any:
    value = rt.settings.get(name) if rt.settings else None
    return PreflightSettings.model_fields[name].default if value is None else value


def path_setting(rt: NativeRuntime, name: str) -> Path | None:
    value = setting(rt, name)
    return Path(str(value)).expanduser() if value else None


def shell(commands: Any) -> Shell:
    """One process through the `Commands` provider, as (exit code, stdout + stderr). The provider has no stdin: text
    for the command's standard input goes through a temporary file and a shell redirect."""

    def sh(cmd: Sequence[object], cwd: object = None, input: str | None = None, env: dict[str, str] | None = None) -> tuple[int, str]:
        argv, where = [str(word) for word in cmd], str(cwd) if cwd else None
        if input is None:
            done = commands.run(argv, cwd=where, env=env)
        else:
            with tempfile.NamedTemporaryFile("w", suffix=".txt") as handle:
                handle.write(input)
                handle.flush()
                done = commands.run(["sh", "-c", 'exec "$@" < "$0"', handle.name, *argv], cwd=where, env=env)
        return done.rc, done.out + done.err

    return sh


class Captured:
    """The run lock's runner for one preflight step: the command runs in its directory and its output is kept for the
    report (the failure quotes it) instead of going to the terminal, where it would end up inside the result."""

    def __init__(self, commands: Any, cwd: str | None):
        self.commands, self.cwd, self.output = commands, cwd, ""

    def run(self, argv: list[str], cwd: str | None = None, timeout: float | None = None) -> Result:
        done: Result = self.commands.run(argv, cwd=cwd, timeout=timeout)
        return done

    def passthrough(self, argv: list[str], cwd: str | None = None, env: dict[str, str] | None = None, timeout: float | None = None) -> int:
        done = self.commands.run(argv, cwd=self.cwd, timeout=timeout, env=env)
        self.output = done.out + done.err
        return int(done.rc)


def gated(allow_full: bool = False) -> Shell:
    """One heavy command through the run lock, as (exit code, output). 75: no slot; 2: the lock refuses the command."""

    def gate(cmd: Sequence[object], cwd: object = None, env: dict[str, str] | None = None) -> tuple[int, str]:
        gate_lock = lock_of(allow_full)
        runner = Captured(gate_lock.commands, str(cwd) if cwd else None)
        gate_lock.commands = runner
        outcome = gate_lock.run(Request(argv=[str(word) for word in cmd], environ=child_environment() | (env or {}), cwd=str(cwd or "")))
        if outcome.status != "ran":
            return outcome.exit_code, f"sm gate: {'refusing: ' if outcome.status == 'refused' else ''}{outcome.reason}"
        return outcome.exit_code, runner.output

    return gate


def meta_of(rt: NativeRuntime, worktree: Path, ref: str = "", pr: int | None = None, *, quote: bool = True) -> CommitMeta:
    """`quote=False`: a problem line names where a text is (commit 2 subject, branch name), never the text, and a
    check that could not be made raises `Refuse` (what a command that publishes needs)."""
    providers: Any = rt.providers
    repo, terms = str(rt.settings.get("repo") or ""), path_setting(rt, "preflight_terms_file")
    gh = Gh(providers.commands, repo=repo, clock=providers.clock)
    base, number = str(setting(rt, "preflight_base")), str(pr) if pr else None
    try:
        if quote:
            found = ci_meta.check(worktree, providers.commands, gh, repo, ref, number, base, terms)
        else:
            subjects, title, branch = ci_meta.gather(worktree, providers.commands, gh, repo, ref, number, base)
            found = {"problems": texts.meta_check(providers.commands, Path(worktree), subjects, title, branch, number, repo, terms),
                     "commits": len(subjects), "title": title, "branch": branch}
    except (GhError, KeyError, TypeError) as err:
        raise Refuse(f"could not read pull request {pr}: {err}", "sm head meta WORKTREE") from None
    except texts.Unchecked as why:
        raise Refuse(f"the commit subjects and the branch name were not checked, nothing may be published: {why}", "sm self config") from None
    where = f"no terms file at {terms}" if terms else "no terms file is configured (setting preflight_terms_file)"
    notes = [] if terms and terms.is_file() else [f"note: {where}: confidential terms are checked only if the environment supplies them"]
    return CommitMeta(worktree=str(worktree), branch=found["branch"], pull_request=pr, title=found["title"], commits=found["commits"],
                      problems=found["problems"], notes=notes)


def texts_check(rt: NativeRuntime, worktree: Path, *, title: str, body: str = "") -> list[str]:
    """The problems of a pull request title and body that are about to be published; [] when both may be.
    The title must be a conventional commit subject of at most 72 characters; title and body must be free of the
    confidential terms (the head's own checker, with the terms file of the setting `preflight_terms_file`).
    A line names the rule and the fix, never the text and never a term. Reads only. Raises `Refuse` when the terms
    cannot be checked (no checker in the worktree, an unreadable terms file, no terms, a checker without a verdict):
    an unchecked text is not a clean one."""
    providers: Any = rt.providers
    try:
        return texts.check(providers.commands, Path(worktree).expanduser(), title, body, path_setting(rt, "preflight_terms_file"))
    except texts.Unchecked as why:
        raise Refuse(f"title and body were not checked, nothing may be published: {why}", "sm self config") from None


def bound(rt: NativeRuntime, worktree: Path, allow_full: bool = False) -> head.Ctx:
    """The context of one preflight: the moved checks, bound to this call's providers and settings."""
    providers: Any = rt.providers
    repo, base = str(rt.settings.get("repo") or ""), str(setting(rt, "preflight_base"))
    head.bind(base)
    sh, gate, gh = shell(providers.commands), gated(allow_full), Gh(providers.commands, repo=repo, clock=providers.clock)

    def read(args: list[str]) -> tuple[int, str]:
        done = gh.run(args)
        return done.rc, done.out + done.err

    def meta(c: head.Ctx) -> tuple[int, str]:
        found = meta_of(rt, c.wt, c.branch or c.sha)
        return found.exit_code, "\n".join(found.render())

    def scoped(wt: Path, sel: Path) -> tuple[int, str]:
        return scoped_cov.run(Path(wt), Path(sel), sh, gate, base)

    return head.Ctx(wt=worktree, standards=list(setting(rt, "preflight_standards")), sh=sh, gate=gate, meta=meta, scoped=scoped, gh=read, repo=repo)


def core_worktree(worktree: Path, usage: str) -> Path:
    wt = worktree.expanduser().resolve()
    if not (wt / "pyproject.toml").is_file():
        raise Refuse(f"{wt} is not a Core worktree (no pyproject.toml)", usage, code=USAGE)
    return wt


# ----------------------------------------------------------------------------- the commands
@native("head preflight", writes=False, replaces=("hz-preflight-head",), help=H(
    "One preflight of a Core head before its packet goes to the coordinator: READY, or the exact failures, each with its fix.",
    "Use before handing a packet over, on the committed head: it reproduces what hosted CI and the release candidate check.",
    "nothing outside this machine: local checks through the run lock, one GitHub read, its summary JSON and logs next to SEL (or --out).",
    "sm head preflight ~/Code/heinzel-chat-composer --sel ~/Code/packets/chat/SEL.json",
    "sm head validate WORKTREE --lanes ... when READY names hosted lanes (web or e2e files changed), else sm head admit WORKTREE",
    "Checks, in this order (one is left out only by --skip NAME, and the verdict names every skipped one):\n"
    "  tree        clean tree, head ahead of the base, merge-base == base tip, local base == remote main\n"
    "  lint        `make lint` (format, ruff, contracts, typos, ...) through the run lock\n"
    "  typos       `uv run typos` over the changed files and over the commit messages\n"
    "  types       the hook's type checks (read from scripts/scoped_checks.py: mypy and pyright, strict) over the whole project\n"
    "              for every change that is not web, desktop, plugins or documents only\n"
    "  standards   the repository-wide standards tests, always: the Core file scripts/preflight_standards.txt (every pattern\n"
    "              must match, the list may not be empty), else the setting preflight_standards (an unmatched pattern warns,\n"
    "              no file at all fails). The hook does not read either list\n"
    "  related     exactly the unit tests Core's own scripts/related_tests.py selects for the changed files (the hook's\n"
    "              selection, uncapped; ALL runs the unit suite or fails), plus at most 30 extras: integration tests naming a\n"
    "              changed CLI or access module, e2e output rules for a web/e2e driver, the documentation test for CLI help.\n"
    "              Without that script an ast import scan stands in and the step warns\n"
    "  meta        `sm head meta`: conventional commit subjects <= 72 chars, branch name, confidential terms\n"
    "  coverage    when src/ changed: `sm head coverage` of SEL (>= 90 %; prints the margin, warns under 2 points)\n"
    "  dup         Sonar duplication estimate: new lines inside a 10-line / 100-token block that exists twice; fails at\n"
    "              >= 2.5 % of new lines (the Sonar gate is 3 %); honours sonar.cpd.exclusions\n"
    "  smells      WARN-ONLY HEURISTIC for the Sonar new-code maintainability rating: functions or control flow nested\n"
    "              more than 4 deep, `await` inside a loop, on changed JS/TS/Python lines. No Sonar run\n"
    "  deps        [project] dependencies or optional-dependencies changed vs the base -> FAIL: plugin consumers need a\n"
    "              relock first. Unchanged and --consumers NAME: build the wheel of the head and run the Consumer check\n"
    "              of that plugin at the plugins ref pinned in the CI workflow\n"
    "  e2e         changed drivers under web/e2e: every owning journey and every journey whose driver imports a changed\n"
    "              helper must be a SEL browser id, or be listed under local_only with a reason\n"
    "  validation  refuses a head SHA that already has check suites on a validation/* branch: checks are posted by SHA,\n"
    "              the PR would sit BLOCKED with every check green\n"
    "SEL is the packet selection ({\"python\": [...], \"web\": [...], \"browser\": [...]}). Journeys kept off hosted CI go\n"
    "under \"local_only\": {\"tests/e2e/x_test.py\": \"reason\"} in SEL, or in the file given by --local-only.\n"
    "The hosted e2e lanes are not run here: READY ends with the `sm head validate` command and its lanes when the head\n"
    "touches web or e2e files. The summary is preflight-head-<sha8>.json next to SEL (--out DIR overrides).\n"
    "Exit 0 READY, 1 NOT READY, 2 usage. --consumers builds a wheel of the head and checks the plugin in a temporary\n"
    "worktree of the plugins repository; its target `check` is a full suite the run lock refuses unless the coordinator\n"
    "adds --allow-full, `--consumers-target check-fast` needs no such call."))
def head_preflight(worktree: Path = typer.Argument(..., metavar="WORKTREE", help="Core worktree whose HEAD is the head to hand over."),
                   sel: Path | None = typer.Option(None, "--sel", metavar="FILE", help="Packet selection JSON (python / web / browser ids, optional local_only)."),
                   local_only: Path | None = typer.Option(None, "--local-only", metavar="FILE", help='JSON file {"tests/e2e/x_test.py": "reason"}: journeys kept off hosted CI.'),
                   consumers: str | None = typer.Option(None, "--consumers", metavar="PLUGIN", help="Also run the Consumer check of this plugin against a wheel of the head."),
                   consumers_target: str = typer.Option("check", "--consumers-target", help="The plugins make target: check (as CI) or check-fast."),
                   plugins_repo: Path | None = typer.Option(None, "--plugins-repo", metavar="DIR", help="Local plugins repository (default: the setting preflight_plugins_repo)."),
                   skip: list[str] | None = typer.Option(None, "--skip", metavar="NAME", help="Leave one check out (repeatable); named in the verdict and the summary."),
                   out: Path | None = typer.Option(None, "--out", metavar="DIR", help="Directory for the summary JSON and the logs (default: next to SEL)."),
                   allow_full: bool = typer.Option(False, "--allow-full", help="Let the consumer check run the full `make check` (the coordinator's call; logged).")) -> HeadPreflight:
    rt, usage = runtime(), "sm head preflight --help"
    providers: Any = rt.providers
    wt = core_worktree(worktree, usage)
    skipped = list(skip or [])
    if unknown := head.checks().unknown(skipped):
        raise Refuse(f"--skip takes one of {', '.join(head.STEPS)}; unknown: {', '.join(unknown)}", usage, code=USAGE)
    if consumers_target not in TARGETS:
        raise Refuse(f"--consumers-target is one of {', '.join(TARGETS)}", usage, code=USAGE)
    c = bound(rt, wt, allow_full)
    try:
        head.select(c, str(sel) if sel else None, str(local_only) if local_only else None)
    except (OSError, ValueError, AttributeError) as e:
        raise Refuse(f"unreadable selection: {e}", usage, code=USAGE) from None
    c.outdir = out.expanduser().resolve() if out else c.sel_path.parent if c.sel_path else path_setting(rt, "preflight_output_dir")
    plugins = plugins_repo.expanduser() if plugins_repo else path_setting(rt, "preflight_plugins_repo")
    steps, left_out, not_run = head.run(c, set(skipped), consumers, consumers_target, plugins, rt.say)
    path = c.outdir / f"preflight-head-{c.sha[:8]}.json" if c.outdir else None
    arguments = [str(wt), *(["--sel", str(c.sel_path)] if c.sel_path else []), *(["--local-only", str(local_only)] if local_only else []),
                 *(["--consumers", consumers, "--consumers-target", consumers_target] if consumers else []), *[word for name in skipped for word in ("--skip", name)],
                 *(["--out", str(out)] if out else []), *(["--allow-full"] if allow_full else [])]
    report = HeadPreflight.of(head.title(c), steps, left_out, not_run, arguments=arguments, worktree=str(wt), head=c.sha, branch=c.branch, base=c.base_sha,
                              generated_at=datetime.fromtimestamp(providers.clock.now()).astimezone().isoformat(timespec="seconds"),
                              selection=str(c.sel_path) if c.sel_path else None, github_calls=c.gh_calls, summary_file=str(path) if path else "",
                              selection_sha256=hashlib.sha256(c.sel_bytes).hexdigest() if c.sel_path else "")
    if report.ready:
        changed = c.changed()
        full = head.full_set(c) if head.hosted_wanted(changed) else None
        if full is not None:
            report.next = f"full set ({full}): hosted PR CI is the validation; sm head admit {wt}"
        elif head.hosted_wanted(changed):
            report.hosted_lanes = head.hosted_lanes(changed)
            report.next = f"sm head validate {wt} --lanes {','.join(report.hosted_lanes)}"
        else:
            report.next = f"sm head admit {wt}"
    providers.store.save(SUMMARY_KIND, c.sha[:8], report.model_dump(mode="json"))  # the last preflight of this head, for `sm head admit` and the coordinator
    if path:
        report.save(path)
    return report


@native("head meta", writes=False, replaces=("hz-preflight-ci-meta",), help=H(
    "The CI commit-meta check before a push: conventional subjects of at most 72 characters, branch name, confidential terms.",
    "Use before the first push of a branch and before a PR is opened; with --pr also for the title of an open PR.",
    "nothing (read-only).",
    "sm head meta ~/Code/heinzel-chat-composer",
    "sm head preflight WORKTREE",
    "Checks the commit subjects since the base (conventional per the worktree's pyproject schema_pattern), the branch name and,\n"
    "with --pr N, the title and the commits of that pull request. Each problem names its exact fix. Exit 0 PASS, 1 FAIL.\n"
    "Subjects cannot change after a push (no force-push): reword BEFORE the first push. A title edit is NOT seen by a rerun\n"
    "(stale event payload): close and reopen the PR.\n"
    "Confidential terms come from a file outside every repository (setting preflight_terms_file); the core's own checker\n"
    "reads it and names a finding by its number, never by the term."))
def head_meta(worktree: Path = typer.Argument(..., metavar="WORKTREE", help="Core worktree on the branch to check."),
              pr: int | None = typer.Option(None, "--pr", metavar="N", help="Check this pull request instead: its title, branch and commits.")) -> CommitMeta:
    return meta_of(runtime(), core_worktree(worktree, "sm head meta --help"), pr=pr)


@native("head coverage", writes=False, replaces=("hz-scoped-cov",), help=H(
    "Scoped coverage of a head, as the hosted 'PR scoped coverage' check measures it: whole changed src/ files, serial run, 90 %.",
    "Use before admitting any Core head that changes src/; `sm head preflight` runs it as its coverage check.",
    "nothing outside this machine: runs the selected tests under coverage in the worktree's environment, through the run lock.",
    "sm head coverage ~/Code/heinzel-chat-composer ~/Code/packets/chat/SEL.json",
    "sm head preflight WORKTREE --sel SEL.json",
    "SEL is {\"python\": [test files]}. Production = the Python files under src/ changed between the base and HEAD; the\n"
    "denominator is every executable line plus every branch arc of those WHOLE files. One line per file with its missing\n"
    "lines and branches, then `SCOPED n% (covered/total) threshold 90`. Exit 0 at or above 90 %, 1 below it or without a result."))
def head_coverage(worktree: Path = typer.Argument(..., metavar="WORKTREE", help="Core worktree at the head to measure (with its .venv)."),
                  sel: Path = typer.Argument(..., metavar="SEL", help='Selection JSON: {"python": [test files]}.')) -> ScopedCoverage:
    rt = runtime()
    providers: Any = rt.providers
    wt, base = core_worktree(worktree, "sm head coverage --help"), str(setting(rt, "preflight_base"))
    sh, selection = shell(providers.commands), sel.expanduser().resolve()
    if not scoped_cov.production(sh, wt, base):
        return ScopedCoverage(worktree=str(wt), selection=str(selection), applies=False)
    code, text = scoped_cov.run(wt, selection, sh, gated(), base)
    if code == USAGE:
        raise Refuse(text, "sm head coverage --help", code=USAGE)
    percent = head.parse_scoped(text)
    lines = text.strip().splitlines()
    return ScopedCoverage(worktree=str(wt), selection=str(selection), percent=percent, code=code,
                          files=[line for line in lines if " missing lines " in line or line.startswith("SCOPED ")] if percent is not None else [],
                          output=lines[-30:] if percent is None else [])
