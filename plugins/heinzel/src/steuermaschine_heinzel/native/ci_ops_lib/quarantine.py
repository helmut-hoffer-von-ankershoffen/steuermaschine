"""The core quarantine lane from the coordinator's side (core: tests/quarantine.toml, scripts/quarantine.py).

`sm ci quarantine list` prints every entry with its age, days left and the last lane results; `--line` prints one
line for the release status; `--digest` prints the anomaly lines the tick digest shows. The core validates the
registry; this module only reads it (from main, through `gh api`) and the `quarantine-report-<os>` artifacts of
main's CI runs (push and nightly schedule), remembered per run in the store (kind `heinzel.ci_ops.quarantine_reports`).

Digest lines:
  QUARANTINE n/8: bead test age, days left          every entry (a known flake is out of the required jobs)
  QUARANTINE-EXPIRING(bead) ... until D             at most 1 day left: fix it or it fails CI OK
  QUARANTINE-OVERDUE(bead) ... past until D         the required `Quarantine registry` job fails CI OK now
  QUARANTINE-STREAK(bead) ... failed N days in a row  3+ days: the bead is flagged with a note (once a day)
Pure functions are tested in tests/test_native_ci_ops_quarantine.py.

Moved from the former quarantine module. What changed is how it reaches the world (`Edge`, `bind`): GitHub through the
one `Gh` client, the artifact cache and the once-a-day marks through the `Store`, the bead note through the tracker
provider or the `bd` CLI, the date through the `Clock`.
"""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import tomllib
from pathlib import Path

from steuermaschine.github import Gh

REPORTS = "heinzel.ci_ops.quarantine_reports"  # per run id: the quarantine reports of that run's artifacts
FLAGGED = "heinzel.ci_ops.quarantine_flagged"  # per day: the bead notes written that day


class Edge:
    """How the functions below reach the world. `bind` sets it for one command; a test sets what it needs."""

    def __init__(self, commands=None, clock=None, store=None, tracker=None, repo: str = "", beads_dir: Path | None = None, bead_prefix: str = ""):
        self.commands, self.clock, self.store, self.tracker = commands, clock, store, tracker
        self.repo, self.beads_dir, self.bead_prefix = repo, beads_dir, bead_prefix


EDGE = Edge()
R = ""  # OWNER/NAME of the core repository (setting `repo`)


def bind(edge: Edge) -> None:
    global EDGE, R
    EDGE, R = edge, edge.repo


def _today() -> dt.date:
    return dt.datetime.fromtimestamp(EDGE.clock.now(), dt.UTC).date()


MAX_ENTRIES = 8
STREAK_DAYS = 3
FAILING = ("fail", "flaky")


def _sh(args, timeout=60):
    """One read through the one `gh` client: (exit code, stdout)."""
    done = Gh(EDGE.commands, repo=R, timeout=timeout, clock=EDGE.clock).run(list(args[1:]))
    return done.rc, done.out


def parse(text):
    """Registry text -> list of entry dicts with dates; (None, error) on unreadable TOML."""
    try:
        rows = tomllib.loads(text).get("quarantine", [])
    except tomllib.TOMLDecodeError as error:
        return None, str(error)
    entries = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("added"), dt.date) and isinstance(row.get("until"), dt.date):
            entries.append({k: row.get(k) for k in ("test", "bead", "added", "until", "reason")})
    return entries, None


def _short(test):
    path, _, name = str(test).partition("::")
    return f"{path.rsplit('/', 1)[-1]}::{name}"[:90]


def registry_lines(entries, today):
    lines = []
    if entries:
        parts = [f"{e['bead']} {_short(e['test'])} {(today - e['added']).days}d old, {(e['until'] - today).days}d left"
                 for e in entries if today <= e["until"]]
        if parts:
            lines.append(f"QUARANTINE {len(entries)}/{MAX_ENTRIES}: " + "; ".join(parts))
    for e in entries:
        left = (e["until"] - today).days
        if left < 0:
            lines.append(f"QUARANTINE-OVERDUE({e['bead']}) {_short(e['test'])} past until {e['until']}: the required "
                         "`Quarantine registry` job fails CI OK; fix the test or remove the entry")
        elif left <= 1:
            lines.append(f"QUARANTINE-EXPIRING({e['bead']}) {_short(e['test'])} until {e['until']}: fix it now or it "
                         "fails CI OK")
    return lines


