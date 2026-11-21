"""The merge gate of a Core pull request: `sm head sonar PR` (read) and `sm ci merge-gate rejudge PR` (write).

Heinzelmännchen knowledge kept here: the checks `merge-gate` and `coverage-floor` are posted through the checks API by
the workflow "Merge gate", which runs (workflow_run) after the head's CI has completed. Each check's details URL names
that Merge gate run. Nothing judges the gate again by itself: after a reviewed Sonar closure the Merge gate run of the
head is re-run, never another suite (a re-run of the Claude review suite only copies the old verdicts).

`trace(...)` and `GATE_CHECKS` are for other areas (`sm ci why`, `sm ci sonar-close --apply`), and so is `rejudge(...)`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol, cast

import typer

from steuermaschine.engine import Refuse, Wait
from steuermaschine.github import Gh
from steuermaschine.github.sonar import Sonar, urllib_fetch
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import Group, native, runtime
from steuermaschine.protocols import Providers

from .queue_lib import core_facts

CI_WORKFLOW = "CI"
GATE_WORKFLOW = "Merge gate"
GATE_CHECKS = core_facts.API_CHECKS  # the merge-gate workflow's checks (Core's release policy)
RUN_URL = re.compile(r"/actions/runs/(\d+)")
RATINGS = {"new_reliability_rating": "reliability", "new_security_rating": "security", "new_maintainability_rating": "maintainability"}
LETTERS = {"1": "A", "2": "B", "3": "C", "4": "D", "5": "E"}
POLL_SECONDS = 30


class Reader(Protocol):
    """What the reads need: GitHub for the core repository, and the clock (the world of `sm ci why` is one too)."""

    @property
    def gh(self) -> Gh: ...

    @property
    def clock(self) -> Any: ...


@dataclass
class Edge:
    gh: Gh
    clock: Any


def edge() -> Edge:
    rt = runtime()
    repo = str(rt.settings.get("repo") or "")
    if not repo:
        raise Refuse("the setting `repo` (OWNER/NAME of the core repository) is empty", "sm self config")
    providers = cast(Providers, rt.providers)
    return Edge(gh=Gh(providers.commands, repo=repo, timeout=12.0, clock=providers.clock), clock=providers.clock)


# ------------------------------------------------------------------ reads
def run_of(w: Reader, run_id: str) -> dict[str, Any] | None:
    data, why = w.gh.api(f"actions/runs/{run_id}")
    return data if not why and isinstance(data, dict) else None


def is_gate_run(run: dict[str, Any]) -> bool:
    judge = core_facts.judge_file()
    return run.get("name") == GATE_WORKFLOW or bool(judge) and str(run.get("path") or "").endswith(judge)


def trace(w: Reader, urls: list[str], head: str = "") -> tuple[dict[str, Any] | None, str]:
    """(the Merge gate run that posted the gate checks, how it was found). First the run the checks' details URLs name,
    then the newest Merge gate run of the head. (None, why) when neither names one."""
    for run_id in dict.fromkeys(m.group(1) for url in urls if (m := RUN_URL.search(url or ""))):
        run = run_of(w, run_id)
        if run is not None and is_gate_run(run):
            return run, "the check's details URL"
    if not head:
        return None, "the check's details URL names no Merge gate run and the head is unknown"
    if not core_facts.judge_file():
        return None, "this Core's release policy has no merge-gate workflow"
    data, why = w.gh.api(f"actions/workflows/{core_facts.judge_file()}/runs", {"head_sha": head, "per_page": 20})
    if why:
        return None, f"the Merge gate runs of the head cannot be read: {why}"
    runs = [r for r in (data or {}).get("workflow_runs") or [] if r.get("head_sha") == head]
    if not runs:
        return None, f"no Merge gate run for head {head[:8]}"
    return max(runs, key=lambda r: int(r.get("id") or 0)), "the newest Merge gate run of the head"


def gate_checks(w: Reader, head: str) -> tuple[list[dict[str, Any]], str]:
    """The latest check runs of the merge-gate workflow on the head."""
    data, why = w.gh.api(f"commits/{head}/check-runs", {"filter": "latest", "per_page": 100})
    if why:
        return [], why
    return [c for c in (data or {}).get("check_runs") or [] if c.get("name") in GATE_CHECKS], ""


def ci_run(w: Reader, head: str) -> tuple[dict[str, Any] | None, str]:
    data, why = w.gh.api("actions/runs", {"head_sha": head, "per_page": 50})
    if why:
        return None, why
    runs = [r for r in (data or {}).get("workflow_runs") or [] if r.get("name") == CI_WORKFLOW and r.get("head_sha") == head]
    return (max(runs, key=lambda r: int(r.get("id") or 0)) if runs else None), ""


GREEN = ("success", "neutral", "skipped")


def is_red(checks: list[dict[str, Any]], gate: dict[str, Any]) -> bool:
    """A gate check ended not green; without any gate check, the gate run itself failed (it posted none)."""
    if checks:
        return any(str(c.get("conclusion") or "") not in ("", *GREEN) for c in checks)
    return str(gate.get("conclusion") or "") not in ("", *GREEN)


def gate_state(checks: list[dict[str, Any]], gate: dict[str, Any]) -> str:
    if checks:
        return ", ".join(f"{c.get('name')} {c.get('status') or '?'} {c.get('conclusion') or ''}".rstrip() for c in checks)
    return f"no gate check; Merge gate run {gate.get('id')} {gate.get('conclusion') or 'no conclusion'}"


class SonarHead(Model):
    """Sonar's verdict on one pull request: the quality gate, the new-code ratings, open bugs and vulnerabilities."""

    pr: int
    project: str
    gate: str = ""  # OK | ERROR | WARN | NONE, or unknown: <why>
    failing: list[str] = []  # failing gate conditions: metric actual (threshold)
    ratings: dict[str, str] = {}  # reliability | security | maintainability -> A..E, or ? when unread
    issues: list[str] = []  # open bugs and vulnerabilities: TYPE SEVERITY key file:line rule message
    open_issues: int | None = None
    unknown: list[str] = []
    read_only: bool = True

    @property
    def clean(self) -> bool:
        return self.gate == "OK" and bool(self.ratings) and all(v == "A" for v in self.ratings.values()) and not self.unknown

    @property
    def next(self) -> str:
        if self.clean:
            return f"sm ci merge-gate rejudge {self.pr}   (only when merge-gate is red at this head)"
        if self.unknown:
            return f"sm head sonar {self.pr}   (again, once Sonar answers)"
        if self.issues:
            return f"sm ci sonar-close --pull-request {self.pr} --issue KEY   (a reviewed false positive), else fix it on a new head"
        return "a new head: smells, duplication and the maintainability rating clear only with new code"

    def render(self) -> list[str]:
        out = [f"SONAR    PR {self.pr}: gate {self.gate or '?'}; " + ", ".join(f"{k} {v}" for k, v in self.ratings.items())]
        out += [f"FAILING  {c}" for c in self.failing]
        out += [f"ISSUE    {i}" for i in self.issues]
        out += [f"UNKNOWN  {u}" for u in self.unknown]
        return [*out, f"NEXT     {self.next}"]


