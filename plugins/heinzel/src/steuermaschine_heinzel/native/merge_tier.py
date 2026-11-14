"""Core's merge tier (ADR 0149), as sm reads it: is it at main, what its verdicts and ledgers say.

The merge tier runs selected, rerunnable CI without a manifest. sm follows the Core that is at main, so this set can
go live before the Core change merges: every caller asks `active(...)` and keeps the older behaviour when it is not.
The capability is detected from main's own scripts/focused_plan.py (WHOLE_SUITES and MAIN_PROOF_CONTEXT); the
setting merge_tier_mode can force it on or off.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

MAIN_PROOF_CONTEXT = "hz/main-proof"
PLAN_PATH = "scripts/focused_plan.py"
FLAKE_ARTIFACT = re.compile(r"flake-ledger-(\d+)-(\d+)")
FLAKE_LINE = re.compile(r"passed on rerun: (?P<job>.+?) \(attempt (?P<attempt>\d+) (?P<conclusion>[a-z_]+), "
                        r"run (?P<run>\d+), head (?P<head>[0-9a-f]{7,40})\)")
CONSUMERS = re.compile(r"(?P<lane>[a-z0-9-]+) advisory: (?:(?P<n>\d+) failing: (?P<names>[^;]*)|(?P<state>passed|did not finish))")
RERUN_TAIL = re.compile(r"; (\d+) job\(s\) passed on rerun$")


class MergeTierSettings(BaseModel):
    """Settings of the merge-tier detection (mixin of the plugin's settings table)."""

    merge_tier_mode: Literal["auto", "on", "off"] = Field("auto",
                                 description="auto: follow the Core at main (its scripts/focused_plan.py defines WHOLE_SUITES); "
                                             "on or off: force the merge-tier rules (selected, rerunnable CI without a manifest).")


def capable(plan: str) -> bool:
    """The Core of this focused_plan.py text has the merge tier: one selector, whole suites, reruns allowed."""
    return bool(re.search(r"^WHOLE_SUITES\s*=", plan, re.M))


def proof_capable(plan: str) -> bool:
    """The Core counts a main commit as green only with the hz/main-proof status (ADR 0149 S2)."""
    return MAIN_PROOF_CONTEXT in plan and capable(plan)


def active(mode: str, read_plan: Callable[[], str | None]) -> bool:
    """Whether the merge-tier rules apply: forced by the setting, else read from main (unreadable: the older rules)."""
    if mode in ("on", "off"):
        return mode == "on"
    try:
        plan = read_plan()
    except (OSError, RuntimeError, ValueError):
        return False
    return bool(plan) and capable(plan or "")


def main_plan(gh: Any, ref: str = "main") -> str | None:
    """The text of scripts/focused_plan.py at REF of the configured repository (a GET; None when unreadable)."""
    data, why = gh.api(f"contents/{PLAN_PATH}", {"ref": ref})
    if why or not isinstance(data, dict) or data.get("encoding") != "base64":
        return None
    try:
        return base64.b64decode(str(data.get("content") or ""), validate=False).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None


def active_at_main(gh: Any, settings: dict[str, Any] | None) -> bool:
    """`active` for a command that has a Gh client and the plugin's settings."""
    return active(str((settings or {}).get("merge_tier_mode") or "auto"), lambda: main_plan(gh))


@dataclass
class Verdict:
    """The merge gate's verdict (the coverage-floor check summary), split into what blocks and what only informs."""

    text: str
    info: list[str] = field(default_factory=list)  # reviewed workflow change, advisory lanes, reruns: never red
    advisory_failing: dict[str, list[str]] = field(default_factory=dict)  # lane -> failing names (consumers: plugins)
    passed_on_rerun: int = 0


def verdict(text: str) -> Verdict:
    """Read the clauses Core may append to a PR verdict (d', h): they are information, not a failure."""
    found = Verdict(text)
    body = text.strip()
    if tail := RERUN_TAIL.search(body):
        found.passed_on_rerun = int(tail[1])
        found.info.append(f"{tail[1]} job(s) passed on rerun")
        body = body[: tail.start()]
    for clause in (c.strip() for c in body.split(";")):
        if clause.startswith("reviewed workflow change:"):
            found.info.append(clause)
        elif match := CONSUMERS.fullmatch(clause):
            found.info.append(clause)
            if match["n"]:
                found.advisory_failing[match["lane"]] = [n.strip() for n in match["names"].split(",") if n.strip()]
    return found


@dataclass(frozen=True)
class Flake:
    """One line of a flake ledger: a job that failed on an earlier attempt and passed on the latest."""

    job: str
    attempt: int
    conclusion: str
    run: int
    head: str
    line: str


def flakes(text: str) -> list[Flake]:
    """The ledger lines of a flake-ledger artifact or of verify_main's passed_on_rerun; other lines are ignored."""
    out = []
    for line in text.splitlines():
        if match := FLAKE_LINE.fullmatch(line.strip()):
            out.append(Flake(match["job"], int(match["attempt"]), match["conclusion"], int(match["run"]), match["head"], line.strip()))
    return out
