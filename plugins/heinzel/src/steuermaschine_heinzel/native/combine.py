"""head combine status | plan: one combine PR per area per release window, the read half. Read-only.

The selection, caps and output lines are the moved code of combine_lib/combine.py; this module gives it its IO (GitHub
reads through the one process runner, the clock provider, collected output lines) and turns its results into models.
Building, publishing, shipping, rebasing and bisecting a combine are writes and are not ported yet: they belong on top
of `sm head ship` and `sm head pr`.
"""

from __future__ import annotations

from typing import Any

import typer
from pydantic import BaseModel, Field

from steuermaschine.engine import Refuse
from steuermaschine.github.client import Gh
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import Group, native, runtime

from .combine_lib import combine as moved

COMBINE = Group("head combine", H(
    "head combine: one combine PR per area per release window, from ready draft member PRs (status and plan).",
    "Use when several small draft PRs of one area wait for CI, to see what one combine would carry.",
    "nothing (read-only: GitHub reads). Building and shipping a combine are not ported yet.",
    "sm head combine status",
    "sm head combine plan --area web"))


class CombineSettings(BaseModel):
    """Settings of `sm head combine` (mixin of the plugin's settings table)."""

    combine_plugins_repo: str | None = Field(None, description="OWNER/NAME of the plugins repository, for `--repo plugins` (one combine per plugin). None: refused.")


class InFlight(Model):
    number: int
    branch: str = ""
    area: str = ""
    after: int | None = None  # the in-flight combine this one is chained after
    members: list[int] = []


class CombineMember(Model):
    number: int
    title: str
    head: str
    sha: str
    base: str
    area: str
    lines: int
    files: int
    reason: str = ""  # why it cannot join a combine of its area; empty: ready


class CombineStatus(Model):
    """Combines in flight against the caps, and the draft members per area."""

    repo: str
    cap: str
    inflight: list[InFlight]
    drafts: list[CombineMember]
    lines: list[str]
    read_only: bool = True

    def render(self) -> list[str]:
        return self.lines


class Refused(Model):
    number: int
    reason: str


class CombinePlan(Model):
    """What one combine of an area would carry now."""

    repo: str
    area: str
    members: list[CombineMember]
    deferred: list[Refused]
    refused: list[Refused]
    files: list[str]
    lines_changed: int
    after: int | None = None
    overlap: list[str] = []
    notes: list[str] = []
    inflight: int = 0
    lines: list[str] = []
    read_only: bool = True

    def render(self) -> list[str]:
        return self.lines


BUILD_HINT = "build the combine (not ported yet: `sm tools gaps`; until then by hand): sm head combine build"


def sm_words(line: str) -> str:
    """The moved code names its old command in its hints; what sm prints names sm."""
    return line.replace("combine build", BUILD_HINT if line.startswith("next:") else "sm head combine build")


class Ctx:
    """The IO of the moved combiner: GitHub reads through the one process runner, the clock provider, lines collected."""

    def __init__(self, repo: str, plugins: bool, rt: Any):
        self.repo, self.plugins, self.lines = repo, plugins, []
        self._gh = Gh(rt.providers.commands, repo=repo, timeout=60.0, clock=rt.providers.clock)
        self.now = rt.providers.clock.now

    def gh(self, *args: str) -> tuple[int, str]:
        res = self._gh.run(list(args))
        return res.rc, (res.out.strip() if res.rc == 0 else (res.out + res.err).strip())

    def say(self, line: str = "") -> None:
        self.lines.append(sm_words(line))


def _context(repo_choice: str) -> Ctx:
    rt = runtime()
    settings = rt.settings
    if repo_choice == "plugins":
        repo = settings.get("combine_plugins_repo")
        if not repo:
            raise Refuse("--repo plugins needs the setting combine_plugins_repo (OWNER/NAME of the plugins repository)", "sm self config", code=2)
        return Ctx(str(repo), True, rt)
    if not settings.get("repo"):
        raise Refuse("the setting repo (OWNER/NAME of the core repository) is not set", "sm self config", code=2)
    return Ctx(str(settings["repo"]), False, rt)


