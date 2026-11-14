"""`sm ci watch`, `sm tick` and `sm timing ...`: wait for one run or PR, the deadline watch, and measured timings.

The logic is in `tick_lib/` (moved): `checks.py` on `hub.py` (the watch of one run or PR), `watch.py` (the deadline
watch), `timing.py` (gate, hosted and stage timings), `legacy.py` (marker files of the scripted era, read-only).
This module is the edge: typed options, the result models, and the binding of the library to this call's providers
and settings. Everything here reads. A crash report or a change of the tool environment is acknowledged with the
core's `sm alarm ack tick:crash` / `sm alarm ack tick:venv`; the tick only reads those acknowledgements.

What the former digest and wait tools printed and `go` shows now is not here: see parity/tick.md and
parity/timing-gate.md for the table of every former output section and where it is.
"""

from __future__ import annotations

import inspect
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, Field

from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import Group, NativeCommand, native, runtime

from .. import state as statedir  # the plugin's state paths
from .go_lib import legacy as state_files
from .lock import LockSettings
from .tick_lib import checks, hub, legacy, timing, watch

ACK = "heinzel.tick.ack"  # store kind of the former `--ack-*` options: keys `crash` and `venv`, each {"at": seconds}; read only
CORE_ACKS = ("alarms", "acks")  # the core's acknowledged list (`sm alarm ack KEY`): {key: seconds}
ACK_KEYS = {"crash": "tick:crash", "venv": "tick:venv"}
QUEUE_WAITING = ("queued", "ready", "merging")
ALLOWED_VENV_CHANGES = "heinzel.guards.live_uv"  # store kind of `sm guard uv` (guards.LIVE_UV_KIND): one record per allowed live change, {"at": ISO time}


class TickSettings(BaseModel):
    """Settings of the watch, the deadline watch and the timings, a mixin of the plugin's table."""

    tick_poll_seconds: int = Field(30, description="Seconds between two looks at a run or a PR in `ci watch`.")
    tick_watch_timeout_minutes: int = Field(90, description="How long `ci watch` waits when --timeout is not given.")
    tick_diagnostics_dir: Path | None = Field(None, description="Where the system writes crash reports (.ips, .crash); none: crash reports are not read.")
    tick_tool_venv: Path | None = Field(None, description="The live tool environment of the product; a change must be acknowledged. None: not watched.")
    tick_venv_slack_seconds: int = Field(60, description="A change of the tool environment this close to its acknowledgement is not reported.")
    tick_research_dir: Path | None = Field(None, description="Directory with the watch's noise rules (watch-ignore.toml), agent-ids.md and the review-*.md "
                                                            "files; none: no noise rules, no reviewed or agent lists.")
    tick_beads_dir: Path | None = Field(None, description="The tracker's directory, for the creation time of a bead; none: `sm timing stages` refuses.")
    tick_bead_prefix: str = Field("", description="The prefix of every bead id of the tracker; an id given without it gets it.")


def setting(name: str) -> Any:
    """A setting of this area; before the mixin is part of the plugin's table, its default."""
    value = (runtime().settings or {}).get(name)
    return TickSettings.model_fields[name].default if value is None else value


def providers() -> Any:
    return runtime().providers


def folder(name: str) -> Path | None:
    value = setting(name)
    return Path(str(value)).expanduser() if value else None


def state_dir() -> Path | None:
    """The directory of the marker files (setting legacy_state_dir; none: the project's state directory)."""
    return statedir.dashboard_dir(runtime().settings or {})


def repository() -> str:
    return str((runtime().settings or {}).get("repo") or hub.CORE)


# ----------------------------------------------------------------------------- ci watch
class Watched(Model):
    """How one run or one pull request's checks ended."""

    kind: str  # run | pr
    repository: str
    number: int
    outcome: str  # run: success or its conclusion; pr: green | red | merged | closed; both: timeout
    code: int  # 0 green, 1 red, 2 not done within the timeout
    failed: list[str] = []  # failing jobs (run) or checks (pr)
    tests: list[str] = []  # failing test names from the log
    text: str = ""

    @property
    def exit_code(self) -> int:
        return self.code

    def render(self) -> list[str]:
        return self.text.splitlines()


