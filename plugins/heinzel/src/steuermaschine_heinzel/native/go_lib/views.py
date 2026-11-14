"""The read-only views of `sm go --view start|next|prs|wait|tick`: the startup orientation, the ranked next actions, the
pull request table, the biggest wait and the anomaly digest. They are built from the same probes `go` decides on (one
collection per call); `prs` and the quality and owner items of `next` reuse `sm status`. Nothing here writes: no claim,
no audit watermark, no CI deferral, no announcement, no bead note.
"""

from __future__ import annotations

import os
import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from steuermaschine.models import Model

from . import legacy, probes
from . import views_moved as moved
from .decide import classify_pr, known

VIEWS = ("start", "next", "prs", "wait", "tick")
RUNNER_CAP = 5  # PR CI runs GitHub gives the macOS lanes at once
STARVED_MINUTES = 15
LOAD_HIGH = 45.0


class GoViewItem(Model):
    priority: int
    rank: float
    key: str
    what: str
    do: str
    watching: str = ""  # who holds a live claim on it


class GoViewPr(Model):
    pr: str
    number: int
    group: str
    summary: str
    state: str
    failing: str
    age: str
    action: str
    owner: str
    updated: str


class GoView(Model):
    """One view of `sm go --view`."""

    name: str  # start | next | prs | wait | tick
    lines: list[str]
    items: list[GoViewItem] = []  # next
    prs: list[GoViewPr] = []  # prs
    buckets: dict[str, list[int]] = {}  # wait: non-draft PRs by check state
    biggest_wait: str = ""  # wait
    unblock: str = ""  # wait
    unknown: list[str] = []  # what a view could not read, with the reason

    def render(self) -> list[str]:
        return self.lines + [f"UNKNOWN  {u}" for u in self.unknown]


def build(name: str, w: probes.World, state: dict, go: Any, show_all: bool = False, thread: str | None = None) -> GoView:
    if name == "start":
        return start(w, go, thread)
    if name == "next":
        return next_actions(w, state, go, show_all)
    if name == "prs":
        return prs(w)
    if name == "wait":
        return wait(w, state, go)
    return tick(w, state, go)


# ---------------------------------------------------------------- helpers
def setting_path(w: probes.World, name: str) -> Path | None:
    value = w.settings.get(name)
    return Path(str(value)).expanduser() if value else None


def doc_lines(w: probes.World) -> tuple[list[str], str]:
    path = setting_path(w, "go_tracking_doc")
    if path is None:
        return [], "the tracking document is not configured (setting go_tracking_doc)"
    try:
        return path.read_text().splitlines(), ""
    except (OSError, UnicodeError) as exc:
        return [], f"the tracking document is unreadable: {exc}"


def status_of(w: probes.World, view: str = "table") -> Any:
    from .. import status  # here, not at the top: status builds on go

    return status.collect(w, view)


def main_runs(state: dict) -> list[dict]:
    queue = state.get("queue")
    return list(queue.get("main_runs") or []) if known(queue) and isinstance(queue, dict) else []


def newest_finished(state: dict) -> dict | None:
    return next((r for r in main_runs(state) if r.get("status") == "completed"), None)


def open_prs(state: dict) -> list[dict]:
    rows = state.get("prs")
    return [r for r in rows if isinstance(r, dict)] if known(rows) and isinstance(rows, list) else []


