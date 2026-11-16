"""The generic commands `sm ci ...` and `sm pr ...`: built only on the Protocols `ForgeReader`, `CIRuns` and
`QualityProvider` (base.py), so any project has them without a plugin.

Which provider answers, first match:
    1. what a plugin put into `Providers.extra`: "forge_reader", "ci_runs", "quality"
    2. the project's `forge` / `ci` provider, when it satisfies the reader Protocol
    3. the core GitHub provider from the table [github], Sonar from the table [sonar]

Every command prints one result model: text through `render()`, `--format json` as exactly one document.
Exit 0 when the question was answered (the state field carries the answer), 1 when it could not be read
(`unknown: <why>`) or a write was refused, 2 for usage. `ci cancel` is dry unless `--live`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import typer
from pydantic import BaseModel, ValidationError

from .. import PROG, USAGE, config, models
from ..helpdoc import H
from .base import CIRuns, ForgeReader, QualityProvider
from .models import PROVIDER_MODELS, FailedJob, FailedReport, is_unknown
from .settings import GitHubSettings, SonarSettings

if TYPE_CHECKING:
    from ..app import Runtime
    from ..models import Model
    from ..native import NativeRuntime
    from .forge import GitHub

TABLES: dict[str, type[BaseModel]] = {"github": GitHubSettings, "sonar": SonarSettings}  # the core tables this package reads


def unit_anchor() -> None:
    """Anchor of the unit of `ci cancel` (the one write here): this module and every core file it imports."""


def table(rt: Runtime | NativeRuntime, name: str) -> dict[str, Any]:
    """One core table, layered like every setting: environment SM_<TABLE>__<FIELD>, project file, user file, defaults."""
    from ..app import fail

    merged: dict[str, Any] = {}
    for source, data in reversed(rt.project.layers if rt.project is not None else []):  # lowest first
        if not source.startswith(("environment", "command line")) and isinstance(data.get(name), dict):
            merged.update(data[name])
    merged.update({k: v for k, v in config.prefixed(f"SM_{name.upper()}__").items() if k in TABLES[name].model_fields})
    try:
        return TABLES[name].model_validate(merged).model_dump()
    except ValidationError as exc:
        fail(f"[{name}] is not valid: " + "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()), USAGE,
             f"known settings: {', '.join(TABLES[name].model_fields)}")
        raise  # not reached


def _base(rt: Runtime) -> tuple[dict[str, Any], Any, Any, Any, Any]:
    """(extra, forge, ci, commands, clock) of the project, or the plain defaults when there is no project file."""
    if rt.project is None:
        from ..defaults import Subprocess, SystemClock

        return {}, None, None, Subprocess(), SystemClock()
    p = rt.providers
    return p.extra, p.forge, p.ci, p.commands, p.clock


def _github(rt: Runtime, commands: Any, clock: Any) -> GitHub:
    from .forge import GitHub

    return GitHub.from_settings(commands, table(rt, "github"), clock)


def forge_reader(rt: Runtime) -> ForgeReader:
    extra, forge, _, commands, clock = _base(rt)
    for candidate in (extra.get("forge_reader"), forge):
        if isinstance(candidate, ForgeReader):
            return candidate
    return _github(rt, commands, clock)


def ci_runs(rt: Runtime) -> CIRuns:
    extra, forge, ci, commands, clock = _base(rt)
    for candidate in (extra.get("ci_runs"), ci, forge):
        if isinstance(candidate, CIRuns):
            return candidate
    return _github(rt, commands, clock)


def quality(rt: Runtime) -> QualityProvider:
    from ..app import fail

    extra, _, _, _, clock = _base(rt)
    if isinstance(extra.get("quality"), QualityProvider):
        return cast(QualityProvider, extra["quality"])
    s = table(rt, "sonar")
    if not s["project_key"]:
        fail("this project has no quality provider", USAGE, 'add to the settings file:  [sonar]  project_key = "ORGANISATION_PROJECT"')
    from .sonar import Sonar

    return Sonar(s["project_key"], s["host"], token=config.secret(s["token_env"]), timeout=s["timeout_seconds"], clock=clock)


def failed_report(ci: CIRuns, run_id: str, lines: int = 40, max_jobs: int = 5) -> FailedReport:
    """The failed and cancelled jobs of one run, each with the end of its log. At most `max_jobs` logs are read."""
    run = ci.run(run_id)
    if is_unknown(run.status):
        return FailedReport(run=run, state=run.status)
    jobs = ci.jobs(run_id)
    by_name = {j.name: j for j in jobs.jobs}
    wanted = [(by_name[n], "failed") for n in jobs.failed] + [(by_name[n], "cancelled") for n in jobs.cancelled]
    out: dict[str, list[FailedJob]] = {"failed": [], "cancelled": []}
    for job, kind in wanted[:max(0, max_jobs)]:
        out[kind].append(FailedJob(job=job, log=ci.log_tail(job.id, lines)))
    return FailedReport(run=run, state=jobs.state, omitted=[job.name for job, _ in wanted[max(0, max_jobs):]], **out)


def register(rt: Runtime, app: typer.Typer) -> None:
    """Add the groups `ci` and `pr` to the core command tree and publish the result models for `sm self schema`."""
    from ..app import CTX, emit, group

    cast("dict[str, type[Model]]", models.MODELS).update(PROVIDER_MODELS)

    def show(model: Any) -> None:
        emit(False, model.model_dump(), model.render(), type(model))
        raise typer.Exit(model.exit_code)

    def number(ref: str) -> int:
        from ..app import fail

        if not ref.lstrip("#").isdigit():
            fail(f"'{ref}' is not a pull request number", USAGE, f"find it with: {PROG} pr status {ref}")
        return int(ref.lstrip("#"))

    # ------------------------------------------------------------------ ci
    ci = group(app, "ci", "ci: the runs of the project's CI; what is active, what failed and why, cancel one.",
               "Use when CI is slow, queued or red, and before you cancel or re-run anything.",
               "`ci cancel --live` cancels one run on the CI host; every other command here reads only.", f"{PROG} ci active", f"{PROG} ci failed RUN")

    @ci.command("active", context_settings=CTX, short_help="Every run that is not finished: in progress, queued, waiting, pending.", help=H(
        "Every run that is not finished: in progress, queued, waiting, pending.",
        "Use when CI seems slow or stuck. Runner usage depends on the jobs assigned to each run.",
        "nothing (read-only).", f"{PROG} ci active", f"{PROG} ci failed RUN for one that ended red; {PROG} ci cancel RUN for one that should not run"))
    def active(limit: int = typer.Option(100, "--limit", metavar="N", help="At most N runs.")) -> None:
        show(ci_runs(rt).active(limit))

    @ci.command("runs", context_settings=CTX, short_help="The runs of one commit or one branch, newest first.", help=H(
        "The runs of one commit or one branch, newest first.",
        "Use when you need the run id of a head, or want to know whether its CI started at all.",
        "nothing (read-only).", f"{PROG} ci runs --commit 3f2a9c1d0b7e", f"{PROG} ci failed RUN"))
    def runs(commit: str = typer.Option("", "--commit", metavar="SHA", help="Only runs of this head commit (the full SHA)."),
             branch: str = typer.Option("", "--branch", metavar="NAME", help="Only runs of this branch."),
             status: str = typer.Option("", "--status", metavar="STATUS", help="Only this status or conclusion: queued, in_progress, completed, failure, success, cancelled ..."),
             limit: int = typer.Option(20, "--limit", metavar="N", help="At most N runs.")) -> None:
        show(ci_runs(rt).runs(commit=commit, branch=branch, status=status, limit=limit))

    @ci.command("failed", context_settings=CTX, short_help="What failed in one run: its failed and cancelled jobs, each with the end of its log.", help=H(
        "What failed in one run: its failed and cancelled jobs, each with the end of its log.",
        "Use when a run is red or was cancelled, before you re-run it or brief a builder. Works while other jobs still run.",
        "nothing (read-only).", f"{PROG} ci failed 43304628424 --lines 60", "fix the cause on the head; a re-run without a change fails the same way"))
    def failed(run: str = typer.Argument(..., metavar="RUN", help="The run id."),
               lines: int = typer.Option(40, "--lines", metavar="N", help="Log lines per job."),
               jobs: int = typer.Option(5, "--jobs", metavar="N", help="Read the log of at most N jobs; the others are named.")) -> None:
        show(failed_report(ci_runs(rt), run, lines, jobs))

    @ci.command("cancel", context_settings=CTX, short_help="Cancel one run. Dry by default: says what it would do.", help=H(
        "Cancel one run. Dry by default: says what it would do.",
        "Use when a run is superseded or blocks runners, after `ci active` showed which one. Check branch and event first.",
        "with --live: cancels this one run on the CI host (its jobs stop; it cannot be resumed, only re-run). Without --live: nothing.",
        f"{PROG} ci cancel 43304628424", f"the same command with --live, then {PROG} ci active"))
    def cancel(run: str = typer.Argument(..., metavar="RUN", help="The run id."),
               live: bool = typer.Option(False, "--live", help="Really cancel it.")) -> None:
        from ..app import fail

        if not (run.isascii() and run.isdigit()):  # a run id is ASCII digits only: anything else would be spliced into the API path
            fail(f"'{run}' is not a run id (digits only)", USAGE, f"find it with: {PROG} ci active")
        show(ci_runs(rt).cancel_run(run, live=live))

    # ------------------------------------------------------------------ pr
    pr = group(app, "pr", "pr: one pull request; where it stands, its checks, why it does not merge, its code quality.",
               "Use when a pull request should have merged, or before you tell anyone it is ready.",
               "nothing (read-only).", f"{PROG} pr why 12", f"{PROG} pr checks 12")

    @pr.command("status", context_settings=CTX, short_help="One pull request by number, head branch or head commit.", help=H(
        "One pull request by number, head branch or head commit: state, head, base, mergeability.",
        "Use when you need the number of a head's pull request, or want to know whether it is open, merged or closed.",
        "nothing (read-only).", f"{PROG} pr status feat/login", f"{PROG} pr why NUMBER"))
    def status(ref: str = typer.Argument(..., metavar="NUMBER|HEAD", help="A number, a head branch name or a head commit.")) -> None:
        show(forge_reader(rt).pr(ref))

    @pr.command("list", context_settings=CTX, short_help="The open pull requests.", help=H(
        "The open pull requests: number, head, merge state, title.",
        "Use when you need an overview of what is in flight on the code host.",
        "nothing (read-only).", f"{PROG} pr list", f"{PROG} pr why NUMBER"))
    def list_(limit: int = typer.Option(50, "--limit", metavar="N", help="At most N pull requests.")) -> None:
        show(forge_reader(rt).open_prs(limit))

    @pr.command("checks", context_settings=CTX, short_help="The checks of one pull request: required ones apart from optional ones.", help=H(
        "The checks of one pull request: required ones apart from optional ones, and required ones that never reported.",
        "Use when a pull request is not green, or looks green and still does not merge.",
        "nothing (read-only).", f"{PROG} pr checks 12", f"{PROG} ci failed RUN for a red one; {PROG} pr why 12 for everything else"))
    def checks(ref: str = typer.Argument(..., metavar="NUMBER", help="The pull request number.")) -> None:
        show(forge_reader(rt).pr_checks(number(ref)))

    @pr.command("why", context_settings=CTX, short_help="Why one pull request is not mergeable: every cause that can be seen.", help=H(
        "Why one pull request is not mergeable: every cause that can be seen, each with what helps.",
        "Use when a pull request should have merged: missing required contexts, red checks, behind, conflicts, review, threads.",
        "nothing (read-only).", f"{PROG} pr why 12", "the line after `->` of the first cause"))
    def why(ref: str = typer.Argument(..., metavar="NUMBER", help="The pull request number.")) -> None:
        show(forge_reader(rt).why(number(ref)))

    @pr.command("sonar", context_settings=CTX, short_help="Code quality of one pull request: gate, new-code debt ratio, duplication, open issues.", help=H(
        "Code quality of one pull request: gate, new-code debt ratio, duplication, open issues.",
        "Use when the quality gate is red or before you call a pull request clean. Needs [sonar] project_key in the settings.",
        "nothing (read-only).", f"{PROG} pr sonar 12", "fix the listed issues on the head and push; the analysis runs again"))
    def sonar(ref: str = typer.Argument(..., metavar="NUMBER", help="The pull request number."),
              issues: int = typer.Option(20, "--issues", metavar="N", help="List at most N open issues (the count is always complete).")) -> None:
        show(quality(rt).pr_quality(number(ref), issues))