@native("ci watch", writes=False, replaces=("hz-watch",), help=H(
    "Block until a CI run or a pull request's checks are done; then only the outcome and the failing job and test names.",
    "Use after a push or a re-run, as ONE background wait with a ceiling instead of a polling chain.",
    "nothing (read-only: it reads GitHub every 30 seconds until the run or the checks are done).",
    "sm ci watch 795 --timeout 40     (a plugins PR: sm ci watch 38 --repo plugins)",
    "red: sm ci why PR (then sm ci faillog PR); green: sm head sonar PR",
    "The target is a PR number (below 10,000,000), a run id (above), a pull-request or run URL, or `plugins 38` for a PR of\n"
    "another repository of the project. The advisory `review` check is ignored. A merged PR counts as green.\n"
    "Exit codes: 0 green, 1 red or closed, 2 not done after --timeout minutes (nothing is cancelled)."))
def ci_watch(target: list[str] = typer.Argument(..., metavar="RUN|PR|URL", help="A PR number, a run id, a URL, or `plugins 38`."),
             repo: str | None = typer.Option(None, "--repo", "-R", metavar="OWNER/NAME", help="The repository of a bare number, OWNER/NAME or a "
                                             "repository word (plugins, acme, sf, core); default: the core repository."),
             timeout: int | None = typer.Option(None, "--timeout", metavar="MINUTES", min=0, help="Give up after this long with exit 2; default 90.")) -> Watched:
    p = providers()
    core = repository()
    hub.bind(hub.Edge(commands=p.commands, clock=p.clock), core)
    checks.POLL = int(setting("tick_poll_seconds"))
    minutes = int(setting("tick_watch_timeout_minutes")) if timeout is None else timeout
    if repo and "/" not in repo:  # a repository word, as in `plugins 38`
        try:
            repo = hub.split_key([repo, "0"])[0]
        except ValueError:
            raise Refuse(f"--repo {repo}: neither OWNER/NAME nor a repository word (plugins, acme, sf, core)", "sm ci watch --help", code=2) from None
    try:
        kind, where, number, code, text = checks.watch(list(target), repo or core, minutes)
    except ValueError as no:  # not a number, a URL or a known repository word
        raise Refuse(str(no), "sm ci watch --help", code=2) from None
    return Watched(kind=kind, repository=where, number=int(number), outcome=text.splitlines()[0].split(": ", 1)[1], code=code,
                   failed=checks.FOUND.get("failed", []), tests=checks.FOUND.get("tests", []), text=text)


# ----------------------------------------------------------------------------- tick: the deadline watch
DIGEST: tuple[str, ...] = ()
"""The paths of the registered READ commands whose text lines are the anomaly digest (empty while that digest is not
native). It is a seam for the digest area: name a command here, never bind code. `sm tick` runs each with its
defaults and filters and summarizes their lines as before; a path that is not a registered read command is refused
before anything runs, so the tick stays read-only (review T2)."""


def registered() -> list[Any]:
    """Every native command of the plugin (a seam for tests)."""
    from . import commands

    return [c for c in commands() if isinstance(c, NativeCommand)]


def digest_lines() -> list[str] | None:
    """The lines of the DIGEST commands; None when no digest command is named."""
    if not DIGEST:
        return None
    found = {c.path: c for c in registered()}
    picked = [found.get(path) for path in DIGEST]
    for path, command in zip(DIGEST, picked, strict=True):
        if command is None or command.writes:
            raise Refuse(f"the digest source `{path}` is not a registered read command", "sm tick --help")
    lines: list[str] = []
    for command in picked:
        assert command is not None
        defaults = {name: getattr(p.default, "default", p.default) for name, p in inspect.signature(command.fn).parameters.items()}
        lines += command.fn(**defaults).render()
    return lines

