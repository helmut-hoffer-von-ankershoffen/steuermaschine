"""status: the hourly view. The merge gate per repository, the focused slot usage, and every open pull request
grouped ready, needs work, held and draft. Read-only.

The gate is in status_lib/gate.py, the table in status_lib/table.py. The slot sizes come from the probe `go` uses.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, Field

from steuermaschine import journal as core_journal
from steuermaschine.engine import Refuse
from steuermaschine.github.sonar import Sonar, urllib_fetch
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime

from .go import world
from .go_lib import legacy, probes
from .status_lib import gate, table

DEFAULT_REPOS = {"heinzel": "", "heinzel-plugins": "plugins ", "heinzel-acme": "acme "}


class StatusSettings(BaseModel):
    """Settings of `sm status` (mixin of the plugin's settings table)."""

    status_repos: dict[str, str] = Field(default=DEFAULT_REPOS,
                                         description="Repositories of the pull request table (same owner as `repo`): name -> prefix of its keys in the action table.")
    status_gate_repos: list[str] = Field(default=list(DEFAULT_REPOS), description="Repositories whose merge gate is read (Sonar, coverage badge, ruleset).")
    status_gate_notes: dict[str, str] = Field(default={}, description="Repositories without a readable gate: name -> the one line shown for it.")
    status_doc_cap_bytes: int = Field(16 * 1024, description="`sm status --write`: the size the tracking document may not grow past; older checkpoints and log lines move out.")
    status_keep_checkpoints: int = Field(3, description="`sm status --write`: the newest checkpoints that stay in the tracking document.")
    status_log_dir: Path | None = Field(None, description="Where moved text goes, one file per day (YYYY-MM-DD.md); none: research/log in the tracker's repository.")


class StatusGate(Model):
    repo: str
    sonar: str = ""
    coverage: str = ""
    required: list[str] = []
    not_yet: list[str] = []
    marks: list[str] = []  # NOT A | RED | BYPASS | NO RULESET: what to act on; RULESET UNREAD: GitHub did not answer
    note: str = ""  # a repository without a readable gate: why

    def line(self) -> str:
        return gate.gate_line(self.model_dump())


class StatusSlots(Model):
    used_bytes: dict[str, int]
    limit_bytes: dict[str, int]
    free_bytes: int
    heads_left: int
    source: str = "live"  # live | cache (the last hourly read, when GitHub did not answer)

    def line(self) -> str:
        return table.slot_line(self.used_bytes)


class StatusPr(Model):
    pr: str  # "#390", "plugins #38"
    number: int
    group: str  # ready | needs work | held | draft
    summary: str
    state: str  # GitHub's merge state
    failing: str  # failing checks that hold it, comma separated
    age: str  # of the head commit
    action: str
    owner: str  # NO OWNER when the action table has no row
    updated: str


class Status(Model):
    """The hourly view: merge gate, focused slots, pull requests."""

    gate: list[StatusGate] = []
    slots: StatusSlots | None = None
    prs: list[StatusPr] = []
    stale: list[StatusPr] = []  # unchanged for more than six hours
    no_owner: list[str] = []
    unknown: list[str] = []  # what could not be read, with the reason
    read_only: bool = True
    written: list[str] = []  # `--write`: what was moved and written
    view: str = Field("table", exclude=True)  # table | markdown | stale
    check: bool = Field(False, exclude=True)
    now: float = Field(0.0, exclude=True)

    def render(self) -> list[str]:
        unknown = [f"UNKNOWN  {u}" for u in self.unknown]
        if self.view == "stale":
            now = datetime.fromtimestamp(self.now, UTC)
            return [f"{r.pr}  unchanged {table.age(r.updated, now)}  {r.owner}  {r.summary}" for r in self.stale] + unknown
        markdown = self.view == "markdown"
        lines = [g.line() for g in self.gate] + ([self.slots.line()] if self.slots else [])
        out = ["**Merge gate**", "", *[("- " if markdown else "") + line for line in lines], ""]
        out += table.render([p.model_dump() for p in self.prs], markdown).split("\n")
        while out and not out[-1]:
            out.pop()
        return out + unknown + ([f"NO OWNER: {', '.join(self.no_owner)}"] if self.check and self.no_owner else []) + [f"WRITTEN  {w}" for w in self.written]

    @property
    def exit_code(self) -> int:
        return 1 if self.check and self.no_owner else 0


def fetch_prs(gh: Any, owner: str, repo: str) -> tuple[list[dict[str, Any]], str]:
    data, why = gh.try_json(["api", "graphql", "-f", f"query={table.QUERY}", "-F", f"owner={owner}", "-F", f"repo={repo}"], timeout=30)
    if why:
        return [], f"could not list {repo}: {why}"
    try:
        return [table.normalise(n) for n in data["data"]["repository"]["pullRequests"]["nodes"]], ""
    except (KeyError, TypeError):
        return [], f"could not list {repo}: no pull requests in the answer"


def slots(w: probes.World) -> tuple[StatusSlots | None, str]:
    source = "live"
    try:
        sizes = probes.probe_slots(w)
    except (RuntimeError, KeyError, TypeError) as exc:
        cached = legacy.slot_sizes_cache(w.state_dir)
        if cached is None:
            return None, f"focused slots: {exc}"
        sizes, source = {str(k): int(v) for k, v in cached["sizes"].items()}, "cache"
    free = table.slot_free(sizes)
    return StatusSlots(used_bytes={n: sizes.get(n, 0) for n in table.SLOT_NAMES}, limit_bytes=dict(table.SLOT_LIMITS), free_bytes=free,
                       heads_left=free // table.HEAD_BYTES, source=source), ""


def collect(w: probes.World, view: str = "table", check: bool = False) -> Status:
    s = w.settings
    fetch = getattr(runtime().providers, "extra", {}).get("http_fetch") or urllib_fetch  # the one HTTP GET function; a test injects a fake
    owner = w.repo.split("/")[0]
    prefixes: dict[str, str] = dict(s.get("status_repos") or DEFAULT_REPOS)
    gate_repos: list[str] = [] if view == "stale" else list(s.get("status_gate_repos") or [])
    notes: dict[str, str] = dict(s.get("status_gate_notes") or {})
    unknown: list[str] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        listed = pool.map(lambda repo: fetch_prs(w.gh, owner, repo), prefixes)
        advisory = pool.map(lambda repo: gate.advisory_checks(w.gh, f"{owner}/{repo}"), prefixes)
        gates = pool.map(lambda repo: gate.gate_row(Sonar(f"{owner}_{repo}", fetch=fetch, clock=w.clock), fetch, w.gh, owner, repo), gate_repos)
        slot = pool.submit(slots, w) if view != "stale" else None
        prs = {}
        for repo, (items, why) in zip(prefixes, listed, strict=True):
            prs[repo] = items
            unknown += [why] if why else []
        ignored = dict(zip(prefixes, advisory, strict=True))
        read = list(gates)
        unknown += [row.pop("unknown") for row in read if row["unknown"]]
        gate_rows = [StatusGate(**{k: v for k, v in row.items() if k != "unknown"}) for row in read] + [StatusGate(repo=repo, note=note) for repo, note in notes.items()]
        usage, why = slot.result() if slot else (None, "")
        unknown += [why] if why else []
    try:
        path = s.get("go_pr_actions_file")
        actions = legacy.pr_actions(Path(str(path)).expanduser() if path else None)
    except (OSError, ValueError) as exc:
        actions = {}
        unknown.append(f"the action table is unreadable: {exc}")
    now = datetime.fromtimestamp(w.now(), UTC)
    rows = table.rows(prs, actions, now, prefixes, ignored)
    return Status(gate=gate_rows, slots=usage, prs=[StatusPr(**r) for r in rows], stale=[StatusPr(**r) for r in table.stale(rows, now)],
                  no_owner=[r["pr"] for r in rows if r["owner"] == "NO OWNER"], unknown=unknown, view=view, check=check, now=w.now())


@native("status", writes=True, replaces=("hz-status",), help=H(
    "The hourly view: merge gate per repository, focused slot usage, every open pull request with its action and owner.",
    "Use for the hourly owner update and the handoff, and when you need every open pull request on one screen.",
    "without --write nothing (GitHub reads only). With --write: caps the tracking document (older checkpoints and log lines move to"
    " the dated log file) and writes the hourly facts; with --write --live also commits and pushes the handoff.",
    "sm status            (markdown: sm status --md; every hour: sm status --write --live)",
    "sm why NUMBER for one pull request under Needs work.",
    "The first lines are the merge gate: Sonar ratings, coverage, what main requires. `<<< NOT A`, `RED`, `BYPASS` or\n"
    "`NO RULESET` is what to act on. Then `Focused slots`: bytes used per registry variable and about how many heads\n"
    "still fit. Then the pull requests, grouped Ready, Needs work, Held and Draft. A failing `review` check and a\n"
    "diagnostic `Focused ...` run never put a pull request under Needs work; the checks merge-gate and coverage-floor\n"
    "do only once main requires them. A pull request without a row in the action table prints NO OWNER.\n"
    "--write takes over the hourly update, the handoff and the checkpoint upkeep (the old `doc hour`, `doc handoff`): the\n"
    "tracking document never grows past the setting status_doc_cap_bytes; the newest status_keep_checkpoints stay."))
def status(md: bool = typer.Option(False, "--md", help="Markdown tables, for the handoff document."),
           check: bool = typer.Option(False, "--check", help="Exit 1 when a pull request has no owner."),
           stale: bool = typer.Option(False, "--stale", help="Only the pull requests unchanged for more than six hours."),
           write: bool = typer.Option(False, "--write", help="Cap the tracking document and write the hourly facts (local)."),
           live: bool = typer.Option(False, "--live", help="With --write: also commit and push the handoff.")) -> Status:
    if live and not write:
        raise Refuse("--live goes with --write", "sm status --write --live", code=2)
    found = collect(world(), "stale" if stale else "markdown" if md else "table", check)
    if write:
        found.written = write_document(live)
        found.read_only = False
    return found


def write_document(live: bool) -> list[str]:
    """Cap the tracking document, then the hourly facts; with `live` the handoff commit and push."""
    from .coord_lib.base import document, journal_lock, tracker_dir
    from .handoff_lib import hour, snapshot
    from .status_lib import cap

    rt: Any = runtime()
    s = rt.settings
    path = document(s)
    limits = cap.Cap(limit=int(s.get("status_doc_cap_bytes") or 16 * 1024), keep=int(s.get("status_keep_checkpoints") or 3))
    out: list[str] = []
    day = datetime.fromtimestamp(rt.providers.clock.now(), UTC).strftime("%Y-%m-%d")
    folder = Path(os.path.expanduser(str(s.get("status_log_dir")))) if s.get("status_log_dir") else tracker_dir(s) / "research" / "log"
    archive = folder / f"{day}.md"

    def keep(moved: str) -> None:  # the moved text is safe before the document shrinks
        folder.mkdir(parents=True, exist_ok=True)
        with archive.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## Moved from {path.name} by sm status --write\n\n{moved}\n")

    text, capped = core_journal.rewrite(path, lambda t: cap.apply(t, limits), keep, [journal_lock(s)])
    if capped != text:
        out.append(f"{path.name}: {len(text.encode())} -> {len(capped.encode())} bytes; the rest moved to {archive}")
    if (size := len(capped.encode())) > limits.limit:
        out.append(f"{path.name} is {size} bytes, over the cap of {limits.limit}: the rest is not checkpoints or log; shorten it by hand")
    try:
        done = hour.build(rt, rt.commands(), not live, include_local_commits=False)
    except snapshot.StepFailed as stop:
        raise Refuse(str(stop), "sm status --write") from None
    return out + [line for line in done.render()[:3]]

