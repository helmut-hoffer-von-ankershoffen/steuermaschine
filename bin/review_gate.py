"""The review gate of a promote: every gated write command of the candidate must be approved at its exact digest.

    review_gate.py REVIEWS.json ALLOW.toml TODAY

REVIEWS.json is `sm --format json self reviews` of the candidate. ALLOW.toml (bin/promote-allow.toml) lists the units
that may go live although their review is not approved, each with `unit` (the review file, e.g.
"reviews/beads-tasks.md"), `reason`, `owner` (who carries the exception) and `expires` (YYYY-MM-DD, last day it holds).
Adding an entry is a reviewed change of this repository. A unit with a command of the merge queue, the daemons, head
or release can never be allowed: promoting it unreviewed would switch off a live write path.

Prints every allowed entry and every refused unit; exit 0 when the gate holds, 1 otherwise, 2 on bad input.
"""

from __future__ import annotations

import datetime
import json
import sys
import tomllib
from pathlib import Path

NEVER = ("daemon ", "queue ", "head ", "release ")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    rows = json.loads(Path(argv[0]).read_text())
    allow_path, today = Path(argv[1]), datetime.date.fromisoformat(argv[2])
    entries = tomllib.loads(allow_path.read_text()).get("allow", []) if allow_path.exists() else []
    allowed: dict[str, dict[str, str]] = {}
    bad = False
    for entry in entries:
        missing = [k for k in ("unit", "reason", "owner", "expires") if not str(entry.get(k, "")).strip()]
        if missing:
            print(f"review gate: allow entry {entry.get('unit', '?')} lacks {', '.join(missing)}", file=sys.stderr)
            bad = True
            continue
        if datetime.date.fromisoformat(str(entry["expires"])) < today:
            print(f"review gate: allow entry {entry['unit']} expired on {entry['expires']}", file=sys.stderr)
            bad = True
            continue
        allowed[str(entry["unit"])] = entry
    units: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        units.setdefault(row["file"], []).append(row)
    refused = 0
    for unit, commands in sorted(units.items()):
        states = {c["state"] for c in commands}
        if states == {"approved"}:
            continue
        paths = [c["path"] for c in commands]
        never = [p for p in paths if p.startswith(NEVER)]
        entry = allowed.get(unit)
        if entry and not never:
            print(f"review gate: ALLOWED {unit} ({', '.join(sorted(states))}): {entry['reason']} (owner {entry['owner']}, until {entry['expires']})", file=sys.stderr)
            continue
        why = "; never allowed: it switches off a live write path" if entry and never else ""
        print(f"review gate: REFUSED {unit} is {', '.join(sorted(states))} for sm {', sm '.join(paths)}{why}", file=sys.stderr)
        refused += 1
    approved = sum(1 for c in rows if c["state"] == "approved")
    print(f"review gate: {approved}/{len(rows)} gated commands approved, {refused} unit(s) refused", file=sys.stderr)
    return 1 if refused or bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