DIGEST_MISSING = "anomaly digest: not read here (PR lines: sm go and sm ci triage prs; alarms: sm alarm check)"


class Tick(Model):
    """What the deadline watch found beyond `go`: new crash reports, a changed tool environment, and the digest lines
    that are an action for the coordinator."""

    status: str  # ok | action
    crashes: list[str] = []  # crash reports newer than the acknowledgement
    venv_warning: str = ""  # the live tool environment changed and nobody acknowledged it
    actions: list[str] = []  # each one is something to act on now
    notes: list[str] = []  # INFO lines
    unreachable: int = 0  # stale or flagged PRs whose owner is not a live agent
    acknowledged: list[str] = []  # what this call acknowledged: crash, venv
    not_read: list[str] = []  # what the watch could not look at
    raw: bool = False
    lines: list[str] = []

    def render(self) -> list[str]:
        return self.lines


def acknowledged_at(store: Any, key: str) -> float:
    """The latest acknowledgement of `key` (crash, venv): the core's `sm alarm ack tick:KEY`, or a record the former
    `sm tick --ack-*` option left (read only)."""
    old = float((store.load(ACK, key) or {}).get("at") or 0.0)
    try:
        core = float((store.load(*CORE_ACKS) or {}).get(ACK_KEYS[key]) or 0.0)
    except (TypeError, ValueError):
        core = 0.0
    return max(old, core)


def last_allowed_venv_change(store: Any) -> float:
    """When the tool-environment guard last let a change of the live environment through (its records in the store)."""
    latest = 0.0
    for key in store.keys(ALLOWED_VENV_CHANGES):
        try:
            latest = max(latest, datetime.strptime(str((store.load(ALLOWED_VENV_CHANGES, key) or {}).get("at")), "%Y-%m-%dT%H:%M:%S%z").timestamp())
        except ValueError:
            continue
    return latest


def queued_prs(root: Path | None) -> set[int]:
    """Core PR numbers the merge queue still works on (queued, ready or merging)."""
    if root is None:
        return set()
    items = state_files.queue_state(root)["items"]
    return {int(str(i.get("key"))) for i in items if isinstance(i, dict) and str(i.get("key", "")).isdigit() and i.get("status") in QUEUE_WAITING}


@native("tick", writes=False, replaces=("hz-tick",), help=H(
    "The deadline watch: what a tick must look at beyond `sm go`. New crash reports, a changed tool environment, watch actions.",
    "Use at every watch tick, after bare `sm` and its ALERT lines.",
    "nothing (read-only).",
    "sm tick",
    "act on each ACTION line; a crash: investigate, then sm alarm ack tick:crash",
    "Prints one line per finding: `WARN:` (the live tool environment changed), `ACTION:` (P0 NEW CRASH for a new .ips or\n"
    ".crash report; digest lines the noise rules keep), `INFO:`; or one `OK` line that says what was looked at.\n"
    "Alarms, the phase and the open PRs are not repeated here: bare `sm` shows them, ALERT lines first.\n"
    "This command never cancels or defers CI, never notifies, never writes a note and never acknowledges: it only looks.\n"
    "Acknowledge with the core: `sm alarm ack tick:crash` (crash reports) and `sm alarm ack tick:venv` (tool environment).\n"
    "A deadline watch that needs exit 3 on an unacknowledged ALERT runs `sm alarm check`; this command always exits 0.\n"
    "Noise rules: watch-ignore.toml in the setting tick_research_dir. --raw prints the lines before the rules."))
