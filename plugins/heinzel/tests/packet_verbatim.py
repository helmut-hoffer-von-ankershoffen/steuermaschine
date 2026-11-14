#!/usr/bin/env python3
"""Verbatim-move check for the packet-building port (everything before `head prepare`).

Every moved file is its original after the substitutions in RULES. `move` writes the moved files; `diff` (default)
prints, per file, the unified diff between "original after RULES" and the file in this repository. An empty diff
means: moved verbatim. Every remaining hunk is a deliberate change, listed in parity/head-packet.md.
This file owns the rows of the manifest builder, the wave builder, sizing, the floor check and the case collector
(scoped coverage moved with the preflight area).

usage: packet_verbatim.py [diff|move|sums] [--installed DIR] [--collector DIR] [--only NAME]
  --installed  the former tools directory (default ~/Code/heinzel-management/tools)
  --collector  directory of the case collector (default ~/Code/tmp-pr-fill/admit-A/bin)
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
LIB = "src/steuermaschine_heinzel/native/packet_lib"
PKG = "steuermaschine_heinzel.native.packet_lib"

# (pattern, replacement) applied line by line, in order, to SOURCE files.
SRC_RULES = [
    (r"^    import hz_focused_manifest as fm  # noqa: E402$", "    from . import focused_manifest as fm"),
    (r"^sys\.path\.insert\(0, str\(Path\(__file__\)\.resolve\(\)\.parent\)\)$", "# (moved: the package is imported, no path insert)"),
]
# applied to TEST files
TEST_RULES = [
    (r"^from conftest import TOOLS$", "from packet_paths import TOOLS"),
    (r"^sys\.path\.insert\(0, str\(TOOLS\)\)$", "# (moved: the package is imported, no path insert)"),
    (r"^import hz_focused_manifest as m$", f"from {PKG} import focused_manifest as m"),
    (r"^import hz_admit_wave as w$", f"from {PKG} import admit_wave as w"),
    (r'tool\("hz-wave-size"\)', 'tool("wave_size")'),
    (r'tool\("hz-floor-check"\)', 'tool("floor_check")'),
]
# name -> (origin, original relative path, moved relative path, rules)
FILES = {
    "focused_manifest": ("installed", "hz_focused_manifest.py", f"{LIB}/focused_manifest.py", SRC_RULES),
    "wave_size": ("installed", "hz-wave-size", f"{LIB}/wave_size.py", SRC_RULES),
    "floor_check": ("installed", "hz-floor-check", f"{LIB}/floor_check.py", SRC_RULES),
    "admit_wave": ("installed", "hz_admit_wave.py", f"{LIB}/admit_wave.py", SRC_RULES),
    "collect_cases": ("collector", "collect_cases.py", f"{LIB}/collect_cases.py", SRC_RULES),
    "test_manifest": ("installed", "tests/test_focused_manifest.py", "tests/test_native_packet_manifest.py", TEST_RULES),
    "test_wave_size": ("installed", "tests/test_wave_size.py", "tests/test_native_packet_size.py", TEST_RULES),
    "test_floor_check": ("installed", "tests/test_floor_check.py", "tests/test_native_packet_floor.py", TEST_RULES),
    "test_admit_wave": ("installed", "tests/test_admit_wave.py", "tests/test_native_packet_wave.py", TEST_RULES),
}


def transformed(text: str, rules) -> str:
    out = []
    for line in text.split("\n"):
        for pattern, replacement in rules:
            line = re.sub(pattern, replacement, line)
        out.append(line)
    return "\n".join(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", nargs="?", default="diff", choices=("diff", "move", "sums"))
    p.add_argument("--installed", type=Path, default=Path.home() / "Code/heinzel-management/tools")
    p.add_argument("--collector", type=Path, default=Path.home() / "Code/tmp-pr-fill/admit-A/bin")
    p.add_argument("--only", action="append", default=[])
    p.add_argument("--stat", action="store_true", help="one line per file instead of the hunks")
    args = p.parse_args(argv)
    roots = {"installed": args.installed, "collector": args.collector}
    changed = 0
    for name, (origin, rel, moved, rules) in FILES.items():
        if args.only and name not in args.only:
            continue
        original = (roots[origin] / rel).read_text()
        target = HERE / moved
        if args.mode == "sums":
            print(f"{hashlib.sha256(original.encode()).hexdigest()}  {origin}:{rel}")
            continue
        expect = transformed(original, rules)
        if args.mode == "move":
            if target.exists() and not args.only:
                print(f"kept  {moved} (exists; name it with --only to overwrite)")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(expect)
            print(f"moved {origin}:{rel} -> {moved}")
            continue
        if not target.exists():
            print(f"-- {name}: NOT MOVED YET ({moved})")
            continue
        hunks = list(difflib.unified_diff(expect.splitlines(), target.read_text().splitlines(), f"{origin}:{rel} (after RULES)", moved, lineterm="", n=1))
        if hunks:
            changed += 1
            print(f"-- {name}: {sum(1 for h in hunks if h.startswith('@@'))} hunk(s), {sum(1 for h in hunks if h[:1] in '+-' and h[:3] not in ('+++', '---'))} line(s)")
            if not args.stat:
                print("\n".join(hunks))
        else:
            print(f"-- {name}: VERBATIM")
    return 1 if changed and args.mode == "diff" else 0


if __name__ == "__main__":
    raise SystemExit(main())