def letter(raw: Any) -> str:
    return LETTERS.get(str(raw).split(".")[0], "?") if raw not in (None, "") else "?"


def sonar_head(pr: int) -> SonarHead:
    """Read Sonar for one pull request (the plugin's sonar_project; the HTTP function a test injects)."""
    rt = runtime()
    fetch = getattr(rt.providers, "extra", {}).get("http_fetch") or urllib_fetch
    project = str(rt.settings.get("sonar_project") or "")
    if not project:
        raise Refuse("the setting `sonar_project` is empty", "sm self config")
    sonar = Sonar(project, fetch=fetch, clock=cast(Providers, rt.providers).clock)
    report = SonarHead(pr=pr, project=project)
    data, why = sonar.get("qualitygates/project_status", {"projectKey": project, "pullRequest": pr})
    if why:
        report.gate, report.unknown = f"unknown: {why}", [f"gate: {why}"]
    else:
        status = data.get("projectStatus") or {}
        report.gate = str(status.get("status") or "")
        report.failing = [f"{c.get('metricKey')} {c.get('actualValue')} ({c.get('comparator', '')} {c.get('errorThreshold')})"
                          for c in status.get("conditions") or [] if c.get("status") == "ERROR"]
    data, why = sonar.get("measures/component", {"component": project, "pullRequest": pr, "metricKeys": ",".join(RATINGS)})
    if why:
        report.unknown.append(f"ratings: {why}")
    else:
        for m in (data.get("component") or {}).get("measures") or []:
            if m.get("metric") in RATINGS:
                raw = (m.get("period") or {}).get("value") or ((m.get("periods") or [{}])[0].get("value")) or m.get("value")
                report.ratings[RATINGS[m["metric"]]] = letter(raw)
        missing = [name for name in RATINGS.values() if name not in report.ratings]
        report.unknown += [f"ratings not reported: {', '.join(missing)}"] if missing else []
    data, why = sonar.get("issues/search", {"componentKeys": project, "pullRequest": pr, "resolved": "false", "types": "BUG,VULNERABILITY", "ps": 20})
    if why:
        report.unknown.append(f"issues: {why}")
    else:
        report.open_issues = data.get("total", (data.get("paging") or {}).get("total"))
        report.issues = [f"{i.get('type')} {i.get('severity')} {i.get('key')} {str(i.get('component', '')).split(':', 1)[-1]}:{i.get('line', '')} "
                         f"{i.get('rule')} {str(i.get('message', ''))[:80]}" for i in data.get("issues") or []]
    return report


