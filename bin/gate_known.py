"""The known structural findings of the plugin (tests/gate_known.json): what the promote gate tolerates, with the owner
who has to remove each. `record OWNER` adds the findings that are new right now under that owner and drops the ones
that are gone; the gate itself (tests/test_gate_structure.py) fails on any finding that is not in the file, and on
any entry that no longer occurs. Used by bin/merge-slice --accept-structural OWNER at a slice's FIRST merge only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "tests" / "gate_known.json"
# a __main__ block that is not a hole: it stays listed, with the reason it cannot write
PERMANENT = {"native/admission_lib/focused_replay.py": "run as a script by focused_resulting.replay_original (its bytes are copied into a driver); it only replays and prints"}


def key(finding: str) -> str:
    path, _, rest = finding.partition(":")
    return f"{path}: {rest.split(': ', 1)[1] if ': ' in rest else rest.strip()}"


def current() -> dict[str, set[str]]:
    import steuermaschine_heinzel
    from steuermaschine import lint
    from steuermaschine.plugins import stack_of
    from steuermaschine_heinzel import native
    from steuermaschine_heinzel.plugin import plugin
    from steuermaschine_heinzel.toolmap import ROWS

    package = Path(steuermaschine_heinzel.__file__).parent
    commands = [c for c in native.commands() if hasattr(c, "fn")]
    model = stack_of(plugin).settings_models()["heinzel"]
    defaults = set()
    for name, info in model.model_fields.items():
        default = "" if info.default is None else str(info.default)
        if default.startswith(("/", "~")) or "/Users/" in default or "Code/" in default:
            defaults.add(name)
    return {
        "main blocks": {entry.rsplit(":", 1)[0] for entry in lint.main_blocks(package)},
        "process starts": {key(f) for f in lint.script_calls(package, {r.tool for r in ROWS}, old_dir="heinzel-management/tools") if not f.startswith("toolmap.py")},
        "path defaults": defaults,
        "read commands that reach a write": {f"{c.path}: {hit}" for c in commands if not c.writes for hit in lint.write_reach(c)},
    }


def load() -> dict[str, dict[str, str]]:
    return json.loads(FILE.read_text()) if FILE.exists() else {}


def main() -> int:
    if sys.argv[1:2] != ["record"] or len(sys.argv) < 3:
        print("usage: gate_known.py record OWNER", file=sys.stderr)
        return 2
    owner, known, now = sys.argv[2], load(), current()
    added = 0
    for rule, findings in now.items():
        table = {k: v for k, v in known.get(rule, {}).items() if k in findings}
        for finding in sorted(findings - set(table)):
            table[finding] = PERMANENT.get(finding, owner)
            added += 1
        known[rule] = dict(sorted(table.items()))
    FILE.write_text(json.dumps(known, indent=1, sort_keys=True) + "\n")
    print(f"gate_known: {added} new finding(s) recorded for {owner}; {sum(len(v) for v in known.values())} known in all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