def age_minutes(created: str, now: float) -> int | None:
    try:
        return int((now - datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()) // 60)
    except (ValueError, AttributeError):
        return None


def load_average() -> float | None:
    try:
        return os.getloadavg()[0]
    except OSError:
        return None


# ---------------------------------------------------------------- start
def start(w: probes.World, go: Any, thread: str | None) -> GoView:
    unknown: list[str] = []
    doc = setting_path(w, "go_tracking_doc")
    lines = [f"PHASE    {go.phase}", f"NEXT     {go.next or '(nothing runnable now)'}"] + ([f"TRAP     {go.trap}"] if go.trap else [])
    lines += [f"Tracking document: {doc or 'not configured (setting go_tracking_doc)'}",
              "1. Read sections 1-3 of the tracking document and the coordinator skill.",
              "2. Check live state: sm go (NEXT after TRAP), sm status, sm queue list, sm alarm list, sm release status.",
              "3. Reconcile timers with the timers README, then resume the highest-priority interrupted work.",
              "4. Refill safe capacity with independent PR and bead work. Record exact next actions in section 5 of the tracking document.",
              "", "Latest checkpoint (historical):"]
    text, why = doc_lines(w)
    lines += moved.latest_checkpoint("\n".join(text)).splitlines() if text else ["Checkpoint unavailable. Locate the tracking document and reconcile live state."]
    unknown += [why] if why else []
    lines += ["", "Timer configuration (local Codex files, not proof of execution):"]
    manifest = setting_path(w, "go_timers_file")
    if manifest is None:
        unknown.append("the timer manifest is not configured (setting go_timers_file)")
    else:
        try:
            lines += moved.timer_rows(tomllib.loads(manifest.read_text()), setting_path(w, "go_codex_home"), thread)
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError, AttributeError) as exc:
            unknown.append(f"timer manifest unavailable or malformed: {type(exc).__name__}: {exc}")
    lines.append("Claude: list timers using the available scheduler API. See the timers README before changes.")
    return GoView(name="start", lines=lines, unknown=unknown)


# ---------------------------------------------------------------- next
def next_actions(w: probes.World, state: dict, go: Any, show_all: bool) -> GoView:
    now = w.now()
    items: list[dict] = []
    unknown: list[str] = []
    items += main_red_items(state)
    queued, stale_queue = current_pr_rows(w, state, queue_items(w, state))
    items += queued
    text, why = doc_lines(w)
    unknown += [why] if why else []
    items += [moved.item(4, moved.slug("ask", t), f"ask, {int(m)} min, no PR: {t[:80]}",
                         moved.brief_cmd(moved.slug("ask", t), f"ask/{moved.slug('ask', t)[4:]}", f"Do the owner's ask: {t[:300]}. Done = a PR, seen on the live thing.")
                         + moved.ship_hint(t), moved.classify(t))
              for m, t in sorted(moved.open_ask_rows(text, now), reverse=True)]
    paused, stale = current_pr_rows(w, state, moved.src_paused(text, now))
    stale = stale_queue + stale
    items += paused
    try:
        table = status_of(w)
        items += quality_items(table) + pr_items(table, now)
        unknown += list(table.unknown)
    except Exception as exc:  # a broken source is a line, never a silent all clear
        unknown.append(f"status: {type(exc).__name__}: {exc}")
    if go.next and go.kind not in ("idle", "unknown"):
        items.append(moved.item(8, f"ship-{go.kind}", f"ship pipeline: {go.phase}"[:140], go.next))
    if moved.update_due(now, legacy.next_last_update(w.state_dir)):
        items.append(moved.item(9, "hourly-update", "hourly update to the owner is due", "sm status --md; send the update in the section 1 shape"))
    items.sort(key=lambda i: (i["rank"], i["prio"]))
    claims = legacy.next_claims(w.state_dir, now)
    newest = newest_finished(state)
    kind = state.get("main_workload") if isinstance(state.get("main_workload"), dict) else {}
    ran = kind.get("workload") if newest and kind.get("run") == newest.get("databaseId") else "unknown"
    evidence = {"full": "full-candidate evidence: this run", "focused": "focused CI only, full-candidate evidence: none"}.get(ran, "full-candidate evidence: unknown")
    main_ci = (f"main CI: newest finished run {str(newest.get('headSha'))[:8]} {newest.get('conclusion')}; {evidence}"
               if newest else "main CI: no finished run found; full-candidate evidence: unknown")
    lines = moved.render(items, claims, now, show_all, bugs_line(w, text), None, main_ci)
    lines += [f"not offered: {line}" for line in stale]  # the tracking document keeps the row; only the suggestion is dropped
    rows = [GoViewItem(priority=i["prio"], rank=i["rank"], key=i["key"], what=i["what"], do=i["do"], watching=str((claims.get(i["key"]) or {}).get("owner", "")))
            for i in items]
    return GoView(name="next", lines=lines, items=rows, unknown=unknown)


