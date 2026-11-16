"""mini leakwatch report | check | sample: is the long-running live hz on this machine leaking? Read-only toward hz.

`report` and `check` only read (the samples file; `check` takes one live sample for its judgement when the sampler's
newest one is stale, and does not store it). `sample` appends one line to the samples file: a write command with its
own review, called every five minutes by the service manager. The logic is leakwatch_lib/watch.py, moved verbatim.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer
from pydantic import BaseModel, Field

from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import Group, native, runtime

from .leakwatch_lib import watch

LEAKWATCH = Group("mini leakwatch", H("Leak watch of the live hz on this machine: file descriptors, sockets, children, memory, CPU, restarts.",
                                      "Use at every tick (check), and when a finding needs its trend (report).",
                                      "nothing by itself; `sample` appends one line to the samples file.", "sm mini leakwatch check", "sm mini leakwatch report"))


class LeakwatchSettings(BaseModel):
    """Settings of `sm mini leakwatch` (mixin of the plugin's settings table)."""

    leakwatch_state_dir: Path | None = Field(default=None, description="Directory of the samples file (samples.jsonl) the sampler appends to.")
    leakwatch_config_file: Path | None = Field(default=None, description="A TOML file whose values override the built-in thresholds; none: the built-in ones.")
    leakwatch_daemon_label: str = Field(default="", description="The launchd label of the product's daemon; empty: io.github.<owner of `repo`>.heinzel.daemon.")


class LeakMetric(Model):
    metric: str
    now: float
    slope_per_hour: float
    samples: int
    leak: bool
    monotonic_samples: int = 0


class LeakReport(Model):
    """Current values and the trend per hour over the window."""

    hours: float
    running: bool
    samples: int
    version: str = ""  # the package on disk
    server_pid: int | None = None
    uptime_seconds: int | None = None
    metrics: list[LeakMetric] = []
    lines: list[str] = Field(default=[], exclude=True)
    read_only: bool = True

    def render(self) -> list[str]:
        return self.lines


class LeakCheck(Model):
    """The findings of the leak watch; none is `ok`."""

    findings: list[str] = []
    samples: int = 0
    judged_on: str = "samples"  # samples | live (the sampler's newest sample was stale: one live sample was taken, not stored)
    newest_sample_age_minutes: float | None = None
    read_only: bool = True

    def render(self) -> list[str]:
        return self.findings or ["ok"]

    @property
    def exit_code(self) -> int:
        return 1 if self.findings else 0


class LeakSample(Model):
    """The sample that was appended to the samples file."""

    time: str
    running: bool
    processes: int
    fds: int
    rss_mb: int
    load_1m: float
    web_seconds: float | None = None
    file: str = ""

    def render(self) -> list[str]:
        return [f"{self.time[11:19]}Z running={self.running} procs={self.processes} fds={self.fds} rss={self.rss_mb}MB load={self.load_1m} web_s={self.web_seconds}"]


def configured() -> dict:
    """Point the moved library at this call's providers and settings; return the thresholds."""
    rt = runtime()
    s = rt.settings or {}
    owner = str(s.get("repo") or "").split("/")[0]
    if not s.get("leakwatch_state_dir"):
        raise Refuse("the setting leakwatch_state_dir is not set: the directory of the samples file", "sm self config")
    watch.configure(run=rt.providers.commands.run, state=str(s["leakwatch_state_dir"]), config=s.get("leakwatch_config_file"),
                    daemon_label=str(s.get("leakwatch_daemon_label") or f"io.github.{owner}.heinzel.daemon"))
    return watch.load_config()


def live_sample(cfg: dict) -> dict:
    clock = runtime().providers.clock
    return watch.take_sample(cfg, sleep=clock.sleep, clock=clock.now, probe=watch.probe_web)


@native("mini leakwatch report", writes=False, replaces=("hz-leakwatch",), help=H(
    "The live hz now and its trend per hour: open fds, sockets, CLOSE_WAIT, children, memory; LEAK? marks a monotonic rise.",
    "Use after `sm mini leakwatch check` printed a finding, to see the trend behind it.",
    "nothing (read-only: the samples file).",
    "sm mini leakwatch report --hours 12", "sm mini verify; a growing series is a product bug: file it with the table"))
def leakwatch_report(hours: float = typer.Option(6.0, "--hours", metavar="N", help="The window of the trend.")) -> LeakReport:
    cfg = configured()
    samples = watch.read_samples(runtime().providers.clock.now() - hours * 3600)
    lines = watch.table(samples, cfg)
    if not samples or not samples[-1].get("running"):
        return LeakReport(hours=hours, running=False, samples=len(samples), lines=lines)
    last, metrics = samples[-1], []
    for name, (get, div, _, label) in watch.METRICS.items():
        tr = watch.trend(watch.series(samples, name), samples, cfg, name)
        metrics.append(LeakMetric(metric=label, now=round(get(last) / div, 1), slope_per_hour=round(tr["slope"], 1), samples=tr["n"], leak=tr["leak"], monotonic_samples=tr["run"]))
    return LeakReport(hours=hours, running=True, samples=len(samples), version=str(last.get("version") or ""), server_pid=last.get("server_pid"),
                      uptime_seconds=last.get("uptime_s"), metrics=metrics, lines=lines)


@native("mini leakwatch check", writes=False, replaces=("hz-leakwatch",), help=H(
    "One line per finding of the leak watch of the live hz, or `ok`. Exit 1 with findings.",
    "Use at every tick, and before telling the owner the live hz is healthy.",
    "nothing (read-only: the samples file; with a stale sampler one live sample from ps, lsof, netstat and one local GET, not stored).",
    "sm mini leakwatch check", "sm mini leakwatch report for the trend; never restart the live hz to clear a finding",
    "Findings: absolute limits (fds, memory, CLOSE_WAIT, orphans, zombies, stray listening ports), idle CPU, a restart or\n"
    "a changed package on disk, a series that grows monotonically, the load of this machine, a web that did not answer."))
def leakwatch_check() -> LeakCheck:
    cfg = configured()
    now = runtime().providers.clock.now()
    samples = watch.read_samples(now - max(cfg["window_hours"], 1) * 3600 - 3600)
    age = round((now - samples[-1]["ts"]) / 60, 1) if samples else None
    judged = "samples"
    if not samples or now - samples[-1]["ts"] > cfg["stale_minutes"] * 60:
        samples.append(live_sample(cfg))
        judged = "live"
    return LeakCheck(findings=watch.findings(samples, cfg, now), samples=len(samples), judged_on=judged, newest_sample_age_minutes=age)


@native("mini leakwatch sample", writes=True, replaces=("hz-leakwatch",), help=H(
    "Take one sample of the live hz and append it to the samples file. For the service manager, every five minutes.",
    "Use for the sampler job; by hand only to prove the sampler works.",
    "appends one JSON line to samples.jsonl in the leak watch state directory (rotated at 20 MB). Read-only toward hz: ps, lsof, netstat, launchctl list, one local GET.",
    "sm mini leakwatch sample", "sm mini leakwatch check"))
def leakwatch_sample() -> LeakSample:
    cfg = configured()
    sample = live_sample(cfg)
    watch.append_sample(sample)
    t = sample["totals"]
    return LeakSample(time=datetime.fromtimestamp(sample["ts"], UTC).isoformat(), running=sample["running"], processes=t["procs"], fds=t["fds"], rss_mb=t["rss_kb"] // 1024,
                      load_1m=sample["load1"], web_seconds=sample["web_s"], file=str(watch.samples_path()))
