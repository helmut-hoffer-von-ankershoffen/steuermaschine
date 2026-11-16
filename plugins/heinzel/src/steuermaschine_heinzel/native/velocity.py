"""`sm doc velocity` and `sm doc velocity-record`: bounded throughput samples. The logic is velocity_lib/samples.py."""
# mypy: disable-error-code="no-untyped-call"

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import typer
from pydantic import BaseModel, Field

from steuermaschine import USAGE
from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime

from .coord_lib.base import child_env
from .velocity_lib import samples



class VelocitySettings(BaseModel):
    """Settings of the throughput samples (mixin of the plugin's settings table)."""

    velocity_cache_dir: Path | None = Field(None, description="Directory of cached completed run attempts and the eligibility journal; none: velocity/ in this project's state home.")


class Velocity(Model):
    """Throughput of a few explicitly chosen samples. Read `limits` before quoting a number."""

    repo: str
    observed_at: str
    samples: list[dict[str, Any]] = []
    release: dict[str, Any] | None = None
    missing: list[Any] = []
    limits: list[str] = []
    summary: dict[str, Any] = {}
    api_calls: int = 0
    cache_hits: int = 0

    def render(self) -> list[str]:
        lines = [f"velocity: {len(self.samples)} explicit samples; {self.api_calls}/{samples.MAX_CALLS} API calls, {self.cache_hits} cache hits"]
        for row in self.samples:
            lines.append(f"#{row['pr']} run {row['run_id']} attempt {row['attempt']}: attempt elapsed proxy {row.get('attempt_elapsed_proxy_s')}s; "
                         f"jobs {row.get('executed_jobs_observed')}, macOS {row.get('macos_executed_jobs_observed')}, tail wait {row.get('job_queue_tail_max_s')}s; "
                         f"{row.get('missing') or row.get('binding')}")
        lines.append(json.dumps(self.summary))
        if self.release:
            lines.append("release: " + json.dumps(self.release))
        return [*lines, *("limit: " + limit for limit in self.limits)]


class EligibleRecorded(Model):
    """The first caller-attested observation for one pull request head, as the journal holds it."""

    repo: str
    pr: int
    head: str
    eligible_observed_at: str
    source: str

    def render(self) -> list[str]:
        return [f"#{self.pr} {self.head[:8]}: eligible observed {self.eligible_observed_at} ({self.source})"]


def _cache(rt: Any) -> Path:
    given = (rt.settings or {}).get("velocity_cache_dir")
    path = Path(os.path.expanduser(str(given))) if given else Path(rt.project.home) / "velocity"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _selection(value: str, option: str) -> tuple[int, int, int]:
    try:
        return samples.selection(value)  # type: ignore[no-any-return]  # noqa: RUF100
    except Exception:
        raise Refuse(f"{option} {value}: use PR:RUN:ATTEMPT, all positive integers", "sm doc velocity --sample PR:RUN:ATTEMPT", code=USAGE) from None


@native("doc velocity", writes=False, replaces=("hz-velocity",), help=H(
    "Throughput of one to three chosen pull request runs, and optionally one release: bounded, never a repository crawl.",
    "Use when a claim about speed needs numbers: pick the pull requests and their run attempts yourself.",
    "nothing outside this tool (keeps completed run attempts in its own cache directory).",
    "sm doc velocity --sample 796:43299098260:1 --release 797:43301234567:1",
    "quote the numbers together with the printed limits.",
    "At most 15 GitHub calls, no pagination. A sample is PR:RUN:ATTEMPT. The run must belong to the pull request, its\n"
    "branch and the CI workflow; otherwise the sample is reported as missing, not measured. The release sample is the\n"
    "acceptance run of a release pull request. Exit 1 when GitHub or the cache is unavailable."))
def doc_velocity(sample: list[str] = typer.Option([], "--sample", metavar="PR:RUN:ATTEMPT", help="One CI sample; one to three."),
                 release: str | None = typer.Option(None, "--release", metavar="PR:RUN:ATTEMPT", help="The acceptance run of one release pull request."),
                 repo: str | None = typer.Option(None, "--repo", metavar="OWNER/NAME", help="Repository; default: the core repository.")) -> Velocity:
    rt: Any = runtime()
    repo = repo or str((rt.settings or {}).get("repo") or "")
    if not samples.valid_repo(repo):
        raise Refuse("invalid repository slug", "sm doc velocity --repo OWNER/NAME --sample PR:RUN:ATTEMPT", code=USAGE)
    if not 1 <= len(sample) <= samples.MAX_SAMPLES:
        raise Refuse("choose one to three --sample PR:RUN:ATTEMPT values", "sm doc velocity --sample PR:RUN:ATTEMPT", code=USAGE)
    chosen = [_selection(value, "--sample") for value in sample]
    env = child_env(rt.settings)

    def runner(args: list[str], **_: Any) -> SimpleNamespace:
        done = rt.providers.commands.run(args, env=env, timeout=60)
        return SimpleNamespace(returncode=done.rc, stdout=done.out)

    try:
        report = samples.collect(samples.Api(_cache(rt), runner), repo, chosen, _selection(release, "--release") if release else None)
    except (samples.Unavailable, OSError, ValueError) as error:
        raise Refuse(str(error), "sm doc velocity --sample PR:RUN:ATTEMPT   (again later)") from None
    return Velocity(**report)


@native("doc velocity-record", writes=True, replaces=("hz-velocity",), help=H(
    "Record that a pull request head was seen eligible to merge: one caller-attested line in the measurement journal.",
    "Use after the caller's own gates passed for exactly this head, and only then, while throughput is being measured.",
    "appends at most one line to this tool's eligibility journal (first observation per head); nothing else.",
    "sm doc velocity-record 796:7dc85604ba06bbb9154313d9883ced6b62cf9bdd",
    "sm doc velocity --sample PR:RUN:ATTEMPT",
    "It neither decides merge eligibility nor changes the merge queue. A head that is already recorded keeps its first\n"
    "observation; the stored line is printed."))
def doc_velocity_record(head: str = typer.Argument(..., metavar="PR:HEAD", help="The pull request and its full 40-character head commit."),
                        repo: str | None = typer.Option(None, "--repo", metavar="OWNER/NAME", help="Repository; default: the core repository.")) -> EligibleRecorded:
    rt: Any = runtime()
    repo = repo or str((rt.settings or {}).get("repo") or "")
    match = re.fullmatch(r"([1-9]\d*):([0-9a-f]{40})", head)
    if not match or not samples.valid_repo(repo):
        raise Refuse("give PR:HEAD with the full 40-character head commit", "sm doc velocity-record PR:HEAD", code=USAGE)
    try:
        return EligibleRecorded(**samples.record_eligible(_cache(rt), repo, int(match[1]), match[2]))
    except (OSError, ValueError) as error:
        raise Refuse(f"the journal is unusable: {error}", "sm self config   (setting velocity_cache_dir)") from None