def day_results(reports):
    """reports: [(date, report dict)] -> {test: {date: set of statuses}} over every OS and run."""
    out = {}
    for day, report in reports:
        for row in report.get("entries", []) if isinstance(report, dict) else []:
            out.setdefault(row.get("test"), {}).setdefault(day, set()).add(row.get("status"))
    return out


def streak(days, today):
    """Consecutive calendar days, ending today (or yesterday when today has no result yet), with a failing result."""
    day = today if today in days else today - dt.timedelta(days=1)
    count = 0
    while day in days and days[day] & set(FAILING):
        count += 1
        day -= dt.timedelta(days=1)
    return count


def streak_lines(entries, reports, today):
    results = day_results(reports)
    lines, flags = [], []
    for e in entries:
        n = streak(results.get(e["test"], {}), today)
        if n >= STREAK_DAYS and today <= e["until"]:
            lines.append(f"QUARANTINE-STREAK({e['bead']}) {_short(e['test'])} failed {n} days in a row in the lane: "
                         f"P0, fix the cause before {e['until']} (bead flagged)")
            flags.append((e["bead"], e["test"], n))
    return lines, flags


def last_results(entries, reports):
    """{test: 'pass ubuntu, flaky macos (2026-11-08)'} from the newest report per OS."""
    newest = {}
    for day, report in sorted(reports, key=lambda r: r[0]):
        for row in report.get("entries", []):
            newest[(row.get("test"), report.get("os", "?"))] = (day, row.get("status"))
    out = {}
    for e in entries:
        found = [(os_, day, st) for (test, os_), (day, st) in newest.items() if test == e["test"]]
        if found:
            out[e["test"]] = ", ".join(f"{st} {os_}" for os_, _, st in sorted(found)) + f" ({max(d for _, d, _ in found)})"
    return out


def registry_text():
    """The registry on main: its text, "" when main has none (HTTP 404), None when it cannot be read."""
    done = Gh(EDGE.commands, repo=R, timeout=60, clock=EDGE.clock).run(["api", f"repos/{R}/contents/tests/quarantine.toml?ref=main", "-H",
                                                                        "Accept: application/vnd.github.raw"])
    if done.rc == 0:
        return done.out
    return "" if "HTTP 404" in f"{done.out}\n{done.err}" else None


def fetch_reports(since, limit=30):
    """[(date, report)] for main's completed CI runs since `since`, artifacts cached per run id."""
    code, out = _sh(["gh", "run", "list", "-R", R, "--workflow", "CI", "--branch", "main", "--status", "completed",
                     "--limit", str(limit), "--json", "databaseId,createdAt"])
    try:
        runs = json.loads(out) if code == 0 else []
    except ValueError:
        runs = []
    found = []
    for run in runs:
        day = dt.date.fromisoformat(run["createdAt"][:10])
        if day < since:
            continue
        kept = EDGE.store.load(REPORTS, str(run["databaseId"]))
        if kept is None:
            kept = {"reports": _download(run["databaseId"])}
            EDGE.store.save(REPORTS, str(run["databaseId"]), kept)
        found += [(day, report) for report in kept.get("reports", [])]
    return found


def _download(run_id):
    """The quarantine reports among one run's artifacts, read from a temporary directory that is gone afterwards."""
    reports = []
    with tempfile.TemporaryDirectory(prefix="quarantine-reports-") as folder:
        _sh(["gh", "run", "download", str(run_id), "-R", R, "--pattern", "quarantine-report-*",
             "-D", str(folder)], timeout=90)
        for path in sorted(Path(folder).glob("*/quarantine.json")):
            try:
                reports.append(json.loads(path.read_text()))
            except (OSError, ValueError):
                continue
    return reports


def _note(args):
    """Append one note to a bead: through the tracker provider when the project has one, else with the `bd` CLI."""
    if EDGE.tracker is not None:
        try:
            EDGE.tracker.comment(args[2], args[4])
        except Exception as exc:  # noqa: BLE001 - any failure of the tracker is a note not written
            return 1, str(exc)
        return 0, ""
    done = EDGE.commands.run(list(args), cwd=str(EDGE.beads_dir) if EDGE.beads_dir else None, timeout=60)
    return done.rc, done.out if done.rc == 0 else (done.err or done.out)


