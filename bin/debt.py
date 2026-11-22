"""The debt ratchet of the plugin: what the reviewed and moved code may not get worse at.
Numbers: coverage per package (line+branch), mypy strict findings, ruff findings, reads of the process environment.
The core has no debt: bin/check gates it at zero findings and 90% coverage. Compared with docs/debt.json:
`--gate` exits 1 when a number got worse, cannot be read (no coverage data, no tool summary line) or is missing from
docs/debt.json (bin/check runs it so); `--record` writes the current numbers (do it in the
commit that makes a number better, never to hide a worse one: it refuses to lower a recorded number unless `--lower` is
given). Coverage is recorded per platform (key `coverage core % [linux]`): the same tests cover different lines on
macOS and Linux, so each platform is gated against the baseline recorded for it, and a platform with no baseline is not
gated on coverage (listed as such, never red, never borrowed from another platform). Reads the .coverage file of the
test run."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RATCHET = ROOT / "docs" / "debt.json"
PLATFORM = sys.platform.rstrip("0123456789")  # darwin, linux
GROUPS = {"core": "src/steuermaschine/*", "plugin new code": "plugins/heinzel/src/steuermaschine_heinzel/*", "plugin moved libraries": "*/native/*_lib/*"}


def run(*argv: str) -> str:
    return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, check=False).stdout


def coverage(include: str, omit: str = "") -> float | None:
    args = ["uv", "run", "--quiet", "coverage", "report", f"--include={include}", "--format=total", "--fail-under=0"] + ([f"--omit={omit}"] if omit else [])
    out = run(*args).strip().splitlines()
    try:
        return float(out[-1])
    except (IndexError, ValueError):
        return None


def count(pattern: str, text: str, clean: str) -> int | None:
    """The finding count of a tool's summary line, 0 for its clean line, None when it printed neither (a crash, an
    output format that changed): an unreadable number fails the gate, it never counts as 0."""
    found = re.search(pattern, text, re.MULTILINE)
    if found:
        return int(found[1])
    return 0 if re.search(clean, text, re.MULTILINE) else None


MYPY = (r"Found (\d+) errors? in \d+ files?", r"^Success: no issues found")
RUFF = (r"Found (\d+) errors?", r"^All checks passed!")


def worse(key: str, value: float | int, old: float | int) -> bool:
    return (value < old - 0.05) if key.startswith("coverage") else (value > old)


def ungated(now: dict[str, float | int | None], before: dict[str, float | int]) -> list[str]:
    """Coverage numbers of a platform that has no baseline in docs/debt.json: not gated there."""
    return [key for key in now if key.startswith("coverage") and key not in before]


def problems(now: dict[str, float | int | None], before: dict[str, float | int], strict: bool = False) -> list[str]:
    """Why the gate is red: a number that got worse, could not be read, or has no recorded value (empty: green).
    A coverage number with no recorded value is skipped (see `ungated`) unless `strict` (the hosted promotion, which
    needs its platform recorded)."""
    out = []
    for key, value in now.items():
        old = before.get(key)
        if value is None:
            out.append(f"{key}: could not be read (no coverage data, or the tool printed no summary line)")
        elif old is None:
            if strict or not key.startswith("coverage"):
                out.append(f"{key}: not recorded in docs/debt.json (record it: bin/debt.py --record)")
        elif worse(key, value, old):
            out.append(f"{key}: {value} is WORSE than recorded {old}")
    return out


def env_reads() -> int:
    """Lines of plugin code that use os.environ / os.getenv (hand-ons to a child process included: they go through
    steuermaschine.config.child_environment). Code only: the old text a moved_map quotes is a string, not a read.
    tests/test_core_settings.py names the ones that may stay."""
    lines = set()
    for path in (ROOT / "plugins" / "heinzel" / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv") and isinstance(node.value, ast.Name) and node.value.id == "os":
                lines.add((path, node.lineno))
    return len(lines)


OLD_NAME = re.compile(r"\bhz-[a-z]|\bhz" + r"c\b|\.cache/hz")
MAPS = {"toolmap.py", "moved_map.py", "names.py", "state.py"}  # the maps of old names (tools, output, state paths): naming them is their job
KEPT = "# old name kept:"  # a line that keeps an old name on purpose says why (a launchd label, a marker in data on GitHub)


def old_names() -> int:
    """String literals of plugin code that name an old tool or an old state path (`hz-...`, `.cache/hz...`): output,
    help, state paths. Not counted: docstrings, the maps of old names (MAPS), `replaces=` of a command (its
    entry in the tool map) and a line that says why it keeps the name (KEPT). The gate holds this at zero."""
    count = 0
    for path in (ROOT / "plugins" / "heinzel" / "src").rglob("*.py"):
        if path.name in MAPS:
            continue
        text = path.read_text()
        kept = {at for number, line in enumerate(text.splitlines(), 1) if KEPT in line
                for at in ((number, number + 1) if line.lstrip().startswith("#") else (number,))}  # the reason on the line, or above it
        tree = ast.parse(text)
        skip = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and body and isinstance(body[0], ast.Expr):
                skip.add(id(body[0].value))
            if isinstance(node, ast.keyword) and node.arg == "replaces":
                skip |= {id(sub) for sub in ast.walk(node.value)}
        count += sum(1 for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
                     and node.lineno not in kept and OLD_NAME.search(node.value))
    return count


def coverage_metrics() -> dict[str, float | int | None]:
    return {
        f"coverage core % [{PLATFORM}]": coverage(GROUPS["core"]),
        f"coverage plugin new code % [{PLATFORM}]": coverage(GROUPS["plugin new code"], GROUPS["plugin moved libraries"]),
        f"coverage plugin moved libraries % [{PLATFORM}]": coverage(GROUPS["plugin moved libraries"]),
    }


def source_metrics() -> dict[str, float | int | None]:
    return {
        "mypy strict findings core": count(MYPY[0], run("uv", "run", "--quiet", "mypy", "-p", "steuermaschine"), MYPY[1]),
        "mypy strict findings plugin": count(MYPY[0], run("uv", "run", "--quiet", "mypy", "-p", "steuermaschine_heinzel"), MYPY[1]),
        "ruff findings moved libraries and plugin tests": count(RUFF[0], run("uv", "run", "--quiet", "ruff", "check", "plugins"), RUFF[1]),
        "environment reads plugin": env_reads(),
        "old names in plugin strings": old_names(),
    }


def main() -> None:
    source_only = "--source-only" in sys.argv
    if source_only and "--record" in sys.argv:
        print("partial debt measurements cannot be recorded", file=sys.stderr)
        sys.exit(2)
    now = ({} if source_only else coverage_metrics()) | source_metrics()
    before = json.loads(RATCHET.read_text()) if RATCHET.exists() else {}
    print("check: DEBT of the plugin (the ratchet docs/debt.json; the bar for 'clean' is 90% coverage, 0 findings)")
    for key, value in now.items():
        print(f"    {key}: {value}")
    if "--record" in sys.argv:
        if any(value is None for value in now.values()):
            print("    not recorded: a number could not be read")
            sys.exit(1)
        lowered = [f"{key}: {value} is worse than recorded {before[key]}" for key, value in now.items() if value is not None and key in before and worse(key, value, before[key])]
        if lowered and "--lower" not in sys.argv:
            print("\n".join(f"    RED {line}" for line in lowered) + "\n    not recorded: a recorded number would be lowered (--lower says so on purpose)")
            sys.exit(1)
        RATCHET.write_text(json.dumps(before | now, indent=1, sort_keys=True) + "\n")  # other platforms' numbers stay
        print(f"    recorded in {RATCHET.relative_to(ROOT)}")
        return
    found = problems(now, before)
    for key in ungated(now, before):
        print(f"    not gated: {key} has no baseline for {PLATFORM} in docs/debt.json (bin/debt.py --record on {PLATFORM} adds it)")
    for line in found:
        print(f"    RED {line}")
    if found:
        print(f"    {len(found)} number(s) worse, unreadable or unrecorded: fix it; the gate is red")
        if "--gate" in sys.argv:
            sys.exit(1)


if __name__ == "__main__":
    main()