def tick(raw: bool = typer.Option(False, "--raw", help="The lines before the noise rules (reviewed and queued PRs already removed).")) -> Tick:
    rt = runtime()
    p, root, research = providers(), state_dir(), folder("tick_research_dir")
    done: list[str] = []
    digest = digest_lines()
    lines = [line for line in digest or [] if not line.startswith("RULES:")]
    try:
        queued = queued_prs(root) if lines else set()
    except (OSError, ValueError, KeyError, TypeError):
        queued = set()
    out = watch.filter_lines(lines, watch.review_files(research) if research else [], queued, watch.agent_ids(research) if research else "") if lines else []
    diagnostics = folder("tick_diagnostics_dir")
    crashes = watch.scan_crashes(str(diagnostics), max(acknowledged_at(p.store, "crash"), legacy.crash_ack(root) if root else 0.0)) if diagnostics else []
    if crashes:
        out.insert(0, watch.crash_line(crashes))
    missing = ([DIGEST_MISSING] if digest is None else []) + ([] if diagnostics else ["crash reports: the setting tick_diagnostics_dir is not set"]) + \
        ([] if folder("tick_tool_venv") else ["tool environment: the setting tick_tool_venv is not set"])
    if raw:
        return Tick(status="action" if out else "ok", crashes=crashes, acknowledged=done, not_read=missing, raw=True, lines=out or ["quiet"])
    cfg = watch.load_cfg(str(research / "watch-ignore.toml") if research else "")
    actions_file = (rt.settings or {}).get("go_pr_actions_file")
    cfg["owned_prs"] = list(set(cfg["owned_prs"]) | watch.pr_actions_holds(str(Path(str(actions_file)).expanduser()) if actions_file else None))
    starved = watch.starved_age(p.commands, p.clock, repository()) if any("starved" in line for line in out) else None
    acts, info = watch.summarize(out, cfg, starved)
    venv = folder("tick_tool_venv")
    warn = watch.live_uv_warning(str(venv), str(legacy.venv_ack(root)) if root else "", str(legacy.venv_allowed(root)) if root else "",
                                 int(setting("tick_venv_slack_seconds")), max(acknowledged_at(p.store, "venv"), last_allowed_venv_change(p.store))) if venv else None
    found = [a for a in acts if a != "OK"]
    actions = [a for a in found if not a.startswith("INFO:")]
    notes = [a for a in found if a.startswith("INFO:")]
    if info:
        notes.append(f"INFO: {info} stale/flagged PRs have no live agent (not actionable here; sm tick --raw)")
    shown = ([warn] if warn else []) + actions
    if not shown:
        shown = ["OK: no new crash report, tool environment unchanged" + ("" if digest is None else ", no watch action") + " (alarms and the ship path: sm go)"]
    shown += notes + [f"NOT READ: {m}" for m in missing]
    return Tick(status="action" if warn or actions else "ok", crashes=crashes, venv_warning=warn or "", actions=actions, notes=notes, unreachable=info,
                acknowledged=done, not_read=missing, lines=shown)


# ----------------------------------------------------------------------------- timing
TIMING = Group("timing", H(
    "timing: measured waits of the delivery path, from records: the local gate, hosted runs, bead to publication.",
    "Use for the velocity review, and before you claim that something is slow or fast.",
    "nothing (read-only).",
    "sm timing gate",
    "sm timing hosted RUN   (what waits now: sm go)"))


def bound(lines: list[str], problems: list[str]) -> timing.Edge:
    rt = runtime()

    def say(text: str) -> None:
        problems.append(text)
        rt.say(text)

    history = (rt.settings or {}).get("lock_history") or LockSettings.model_fields["lock_history"].default
    p = providers()
    edge = timing.Edge(commands=p.commands, clock=p.clock, out=lines.append, err=say)
    beads = folder("tick_beads_dir")
    timing.bind(edge, repository(), str(setting("tick_bead_prefix")), str(beads) if beads else "", Path(str(history)).expanduser())
    return edge


