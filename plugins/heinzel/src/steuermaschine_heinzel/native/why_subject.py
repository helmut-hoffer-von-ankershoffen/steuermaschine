"""`sm why [SUBJECT]`: one place to ask why something does not move. The subject says what is read:

    sm why                nothing named: the merge queue and the release in flight (was `queue why`, `release status`)
    sm why 890            a pull request: why it does not merge; a red check adds the CI cause, a red merge gate Sonar
    sm why 44934734258    a run id (over 100000): why the run failed (was `ci why RUN`)
    sm why 0.9.54         a version: the release in flight for that version (was `release status`)
    sm why main           main's CI evidence (was `ci main`)
    sm why fix/topic      a branch: its pull request, as above
    sm why lb7p           a bead: the bead in full

Read-only. The functions of the old commands do the reading; this module only picks one by the subject.
"""

from __future__ import annotations

import re

import typer

from steuermaschine.engine import Refuse
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime

from . import ci_evidence, ci_read, queue, rejudge, release_read
from .ci_evidence import CiMain
from .ci_read import CiWhy
from .go import world
from .queue import QueueWhy
from .release_read import ReleaseStatus
from .tasks_lib import beads
from .tasks_lib.beads import TaskShown
from .why import PullRequestWhy, explain

RUN_IDS = 100000  # a number at or above this is a run id, below it a pull request
VERSION = re.compile(r"v?(\d+\.\d+\.\d+)")
MERGE_GATE = "merge-gate"


class WhyNow(Model):
    """Nothing named: why the merge queue does not merge, and where the release in flight stands."""

    queue: QueueWhy | None = None
    release: ReleaseStatus | None = None
    unknown: list[str] = []

    def render(self) -> list[str]:
        out = ["QUEUE"] + [f"  {line}" for line in (self.queue.render() if self.queue else [])]
        out += ["RELEASE"] + [f"  {line}" for line in (self.release.render() if self.release else ["no release in flight"])]
        return out + [f"UNKNOWN  {u}" for u in self.unknown]


class WhyRelease(Model):
    """A version: the release in flight, or that this version is not in flight."""

    version: str
    status: ReleaseStatus

    def render(self) -> list[str]:
        if self.status.active and self.status.version and self.status.version != self.version:
            return [f"v{self.version} is not in flight; the release in flight is v{self.status.version}:", *self.status.render()]
        return self.status.render() or [f"v{self.version}: no release in flight"]


def kind(subject: str | None) -> str:
    """none, pr, run, version, main, branch or bead."""
    if not subject:
        return "none"
    if subject.isdigit():
        return "run" if int(subject) >= RUN_IDS else "pr"
    if VERSION.fullmatch(subject):
        return "version"
    if subject == "main":
        return "main"
    return "branch" if "/" in subject else "bead"


def pull_request(number: int) -> PullRequestWhy:
    """The PR explained; a red required check adds its CI cause, a red merge gate adds Sonar's verdict."""
    w = world()
    found = explain(w, number)
    red = {c.name for c in found.checks if c.latest == "FAILURE"}
    more: list[str] = []
    if red:
        try:
            more += ["CI"] + [f"  {line}" for line in ci_read.explain_ci(w, number).render()]
        except Exception as error:
            more.append(f"CI       unknown: {type(error).__name__}: {error}")
    if MERGE_GATE in red:
        try:
            more += ["SONAR"] + [f"  {line}" for line in rejudge.sonar_head(number).render()]
        except Exception as error:
            more.append(f"SONAR    unknown: {type(error).__name__}: {error}")
    found.more = more
    return found


def branch(name: str) -> PullRequestWhy:
    w = world()
    rows, _ = w.gh.try_json(["pr", "list", "-R", w.repo, "--head", name, "--state", "all", "--json", "number", "--limit", "1"], timeout=30)
    if not rows:
        raise Refuse(f"no pull request has the head branch {name}", "sm why PR")
    return pull_request(int(rows[0]["number"]))


def now() -> WhyNow:
    out = WhyNow()
    for field, read in (("queue", queue.queue_why), ("release", lambda: release_read.release_status(line=False))):
        try:
            setattr(out, field, read())
        except Refuse as no:
            out.unknown.append(f"{field}: {no.message}")
    return out


@native("why", writes=False, replaces=("hz-why",), help=H(
    "Why does this not move? A pull request, a version, a run, a branch or a bead; nothing named: the queue and the release.",
    "Use when something is unclear or stuck: a green PR unmerged after 15 minutes, a red run, a release that stopped.",
    "nothing (read-only; GitHub reads, the queue state and the hold files).",
    "sm why 795        (sm why 0.9.54, sm why 44934734258, sm why main, sm why fix/topic, sm why lb7p, sm why)",
    "the command on the WHY or NEXT line it prints",
    "SUBJECT: a number under 100000 is a pull request of the core repository; a larger number is a run id; X.Y.Z a version;\n"
    "`main` the main branch; a word with a slash a branch (its pull request); any other word a bead. Without SUBJECT: why the\n"
    "merge queue does not merge, and the release in flight. A pull request with a red required check also shows the CI cause;\n"
    "a red merge gate also shows Sonar's verdict."))
def why(subject: str | None = typer.Argument(None, metavar="[SUBJECT]", help="A PR number, a version, a run id, `main`, a branch or a bead; none: the queue and the release.")
        ) -> PullRequestWhy | CiWhy | WhyNow | WhyRelease | CiMain | TaskShown:
    what = kind(subject)
    if what == "pr":
        return pull_request(int(str(subject)))
    if what == "run":
        return ci_read.explain_ci(world(), int(str(subject)))
    if what == "version":
        found = VERSION.fullmatch(str(subject))
        return WhyRelease(version=found.group(1) if found else str(subject), status=release_read.release_status(line=False))
    if what == "main":
        return ci_evidence.ci_main()
    if what == "branch":
        return branch(str(subject))
    if what == "bead":
        try:
            return beads.Tracker(runtime()).show(str(subject))
        except Exception as error:
            try:
                return branch(str(subject))
            except Refuse:
                raise Refuse(f"'{subject}' is no PR number, version, run id, branch or bead ({type(error).__name__})", "sm why --help", code=2) from None
    return now()
