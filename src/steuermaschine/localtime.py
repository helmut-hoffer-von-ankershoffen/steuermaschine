"""Times in text output: local time with the zone, or a duration, never a raw UTC stamp.

`localize(text)` rewrites every ISO 8601 stamp that carries a UTC offset (`2026-11-21T09:05:12Z`,
`2026-11-21T09:05:12.123+00:00`, `2026-11-21 09:05:12+00:00`) into the local time of this machine with its zone
abbreviation (`2026-11-21 10:05 CET`). Stamps without an offset are not guessed at and stay as they are. JSON output
keeps its exact stamps, and so does a stamp that is the argument of an `sm ...` command printed for typing back, and a
result that says it is `verbatim` (log lines): only the text a person reads goes through here.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

STAMP = re.compile(r"(?<![0-9T:-])(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::(\d{2})(?:[.,]\d+)?)?(Z|[+-]\d{2}:?\d{2})(?![0-9])")


def local(moment: datetime) -> str:
    """`2026-11-21 10:05 CET`: the minute, in the local zone of this machine."""
    return moment.astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()


COMMAND_ARGUMENT = re.compile(r"\bsm(?: [\w.:=-]+)* $")  # `sm doc asks-ack 2026-...`: a command to type back keeps its exact stamp


def _replace(match: re.Match[str]) -> str:
    if COMMAND_ARGUMENT.search(match.string[max(0, match.start() - 120):match.start()]):
        return match.group(0)
    day, clock, seconds, zone = match.groups()
    offset = "+00:00" if zone == "Z" else (zone if ":" in zone else f"{zone[:3]}:{zone[3:]}")
    try:
        moment = datetime.fromisoformat(f"{day}T{clock}:{seconds or '00'}{offset}")
    except ValueError:
        return match.group(0)
    return local(moment)


def localize(text: str) -> str:
    return STAMP.sub(_replace, text)


def duration(seconds: float) -> str:
    """`5m`, `3h 20m`, `2d 4h`: how long, from a number of seconds (negative counts as zero)."""
    total = max(int(seconds), 0)
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        return f"{total // 3600}h {total % 3600 // 60}m"
    return f"{total // 86400}d {total % 86400 // 3600}h"


def since(then: str, now: datetime | None = None) -> str:
    """A duration from an ISO stamp (`Z` or an offset) to now."""
    moment = datetime.fromisoformat(then.replace("Z", "+00:00"))
    return duration(((now or datetime.now(UTC)) - moment).total_seconds())
