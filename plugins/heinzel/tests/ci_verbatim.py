#!/usr/bin/env python3
"""Verbatim-move check for the advisory classifier library of `sm ci classify` and the leak watch of `sm mini leakwatch`.

Each moved file is its original after the substitutions in its rules (the environment seam, the injected process
runner, names) and without its command-line `main`. `move` writes the moved file; `diff` (default) prints the unified diff
between "original after RULES" and the file in this repository. An empty diff means: moved verbatim.

usage: ci_verbatim.py [diff|move|extract FILE] [--tools DIR]

Also checked: the single definitions moved out of the triage and digest modules into ci_lib/triage.py (EXTRACTS):
each must be in the moved file word for word after its rules.
"""
from __future__ import annotations

import difflib
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
MOVED = HERE / "src/steuermaschine_heinzel/native/ci_classify_lib/jev.py"

DOC = '''# ruff: noqa: SIM115, RUF100  (moved verbatim; see tests/ci_verbatim.py)
"""Thin stdlib client for the typed-question classifier "Jev" (TypeSafe AI System One, not an LLM). Advisory only.

Default OFF: nothing is sent unless CONFIG["mode"] is "on" AND a key exists. Mode "off" or the file <dir>/OFF is the
kill switch. The caller fills CONFIG from the settings (`configure`): mode, dir (state directory: the daily budget,
the kill switch, the one-hour pause after a rejected key), api_key, url (tests; https or loopback only),
keychain_item, run (the process runner, for the keychain read). The key is never logged, printed or stored. Every
failure returns None and callers keep their behaviour. Jev may sort and label; it never decides, closes, merges or
reruns. Only PUBLIC CI logs of the allowlisted repositories may be sent (check_repo)."""'''

# (pattern, replacement), applied in order to the whole original text
RULES = [
    (r'\A#!/usr/bin/env python3\n""".*?"""', DOC),
    (r"import fcntl, json, os, random, re, subprocess, sys, time, urllib\.error, urllib\.request",
     "import fcntl, json, os, random, re, sys, time, urllib.error, urllib.request  # noqa: E401, I001"),
    (r"_sleep = time\.sleep  # tests replace it", "_sleep = time.sleep  # tests replace it\nCONFIG: dict = {}  # mode, dir, api_key, url, keychain_item, run: set by the caller\n\n\n"
     "def configure(**values):\n    \"\"\"Replace the configuration with the caller's settings.\"\"\"\n    CONFIG.clear()\n    CONFIG.update(values)"),
    (r'os\.environ\.get\("HZ_JEV_DIR"\) or os\.path\.expanduser\("~/\.cache/hz-jev"\)', 'os.path.expanduser(str(CONFIG.get("dir") or "~/.cache/jev"))'),
    (r'os\.environ\.get\("HZ_JEV_API_KEY", ""\)\.strip\(\)', 'str(CONFIG.get("api_key") or "").strip()'),
    (r'    try:\n        r = subprocess\.run\(\["security", "find-generic-password", "-s", "hz-jev", "-w"\], capture_output=True, text=True, timeout=5\)\n'
     r'        return r\.stdout\.strip\(\) if r\.returncode == 0 else ""\n    except \(OSError, subprocess\.SubprocessError\):\n        return ""',
     '    run, item = CONFIG.get("run"), CONFIG.get("keychain_item")\n    if not run or not item:\n        return ""\n'
     '    r = run(["security", "find-generic-password", "-s", str(item), "-w"], timeout=5)\n    return r.out.strip() if r.rc == 0 else ""'),
    (r'os\.environ\.get\("HZ_JEV", ""\)\.lower\(\)', 'str(CONFIG.get("mode") or "").lower()'),
    (r"Opt-in: default OFF, only HZ_JEV=on enables, kill switch wins\.", "Opt-in: default OFF, only mode on enables, kill switch wins."),
    (r'os\.environ\.get\("HZ_JEV_URL"\) or BASE', 'CONFIG.get("url") or BASE'),
    (r'"User-Agent": "hz-jev/1"', '"User-Agent": "ci-classifier/1"'),
    (r'hint="401: key rejected; run `hz-jev ping` to re-check"', 'hint="401: key rejected; paused for one hour"'),
    (r"\n\ndef main\(argv\):.*\Z", "\n"),
]