class GateWaits(Model):
    """The gated local commands of the last hour: how long they waited for the run lock and how long they ran."""

    window_minutes: int = 60
    runs: int
    wait_median_seconds: float | None = None
    wait_p90_seconds: float | None = None
    wait_total_seconds: float = 0
    ran_median_seconds: float | None = None
    ran_p90_seconds: float | None = None
    ran_total_seconds: float = 0
    nonzero_exit: int = 0

    def render(self) -> list[str]:
        def s(value: float | None) -> str:
            return "-" if value is None else f"{value:g}s"

        return [f"gate {self.window_minutes} min: {self.runs} commands; waited median {s(self.wait_median_seconds)}, p90 {s(self.wait_p90_seconds)}, "
                f"total {s(self.wait_total_seconds)}; ran median {s(self.ran_median_seconds)}, p90 {s(self.ran_p90_seconds)}, total {s(self.ran_total_seconds)}; "
                f"{self.nonzero_exit} with a non-zero exit"]


@native("timing gate", writes=False, replaces=("hz-wait",), help=H(
    "The last hour of the local run lock: how long gated commands waited and ran. No GitHub call.",
    "Use for the hourly velocity review, to tell a local gate wait from command time and from hosted waits.",
    "nothing (read-only: it reads the gate history file).",
    "sm timing gate",
    "sm gate status   (who holds the lock now)"))
def timing_gate() -> GateWaits:
    bound([], [])
    stats = timing.gate_stats()
    return GateWaits(runs=stats["runs"], wait_median_seconds=stats["wait_med_s"], wait_p90_seconds=stats["wait_p90_s"], wait_total_seconds=stats["wait_total_s"],
                     ran_median_seconds=stats["ran_med_s"], ran_p90_seconds=stats["ran_p90_s"], ran_total_seconds=stats["ran_total_s"],
                     nonzero_exit=stats["nonzero_exit"])


class HostedJob(Model):
    id: int
    head_sha: str
    name: str
    status: str
    conclusion: str | None = None
    runner_id: int | None = None
    started_at: str | None = None
    completed_at: str | None = None
    timing: str  # measured | running | queued | not_started | unverified_start | missing_or_invalid_start | missing_or_invalid_completion
    dispatch_to_start_seconds: float | None = None  # only for the first attempt of a manual dispatch
    job_wall_seconds: float | None = None


class HostedRun(Model):
    id: int
    head_sha: str
    attempt: int
    workflow: str
    event: str
    status: str
    conclusion: str | None = None
    created_at: str | None = None
    jobs: list[HostedJob] = []


class HostedTiming(Model):
    """Exact Actions runs with the observed timing of each job of their current attempt."""

    repository: str
    runs: list[HostedRun]
    lines: list[str] = Field([], exclude=True)

    def render(self) -> list[str]:
        return self.lines


@native("timing hosted", writes=False, replaces=("hz-wait",), help=H(
    "Time 1 to 8 exact Actions runs of the core repository: dispatch wait and wall time of every job of the attempt.",
    "Use when you need the hosted runner wait or a job's duration as a number, for the velocity review or a study.",
    "nothing (read-only: two GitHub reads per run).",
    "sm timing hosted 43301234567 43301234999",
    "sm timing gate   (the local half of the wait)",
    "Dispatch wait is run creation to job start, and only for the first attempt of a manual dispatch; wall time is job\n"
    "start to completion. A job without an observed start is named so and gets no number. An incomplete or mismatched\n"
    "job inventory refuses the whole report (exit 1): no partial timing."))
def timing_hosted(runs: list[str] = typer.Argument(..., metavar="RUN...", help="1 to 8 distinct run ids of the core repository.")) -> HostedTiming:
    if not 1 <= len(runs) <= 8 or len(set(runs)) != len(runs) or any(not re.fullmatch(r"[1-9][0-9]{0,19}", value) for value in runs):
        raise Refuse("hosted requires 1 to 8 distinct positive run IDs", "sm timing hosted --help", code=2)
    lines: list[str] = []
    problems: list[str] = []
    edge = bound(lines, problems)
    if timing.hosted(list(runs), False) or edge.result is None:
        raise Refuse("; ".join(problems) or "hosted timing unavailable", "sm timing hosted --help")
    rows = [HostedRun(**{**run, "jobs": [HostedJob(**{("dispatch_to_start_seconds" if k == "dispatch_to_start_s" else "job_wall_seconds" if k == "job_wall_s" else k): v
                                                      for k, v in job.items()}) for job in run["jobs"]]}) for run in edge.result["runs"]]
    return HostedTiming(repository=edge.result["repository"], runs=rows, lines=lines)


