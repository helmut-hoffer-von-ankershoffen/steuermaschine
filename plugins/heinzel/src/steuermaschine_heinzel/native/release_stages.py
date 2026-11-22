"""The stage table of one release, cut to LIVE-OK, from GitHub run timings (`sm why VERSION`).

Stages: candidate CI, merge, tag and release, PyPI, compose, acceptance (and one row per shard job), Mini follow, iOS.
`stage_rows` is pure (facts in, rows out); `gather` reads the facts from GitHub and the receipts; `record` keeps the
rows in the state directory (stages/vX.Y.Z.json) so the table survives the expiry of old runs. Read-only except `record`.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from steuermaschine.models import Model

from .go_lib import probes
from .queue_lib import hzlib
from .release_lib import ios

CORE, PLUGINS = hzlib.CORE, hzlib.PLUGINS


class Stage(Model):
    name: str
    start: str = ""
    end: str = ""
    result: str = ""

    @property
    def seconds(self) -> int | None:
        a, b = parse(self.start), parse(self.end)
        return None if a is None or b is None else max(0, int(b - a))


def parse(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None


def minutes(seconds: int | None) -> str:
    return "?" if seconds is None else f"{seconds // 60}m{seconds % 60:02d}s"


@dataclass
class Facts:
    ci: list[dict[str, Any]]  # runs of the CI workflow at the release PR head: createdAt, updatedAt, conclusion
    merged_at: str
    released_at: str  # the GitHub release is published
    pypi: dict[str, Any] | None  # a run: createdAt, updatedAt, conclusion
    compose: dict[str, Any] | None
    acceptance: dict[str, Any] | None  # a run with `jobs` (name, startedAt, completedAt, conclusion)
    live_at: str  # LIVE-OK recorded by the driver
    ios: dict[str, Any] | None  # the iOS receipt: started, at, clean


def run_stage(name: str, run: dict[str, Any] | None) -> Stage:
    if not run:
        return Stage(name=name)
    return Stage(name=name, start=str(run.get("startedAt") or run.get("createdAt") or ""), end=str(run.get("updatedAt") or run.get("completedAt") or ""),
                 result=str(run.get("conclusion") or run.get("status") or ""))


def stage_rows(f: Facts) -> list[Stage]:
    """The rows in order. A missing fact leaves the row's times empty (shown as `-`)."""
    ci_start = min((str(r.get("createdAt") or "") for r in f.ci if r.get("createdAt")), default="")
    ci_end = max((str(r.get("updatedAt") or "") for r in f.ci if r.get("updatedAt")), default="")
    ci_result = "" if not f.ci else ("success" if all(r.get("conclusion") == "success" for r in f.ci) else "failure")
    rows = [Stage(name="candidate CI", start=ci_start, end=ci_end, result=ci_result),
            Stage(name="merge", start=ci_end, end=f.merged_at, result="merged" if f.merged_at else ""),
            Stage(name="tag and release", start=f.merged_at, end=f.released_at, result="published" if f.released_at else ""),
            run_stage("PyPI", f.pypi), run_stage("compose", f.compose), run_stage("acceptance", f.acceptance)]
    for job in (f.acceptance or {}).get("jobs") or []:
        rows.append(Stage(name=f"  shard {job.get('name')}", start=str(job.get("startedAt") or ""), end=str(job.get("completedAt") or ""), result=str(job.get("conclusion") or "")))
    accepted_end = rows[5].end
    rows.append(Stage(name="Mini follow", start=accepted_end, end=f.live_at, result="LIVE-OK" if f.live_at else ""))
    if f.ios:
        started = datetime.fromtimestamp(float(f.ios["started"]), UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if f.ios.get("started") else ""
        rows.append(Stage(name="iOS (TestFlight)", start=started, end=str(f.ios.get("at") or ""), result="clean" if f.ios.get("clean") else ("running" if f.ios.get("running") else "NOT-CLEAN")))
    else:
        rows.append(Stage(name="iOS (TestFlight)"))
    return rows


def total_seconds(rows: list[Stage]) -> int | None:
    """Cut to LIVE-OK: the first start to the end of the Mini follow (else to the last known end of a main stage)."""
    main = [r for r in rows if not r.name.startswith(" ") and not r.name.startswith("iOS")]
    starts = [t for r in main if (t := parse(r.start)) is not None]
    ends = [t for r in main if (t := parse(r.end)) is not None]
    return int(max(ends) - min(starts)) if starts and ends else None


def render(rows: list[Stage]) -> list[str]:
    width = max(len(r.name) for r in rows)
    out = [f"{r.name.ljust(width)}  {(r.start[11:19] or '-').ljust(8)} -> {(r.end[11:19] or '-').ljust(8)}  {minutes(r.seconds).rjust(7)}  {r.result}".rstrip() for r in rows]
    return [*out, f"{'TOTAL cut to LIVE-OK'.ljust(width)}  {minutes(total_seconds(rows)).rjust(28)}"]


# ----------------------------------------------------------------------------- reading
def run_view(w: probes.World, repo: str, run_id: Any) -> dict[str, Any] | None:
    if not isinstance(run_id, int):
        return None
    try:
        found = w.gh_json(["run", "view", str(run_id), "-R", repo, "--json", "createdAt,startedAt,updatedAt,conclusion,status,jobs"], timeout=30)
    except RuntimeError:
        return None
    return found if isinstance(found, dict) else None


def pull_request_facts(w: probes.World, version: str) -> tuple[list[dict[str, Any]], str]:
    try:
        rows = w.gh_json(["pr", "list", "-R", w.repo, "--state", "merged", "--search", f"v{version} in:title", "--json", "title,headRefOid,mergedAt", "-L", "10"], timeout=30) or []
        pr = next(r for r in rows if version in str(r.get("title")))
        runs = w.gh_json(["run", "list", "-R", w.repo, "--commit", pr["headRefOid"], "--json", "workflowName,createdAt,updatedAt,conclusion", "-L", "40"], timeout=30) or []
    except (RuntimeError, StopIteration, KeyError):
        return [], ""
    return [r for r in runs if r.get("workflowName") == "CI"], str(pr.get("mergedAt") or "")


def released_at(w: probes.World, version: str) -> str:
    try:
        found = w.gh_json(["release", "view", f"v{version}", "-R", w.repo, "--json", "publishedAt"], timeout=30)
    except RuntimeError:
        return ""
    return str((found or {}).get("publishedAt") or "")


def gather(w: probes.World, version: str, receipt: dict[str, Any]) -> list[Stage]:
    saved = read_record(w.state_dir, version)
    ci, merged = pull_request_facts(w, version)
    facts = Facts(ci=ci, merged_at=merged, released_at=released_at(w, version),
                  pypi=run_view(w, CORE, receipt.get("pypi_run_id")), compose=run_view(w, PLUGINS, receipt.get("compose_run_id")),
                  acceptance=run_view(w, CORE, receipt.get("acceptance_run_id")), live_at=str(saved.get("live_at") or ""),
                  ios=ios.read_receipt(w.state_dir, version))
    rows = stage_rows(facts)
    old = {r["name"]: r for r in saved.get("stages", [])}
    rows = [r if r.start or r.end else Stage.model_validate(old[r.name]) if r.name in old else r for r in rows]  # an expired run keeps its recorded row
    record(w.state_dir, version, rows, str(saved.get("live_at") or ""))
    return rows


# ----------------------------------------------------------------------------- recording
def record_path(state_dir: Path, version: str) -> Path:
    return state_dir / "stages" / f"v{version}.json"


def read_record(state_dir: Path, version: str) -> dict[str, Any]:
    try:
        data = json.loads(record_path(state_dir, version).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(state_dir: Path, version: str, rows: list[Stage], live_at: str) -> None:
    target = record_path(state_dir, version)
    with contextlib.suppress(OSError):
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".json.partial")
        partial.write_text(json.dumps({"version": version, "live_at": live_at, "stages": [r.model_dump() for r in rows]}, indent=2) + "\n")
        partial.replace(target)


def mark_live(state_dir: Path, version: str, now: float) -> None:
    """The driver calls this at LIVE-OK: the end of the Mini follow stage."""
    saved = read_record(state_dir, version)
    stamp = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with contextlib.suppress(OSError):
        record_path(state_dir, version).parent.mkdir(parents=True, exist_ok=True)
        record_path(state_dir, version).write_text(json.dumps({**saved, "version": version, "live_at": stamp}, indent=2) + "\n")