# ---------------------------------------------------------------- extracts: single definitions moved out of a file
LIB = HERE / "src/steuermaschine_heinzel/native/ci_lib"
TRIAGE_RULES = [
    (r"\bd\.known_bead\(", "known_failures.known_bead("), (r"\bd\.(?=[A-Za-z_])", ""),
    (r"hz-ci-one's dispatched", "A dispatched"), (r"\(hz-ci-one dispatch\)", "(a hosted dispatch)"),
    (r"-> hz-ship \{number\} \(queue", "-> sm head ship {number} (queue"), (r"no push \(hz-push-gate\)", "no push (sm head push)"),
    (r"-> hz-rerun-batch when", "-> sm ci rerun when"), (r"-> hz-ci-one rerun --job \(once\)", "-> sm ci one rerun {number} --job NAME --wait (once)"),
    (r"-> hz-faillog \{number\}; open", "-> sm ci why {number}; open"), (r"-> hz-ship \{n\} --reviewed", "-> sm head ship {n} --reviewed"),
    (r"re\.escape\(R\)", "re.escape(repo)"), (r"def deferred_check\(check, recorded\):", "def deferred_check(check, recorded, repo=R):"),
    (r"def classify_deadline_checks\(checks, recorded=\(\)\):", "def classify_deadline_checks(checks, recorded=(), repo=R):"),
    (r"deferred_check\(check, recorded\) for check in cancelled", "deferred_check(check, recorded, repo) for check in cancelled"),
]
EXTRACTS = {  # moved file -> [(old file, [definition names in order])], rules
    LIB / "triage.py": ([("hz_digest.py", ["REAL_BAD", "is_focused", "classify_pr"]),
                         ("hz_triage.py", ["R", "VERDICT", "DEADLINE_ACTIVE", "DEADLINE_FAILURE", "DEADLINE_COMPLETE", "DEADLINE_AGGREGATES", "DEADLINE_QUERY",
                                           "triage_pr", "review_verdict", "review_line", "complete_nodes", "deadline_checks", "normalize_deadline_checks",
                                           "deferred_check", "classify_deadline_checks"])], TRIAGE_RULES),
}


def definitions(text: str, names: list[str]) -> list[str]:
    """The source text of the named top-level definitions (assignments and functions), in the order asked."""
    import ast

    found = {}
    for node in ast.parse(text).body:
        name = node.name if isinstance(node, ast.FunctionDef) else node.targets[0].id if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) else None
        if name in names:
            found[name] = ast.get_source_segment(text, node)
    return [found[name] for name in names]


def extracted(tools: Path, moved: Path) -> list[str]:
    """Every definition of one extract, after its rules."""
    sources, rules = EXTRACTS[moved]
    out = []
    for old, names in sources:
        for segment in definitions((tools / old).read_text(), names):
            for pattern, replacement in rules:
                segment = re.sub(pattern, lambda _m, r=replacement: r, segment)
            out.append(segment)
    return out


def missing_extracts(tools: Path) -> list[str]:
    """First line of every moved definition that is not in its moved file word for word."""
    return [f"{moved.name}: {segment.splitlines()[0]}" for moved in EXTRACTS for segment in extracted(tools, moved) if segment not in moved.read_text()]


LEAKWATCH = HERE / "src/steuermaschine_heinzel/native/leakwatch_lib/watch.py"
LEAKWATCH_DOC = '''# ruff: noqa: F401, RUF007, UP031  (moved verbatim; see tests/ci_verbatim.py)
"""Watch of the long-running live hz on this machine (its server on its port, the launchd daemon, every child) for leaks.

READ-ONLY toward hz: it only runs ps, lsof, netstat and launchctl list and one GET of the local web; it never signals,
restarts or opens hz. A sample is one JSON line: tree pids with parents, per process fds by type, TCP states, listening
ports, children, orphans, zombies, RSS and virtual memory, CPU over 2 s, threads, uptime, the on-disk package version,
the load and the seconds the web took to answer. `table` is the report, `findings` the check.

The caller fills EDGE (`configure`): run (the process runner), state (directory of samples.jsonl, rotated at 20 MB),
config (an optional TOML file that overrides DEFAULTS), daemon_label (the launchd label of the daemon)."""'''
LEAKWATCH_DEFAULTS = '''EDGE: dict = {}  # run, state, config, daemon_label: set by the caller

DEFAULTS = {  # thresholds of the check and the report; a TOML file (EDGE["config"]) overrides single values
    "port": 8737, "allowed_ports": [8737], "window_hours": 6,
    "fds_max": 500, "rss_max_mb": 800, "close_wait_max": 5, "orphans_max": 0, "zombies_max": 0, "cpu_idle_max": 25.0, "cpu_window_minutes": 10,
    "min_samples": 6, "rise_rel": 0.10, "fds_slope_per_hour": 10, "sockets_slope_per_hour": 10, "close_wait_slope_per_hour": 2,
    "children_slope_per_hour": 3, "rss_slope_mb_per_hour": 20, "restart_window_minutes": 60, "stale_minutes": 15,
}


def configure(**values):
    """Replace the edges with the caller's."""
    EDGE.clear()
    EDGE.update(values)'''
