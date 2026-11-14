#!/usr/bin/env python3
"""Verbatim-move check for the focused admission port.

Every moved file is its original after the substitutions listed in RULES (import lines and renamed file
references only). `move` writes the moved files; `diff` (default) prints, per file, the unified diff between
"original after RULES" and the file in this repository. An empty diff means: moved verbatim. Every remaining
hunk is a deliberate change and must be listed in reviews/*.md with its test.

usage: admission_verbatim.py [diff|move|sums] [--installed DIR] [--operator DIR] [--only NAME]
  --installed  the installed tools directory (default ~/Code/heinzel-management/tools)
  --operator   the reviewed isolated operator copy (default ~/Code/tmp-cut-0939/operator-nonconsumers/tools)
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
LIB = "src/steuermaschine_heinzel/native/admission_lib"
PKG = "steuermaschine_heinzel.native.admission_lib"

# (pattern, replacement) applied line by line, in order, to SOURCE files.
SRC_RULES = [
    (r"^import hz_focused_admit as a$", "from . import focused_admit as a"),
    (r"^import hz_focused_resulting as resulting$", "from . import focused_resulting as resulting"),
    (r"^import hz_focused_update as update$", "from . import focused_update as update"),
    (r"^import hz_focused_update as git$", "from . import focused_update as git"),
    (r"^import hz_focused_slots as slots$", "from . import focused_slots as slots"),
    (r"^import hz_release_scope as scope$", "from . import release_scope as scope"),
    (r"^import hz_release_scope$", "from . import release_scope as hz_release_scope"),
    (r"^import hzlib$", "from . import identity as hzlib"),
    (r"with_name\('hz_focused_replay\.py'\)", "with_name('focused_replay.py')"),
]
# applied to TEST files
TEST_RULES = [
    (r"^from conftest import TOOLS$", "from admission_paths import TOOLS"),
    (r"^sys\.path\.insert\(0, str\(TOOLS\)\)$", "# (moved: the package is imported, no path insert)"),
    (r"^import hz_focused_slots as s$", f"from {PKG} import focused_slots as s"),
    (r"^import hz_focused_manifest as m$", f"from {PKG} import focused_manifest as m"),
    (r"^import hz_admit_wave as w$", f"from {PKG} import admit_wave as w"),
    (r"^import hz_focused_release as release$", f"from {PKG} import focused_release as release"),
    (r"^import test_focused_slots$", "import test_native_admission_slots as test_focused_slots"),
    (r"^from test_focused_slots import ", "from test_native_admission_slots import "),
    (r"^from test_focused_resulting import ", "from test_native_admission_resulting import "),
    (r"^import test_focused_resulting$", "import test_native_admission_resulting as test_focused_resulting"),
    (r"with_name\('hz_focused_replay\.py'\)", "with_name('focused_replay.py')"),
]
REL = "src/steuermaschine_heinzel/native/release_admit_lib"
REL_PKG = "steuermaschine_heinzel.native.release_admit_lib"
# the release route lives in its own package and imports the PR admission
REL_RULES = [
    (r"^import hz_focused_admit as a$", "from ..admission_lib import focused_admit as a"),
    (r"^import hz_focused_update as git$", "from ..admission_lib import focused_update as git"),
    (r"^import hz_release_scope as scope$", "from . import release_scope as scope"),
]
REL_TEST_RULES = [
    (r"^from test_focused_slots import a, env, s(  # noqa: F401  \(env binds HOME/HZ_QUEUE_STATE to tmp_path\))?$",
     f"from test_native_admission_slots import a, env  # noqa: F401\nfrom {REL_PKG} import release_slots as s"),
    (r"^from test_focused_resulting import ", "from test_native_admission_resulting import "),
    (r"^from test_focused_release import ", "from test_native_release_admit import "),
]
# moved blocks that must be in a file byte for byte: file -> [(origin, original, first line, last line)]
SEGMENTS = {
    f"{REL}/release_slots.py": [("operator", "hz_focused_slots.py", 66, 77), ("operator", "hz_focused_slots.py", 79, 90),
                                ("operator", "hz_focused_slots.py", 147, 150), ("operator", "hz_focused_slots.py", 411, 424),
                                ("operator", "hz_focused_slots.py", 362, 373), ("operator", "hz_focused_slots.py", 603, 745)],
    f"{REL}/release_identity.py": [("operator", "hzlib.py", 29, 31), ("operator", "hzlib.py", 34, 48), ("operator", "hzlib.py", 51, 59),
                                   ("operator", "hzlib.py", 62, 64)],
}
# name -> (origin, original relative path, moved relative path, rules)
FILES = {
    "focused_admit": ("installed", "hz_focused_admit.py", f"{LIB}/focused_admit.py", SRC_RULES),
    "focused_slots": ("installed", "hz_focused_slots.py", f"{LIB}/focused_slots.py", SRC_RULES),
    "focused_resulting": ("installed", "hz_focused_resulting.py", f"{LIB}/focused_resulting.py", SRC_RULES),
    "focused_update": ("installed", "hz_focused_update.py", f"{LIB}/focused_update.py", SRC_RULES),
    "focused_replay": ("installed", "hz_focused_replay.py", f"{LIB}/focused_replay.py", SRC_RULES),
    "focused_release": ("operator", "hz_focused_release.py", f"{REL}/focused_release.py", REL_RULES),
    "release_scope": ("operator", "hz_release_scope.py", f"{REL}/release_scope.py", REL_RULES),
    "focused_manifest": ("installed", "hz_focused_manifest.py", f"{LIB}/focused_manifest.py", SRC_RULES),
    "admit_wave": ("installed", "hz_admit_wave.py", f"{LIB}/admit_wave.py", SRC_RULES),
    "wave_size": ("installed", "hz-wave-size", f"{LIB}/wave_size.py", SRC_RULES),
    "floor_check": ("installed", "hz-floor-check", f"{LIB}/floor_check.py", SRC_RULES),
    "scoped_cov": ("installed", "hz_scoped_cov.py", f"{LIB}/scoped_cov.py", SRC_RULES),
    "test_slots": ("installed", "tests/test_focused_slots.py", "tests/test_native_admission_slots.py", TEST_RULES),
    "test_batch": ("installed", "tests/test_focused_batch.py", "tests/test_native_admission_batch.py", TEST_RULES),
    "test_resulting": ("installed", "tests/test_focused_resulting.py", "tests/test_native_admission_resulting.py", TEST_RULES),
    "test_stuck_queued": ("installed", "tests/test_focused_stuck_queued.py", "tests/test_native_admission_stuck_queued.py", TEST_RULES),
    "test_release": ("operator", "tests/test_focused_release.py", "tests/test_native_release_admit.py", REL_TEST_RULES),
    "test_release_stuck": ("operator", "tests/test_focused_release_stuck.py", "tests/test_native_release_admit_stuck.py", REL_TEST_RULES),
    "test_manifest": ("installed", "tests/test_focused_manifest.py", "tests/test_native_admission_manifest.py", TEST_RULES),
    "test_admit_wave": ("installed", "tests/test_admit_wave.py", "tests/test_native_admission_wave.py", TEST_RULES),
    "test_wave_size": ("installed", "tests/test_wave_size.py", "tests/test_native_admission_wave_size.py", TEST_RULES),
    "test_floor_check": ("installed", "tests/test_floor_check.py", "tests/test_native_admission_floor_check.py", TEST_RULES),
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
    p.add_argument("--operator", type=Path, default=Path.home() / "Code/tmp-cut-0939/operator-nonconsumers/tools")
    p.add_argument("--only", action="append", default=[])
    args = p.parse_args(argv)
    roots = {"installed": args.installed, "operator": args.operator}
    changed = 0
    for name, (origin, rel, moved, rules) in FILES.items():
        if args.only and name not in args.only:
            continue
        source = roots[origin] / rel
        target = HERE / moved
        original = source.read_text()
        if args.mode == "sums":
            print(f"{hashlib.sha256(original.encode()).hexdigest()}  {origin}:{rel}")
            continue
        expect = transformed(original, rules)
        if args.mode == "move":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(expect)
            print(f"moved {origin}:{rel} -> {moved}")
            continue
        if not target.exists():
            print(f"-- {name}: NOT MOVED YET ({moved})")
            continue
        actual = target.read_text()
        hunks = list(difflib.unified_diff(expect.splitlines(), actual.splitlines(), f"{origin}:{rel} (after RULES)", moved, lineterm="", n=1))
        if hunks:
            changed += 1
            print(f"-- {name}: {sum(1 for h in hunks if h.startswith('@@'))} hunk(s)")
            print("\n".join(hunks))
        else:
            print(f"-- {name}: VERBATIM")
    if args.mode == "diff" and not args.only:
        for moved, segments in SEGMENTS.items():
            text = (HERE / moved).read_text() if (HERE / moved).exists() else ""
            for origin, rel, first, last in segments:
                block = "\n".join(roots[origin].joinpath(rel).read_text().split("\n")[first - 1:last])
                state = "VERBATIM" if block and block in text else "MISSING OR CHANGED"
                changed += state != "VERBATIM"
                print(f"-- {moved.rsplit('/', 1)[1]}: block {origin}:{rel}:{first}-{last} {state}")
    return 1 if changed and args.mode == "diff" else 0


if __name__ == "__main__":
    raise SystemExit(main())
