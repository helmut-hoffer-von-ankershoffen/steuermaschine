"""Bead sweep evidence: per open bead, PRs (noted, by bead id, by title keywords), release tag containing each merged PR,
days since update, assignee/due. A cheap model only classifies A..E from the table; this module never closes anything."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from steuermaschine.github import Gh
from steuermaschine.models import Model
from steuermaschine.native import runtime

from ..coord_lib.base import child_env, tracker_dir

STOP = set("the a an of to for and in on with from is are be not no add fix make when after before new use uses via per all any".split())  # noqa: SIM905  (moved as it was)
PRS_CACHE = ("heinzel.coord.sweep", "pull-requests")  # store kind and key
PRS_CACHE_SECONDS = 600


# ----------------------------------------------------------------------------- pure (moved)
def vkey(tag: Any) -> Any:
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", tag or "")
    return tuple(int(x) for x in m.groups()) if m else None


def words(title: Any) -> Any:
    return {w for w in re.findall(r"[a-z][a-z0-9-]{3,}", title.lower()) if w not in STOP}


def noted_prs(bead: Any) -> Any:
    text = " ".join(str(bead.get(k) or "") for k in ("title", "description", "notes"))
    return {int(n) for n in re.findall(r"(?:#|PR |pull/)(\d{1,5})\b", text)}


def match_prs(bead: Any, prs: Any) -> Any:
    """PRs (list of gh dicts) relating to the bead: id mention, noted number, keyword overlap. Returns {number: reason}."""
    suffix = bead["id"].split("-")[-1]
    noted = noted_prs(bead)
    kw = words(bead.get("title", ""))
    found = {}
    for p in prs:
        hay = f'{p.get("title","")} {p.get("headRefName","")} {p.get("body") or ""}'.lower()
        if p["number"] in noted:
            found[p["number"]] = "noted"
        elif re.search(r"(?<![a-z0-9])" + re.escape(suffix) + r"(?![a-z0-9])", hay) and len(suffix) >= 3:
            found[p["number"]] = "id"
        elif len(kw & words(p.get("title", ""))) >= 3:
            found[p["number"]] = "kw"
    return found


def days_since(ts: Any, now: Any = None) -> Any:
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None
    return round(((now or datetime.now(UTC)) - t).total_seconds() / 86400, 1)


def classify_hint(row: Any, mini: Any) -> Any:
    """Inventory hint only: A? for a release <= CLI version, B? later/unreleased, C? open, else D?."""
    merged = [p for p in row["prs"] if p["state"] == "MERGED" and p["why"] in ("noted", "id")]
    if any(p["release"] and mini and vkey(p["release"]) <= vkey(mini) for p in merged):
        return "A?"
    if merged:
        return "B?"
    if any(p["state"] == "OPEN" for p in row["prs"]):
        return "C?"
    return "D?"


def build(beads: Any, prs: Any, mini: Any, only_prs: Any = None, stale_hours: Any = None, tags: Any = None, now: Any = None, release_of: Any = None) -> Any:
    tags = {} if tags is None else tags
    by_num = {p["number"]: p for p in prs}
    rows = []
    for b in beads:
        d = days_since(b.get("updated_at", ""), now)
        if stale_hours and (d is None or d * 24 < stale_hours):
            continue
        ev = []
        for n, why in sorted(match_prs(b, prs).items()):
            p = by_num[n]
            sha = (p.get("mergeCommit") or {}).get("oid")
            ev.append({"pr": n, "state": p["state"], "why": why, "title": p["title"][:60], "release": release_of(sha, tags) if p["state"] == "MERGED" else None})
        if only_prs and not {e["pr"] for e in ev} & only_prs:
            continue
        due = next((l for l in b.get("labels") or [] if l.startswith("due-")), "")  # noqa: E741
        row = {"id": b["id"], "prio": b.get("priority"), "title": b["title"][:70], "days": d, "assignee": b.get("assignee") or "", "due": due, "prs": ev}
        row["hint"] = classify_hint(row, mini)
        rows.append(row)
    return rows


def table(rows: Any) -> Any:
    out = ["| bead | P | days | assignee | due | hint | evidence | title |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        e = ", ".join(f'#{p["pr"]} {p["state"].lower()}/{p["why"]}' + (f' in {p["release"]}' if p["release"] else "") for p in r["prs"]) or "-"
        out.append(f'| {r["id"]} | {r["prio"]} | {r["days"]} | {r["assignee"]} | {r["due"]} | {r["hint"]} | {e} | {r["title"]} |')
    return "\n".join(out)


def propose_close(rows: Any, verified_version: Any = None) -> Any:
    if not isinstance(verified_version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", verified_version):
        return []
    cmds = []
    for r in rows:
        if r["hint"] == "A?":
            ev = next((p for p in r["prs"] if p["state"] == "MERGED" and p["why"] in ("noted", "id")
                       and re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+", p["release"] or "")
                       and vkey(p["release"]) <= vkey(verified_version)), None)
            if ev is not None:
                cmds.append(f'bd close {r["id"]} --reason "live: PR #{ev["pr"]} in {ev["release"]}, sm mini verify LIVE-OK {verified_version} '
                            '(per-bead behaviour verification REQUIRED before running)"')
    return cmds


def summary(rows: Any, mini: Any) -> Any:
    p0 = sum(1 for r in rows if r["prio"] == 0)
    rel = sum(1 for r in rows if r["hint"] in ("A?", "B?") and any(p["release"] for p in r["prs"] if p["why"] in ("noted", "id")))
    stale = sum(1 for r in rows if r["days"] is not None and r["days"] > 2)
    return f"beads: {len(rows)} open ({p0} P0), {rel} have merged+released PRs, {stale} untouched >48h", rel


# ----------------------------------------------------------------------------- the result
class Evidence(Model):
    pull_request: int
    state: str  # OPEN | MERGED | CLOSED
    why: str  # noted | id | kw
    title: str = ""
    release: str | None = None


class SweepRow(Model):
    bead: str
    priority: int | None = None
    title: str
    days_since_update: float | None = None
    assignee: str = ""
    due: str = ""
    hint: str  # A? | B? | C? | D?   an inventory hint, never a verdict
    evidence: list[Evidence] = []

    def plain(self) -> dict[str, Any]:
        """The row in the shape the moved table function reads."""
        return {"id": self.bead, "prio": self.priority, "title": self.title, "days": self.days_since_update, "assignee": self.assignee, "due": self.due, "hint": self.hint,
                "prs": [{"pr": e.pull_request, "state": e.state, "why": e.why, "title": e.title, "release": e.release} for e in self.evidence]}


class Sweep(Model):
    """The evidence table of a bead sweep. `inventory_version` is what the installed CLI says, not a live verification."""

    mode: str  # table | summary | proposals
    inventory_version: str | None = None
    verified_version: str | None = None  # set only by --propose-close after an exact LIVE-OK
    rows: list[SweepRow] = []
    summary: str = ""
    proposals: list[str] = []  # commands a person or agent may run after verifying each bead's behaviour; never run here
    refused: str = ""

    @property
    def exit_code(self) -> int:
        return 1 if self.refused else 0

    def render(self) -> list[str]:
        if self.mode == "summary":
            return [self.summary]
        if self.mode == "proposals":
            return [self.refused] if self.refused else (self.proposals or ["no class A candidates"])
        return [f"CLI inventory {self.inventory_version} (not live verification); {len(self.rows)} beads", "", *table([r.plain() for r in self.rows]).splitlines()]


def to_rows(rows: list[dict[str, Any]]) -> list[SweepRow]:
    return [SweepRow(bead=r["id"], priority=r["prio"], title=r["title"], days_since_update=r["days"], assignee=r["assignee"], due=r["due"], hint=r["hint"],
                     evidence=[Evidence(pull_request=p["pr"], state=p["state"], why=p["why"], title=p["title"], release=p["release"]) for p in r["prs"]]) for r in rows]


# ----------------------------------------------------------------------------- the world
class World:
    def __init__(self, rt: Any):
        s = rt.settings or {}
        self.rt, self.commands, self.clock, self.store, self.env = rt, rt.providers.commands, rt.providers.clock, rt.providers.store, child_env(s)
        self.tracker, self.repo = str(tracker_dir(s)), str(s.get("repo") or "")
        self.core_dir = os.path.expanduser(str(s.get("core_repo_dir") or "~/Code/heinzel"))
        self.cli = str(s.get("mini_cli") or "hz")

    def release_of(self, sha: str | None, tags_cache: dict[str, str | None]) -> str | None:
        if not sha:
            return None
        if sha not in tags_cache:
            out = self.commands.run(["git", "-C", self.core_dir, "tag", "--contains", sha, "--sort=creatordate", "-l", "v*"], env=self.env, timeout=90).out.split()
            tags_cache[sha] = next((t for t in out if vkey(t)), None)
        return tags_cache[sha]

    def fetch_beads(self, prio: int | None) -> list[dict[str, Any]]:
        cmd = ["bd", "list", "--status", "open,in_progress,blocked", "--json", "--limit", "0"]
        if prio is not None:
            cmd += ["--priority", str(prio)]
        try:
            found = json.loads(self.commands.run(cmd, cwd=self.tracker, env=self.env, timeout=120).out or "[]")
        except ValueError:
            return []
        return found if isinstance(found, list) else []

    def fetch_prs(self) -> list[dict[str, Any]]:
        cached = self.store.load(*PRS_CACHE) or {}
        if cached.get("rows") and self.clock.now() - float(cached.get("at") or 0) < PRS_CACHE_SECONDS:
            return list(cached["rows"])
        prs, _why = Gh(self.commands, self.repo, timeout=120, clock=self.clock).try_json(
            ["pr", "list", "-R", self.repo, "--state", "all", "--limit", "600", "--json", "number,title,state,headRefName,mergeCommit,body"])
        if not isinstance(prs, list):
            return []
        for p in prs:
            p["body"] = (p.get("body") or "")[:600]
        self.store.save(*PRS_CACHE, {"at": self.clock.now(), "rows": prs})
        return prs

    def inventory_version(self) -> str | None:
        m = re.search(r"hz (\d+\.\d+\.\d+)", self.commands.run([self.cli, "--version"], env=self.env, timeout=20).out)
        return m.group(1) if m else None


def verified_live_version(cli: str | None) -> str | None:
    """The version, when the live verification of this machine says LIVE-OK for exactly the version the CLI reports;
    else None. No verification available: None (no proposal is ever made on the inventory alone)."""
    if not isinstance(cli, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", cli):
        return None
    try:
        from .. import go, mini  # type: ignore[attr-defined]

        verdict = mini.verify(go.world())
    except Exception:
        return None
    return cli if getattr(verdict, "live", False) is True and getattr(verdict, "version", "") == cli else None


def sweep(priority: int | None, everything: bool, stale_hours: int | None = None, only_prs: set[int] | None = None, propose: bool = False, summary_only: bool = False,
          rt: Any = None, verify: Callable[[str | None], str | None] = verified_live_version) -> Sweep:
    """Raises ValueError when no slice is chosen."""
    if priority is None and not everything and not summary_only:
        raise ValueError("choose a slice")
    w = World(rt or runtime())
    mini = w.inventory_version()
    now = datetime.fromtimestamp(w.clock.now(), UTC)
    rows = build(w.fetch_beads(priority), w.fetch_prs(), mini, only_prs, stale_hours, now=now, release_of=w.release_of)
    base = Sweep(mode="table", inventory_version=mini, rows=to_rows(rows), summary=summary(rows, mini)[0])
    if summary_only:
        return base.model_copy(update={"mode": "summary"})
    if propose:
        verified = verify(mini)
        if verified is None:
            return base.model_copy(update={"mode": "proposals", "refused": "no closure proposals: a fresh exact LIVE-OK matching the CLI inventory is unavailable; "
                                                                           "per-bead behaviour remains unverified"})
        return base.model_copy(update={"mode": "proposals", "verified_version": verified, "proposals": propose_close(rows, verified)})
    return base