class Interval(Model):
    state: str  # measured | pending | unavailable | not_applicable
    seconds: float | None = None
    start_at: str | None = None
    end_at: str | None = None
    reason: str = ""


class StageTiming(Model):
    """From a bead to its publication: each stage measured from records, or named as pending or unavailable."""

    repository: str
    bead: str
    pr: int
    release_tag: str | None = None
    tag_commit: str | None = None
    observed_at: str
    intervals: dict[str, Interval]
    bead_time_basis: str
    incomplete: bool = Field(False, exclude=True)
    lines: list[str] = Field([], exclude=True)

    @property
    def exit_code(self) -> int:
        return int(self.incomplete)

    def render(self) -> list[str]:
        return self.lines


@native("timing stages", writes=False, replaces=("hz-wait",), help=H(
    "Time one bead: creation (a proxy for the ask) to core PR, PR to merge, merge to GitHub publication.",
    "Use for the ask-to-published numbers of the velocity review, with the exact release tag that contains the merge.",
    "nothing (read-only: one tracker read and up to five GitHub reads).",
    "sm timing stages --bead 8um1 --pr 801 --tag v0.9.40",
    "sm mini verify   (published is not live: the live proof is separate)",
    "Publication counts only when --tag names a final release whose tag commit contains the PR's merge commit; otherwise\n"
    "the stage is `pending` with the time elapsed since the merge and the reason. Exit 1: an interval is unavailable\n"
    "(a timestamp is missing, or the end precedes the start), or a record could not be read."))
def timing_stages(bead: str = typer.Option(..., "--bead", metavar="ID", help="The bead id, with or without the tracker's prefix."),
                  pr: str = typer.Option(..., "--pr", metavar="PR", help="The core PR number."),
                  tag: str | None = typer.Option(None, "--tag", metavar="vX.Y.Z", help="The exact final release tag.")) -> StageTiming:
    lines: list[str] = []
    problems: list[str] = []
    edge = bound(lines, problems)
    if folder("tick_beads_dir") is None:
        raise Refuse("the setting tick_beads_dir (the tracker's directory) is not set", "sm self config")
    if not timing.bead_id(bead):
        raise Refuse("stages requires an exact management --bead ID and accepts no positional IDs", "sm timing stages --help", code=2)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", pr):
        raise Refuse("stages requires a positive Core --pr number", "sm timing stages --help", code=2)
    if tag is not None and (not tag.startswith("v") or not timing.VERSION.fullmatch(tag[1:]) or len(tag) > 64):
        raise Refuse("--tag requires an exact final Core release tag vX.Y.Z", "sm timing stages --help", code=2)
    code = timing.stages(timing.BEAD_PREFIX + bead.removeprefix(timing.BEAD_PREFIX), int(pr), tag, False)
    found = edge.result
    if found is None:
        reason = next((json.loads(text).get("reason") for text in reversed(problems) if text.startswith("{")), "") or "stage timing unavailable"
        raise Refuse(str(reason), "sm timing stages --help")
    return StageTiming(repository=found["repository"], bead=found["bead"], pr=found["pr"], release_tag=found["release_tag"], tag_commit=found["tag_commit"],
                       observed_at=found["observed_at"], intervals={key: Interval(**row) for key, row in found["intervals"].items()},
                       bead_time_basis=found["bead_time_basis"], incomplete=bool(code), lines=lines)