# ------------------------------------------------------------------ re-judge
class Verdict(Model):
    name: str
    status: str = ""
    conclusion: str = ""


class Rejudge(Model):
    """What `sm ci merge-gate rejudge` re-ran, and the gate verdicts afterwards."""

    pr: int
    head: str
    ci_run: str
    gate_run: str
    found_by: str
    attempt: int = 1  # the attempt of the gate run before the re-run
    live: bool = True
    state: str  # would_rerun | running | done
    sonar: str = ""
    verdicts: list[Verdict] = []

    @property
    def exit_code(self) -> int:
        # done without a gate check is no verdict: never exit 0 on it
        return 1 if self.state == "done" and (not self.verdicts or any(v.conclusion != "success" for v in self.verdicts)) else 0

    def render(self) -> list[str]:
        word = {"would_rerun": "would re-run (dry run, nothing changed)", "running": "re-run started, still running", "done": "re-run finished"}[self.state]
        out = [f"GATE     PR {self.pr} head {self.head[:8]}: Merge gate run {self.gate_run} (from {self.found_by}, after CI run {self.ci_run}): {word}"]
        out += [f"SONAR    {self.sonar}"] if self.sonar else []
        out += [f"VERDICT  {v.name}: {v.status} {v.conclusion}".rstrip() for v in self.verdicts]
        if self.state == "done" and not self.verdicts:
            out.append(f"VERDICT  none: no {' or '.join(GATE_CHECKS)} check found on head {self.head[:8]} after the re-run")
        if self.state == "running":
            out.append(f"NEXT     sm ci why {self.pr}   (the gate run is still judging)")
        elif self.state == "done":
            out.append("NEXT     " + (f"sm queue why {self.pr}" if not self.exit_code else f"sm ci why {self.pr}"))
        else:
            out.append(f"NEXT     sm ci merge-gate rejudge {self.pr}")
        return out


def rejudge(pr: int, wait_minutes: int = 15, dry_run: bool = False) -> Rejudge:
    """Re-run the Merge gate run of the PR's current head after its CI completed and Sonar is OK with all ratings A."""
    w = edge()
    view, why = w.gh.api(f"pulls/{pr}")
    if why or not isinstance(view, dict):
        raise Refuse(f"PR {pr} cannot be read: {why or 'no answer'}", f"sm ci why {pr}")
    if view.get("state") != "open":
        raise Refuse(f"PR {pr} is {view.get('state') or 'unknown'}, not open", f"sm why {pr}")
    head = str((view.get("head") or {}).get("sha") or "")
    run, why = ci_run(w, head)
    if why:
        raise Refuse(f"the CI runs of head {head[:8]} cannot be read: {why}", f"sm ci why {pr}")
    if run is None:
        raise Refuse(f"no CI run for head {head[:8]}: the Merge gate starts after CI", f"sm ci why {pr}")
    if run.get("status") != "completed":
        raise Wait(f"CI run {run.get('id')} of head {head[:8]} is {run.get('status')}: the Merge gate judges after it", f"CI run {run.get('id')}")
    if run.get("conclusion") == "cancelled":
        raise Refuse(f"CI run {run.get('id')} of head {head[:8]} was cancelled: the Merge gate does not judge a cancelled run", "sm ci rerun --dry-run")
    checks, why = gate_checks(w, head)
    gate, found_by = trace(w, [str(c.get("details_url") or "") for c in checks], head)
    if gate is None:
        raise Refuse(f"no Merge gate run to re-run: {found_by}{f' (check runs: {why})' if why else ''}", f"sm ci why {pr}")
    if gate.get("head_sha") != head:
        raise Refuse(f"Merge gate run {gate.get('id')} (from {found_by}) judged head {str(gate.get('head_sha') or '?')[:8]}, "
                     f"not the PR head {head[:8]}: a re-run would judge another head", f"sm ci why {pr}")
    if gate.get("status") != "completed":
        raise Wait(f"Merge gate run {gate.get('id')} is {gate.get('status')}: it is judging now", f"Merge gate run {gate.get('id')}")
    if not is_red(checks, gate):
        raise Refuse(f"the merge gate of head {head[:8]} is not red ({gate_state(checks, gate)}): nothing to re-judge", f"sm queue why {pr}")
    sonar = sonar_head(pr)
    if not sonar.clean:
        raise Refuse(f"Sonar is not clean for PR {pr} ({sonar.render()[0].removeprefix('SONAR    ')}): a re-judge would fail again", f"sm head sonar {pr}")
    gate_id, attempt = str(gate.get("id")), int(gate.get("run_attempt") or 1)
    result = Rejudge(pr=pr, head=head, ci_run=str(run.get("id")), gate_run=gate_id, found_by=found_by, attempt=attempt, live=not dry_run,
                     state="would_rerun" if dry_run else "running", sonar=sonar.render()[0].removeprefix("SONAR    "))
    if dry_run:
        return result
    _, why = w.gh.api(f"actions/runs/{gate_id}/rerun", method="POST")
    if why:
        raise Refuse(f"GitHub refused the re-run of Merge gate run {gate_id}: {why}", f"sm ci why {pr}")
    deadline = w.clock.now() + max(0, wait_minutes) * 60
    while w.clock.now() < deadline:
        w.clock.sleep(POLL_SECONDS)
        now = run_of(w, gate_id)
        if now and int(now.get("run_attempt") or 1) > attempt and now.get("status") == "completed":
            checks, _ = gate_checks(w, head)
            result.state = "done"
            result.verdicts = [Verdict(name=str(c.get("name")), status=str(c.get("status") or ""), conclusion=str(c.get("conclusion") or "")) for c in checks]
            break
    return result