def full_bead_id(bead, prefix):
    """The full bead id: the prefix is prepended only when the id does not carry it already."""
    return bead if not prefix or bead.startswith(prefix) else prefix + bead


def flag_beads(flags, today, run=None, dry=False, failed=None):
    """One note per bead and test per day; returns the flags noted now (with `dry`: the ones that would be).
    A note the tracker refuses is not marked for the day and not returned; its key goes to `failed` when given."""
    run = run or _note
    done = EDGE.store.load(FLAGGED, str(today)) or {}
    noted = []
    for bead, test, days in flags:
        key = f"{bead} {test} {today}"
        if key in done:
            continue
        if not dry:
            stamp = dt.datetime.fromtimestamp(EDGE.clock.now()).strftime("%H:%M")
            rc, out = run(["bd", "update", full_bead_id(bead, EDGE.bead_prefix), "--append-notes", f"{stamp} quarantine lane: {test} failed {days} days in a "
                           "row (all attempts or flaky); P0: fix the root cause before the entry's until"])
            if rc != 0:
                if failed is not None:
                    failed.append(f"{key} {' '.join(str(out).split())[:120]}".rstrip())
                continue
            done[key] = True
        noted.append(key)
    if noted and not dry:
        EDGE.store.save(FLAGGED, str(today), done)
    return noted


def gather(today=None):
    today = today or _today()
    text = registry_text()
    if text is None:
        return today, [], [], "QUARANTINE-UNREADABLE tests/quarantine.toml on main could not be read from GitHub"
    entries, error = parse(text) if text else ([], None)
    if entries is None:
        return today, [], [], f"QUARANTINE-INVALID tests/quarantine.toml unreadable: {error[:80]}"
    since = min((e["added"] for e in entries), default=today) - dt.timedelta(days=1)
    reports = fetch_reports(max(since, today - dt.timedelta(days=STREAK_DAYS + 1))) if entries else []
    return today, entries, reports, None


def release_line(today: dt.date | None = None) -> str:
    """For the release status: count and ages from main's registry only (no artifact downloads). The candidate runs the
    lane too, but it never blocks: a quarantined failure needs no rerun and no waiver."""
    today = today or _today()
    text = registry_text()
    if text is None:
        return "QUARANTINE-UNREADABLE tests/quarantine.toml on main could not be read from GitHub"
    entries, error = parse(text) if text else ([], None)
    if entries is None:
        return f"QUARANTINE-INVALID tests/quarantine.toml unreadable: {error[:80]}"
    lines = registry_lines(entries, today)
    return " | ".join(lines) + " (non-blocking lane; no rerun or waiver for these)" if lines else ""


def digest_lines(today: dt.date | None = None) -> list[str]:
    """The digest lines only; never writes a note."""
    return digest(today, flag=False)[0]


def digest(today: dt.date | None = None, flag: bool = False, dry: bool = False, failed: list | None = None) -> tuple[list[str], list[str]]:
    """(digest lines, the bead notes written now or, with `dry`, the ones that would be). Notes only with `flag`;
    a note that could not be written goes to `failed`."""
    today, entries, reports, invalid = gather(today)
    if invalid:
        if failed is not None:
            failed.append(invalid)
        return [invalid], []
    lines = registry_lines(entries, today)
    streaks, flags = streak_lines(entries, reports, today)
    noted = flag_beads(flags, today, dry=dry, failed=failed) if flag and flags else []
    return lines + streaks, noted


def status() -> tuple[int, list[str], list[dict], dict, dt.date]:
    """The status view: (exit code, lines, entries, last lane result per test, today)."""
    today, entries, reports, invalid = gather()
    if invalid:
        return 1, [invalid], [], {}, today
    if not entries:
        return 0, [f"quarantine: 0/{MAX_ENTRIES} entries on main"], [], {}, today
    last = last_results(entries, reports)
    lines = []
    for e in entries:
        lines += (f"{e['bead']}  {e['test']}\n    added {e['added']} ({(today - e['added']).days} d), until {e['until']} "
                  f"({(e['until'] - today).days} d left); last lane: {last.get(e['test'], 'no report yet')}\n    {e['reason']}").split("\n")
    for line in streak_lines(entries, reports, today)[0]:
        lines.append(line)
    return 0, lines, entries, last, today
