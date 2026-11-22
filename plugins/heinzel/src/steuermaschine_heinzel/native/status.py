"""status: the hourly view. The merge gate per repository, the focused slot usage, and every open pull request
grouped ready, needs work, held and draft. Read-only.

The gate is in status_lib/gate.py, the table in status_lib/table.py. The slot sizes come from the probe `go` uses.
"""

from __future__ import annotations

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
from .status_lib import gate, owner, prtable, table

DEFAULT_REPOS = {"heinzel": "", "heinzel-plugins": "plugins ", "heinzel-acme": "acme "}


class StatusSettings(BaseModel):
    """Settings of `sm status` (mixin of the plugin's settings table)."""

    status_repos: dict[str, str] = Field(default=DEFAULT_REPOS,
                                         description="Repositories of the pull request table (same owner as `repo`): name -> prefix of its keys in the action table.")
    status_gate_repos: list[str] = Field(default=list(DEFAULT_REPOS), description="Repositories whose merge gate is read (Sonar, coverage badge, ruleset).")
    status_gate_notes: dict[str, str] = Field(default={}, description="Repositories without a readable gate: name -> the one line shown for it.")
    status_doc_cap_bytes: int = Field(16 * 1024, description="`sm status --write`: the size the tracking document may not grow past; older checkpoints and log lines move out.")
    status_keep_checkpoints: int = Field(3, description="`sm status --write`: the newest checkpoints that stay in the tracking document.")
    status_pr_repos: list[str] = Field(default=prtable.DEFAULT_REPOS, description="Repositories of `sm status --prs` and the owner update (same owner as `repo`).")
    status_decisions_file: Path | None = Field(None, description="The owner's open decisions, one per line (`- [ ] text`; `- [x]` is closed); `sm status --owner` lists the open ones.")


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


class PrTable(Model):
    """`sm status --prs`: every open pull request of every repository, over gh, nothing stored."""

    rows: list[prtable.PrRow] = []
    unknown: list[str] = []

    def render(self) -> list[str]:
        return prtable.render(self.rows) + [f"UNKNOWN  {u}" for u in self.unknown]


def collect_prs(w: probes.World) -> PrTable:
    owner_name = w.repo.split("/")[0]
    now = datetime.fromtimestamp(w.now(), UTC)
    repos = list(w.settings.get("status_pr_repos") or prtable.DEFAULT_REPOS)
    with ThreadPoolExecutor(max_workers=4) as pool:
        got = list(pool.map(lambda repo: prtable.fetch(w.gh, owner_name, repo, now), repos))
    return PrTable(rows=[r for rows, _ in got for r in rows], unknown=[why for _, why in got if why])


