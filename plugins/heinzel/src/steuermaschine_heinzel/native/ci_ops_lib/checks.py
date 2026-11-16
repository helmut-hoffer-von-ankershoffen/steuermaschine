"""How the check rollup of a PR reads: ready, pending, deferred or failing. Pure.

Moved unchanged from the former digest module: only the three names the re-run plan needs. The tick area's digest has
the same function; when that area is native, one of the two imports the other.
"""
from ..queue_lib import core_facts

REAL_BAD = ("FAILURE", "ACTION_REQUIRED", "TIMED_OUT")


def is_focused(name):
    """A check run of core's focused-job.yml (`sm ci one dispatch`): diagnostic, never blocking."""
    return (name or "").lower().startswith("focused ")


def classify_pr(roll):
    """ready | pending | deferred (mass-cancel plus the aggregate red: placeholder, not a code failure) | failing.
    The dispatched `Focused ...` check runs attach to the head too; they are diagnostics and never count."""
    roll = [c for c in roll or [] if not is_focused(c.get("name"))]
    if not roll:
        return "pending", []
    if any(c.get("status") not in ("COMPLETED", None) and c.get("conclusion") in (None, "") for c in roll):
        return "pending", []
    bad = [c["name"] for c in roll if c.get("conclusion") in REAL_BAD + ("CANCELLED",)]  # noqa: RUF005
    if not bad:
        return "ready", []
    cancelled = [c for c in roll if c.get("conclusion") == "CANCELLED"]
    real = sorted({c["name"] for c in roll if c.get("conclusion") in REAL_BAD and c["name"] not in (core_facts.aggregate(), *core_facts.api_checks())})
    if cancelled and not real:
        return "deferred", []
    return "failing", real or sorted(set(bad))
