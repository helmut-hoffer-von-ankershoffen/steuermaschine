"""The hourly owner update: facts first, then a draft in the agreed shape. The facts are the text of read-only
commands (setting hour_facts), gathered by the core's digest; ETAs and decisions are left for the agent to write."""

from __future__ import annotations

import os
import time
from typing import Any

from steuermaschine import digest
from steuermaschine.github import Gh
from steuermaschine.models import Model

from ..queue_lib import core_facts
from . import snapshot


class HourFact(Model):
    command: str  # the command path whose text this is
    status: str  # ok | missing | skipped | refused | failed
    lines: list[str] = []
    exit_code: int = 0


class Hour(Model):
    """The facts of this hour and the draft of the owner update."""

    at: str
    facts: list[HourFact] = []
    main_ci: str = ""
    releases: list[str] = []
    load: str = ""
    handoff: str = ""  # what the handoff did, or `skipped`
    draft: list[str] = []

    def render(self) -> list[str]:
        out = [f"FACTS {self.at}"]
        for fact in self.facts:
            mark = "" if fact.status == "ok" else f"   [{fact.status}]"
            out.append(f"sm {fact.command}:{mark}")
            out += ["  " + line for line in fact.lines] or ["  -"]
        out += [f"Main CI (newest first):\n  {self.main_ci}", "Releases:", *("  " + line for line in self.releases), f"Load:\n  {self.load}", f"Handoff: {self.handoff}", "", *self.draft]
        return "\n".join(out).splitlines()


def indent(text: str) -> str:
    return "\n".join("  " + line for line in text.splitlines()) or "  -"


def build(rt: Any, commands: list[Any], no_handoff: bool = False, load: Any = None, include_local_commits: bool = False) -> Hour:
    s = rt.settings or {}
    repo = str(s.get("repo") or "")
    gh = Gh(rt.providers.commands, repo, clock=rt.providers.clock)
    facts = [HourFact(command=f.path, status=f.status, lines=f.lines[:40] + ([f"... {len(f.lines) - 40} more: sm {f.path}"] if len(f.lines) > 40 else []), exit_code=f.exit_code)
             for f in digest.collect(commands, list(snapshot.get(s, "hour_facts")))]
    workflow = str(snapshot.get(s, "handoff_ci_workflow") or core_facts.ci_file())
    runs, _ = gh.try_json(["run", "list", "-R", repo, "--workflow", workflow, "--branch", "main", "-L", "5", "--json", "conclusion,status,headSha"])
    main_ci = ", ".join(f"{str(r.get('headSha', ''))[:7]} {r.get('conclusion') or r.get('status')}" for r in runs or [] if isinstance(r, dict)) or "unknown"
    releases = []
    for name in [repo, *snapshot.get(s, "handoff_extra_repos")]:
        rel, _ = gh.try_json(["release", "list", "-R", name, "-L", "3", "--json", "tagName,isLatest"])
        releases.append(f"{name.split('/')[-1]}: " + (", ".join(str(r.get("tagName")) + (" (latest)" if r.get("isLatest") else "") for r in rel or [] if isinstance(r, dict)) or "unknown"))
    one, five, fifteen = load() if load else os.getloadavg()
    done = "skipped" if no_handoff else snapshot.handoff(rt, include_local_commits=include_local_commits).render()[0]
    table = "\n".join(line for name in [repo, *snapshot.get(s, "handoff_extra_repos")] for line in snapshot.World(rt).pr_table(name)).rstrip()
    draft = ["DRAFT UPDATE (send 07:00 to 23:00 Europe/Berlin, or when something went live or a decision waits)", "",
             "1. Live now (only what is verified; write the URL next to each)", f"  Releases: {'; '.join(releases)}", f"  Main CI: {main_ci}", "",
             "2. Open PRs, with action and owner", *table.splitlines(), "",
             "3. ETAs with confidence", "  <write>", "",
             '4. Decisions for you, ranked, with recommendation, or "none"', "  <write>"]
    return Hour(at=time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(rt.providers.clock.now())), facts=facts, main_ci=main_ci, releases=releases,
                load=f"{one:.2f} {five:.2f} {fifteen:.2f}", handoff=done, draft=draft)