def _guard(call: Any) -> Any:
    """The moved code stops with SystemExit (a failed GitHub read, no ready plugin member); sm refuses with that line."""
    try:
        return call()
    except SystemExit as stop:
        raise Refuse(str(stop.code), "sm head combine status", code=1) from None


def _member(m: moved.Member, area: str) -> CombineMember:
    return CombineMember(number=m.number, title=m.title, head=m.head, sha=m.sha, base=m.base, area=m.area, lines=m.lines, files=len(m.files),
                         reason=moved.refusal(m, area.split(":")[0]))


REPO = typer.Option("core", "--repo", help="core, or plugins (one combine per plugin; needs the setting combine_plugins_repo).")


@native("head combine status", writes=False, replaces=("hz-combine",), help=H(
    "Combines in flight against the caps (4, hard cap 8) and the ready draft members per area.",
    "Use before building a combine, and when draft PRs pile up in one area.",
    "nothing (read-only: one GitHub read of the open PRs).",
    "sm head combine status",
    "sm head combine plan --area AREA for the area marked with ->.",
    "Members are open DRAFT PRs labelled combine:<area>: no hosted CI of their own, focused tests through the gate.\n"
    "An area with 4 ready members, or a ready member older than 60 minutes, is due (-> on its line)."))
def combine_status(repo: str = REPO) -> CombineStatus:
    c = _context(repo)
    combiner = moved.Combiner(c)
    prs = _guard(combiner.open_prs)
    combiner.open_prs = lambda: prs  # type: ignore[method-assign]  # one read serves the model and the moved status lines
    members, inflight = combiner.split(prs)
    _guard(combiner.status)
    drafts = [_member(m, m.area) for m in members if m.draft and not m.head.startswith("release/")]
    return CombineStatus(repo=c.repo, cap=moved.cap_line(len(inflight)), inflight=[_inflight(f) for f in inflight], drafts=drafts, lines=c.lines)


def _inflight(f: dict[str, Any]) -> InFlight:
    return InFlight(number=f["number"], branch=str(f.get("branch") or ""), area=str(f.get("area") or ""), after=f.get("after"),
                    members=[n for n, _ in f.get("members", [])])


@native("head combine plan", writes=False, replaces=("hz-combine",), help=H(
    "What one combine of an area would carry now: members in build order, deferred and refused PRs with the reason.",
    "Use before building a combine, or to see why a draft PR is not picked.",
    "nothing (read-only: one GitHub read of the open PRs).",
    "sm head combine plan --area web            (named PRs, without the label rule: sm head combine plan 801 802)",
    "build the combine (not ported yet: `sm tools gaps`); fix a refused member as its reason says.",
    "Members: not CONFLICTING, no failing draft check, no pinned release file outside area tools, no\n"
    "update/install/auth/recovery file (those ship alone); stacked bases first; members sharing a file stay together\n"
    "(generated files and docs do not count); at most 6 members, 3000 changed lines web / 1500 system, 60 files.\n"
    "A combine that overlaps one in flight is chained after it."))
def combine_plan(prs: list[int] = typer.Argument(None, help="Explicit member PRs (skips the label requirement)."),
                 area: str = typer.Option("web", "--area", help=f"One of {', '.join(moved.AREAS)}."),
                 plugin: str | None = typer.Option(None, "--plugin", help="With --area plugin: the plugin (one combine per plugin)."),
                 without: list[int] = typer.Option([], "--without", help="Leave this PR out; repeatable."),
                 repo: str = REPO) -> CombinePlan:
    if area not in moved.AREAS:
        raise Refuse(f"--area must be one of {', '.join(moved.AREAS)}", "sm head combine plan --help", code=2)
    c = _context("plugins" if area == "plugin" else repo)
    combiner = moved.Combiner(c)
    p, inflight = _guard(lambda: combiner.plan(area, plugin, prs or [], without))
    combiner.print_plan(p, inflight)
    return CombinePlan(repo=c.repo, area=p.area, members=[_member(m, p.area) for m in p.members],
                       deferred=[Refused(number=m.number, reason=why) for m, why in p.deferred],
                       refused=[Refused(number=m.number, reason=sm_words(why)) for m, why in p.refused],
                       files=p.files, lines_changed=p.lines, after=p.after, overlap=p.overlap, notes=p.notes, inflight=len(inflight), lines=c.lines)
