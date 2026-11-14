"""Record real state once into tests/fixtures/go (run by hand; the parity tests then need no network).

    .venv/bin/python plugins/heinzel/tests/record_go_fixtures.py go [NAME]
    .venv/bin/python plugins/heinzel/tests/record_go_fixtures.py matrix PATH/TO/OLD/tests/test_go.py

`go` imports the old hz_go from the tools directory, runs its read-only collect() (the same probes `sm go` runs
today), and stores the full collected state with the old decision and the old text. Nothing is written outside
tests/fixtures/go. Paths under the home directory are stored as ~ so the fixtures carry no user name.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent / "fixtures" / "go"
TOOLS = Path(os.environ.get("SM_HEINZEL__TOOLS_DIR", "~/Code/heinzel-management/tools")).expanduser()


def scrub(value):
    home = str(Path.home())
    if isinstance(value, str):
        return value.replace(home, "~")
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {scrub(k): scrub(v) for k, v in value.items()}
    return value


def record_go(name: str) -> None:
    sys.path.insert(0, str(TOOLS))
    import hz_go

    hz_go.tool = lambda n: n  # bare tool names, whatever PATH holds
    now = time.time()
    state = hz_go.collect()
    state.pop("seconds", None)
    decision = hz_go.decide(state, now)
    doc = {"now": now, "state": state, "decision": decision, "text": hz_go.render(decision)}
    path = HERE / f"go-{name}.json"
    path.write_text(json.dumps(scrub(doc), indent=1, sort_keys=True, default=str) + "\n")
    print(f"wrote {path} ({path.stat().st_size} bytes): {decision['kind']}", flush=True)
    os._exit(0)


if __name__ == "__main__" and sys.argv[1:2] != ["matrix"]:
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    if what == "go":
        record_go(sys.argv[2] if len(sys.argv) > 2 else time.strftime("%Y%m%dT%H%M"))
    else:
        raise SystemExit(__doc__)


def record_matrix(old_tests: str) -> None:
    """Run the OLD tool's own test file and capture every decide() call it makes: state, now, decision, text.
    `old_tests` is the path of the old test_go.py. Nothing is written next to it (no bytecode, no cache)."""
    import pytest

    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(old_tests).resolve().parent.parent))
    import hz_go

    hz_go.tool = lambda n: n
    real, seen = hz_go.decide, []

    def capturing(state, now=None):
        result = real(json.loads(json.dumps(state, default=str)), now)
        if now is not None:  # without a fixed time the answer cannot be replayed
            seen.append({"now": now, "state": state, "decision": result, "text": hz_go.render(result)})
        return result

    hz_go.decide = capturing
    code = pytest.main(["-q", "-p", "no:cacheprovider", "-o", "addopts=", old_tests])
    path = HERE / "go-matrix.json"
    path.write_text(json.dumps(scrub(seen), indent=1, default=str) + "\n")  # key order kept: the order of UNKNOWN lines follows it
    print(f"old tests exit {code}; wrote {path}: {len(seen)} decisions", flush=True)


if __name__ == "__main__" and len(sys.argv) > 2 and sys.argv[1] == "matrix":
    record_matrix(sys.argv[2])