PR_REF = re.compile(r"(?<![\w/])(?<!plugins )(?:PR\s*)?#([1-9][0-9]{0,5})\b")
QUEUE_KEY = re.compile(r"queue-#?([1-9][0-9]{0,5})")
OTHER_REPO = re.compile(r"queue-[a-z]")  # a queue row of another repository (plugins #71): its number is not a core PR
MAX_PR_READS = 16


def current_pr_rows(w: probes.World, state: dict, rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Drop suggestions to resume work on a PR that is closed or merged now (fresh PR state, never the document's
    word); a PR whose state cannot be read keeps its row, marked unknown. Returns (kept rows, what was dropped)."""
    open_now = {p.get("number") for p in open_prs(state)}
    states: dict[int, str] = {}
    kept, stale = [], []
    for row in rows:
        if OTHER_REPO.match(str(row.get("key", ""))):
            kept.append(row)
            continue
        numbers = [int(n) for n in PR_REF.findall(f"{row.get('what', '')} {row.get('do', '')}")]
        if found := QUEUE_KEY.fullmatch(str(row.get("key", ""))):  # a queue row names its PR in the key only
            numbers.insert(0, int(found.group(1)))
        closed, unread = [], []
        for number in dict.fromkeys(numbers):
            if number in open_now:
                continue
            if number not in states:
                states[number] = pr_state(w, number) if len(states) < MAX_PR_READS else "UNKNOWN"
            if states[number] in ("CLOSED", "MERGED"):
                closed.append(f"#{number} {states[number]}")
            elif states[number] != "OPEN":
                unread.append(f"#{number}")
        if closed:
            stale.append(f"{row.get('what', '')[:80]} ({', '.join(closed)})")
            continue
        if unread:
            row = {**row, "what": f"{row.get('what', '')} (PR {', '.join(unread)} state unknown)"}
        kept.append(row)
    return kept, stale


def pr_state(w: probes.World, number: int) -> str:
    try:
        data = w.gh_json(["pr", "view", str(number), "-R", w.repo, "--json", "state"], timeout=8)
    except RuntimeError:
        return "UNKNOWN"
    return str(data.get("state") or "UNKNOWN").upper() if isinstance(data, dict) else "UNKNOWN"


def main_red_items(state: dict) -> list[dict]:
    done = newest_finished(state)
    if not done or done.get("conclusion") == "success":
        return []
    run, sha = done.get("databaseId"), str(done.get("headSha") or "")[:8]
    claimant = next((p.get("number") for p in open_prs(state) if re.search(r"main.{0,12}(red|ci)|fix main", str(p.get("title") or ""), re.I)
                     or sha[:7] in str(p.get("title") or "")), None)
    key = f"main-red-{sha}"
    if claimant:
        return [moved.item(2, key, f"main red at {sha}, run {run}; claimed by PR #{claimant}", f"sm ci why {claimant}, then sm queue add {claimant}")]
    task = (f"Make main green again. Read sm ci why {run} and sm ci faillog {run} first, name the failing test, fix its cause "
            "(no rerun, no weaker test or limit). Done = a PR whose CI is green and which names the test.")
    return [moved.item(2, key, f"main RED at {sha}, run {run} ({done.get('conclusion')}); no PR claims it; sm ci main shows the evidence",
                       moved.brief_cmd(f"fix-main-{sha}", f"fix/main-{sha}", task))]


def queue_items(w: probes.World, state: dict) -> list[dict]:
    out = []
    try:
        running = any(re.search(r"(?:sm|steuermaschine)\b.*\bdaemon queue\b|[a-z]+-queue run\b", cmd) for _, _, cmd in w.processes())
    except RuntimeError:
        running = True  # unknown is not "down"
    if not running:
        out.append(moved.item(3, "queue-down", "the merge queue loop is not running", "sm daemon queue --help (start it under launchd, never twice)"))
    data = legacy.queue_state(w.state_dir)
    path = w.settings.get("go_pr_actions_file")
    try:
        actions = legacy.pr_actions(Path(str(path)).expanduser() if path else None)
    except (OSError, ValueError):
        actions = {}
    for entry in data["items"]:
        if entry.get("status") != "needs work":
            continue
        key = str(entry.get("key", "?"))
        who = (actions.get(key) or {}).get("owner", "")
        if who and who != "orchestrator":
            continue
        topic = key.replace(" #", "-").replace("#", "")
        detail = str(entry.get("detail", ""))
        task = f"Get {key} through the queue. It needs work: {detail[:200]}. Read the failing logs, fix the cause on its branch, push through sm gate. Done = its checks green."
        out.append(moved.item(3, f"queue-{key.replace(' ', '')}", f"queue: {key} needs work, no owner: {detail[:100]}",
                              moved.brief_cmd(f"fix-{topic}", f"fix/{topic}", task) + f"; when green: sm queue add {key.replace('#', '')}"))
    return out


def quality_items(table: Any) -> list[dict]:
    out = []
    for gate in table.gate:
        bad = [m for m in gate.marks if m in ("NOT A", "RED")]
        if bad:
            label = gate.repo
            out.append(moved.item(6, f"quality-{label}", f"{label}: {', '.join(bad)} ({gate.sonar or 'Sonar unread'}; {gate.coverage or 'coverage unread'})",
                                  moved.brief_cmd(f"quality-{label}", f"quality/{label}", f"Bring {label} to all Sonar ratings A and a coverage badge that is not red, "
                                                  f"starting with: {', '.join(bad)}. Done = a PR that fixes the causes without exclusions or lower thresholds.")))
    return out


def pr_items(table: Any, now: float) -> list[dict]:
    from ..status_lib import table as status_table

    when = datetime.fromtimestamp(now, UTC)
    out = [moved.item(7, "pr-" + r.pr.replace(" ", ""), f"{r.pr} has NO OWNER: {r.summary[:60]}",
                      f'add "{r.pr.lstrip("#")}" to the PR action table (setting go_pr_actions_file) with a named owner, and start the action')
           for r in table.prs if r.owner == "NO OWNER"]
    out += [moved.item(7, "pr-" + r.pr.replace(" ", ""), f"{r.pr} stale {status_table.age(r.updated, when)}, owner {r.owner}: {r.action[:60]}",
                       f"ask {r.owner} for state or take over (sm ci why {r.number})")
            for r in table.stale if r.owner != "NO OWNER" and r.group != "held"]
    return out


def bugs_line(w: probes.World, text: list[str]) -> str:
    owner = w.repo.split("/")[0]
    repos = list((w.settings.get("status_repos") or {}).keys()) or [w.repo.split("/")[-1]]
    counts: list[int | None] = []
    for repo in repos:
        found, why = w.gh.try_json(["issue", "list", "-R", f"{owner}/{repo}", "--label", "bug", "--state", "open", "-L", "200", "--json", "number"], retry=False)
        counts.append(None if why or not isinstance(found, list) else len(found))
    rows = sum(1 for line in text if line.startswith("|") and re.search(r"\bbug\b", line, re.I))
    total = "?" if None in counts else str(sum(c or 0 for c in counts))
    return f"known bugs open: {total} (issues labelled bug), {rows} section 4 row(s) naming a bug"


# ---------------------------------------------------------------- prs
def prs(w: probes.World) -> GoView:
    table = status_of(w)
    from ..status_lib import table as status_table

    lines = status_table.render([p.model_dump() for p in table.prs], False).split("\n")
    while lines and not lines[-1]:
        lines.pop()
    return GoView(name="prs", lines=lines, prs=[GoViewPr(**p.model_dump()) for p in table.prs], unknown=list(table.unknown))


# ---------------------------------------------------------------- wait
def bucket(row: dict) -> str:
    """One PR by check state: draft, failing (a real failure), pending, cancelled (cancelled checks and nothing failing:
    deferred CI), ready (checks green; merge readiness unverified)."""
    if row.get("isDraft"):
        return "draft"
    verdict = classify_pr(row)
    if verdict["checks"] in ("red", "pending"):
        return "failing" if verdict["checks"] == "red" else "pending"
    states = {(c.get("conclusion") or c.get("state") or "").upper() for c in row.get("statusCheckRollup") or []}
    return "cancelled" if "CANCELLED" in states else "ready"


def buckets_of(rows: list[dict]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for row in rows:
        out.setdefault(bucket(row), []).append(int(row.get("number") or 0))
    return {k: sorted(v) for k, v in sorted(out.items())}


def biggest_wait(buckets: dict[str, list[int]], hold_minutes: int | None, merge_blocked: str, acceptance: str, load: float | None) -> tuple[str, str]:
    """The ladder of the former wait tool's diagnosis, in sm words: the first rung that holds is the biggest wait."""
    if buckets.get("cancelled") and (hold_minutes is not None or acceptance):
        release_wait = f"Acceptance {acceptance} finishes" if acceptance else "the local gate hold clears"
        return (f"{len(buckets['cancelled'])} PRs with deferred CI during the release", f"wait until {release_wait}; the release resumes CI")
    if merge_blocked:
        return f"Merges blocked: {merge_blocked}", "the release owner resolves owned holds; incident freezes stay; sm queue why"
    if buckets.get("failing"):
        return f"{len(buckets['failing'])} PRs fail CI for real", "triage the first failing job per PR: sm ci triage prs, then sm ci faillog PR"
    if buckets.get("ready"):
        return f"{len(buckets['ready'])} PRs are check-ready; merge readiness unverified", "sm queue why for holds, mergeability and review"
    if load is not None and load > LOAD_HIGH:
        return f"Mini load {load:.0f} slows the gate", "fewer parallel builders until the load drops (sm gate status shows who holds the gate)"
    if buckets.get("pending"):
        return f"{len(buckets['pending'])} PRs pending hosted CI", "runner capacity; predeclared studies can use idle slots"
    if buckets.get("cancelled"):
        return f"{len(buckets['cancelled'])} PRs with cancelled CI", "sm ci triage prs for current check evidence"
    return "No non-draft PRs in the check snapshot", "check the draft queue if needed"


def wait(w: probes.World, state: dict, go: Any) -> GoView:
    now = w.now()
    unknown = [] if known(state.get("prs")) else [f"pull requests: {state.get('prs', {}).get('unknown')}"]
    buckets = buckets_of(open_prs(state))
    since = legacy.gate_hold_since(w.state_dir)
    hold_minutes = round((now - since) / 60) if since is not None else None
    holds = []
    if legacy.incident_frozen(w.state_dir):
        holds.append("incident merge freeze")
    holds += [f"release hold {h}" for h in legacy.release_holds(w.state_dir)]
    runs = state.get("runs") if known(state.get("runs")) and isinstance(state.get("runs"), list) else []
    acceptance = next((f"run {r.get('databaseId')}" for r in runs if r.get("workflowName") == "Acceptance" and r.get("status") != "completed"), "")
    load = load_average()
    big, unblock = biggest_wait(buckets, hold_minutes, ", ".join(holds), acceptance, load)
    lines = [f"BIGGEST WAIT  {big}", f"UNBLOCKS      {unblock}"]
    if go.waiting_on:
        lines.append(f"go waits on   {go.waiting_on}")
    lines.append("PRs           " + ("; ".join(f"{k} {len(v)}: {' '.join(f'#{n}' for n in v[:8])}" for k, v in buckets.items()) or "none open"))
    lines.append(f"gate hold     {f'{hold_minutes} min' if hold_minutes is not None else 'none'}; merge holds: {', '.join(holds) or 'none'}")
    lines.append(f"acceptance    {acceptance or 'not running'}; load {f'{load:.1f}' if load is not None else 'unknown'}")
    lines += [f"queue         {line}" for line in probes.queue_waits(legacy.queue_state(w.state_dir)["items"], now)]
    return GoView(name="wait", lines=lines, buckets=buckets, biggest_wait=big, unblock=unblock, unknown=unknown)


# ---------------------------------------------------------------- tick
def tick(w: probes.World, state: dict, go: Any) -> GoView:
    """The anomaly digest, read-only: what the coordinator must look at this tick, or `quiet`."""
    now = w.now()
    found: list[str] = []
    train = state.get("train") if known(state.get("train")) and isinstance(state.get("train"), dict) else {}
    found += list(train.get("trains") or []) + list(train.get("tails") or [])
    found += [f"ALERT {a.text}  (silence: sm alarm ack {a.key})" for a in go.alarms]
    done = newest_finished(state)
    if done and done.get("conclusion") not in ("success", None, ""):
        found.append(f"MAIN RED {str(done.get('headSha'))[:8]} run {done.get('databaseId')} {done.get('conclusion')}: sm ci main, then sm ci why {done.get('databaseId')}")
    holds = state.get("holds") if known(state.get("holds")) and isinstance(state.get("holds"), dict) else {}
    found += [f"HOLD {name}" for name in holds.get("files") or []] + [f"HELD #{key}: {why}"[:160] for key, why in (holds.get("prs") or {}).items()]
    queue = state.get("queue") if known(state.get("queue")) and isinstance(state.get("queue"), dict) else {}
    queued = {str(k) for k in queue.get("queued") or []}
    if queued:
        found.append(f"QUEUE {len(queued)} queued: {queue.get('verdict')}")
    cancelled = []
    for row in open_prs(state):
        verdict, kind = classify_pr(row), bucket(row)
        if kind == "failing":
            found.append(f"#{verdict['number']} FAIL {', '.join(verdict['failing'])} ({verdict['title'][:50]}): sm ci why {verdict['number']}")
        elif kind == "cancelled":
            cancelled.append(f"#{verdict['number']}")
        elif kind == "ready" and str(verdict["number"]) not in queued:
            found.append(f"#{verdict['number']} green, not queued ({verdict['merge'] or 'merge state unknown'}): sm head merge-ready {verdict['number']} or sm queue add {verdict['number']}")
    if cancelled:
        found.append(f"DEFERRED CI {len(cancelled)}: {' '.join(cancelled)} (cancelled checks; sm ci triage prs)")
    runs = state.get("runs") if known(state.get("runs")) and isinstance(state.get("runs"), list) else []
    active = [r for r in runs if r.get("event") == "pull_request" and r.get("status") in ("queued", "in_progress", "waiting", "pending", "requested")]
    if len(active) >= RUNNER_CAP:
        found.append(f"PR CI runs in flight {len(active)}/{RUNNER_CAP}: hold new pushes")
    starved = [age for r in runs if r.get("headBranch") == "main" and r.get("status") == "queued" and (age := age_minutes(str(r.get("createdAt") or ""), now)) is not None]
    if starved and max(starved) >= STARVED_MINUTES:
        found.append(f"MAIN CI STARVED: queued {max(starved)} min (this view never cancels PR runs; deferring PR CI is a write command)")
    load = load_average()
    if load is not None and load > LOAD_HIGH:
        found.append(f"LOAD {load:.0f} on this machine")
    found += [f"UNKNOWN {u}" for u in go.unknown]
    lines = ["RULES: run `sm` first: it prints the phase, the ONE next command and its trap", *(found or ["quiet"])]
    return GoView(name="tick", lines=lines)