CI_MERGE_GATE = Group("ci merge-gate", H(
    "ci merge-gate: the Merge gate workflow that posts merge-gate and coverage-floor on a pull request's head.",
    "Use when merge-gate or coverage-floor is red and the cause was cleared without a new head (a reviewed Sonar closure).",
    "nothing by itself; see each command.",
    "sm ci merge-gate rejudge 803",
    "sm ci why 803"))


@native("ci merge-gate rejudge", writes=True, help=H(
    "Judge the merge gate of a pull request's current head again: re-run its Merge gate run and print the new verdicts.",
    "Use after a reviewed Sonar closure (sm ci sonar-close --apply runs it), when merge-gate stays red at an unchanged head.",
    "starts a new attempt of the head's Merge gate run on GitHub; --dry-run changes nothing.",
    "sm ci merge-gate rejudge 803 --dry-run",
    "sm queue why 803   (a needs-work queue entry at a pinned head is requeued by the queue once its checks are green)",
    "The run is found through the details URL of the merge-gate and coverage-floor check runs, else as the newest Merge\n"
    "gate run of the head. Exit 75 while the head's CI or the gate run is still running. Refuses (exit 1) when the PR is not\n"
    "open, CI of the head was cancelled or never ran, no gate run exists, the gate run judged another head, the gate is\n"
    "not red, or Sonar is not OK with all ratings A\n"
    "(sm head sonar). Never re-run another suite instead: a re-run of the Claude review suite only copies the old\n"
    "verdicts. Exit 1 when the re-run finished and a gate is still not green or no gate check is found."))
def ci_merge_gate_rejudge(pr: int = typer.Argument(..., metavar="PR", help="The pull request number of the Core repository."),
                          wait: int = typer.Option(15, "--wait", metavar="MIN", min=0, help="Wait at most MIN minutes for the new verdicts (0: do not wait)."),
                          dry_run: bool = typer.Option(False, "--dry-run", help="Check and name the run, re-run nothing.")) -> Rejudge:
    return rejudge(pr, wait, dry_run)


@native("head sonar", writes=False, help=H(
    "Sonar's verdict on one pull request: the quality gate, the new-code ratings, open bugs and vulnerabilities.",
    "Use when merge-gate is red, after a Sonar closure, or before you call a pull request clean.",
    "nothing (read-only: Sonar's public web API).",
    "sm head sonar 803",
    "the NEXT line: a re-judge when clean, a reviewed closure for a false positive, else a new head",
    "Ratings are A to E (A is 1). A smell, duplication or the maintainability rating clears only with a new head; a bug or\n"
    "vulnerability that was reviewed as a false positive is closed with sm ci sonar-close."))
def head_sonar(pr: int = typer.Argument(..., metavar="PR", help="The pull request number of the Core repository.")) -> SonarHead:
    return sonar_head(pr)
