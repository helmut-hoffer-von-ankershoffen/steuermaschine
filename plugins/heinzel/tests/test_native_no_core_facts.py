"""No fact of Heinzel's CI is typed into the plugin: the workflow files, the aggregate job and the required contexts come
from Core's release policy (queue_lib/core_policy.py, queue_lib/core_facts.py), never from a literal here.

Scanned: every .py file of the plugin's source. Not scanned: the review files and parity notes (prose about earlier
units), the moved_map tables (the recorded text of the port, history by design) and release_lib/rejection.py (the
closure of the 0.9.16 compose failure: the workflow inventory of that past release, immutable evidence, not a rule for
today's CI). Test fixtures live under tests/."""

from __future__ import annotations

import re
from pathlib import Path

import steuermaschine_heinzel

SRC = Path(steuermaschine_heinzel.__file__).resolve().parent
ANYWHERE = ("ci.yml", "merge-gate.yml", "CI OK")  # a file name or the aggregate, in code, a comment or a help text
HISTORY = ("moved_map.py", "rejection.py")
QUOTED = re.compile(r"""(["'])(merge-gate|coverage-floor)\1""")  # a required context as a string of its own


def offenders() -> list[str]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name in HISTORY:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if any(word in line for word in ANYWHERE) or QUOTED.search(line):
                found.append(f"{path.relative_to(SRC)}:{number}: {line.strip()[:120]}")
    return found


def test_no_heinzel_ci_fact_is_typed_into_the_plugin():
    hits = offenders()
    assert not hits, "read these from Core's release policy instead:\n" + "\n".join(hits)
