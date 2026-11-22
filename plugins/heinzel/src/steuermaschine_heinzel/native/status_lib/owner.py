"""The owner update, rendered deterministically from reads: releases since a time, open pull requests per repository,
bead counts, main's CI, merges today against yesterday, and the owner's open decisions from a decisions file. No model
writes any of it; the coordinator prints it and adds only judgment. Times are local with the zone, or durations."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from steuermaschine.localtime import duration, local
from steuermaschine.models import Model

from . import prtable

SINCE = re.compile(r"^(\d+)([mhd])$")
UNIT = {"m": 60, "h": 3600, "d": 86400}
OPEN_ITEM = re.compile(r"^\s*(?:[-*]\s+\[ \]\s+|[-*]\s+(?!\[[xX]\])|\d+[.)]\s+)(.+\S)\s*$")


class OwnerRelease(Model):
    repo: str
    tag: str
    published: str  # ISO UTC; rendered local


class OwnerRepo(Model):
    repo: str
    open: int = 0
    ready: int = 0  # CI ok, no unresolved threads, merge state CLEAN
    red: int = 0
    threads: int = 0  # pull requests with unresolved review threads
    draft: int = 0


class OwnerUpdate(Model):
    """The 2-hourly owner update."""

    now: str  # ISO UTC
    since: str  # ISO UTC
    releases: list[OwnerRelease] = []
    repos: list[OwnerRepo] = []
    beads: dict[str, int] = {}
    main_ci: str = ""
    merged_today: int | None = None
    merged_yesterday: int | None = None
    decisions: list[str] = []
    decisions_source: str = ""  # why there are none to show, or the file
    unknown: list[str] = []

    def render(self) -> list[str]:
        now = datetime.fromisoformat(self.now)
        out = [f"Owner update {local(now)}; since {local(datetime.fromisoformat(self.since))} ({duration((now - datetime.fromisoformat(self.since)).total_seconds())} ago)", ""]
        out.append(f"Releases since then ({len(self.releases)})")
        out += [f"  {r.repo} {r.tag}  {r.published}" for r in self.releases] or ["  none"]
        out += ["", "Pull requests"]
        out += [f"  {r.repo}: {r.open} open, {r.ready} ready, {r.red} red CI, {r.threads} with unresolved threads, {r.draft} draft" for r in self.repos] or ["  unread"]
        out += ["", "Beads: " + (", ".join(f"{k} {v}" for k, v in self.beads.items()) or "unread")]
        out.append(f"Main CI: {self.main_ci or 'unread'}")
        merges = "unread" if self.merged_today is None or self.merged_yesterday is None else f"{self.merged_today} today against {self.merged_yesterday} yesterday"
        out += [f"Merges: {merges}", "", f"Open owner decisions ({len(self.decisions)})"]
        out += [f"  {i}. {d}" for i, d in enumerate(self.decisions, 1)] or [f"  none ({self.decisions_source})" if self.decisions_source else "  none"]
        return out + [f"UNKNOWN  {u}" for u in self.unknown]


def since_of(text: str, now: float) -> float:
    """`2h`, `90m`, `1d`, or an ISO stamp with an offset; ValueError otherwise."""
    found = SINCE.match(text.strip())
    if found:
        return now - int(found.group(1)) * UNIT[found.group(2)]
    moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("a time needs its zone (Z or +01:00), or use a duration such as 2h")
    return moment.timestamp()


def read_decisions(path: Path | None) -> tuple[list[str], str]:
    """The open items of the decisions file: unchecked `- [ ]`, plain `-` bullets and numbered lines; `- [x]` is closed."""
    if path is None:
        return [], "no decisions file configured (setting status_decisions_file)"
    try:
        text = path.read_text()
    except OSError as exc:
        return [], f"cannot read {path.name}: {exc.strerror or exc}"
    return [m.group(1) for line in text.splitlines() if (m := OPEN_ITEM.match(line))], path.name


def repo_summary(repo: str, rows: list[prtable.PrRow]) -> OwnerRepo:
    mine = [r for r in rows if r.repo == repo]
    return OwnerRepo(repo=repo, open=len(mine), ready=sum(r.state == "CLEAN" and r.ci == "ok" and not r.threads for r in mine),
                     red=sum(r.ci.startswith("FAIL") for r in mine), threads=sum(bool(r.threads) for r in mine), draft=sum(r.state == "DRAFT" for r in mine))


def merged_counts(gh: Any, owner: str, repos: list[str], now: float) -> tuple[int | None, int | None, str]:
    today_start = datetime.fromtimestamp(now).astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    day = (yesterday_start.astimezone(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    today = yesterday = 0
    for repo in repos:
        rows, why = gh.try_json(["pr", "list", "-R", f"{owner}/{repo}", "--state", "merged", "--search", f"merged:>={day}", "--json", "mergedAt", "-L", "300"], timeout=30)
        if why or not isinstance(rows, list):
            return None, None, f"merged pull requests of {repo}: {why or 'no list'}"
        for r in rows:
            at = datetime.fromisoformat(str(r.get("mergedAt") or "").replace("Z", "+00:00")) if r.get("mergedAt") else None
            today += bool(at and at >= today_start)
            yesterday += bool(at and yesterday_start <= at < today_start)
    return today, yesterday, ""


def releases_since(gh: Any, owner: str, repos: list[str], since: float) -> tuple[list[OwnerRelease], list[str]]:
    out, unknown = [], []
    for repo in repos:
        rows, why = gh.try_json(["release", "list", "-R", f"{owner}/{repo}", "--json", "tagName,publishedAt", "-L", "30"], timeout=30)
        if why or not isinstance(rows, list):
            unknown.append(f"releases of {repo}: {why or 'no list'}")
            continue
        out += [OwnerRelease(repo=repo, tag=str(r["tagName"]), published=str(r["publishedAt"])) for r in rows
                if r.get("publishedAt") and datetime.fromisoformat(str(r["publishedAt"]).replace("Z", "+00:00")).timestamp() >= since]
    return sorted(out, key=lambda r: r.published), unknown


def main_ci_line(runs: list[dict[str, Any]], now: float) -> str:
    if not runs:
        return ""
    r = runs[0]
    when = r.get("createdAt")
    age = f", started {duration(now - datetime.fromisoformat(str(when).replace('Z', '+00:00')).timestamp())} ago" if when else ""
    state = str(r.get("conclusion") or r.get("status") or "?")
    return f"run {r.get('databaseId')} on {str(r.get('headSha') or '')[:8]}: {state}{age}"


def stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()
