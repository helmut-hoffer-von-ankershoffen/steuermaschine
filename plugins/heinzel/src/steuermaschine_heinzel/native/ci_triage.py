"""ci triage prs | review N | deadline: classify red pull requests and print the one suggested action each. Read-only.

The rules are in ci_lib/triage.py (moved). DEFERRED and KNOWN are not failures: nobody messages a builder about them.
"""

from __future__ import annotations

from pathlib import Path

import typer

from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import Group, native

from .ci_lib import triage
from .ci_read import known_registry
from .go import world
from .go_lib import probes
from .queue_lib import known_failures

TRIAGE = Group("ci triage", H("Classify red pull requests and guard FLAGs: one state and one suggested action each.",
                              "Use when several pull requests are red, and before messaging a builder about a red one.",
                              "nothing (read-only).", "sm ci triage prs", "the command after the arrow on each line"))


class TriageRow(Model):
    number: int
    state: str  # READY | PENDING | DEFERRED | KNOWN | FAIL
    line: str


class TriagePrs(Model):
    """Every open non-draft pull request with its state and the one suggested action."""

    rows: list[TriageRow] = []
    read_only: bool = True

    def render(self) -> list[str]:
        return [r.line for r in self.rows] or ["no open pull request that is not a draft"]


class TriageReview(Model):
    number: int
    state: str  # CLEARED | NEEDS_REVIEW
    line: str
    read_only: bool = True

    def render(self) -> list[str]:
        return [self.line]


class DeadlineRow(Model):
    number: int
    head: str = ""
    state: str  # genuine | known | deferred | cancellation-unverified | pending | ready | not-open
    due: bool = False
    draft: bool = False
    failures: list[str] = []

    def line(self) -> str:
        if self.state == "not-open":
            return f"PR #{self.number} NOT-OPEN due"
        flags = (" due" if self.due else "") + (" draft" if self.draft else "")
        names = " " + ascii(", ".join(self.failures)[:160]) if self.failures else ""
        return f"PR #{self.number} {self.head} {self.state.upper()}{flags}{names}"


class TriageDeadline(Model):
    """Counts of the open non-draft pull requests by state, and details for the due ones and the genuine failures."""

    counts: dict[str, int]
    rows: list[DeadlineRow] = []
    details_omitted: int = 0
    read_only: bool = True

    def render(self) -> list[str]:
        c = self.counts
        head = (f"Core open non-draft PRs: genuine={c.get('genuine', 0)} deferred={c.get('deferred', 0)} known={c.get('known', 0)} "
                f"cancellation-unverified={c.get('cancellation-unverified', 0)} pending={c.get('pending', 0)} ready={c.get('ready', 0)}")
        return [head, *[r.line() for r in self.rows], *([f"Details omitted: {self.details_omitted}"] if self.details_omitted else [])]


def research_dir(w: probes.World) -> Path | None:
    """The directory of the review files, or None when the setting ci_research_dir is not set."""
    path = w.settings.get("ci_research_dir")
    return Path(str(path)).expanduser() if path else None


@native("ci triage prs", writes=False, replaces=("hz-triage",), help=H(
    "Every open pull request: READY, PENDING, DEFERRED, KNOWN(bead) or FAIL, with the one suggested action.",
    "Use when several pull requests are red, before any rerun or builder message.",
    "nothing (read-only: one GitHub read, the registry of known failures).",
    "sm ci triage prs", "the command after the arrow on a line; sm ci why NUMBER for a FAIL",
    "DEFERRED is a mass cancel with a red aggregate: a placeholder, not a code failure. KNOWN names the bead of a registry\n"
    "entry in date. A diagnostic `Focused ...` run never counts."))
def triage_prs() -> TriagePrs:
    w = world()
    known = known_registry(w)
    data, why = w.gh.try_json(["pr", "list", "-R", w.repo, "--state", "open", "--limit", "40", "--json", "number,statusCheckRollup,isDraft"], timeout=60)
    if why or not isinstance(data, list):
        raise Refuse(f"the open pull requests could not be read: {why or 'no list in the answer'}", "sm ci triage prs")
    rows = []
    for p in data:
        if not p.get("isDraft"):
            line = triage.triage_pr(p["number"], p.get("statusCheckRollup"), known)
            rows.append(TriageRow(number=p["number"], state=line.split()[2].split("(")[0], line=line))
    return TriagePrs(rows=rows)


@native("ci triage review", writes=False, replaces=("hz-triage",), help=H(
    "The verdict of the review file of a guard-FLAGGED pull request: CLEARED with the ship command, or NEEDS_REVIEW.",
    "Use after an independent review of a FLAG was written, before shipping the pull request.",
    "nothing (read-only: one file).",
    "sm ci triage review 801", "the command after the arrow"))
def triage_review(number: int = typer.Argument(..., metavar="NUMBER", help="The pull request number.")) -> TriageReview:
    research = research_dir(world())
    if research is None:
        raise Refuse("the setting ci_research_dir is not set: the review files cannot be found", "sm self config")
    line = triage.review_line(number, str(research))
    return TriageReview(number=number, state=line.split()[1], line=line)


@native("ci triage deadline", writes=False, replaces=("hz-triage",), help=H(
    "Deadline view of the Core pull requests: counts by state, details for due ones and genuine failures.",
    "Use before a deadline, to see which due pull request genuinely fails and which is only deferred or known.",
    "nothing (read-only: one GitHub read, the registry of known failures, the defer list).",
    "sm ci triage deadline --due 795 --due 801", "sm ci why NUMBER for a GENUINE row",
    "Fails closed: an incomplete inventory (more than 100 pull requests or checks, an unknown check state, a head that\n"
    "does not match its rollup) or an invalid registry prints nothing and exits 1."))
def triage_deadline(due: list[int] = typer.Option([], "--due", metavar="N", help="A due pull request number; repeat for each."),
                    limit: int = typer.Option(12, "--limit", metavar="N", help="Maximum detail rows, from 1 to 25.")) -> TriageDeadline:
    if not 1 <= limit <= 25 or any(number < 1 for number in due):
        raise Refuse("--limit must be 1 to 25 and --due numbers must be positive", "sm ci triage deadline --due 795")
    w = world()
    path = w.settings.get("ci_known_failures_file")
    try:
        if not path:
            raise OSError("the setting ci_known_failures_file is not set")
        known = known_failures.load_known(str(Path(str(path)).expanduser()), strict=True)
    except (OSError, ValueError, KeyError, TypeError):
        raise Refuse("deadline: known failure registry unavailable or invalid (setting ci_known_failures_file)", "sm ci triage prs") from None
    owner, _, name = w.repo.partition("/")
    try:
        recorded = triage.recorded_deferrals((w.state_dir / "ci-deferred.txt").read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        recorded = set()
    response, why = w.gh.try_json(["api", "graphql", "-f", f"owner={owner}", "-f", f"name={name}", "-f", f"query={triage.DEADLINE_QUERY}"], timeout=60)
    try:
        if why or not isinstance(response, dict):
            raise ValueError("no answer")
        snapshot = triage.deadline_snapshot(response, known, recorded, w.repo)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise Refuse("deadline: incomplete GitHub PR or check inventory (limit: 100 each)", "sm ci triage deadline") from None
    counts, rows, omitted = triage.deadline_rows(snapshot, due, limit)
    return TriageDeadline(counts=dict(counts), rows=[DeadlineRow(**r) for r in rows], details_omitted=omitted)
