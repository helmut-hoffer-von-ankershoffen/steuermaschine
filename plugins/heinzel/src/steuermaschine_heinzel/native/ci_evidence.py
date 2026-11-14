"""ci merge-gate, ci main: the read side of main's merge gate and of main's CI evidence. Read-only.

Both read through the merge queue's moved modules (queue_lib, frozen: imported, never edited): the audit of
queue_lib/merge_gate.py (never its flip or harden: those change a ruleset), and the queue's own verdict on main
(valid_main_run, superseded_by_pending, attempt_jobs, main_state of queue_lib/queue_daemon.py, loaded the way
`sm queue` loads it). The one function moved here is the note list of the main evidence (ci_evidence_lib/main_evidence.py).
"""

from __future__ import annotations

from typing import Any

from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native

from . import queue as queue_commands
from .ci_evidence_lib import main_evidence
from .queue_lib import core_facts


class GateRepo(Model):
    repo: str
    line: str  # the audit line as the gate prints it
    marks: list[str] = []  # NOT A | RED | BYPASS | NO RULESET: what to act on


class CiMergeGate(Model):
    """Per repository: Sonar's gate and ratings, the coverage badge, which of merge-gate and coverage-floor main requires."""

    repos: list[GateRepo]
    read_only: bool = True

    def render(self) -> list[str]:
        return [r.line for r in self.repos]


def gate_rows(lines: list[str]) -> list[GateRepo]:
    rows = []
    for line in lines:
        repo, _, _ = line.partition(":")
        marks = [m.strip() for m in line.rpartition("<<<")[2].split(",")] if "<<<" in line else []
        rows.append(GateRepo(repo=repo.strip(), line=line, marks=[m for m in marks if m and m != "OWNER DECISION"]))
    return rows


@native("ci merge-gate", writes=False, replaces=("hz-merge-gate",), help=H(
    "Which checks main requires per repository, and whether Sonar and the coverage badge would let them pass.",
    "Use when a PR will not merge for a missing or failing merge-gate or coverage-floor check, and for the hourly view.",
    "nothing (read-only: Sonar and badge GETs, GitHub ruleset reads; adding a required check is not part of this command).",
    "sm ci merge-gate",
    "the repository marked `<<< NOT A` or `<<< RED`: its Sonar issues or coverage, through a PR.",
    "One line per repository: Sonar gate and ratings, coverage badge, `required:` (checks main requires now), `not yet:`\n"
    "(checks that pass but are not required). `<<< NOT A`, `RED`, `BYPASS` or `NO RULESET` is what to act on."))
def ci_merge_gate() -> CiMergeGate:
    from .queue_lib import merge_gate

    return CiMergeGate(repos=gate_rows(merge_gate.status_lines()))


class MainRun(Model):
    head: str  # the commit, 8 characters
    run: int | None
    status: str
    conclusion: str
    created: str
    title: str
    failing: list[str] = []  # failing jobs of a red run

    def line(self) -> str:
        return (f"{self.head} run {self.run} {self.status}/{self.conclusion or None} {self.created} {self.title[:50]}"
                + (f"  FAILED: {', '.join(self.failing)}" if self.failing else ""))


class CiMain(Model):
    """Main's CI evidence the way the merge queue judges it."""

    runs: list[MainRun]  # the newest run per commit, newest first
    notes: list[str]  # what makes main unknown, or what is ignored and why
    queue_state: str  # green | red | unknown: the merge queue's verdict on main
    queue_would_record: dict[str, Any] = {}  # what the queue's tick would pin from this evidence (this command writes nothing)
    read_only: bool = True

    def render(self) -> list[str]:
        out = [r.line() for r in self.runs] + [f"  note: {n}" for n in self.notes] + [f"merge queue main_state: {self.queue_state}"]
        if self.queue_would_record:
            out.append(f"  (the next queue tick records {', '.join(sorted(self.queue_would_record))}; this command did not)")
        return out


def main_evidence_of(module: Any) -> CiMain:
    """Read main's runs and the queue's verdict through a fresh copy of the queue module; its state writes are caught."""
    hzlib = module.hzlib
    caught: dict[str, Any] = {}
    module.update = lambda **fields: caught.update(fields)  # main_state pins evidence as a side effect; a read must not
    raw = hzlib.ghj("run", "list", "-R", hzlib.CORE, "--workflow", core_facts.ci_file(), "--branch", "main", "-L", "10",
                    "--json", "status,conclusion,databaseId,headSha,displayTitle,attempt,createdAt")
    runs = hzlib.resolve_main_runs(raw)
    rows, seen = [], set()
    for r in runs or []:
        if r.get("headSha") in seen:
            continue
        seen.add(r.get("headSha"))
        failing = []
        if r.get("conclusion") in ("failure", "timed_out") and type(r.get("databaseId")) is int:
            jobs = module.attempt_jobs(r["databaseId"], r.get("attempt") or 1) or []
            failing = [j["name"] for j in jobs if j.get("conclusion") in ("failure", "timed_out")]
        rows.append(MainRun(head=str(r.get("headSha"))[:8], run=r.get("databaseId") if type(r.get("databaseId")) is int else None,
                            status=str(r.get("status")), conclusion=str(r.get("conclusion") or ""), created=str(r.get("createdAt", "")),
                            title=str(r.get("displayTitle") or ""), failing=failing))
    notes = main_evidence.explain(runs, module)
    state = module.main_state()
    return CiMain(runs=rows, notes=notes, queue_state=state, queue_would_record=_plain(caught))


def _plain(value: dict[str, Any]) -> dict[str, Any]:
    import json

    return dict(json.loads(json.dumps(value, default=str)))


@native("ci main", writes=False, replaces=("hz-main-evidence",), help=H(
    "Main's CI evidence in one command: the newest run per commit, failing jobs, and the merge queue's verdict on main.",
    "Use when the queue or go says main is unknown or red, before any rerun or fix-forward decision.",
    "nothing (read-only: GitHub reads; the pin the queue's tick would record is shown, not written).",
    "sm ci main",
    "sm ci why RUN for a red run; sm queue why when main is green and nothing merges.",
    "One line per commit: run, status/conclusion, created, title and FAILED jobs. `note:` lines say what makes main\n"
    "unknown (a cancelled run with no newer green or pending run, a malformed row, an unproven batch) or what is ignored\n"
    "and why. The last line is the verdict the merge queue acts on: green, red or unknown."))
def ci_main() -> CiMain:
    return main_evidence_of(queue_commands._queue())