def collect_owner(w: probes.World, since: str) -> owner.OwnerUpdate:
    from .tasks_lib import beads

    s = w.settings
    owner_name = w.repo.split("/")[0]
    now = w.now()
    try:
        began = owner.since_of(since, now)
    except ValueError as exc:
        raise Refuse(f"--since {since!r}: {exc}", "sm status --owner --since 2h", code=2) from None
    repos = list(s.get("status_pr_repos") or prtable.DEFAULT_REPOS)
    unknown: list[str] = []
    prs = collect_prs(w)
    unknown += prs.unknown
    releases, why = owner.releases_since(w.gh, owner_name, repos, began)
    unknown += why
    today, yesterday, why_merged = owner.merged_counts(w.gh, owner_name, repos, now)
    unknown += [why_merged] if why_merged else []
    counts: dict[str, int] = {}
    try:
        tracker = beads.Tracker(runtime())
        counts = {"open": len(tracker.jl("open")), "in progress": len(tracker.jl("in_progress")), "blocked": len(tracker.jl("blocked")), "ready": len(tracker.jl(ready=True))}
    except Refuse as exc:
        unknown.append(f"beads: {exc}")
    path = s.get("status_decisions_file")
    decisions, source = owner.read_decisions(Path(str(path)).expanduser() if path else None)
    return owner.OwnerUpdate(now=owner.stamp(now), since=owner.stamp(began), releases=releases, repos=[owner.repo_summary(r, prs.rows) for r in repos], beads=counts,
                             main_ci=owner.main_ci_line(probes.main_runs(w, 1), now), merged_today=today, merged_yesterday=yesterday, decisions=decisions,
                             decisions_source=source, unknown=unknown)


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
    "sm status            (markdown: sm status --md; all repositories: sm status --prs; the 2-hourly owner update: sm status --owner; every hour: sm status --write --live)",
    "sm why NUMBER for one pull request under Needs work.",
    "The first lines are the merge gate: Sonar ratings, coverage, what main requires. `<<< NOT A`, `RED`, `BYPASS` or\n"
    "`NO RULESET` is what to act on. Then `Focused slots`: bytes used per registry variable and about how many heads\n"
    "still fit. Then the pull requests, grouped Ready, Needs work, Held and Draft. A failing `review` check and a\n"
    "diagnostic `Focused ...` run never put a pull request under Needs work; the checks merge-gate and coverage-floor\n"
    "do only once main requires them. A pull request without a row in the action table prints NO OWNER.\n"
    "--write takes over the hourly update, the handoff and the checkpoint upkeep (the old `doc hour`, `doc handoff`): the\n"
    "tracking document never grows past the setting status_doc_cap_bytes; the newest status_keep_checkpoints stay.\n"
    "--prs: one table over gh of every repository (setting status_pr_repos): merge state, CI (the advisory review check and\n"
    "Focused runs never count), Sonar, unresolved review threads, age as a duration, author. Nothing is stored.\n"
    "--owner [--since 2h]: the owner update, rendered from reads only: releases since the time, pull requests per repository,\n"
    "bead counts, main's CI, merges today against yesterday, and the open lines of the decisions file (status_decisions_file)."))
def status(md: bool = typer.Option(False, "--md", help="Markdown tables, for the handoff document."),
           check: bool = typer.Option(False, "--check", help="Exit 1 when a pull request has no owner."),
           stale: bool = typer.Option(False, "--stale", help="Only the pull requests unchanged for more than six hours."),
           write: bool = typer.Option(False, "--write", help="Cap the tracking document and write the hourly facts (local)."),
           live: bool = typer.Option(False, "--live", help="With --write: also commit and push the handoff."),
           prs: bool = typer.Option(False, "--prs", help="One table of the open pull requests of every repository: state, CI, Sonar, unresolved threads, age, author."),
           owner_update: bool = typer.Option(False, "--owner", help="The 2-hourly owner update: releases, pull requests per repository, beads, main CI, merges, open decisions."),
           since: str = typer.Option("2h", "--since", metavar="TIME", help="With --owner: releases since 2h, 90m, 1d or an ISO time with its zone.")) -> Status | PrTable | owner.OwnerUpdate:
    if live and not write:
        raise Refuse("--live goes with --write", "sm status --write --live", code=2)
    if prs and owner_update:
        raise Refuse("--prs and --owner are two views; pick one", "sm status --owner", code=2)
    if (prs or owner_update) and write:
        raise Refuse("--write goes with the plain status view", "sm status --write", code=2)
    if owner_update:
        return collect_owner(world(), since)
    if prs:
        return collect_prs(world())
    found = collect(world(), "stale" if stale else "markdown" if md else "table", check)
    if write:
        found.written = write_document(live, found)
        found.read_only = False
    return found


BEGIN = "<!-- sm status --write: begin (generated; do not edit) -->"
END = "<!-- sm status --write: end -->"


CHECKPOINT_LINES = 25  # the most lines `sm status --write` puts into the tracking document; the full facts go to the log file
LINE_WIDTH = 160
SHOWN = 3  # lines of one list (release, alarms, owner asks) kept in the checkpoint


