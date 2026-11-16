"""How the candidate table classifies a PR's checks, and the registry of KNOWN failures it names beads from."""

import os
import re
import sys
from datetime import datetime, timezone
from ..queue_lib import core_facts

REAL_BAD = ("FAILURE", "ACTION_REQUIRED", "TIMED_OUT")


def is_focused(name):
    """A check run of core's focused-job.yml (hz-ci-one dispatch): diagnostic, never blocking."""
    return (name or "").lower().startswith("focused ")


def classify_pr(roll):
    """ready | pending | deferred (mass-cancel plus the aggregate red: placeholder, not a code failure) | failing.
    hz-ci-one's dispatched `Focused ...` check runs attach to the head too; they are diagnostics and never count."""
    roll = [c for c in roll or [] if not is_focused(c.get("name"))]
    if not roll:
        return "pending", []
    if any(c.get("status") not in ("COMPLETED", None) and c.get("conclusion") in (None, "") for c in roll):
        return "pending", []
    bad = [c["name"] for c in roll if c.get("conclusion") in REAL_BAD + ("CANCELLED",)]
    if not bad:
        return "ready", []
    cancelled = [c for c in roll if c.get("conclusion") == "CANCELLED"]
    real = sorted({c["name"] for c in roll if c.get("conclusion") in REAL_BAD and c["name"] not in (core_facts.aggregate(), *core_facts.api_checks())})
    if cancelled and not real:
        return "deferred", []
    return "failing", real or sorted(set(bad))


BEAD_ID = re.compile(r"^(?:heinzel-management-)?[a-z0-9]{2,6}$")  # bead s1p: a bd id, never error text from a failed `hz-tasks add`
KNOWN_DAYS = 7  # an entry without `until` counts as KNOWN for 7 days after its `added` date (policy 2026-11-07)


def _date(value):
    """TOML date or 'YYYY-MM-DD' -> date, else None."""
    from datetime import date
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def known_entries(path, *, strict=False):
    """Every registry entry as {match, bead, note, until}; `until` = the entry's `until`, else `added` + 7 days, else
    None (no date at all: overdue, fail closed). Missing or broken file -> [] unless strict."""
    from datetime import timedelta
    try:
        import tomllib
        with open(path, "rb") as f:
            rows = tomllib.load(f).get("known", [])
        if strict and not isinstance(rows, list):
            raise ValueError("expected a list of known failures")
        out = []
        for k in rows:
            if strict and (not isinstance(k, dict) or not isinstance(k.get("match"), str) or not k["match"].strip()):
                raise ValueError("invalid known failure match")
            if not isinstance(k.get("bead"), str) or not BEAD_ID.match(k["bead"]):
                if strict:
                    raise ValueError("invalid known failure bead")
                continue  # rejected: not a bead id (see invalid_known_lines)
            until = _date(k.get("until"))
            if until is None and (added := _date(k.get("added"))) is not None:
                until = added + timedelta(days=KNOWN_DAYS)
            out.append({"match": k["match"], "bead": k["bead"], "note": k.get("note", ""), "until": until, "class": k.get("class", "")})
        return out
    except (OSError, ValueError, KeyError, TypeError):
        if strict:
            raise
        return []


def overdue(entry, today=None):
    today = today or datetime.now(timezone.utc).date()
    return entry["until"] is None or today > entry["until"]


def load_known(path, today=None, *, strict=False):
    """research/known-failures.toml -> [(lowercase match, bead, note)] of entries within their deadline; an entry past
    `until` stops counting as KNOWN (it is FAIL again and `overdue_known_lines` names it). Missing file -> []."""
    return [(k["match"].lower(), k["bead"], k["note"]) for k in known_entries(path, strict=strict) if not overdue(k, today)]


def known_bead(names, known):
    """Bead of the first registry entry matching any failing job or test name, else None."""
    for n in names:
        for m, bead, _ in known or []:
            if m in n.lower():
                return bead
    return None