LEAKWATCH_RULES = [
    (r'\A#!/usr/bin/env -S uv run --script\n.*?"""hz-leakwatch sample.*?"""', LEAKWATCH_DOC),
    (r"import re\nimport subprocess\nimport sys\nimport time", "import re\nimport time"),
    (r"HERE = Path\(__file__\)\.resolve\(\)\.parent\n", LEAKWATCH_DEFAULTS + "\n\n\n"),
    (r'return Path\(os\.environ\.get\("HZ_LEAKWATCH_STATE"\) or Path\.home\(\) / "\.local/state/hz-leakwatch"\)', 'return Path(str(EDGE["state"])).expanduser()'),
    (r'return tomllib\.loads\(Path\(os\.environ\.get\("HZ_LEAKWATCH_CONFIG"\) or HERE / "leakwatch\.toml"\)\.read_text\(\)\)',
     'override = tomllib.loads(Path(str(EDGE["config"])).expanduser().read_text()) if EDGE.get("config") else {}\n'
     '    return {**DEFAULTS, "daemon_label": str(EDGE.get("daemon_label") or ""), **override}'),
    (r"    try:\n        done = subprocess\.run\(cmd, capture_output=True, text=True, timeout=30, check=False\)\n"
     r"    except \(OSError, subprocess\.SubprocessError\):\n        return \"\"\n    return done\.stdout", '    return str(EDGE["run"](cmd, timeout=30).out or "")'),
    (r"\n\n# --- commands -+\n\ndef main\(argv: list\[str\]\) -> int:.*\Z", "\n"),
]
WHOLE = {"jev": ("hz_jev.py", MOVED, RULES), "leakwatch": ("hz-leakwatch", LEAKWATCH, LEAKWATCH_RULES)}


def expected(tools: Path, name: str = "jev") -> str:
    old, _, rules = WHOLE[name]
    text = (tools / old).read_text()
    for pattern, replacement in rules:
        text, n = re.subn(pattern, lambda _m, r=replacement: r, text, flags=re.S)
        if n == 0:
            raise SystemExit(f"rule did not apply: {pattern[:60]}")
    return text


def main(argv: list[str]) -> int:
    tools = Path(argv[argv.index("--tools") + 1]) if "--tools" in argv else Path("~/Code/heinzel-management/tools").expanduser()
    if argv[:1] == ["move"]:
        for name, (_, moved, _) in WHOLE.items():
            moved.write_text(expected(tools, name))
            print(f"wrote {moved}")
        return 0
    if argv[:1] == ["extract"]:  # print the definitions of one extract, to paste under the header of a new moved file
        print("\n\n\n".join(extracted(tools, LIB / argv[1])))
        return 0
    diff = []
    for name, (_, moved, _) in WHOLE.items():
        one = list(difflib.unified_diff(expected(tools, name).splitlines(), moved.read_text().splitlines(), "original after RULES", str(moved), lineterm=""))
        print("\n".join(one) if one else f"{name}: moved verbatim")
        diff += one
    gone = missing_extracts(tools)
    print("\n".join(f"NOT VERBATIM {line}" for line in gone) if gone else f"extracts: {sum(len(n) for srcs, _ in EXTRACTS.values() for _, n in srcs)} definitions moved verbatim")
    return 1 if diff or gone else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