def state_block(found: Status, now: float, archive: Path | None = None) -> tuple[str, str]:
    """(the checkpoint for the tracking document, the full facts for the dated log file). The checkpoint is at most
    CHECKPOINT_LINES lines of LINE_WIDTH characters: counts and the first lines of release, status view, alarms and
    owner asks, and where the rest is. The facts come from the same reads as `sm status`, `sm why` and `sm alarm list`."""
    from steuermaschine.native import call

    from . import alarm

    def release_status(line: bool = False) -> Any:  # through the registry of the runtime: an import would widen the reach of every writer
        return call(next(c for c in runtime().commands() if getattr(c, "path", "") == "release status"), line=line)

    def lines(read: Any) -> list[str]:
        try:
            return [line for line in read() if line.strip()]
        except Exception as error:
            return [f"unknown: {type(error).__name__}: {error}"[:200]]

    shown = found.model_copy(update={"view": "markdown", "check": False, "written": []})
    stamp = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d %H:%M UTC")
    parts = {"Release": lines(lambda: release_status(line=False).render()) or ["no release in flight"],
             "Status": [line for line in shown.render() if line.strip()],
             "Alarms": lines(lambda: alarm.alarm_list(check=False).render()),
             "For the owner": lines(alarm.owner_asks) or ["nothing open"]}
    where = f"full facts: {archive.parent.name}/{archive.name}" if archive else "full facts: the dated log file"
    full = "\n".join([f"### State facts {stamp}", *(f"**{name}**\n" + "\n".join(f"- {line}" for line in items) for name, items in parts.items())])
    out = [BEGIN, f"## State now (written by `sm status --write` at {stamp}; {where})"]
    for name, items in parts.items():
        out += [f"- **{name}** ({len(items)})", *(f"  - {item.strip('- ')[:LINE_WIDTH]}" for item in items[:SHOWN])]
        out += [f"  - ... {len(items) - SHOWN} more" for _ in range(len(items) - SHOWN > 0)]
    out = [*out[:CHECKPOINT_LINES - 1], END]
    return "\n".join(out), full


def place(text: str, block: str) -> str:
    """The document with the state section replaced, or inserted before the work instructions (`## 6.`), else at the end."""
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        return head + block + rest.split(END, 1)[1]
    lines = text.split("\n")
    at = next((i for i, line in enumerate(lines) if line.startswith("## 6.")), len(lines))
    return "\n".join([*lines[:at], block, "", *lines[at:]])


def write_document(live: bool, found: Status | None = None) -> list[str]:
    """Render the state section, cap the tracking document, then the hourly facts; with `live` the handoff commit and push."""
    from .coord_lib.base import document, journal_lock, log_file
    from .handoff_lib import hour, snapshot
    from .status_lib import cap

    rt: Any = runtime()
    s = rt.settings
    path = document(s)
    limits = cap.Cap(limit=int(s.get("status_doc_cap_bytes") or 16 * 1024), keep=int(s.get("status_keep_checkpoints") or 3))
    now = rt.providers.clock.now()
    archive = log_file(s, now)
    block, facts = state_block(found or Status(), now, archive)
    out: list[str] = []

    def change(text: str) -> tuple[str, str]:
        placed = place(text, block)
        capped, moved = cap.apply(placed, limits)
        if len(capped.encode()) > limits.limit and len(capped.encode()) > len(text.encode()):  # never grow a document past its cap
            raise Refuse(f"{path.name} would grow to {len(capped.encode())} bytes, over the cap of {limits.limit}: nothing written", "sm status --write")
        return capped, moved or ("\n" if placed != text else "")  # a new state section is a change even when nothing moves

    def keep(moved: str) -> None:  # the moved text is safe before the document shrinks
        if not moved.strip():
            return
        archive.parent.mkdir(parents=True, exist_ok=True)
        with archive.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## Moved from {path.name} by sm status --write\n\n{moved}\n")

    archive.parent.mkdir(parents=True, exist_ok=True)
    with archive.open("a", encoding="utf-8") as handle:  # the long facts live in the log, not in the tracking document
        handle.write(f"\n{facts}\n")
    text, capped = core_journal.rewrite(path, change, keep, [journal_lock(s)])
    out.append(f"{path.name}: state section written; {len(text.encode())} -> {len(capped.encode())} bytes" +
               (f"; older checkpoints and log lines moved to {archive}" if len(capped) < len(place(text, block)) else ""))
    if (size := len(capped.encode())) > limits.limit:
        out.append(f"{path.name} is {size} bytes, over the cap of {limits.limit}: the rest is not checkpoints or log; shorten it by hand")
    try:
        done = hour.build(rt, rt.commands(), not live, include_local_commits=False)
    except snapshot.StepFailed as stop:
        raise Refuse(str(stop), "sm status --write") from None
    return out + [line for line in done.render()[:3]]
