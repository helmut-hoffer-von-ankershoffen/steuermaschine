#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""hz-diff-guard <pr> | plugins <pr> | acme <pr> | sf <pr>: PASS or FLAG for a PR's diff, with file:line.

Reads `gh pr diff` and flags the changes that make a red check go away instead of fixing it:
  timeout    a raised numeric timeout or deadline (timeout-minutes, --session-timeout, STEP_TIMEOUT, any *_timeout / *_deadline)
  threshold  a lowered coverage floor or threshold (fail_under, --cov-fail-under, *_threshold, coverage floors)
  skip       a new skip, skipif, xfail, `.only`, `xit`, `fit`
  deleted    a deleted test function whose name does not reappear in the diff
  retry      retries or reruns (--reruns, retries: N, nick-fields/retry, flaky)
  coe        continue-on-error that is not `false`
  no-verify  --no-verify
  sonar      Sonar exclusions and NOSONAR
  quarantine any change to core's quarantine lane (tests/quarantine.toml, scripts/quarantine.py, tests/_quarantine.py):
             an added or extended entry takes a test out of the required jobs, so it always needs an independent review;
             also any +/- line mentioning quarantine in Makefile, .github/workflows/ci.yml or tests/conftest.py
Markdown, rst and txt files are not scanned. Exit 1 on FLAG, 2 when the diff can't be read (a diff too large for gh is
unreadable too). `hz-queue add` runs this and refuses a flagged PR unless given `--reviewed "reason"`."""

import re
import sys
from pathlib import Path

from . import edge  # moved: siblings are imported relatively, no sys.path entry
from . import hzlib  # noqa: E402

DOC_FILES = (".md", ".mdx", ".rst", ".txt")
HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
NUMBER_KEY = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_.-]*|--[A-Za-z0-9_-]+)\s*(?:=|:|\s)\s*[\"']?(?P<n>\d+(?:\.\d+)?)", re.I)
TIMEOUT_KEY = re.compile(r"timeout|deadline", re.I)
THRESHOLD_KEY = re.compile(r"fail[-_]?under|threshold|coverage|cov[-_]min|min[-_]?cov|floor", re.I)
ADDED_RULES = [
    ("skip", re.compile(r"pytest\.mark\.(skip|skipif|xfail)\b|\bpytest\.(skip|xfail)\(|\bxfail\b|\bunittest\.skip|\b(it|test|describe)\.skip\b|\bskipIf\b|\bxit\(|\bxdescribe\(|\bxtest\(|\bt\.Skip\(")),
    ("skip", re.compile(r"\.only\(|\bfdescribe\(|\bfit\(|\btest\.only\b")),
    ("retry", re.compile(r"--reruns?\b|\breruns?\s*[=:]\s*[1-9]|\bretries\s*[=:]\s*[1-9]|--retries\b|nick-fields/retry|retry-action|@retry\b|\bflaky\b|rerunfailures|rerun-failed|max[_-]attempts", re.I)),
    ("coe", re.compile(r"continue-on-error\s*:\s*(?!false\b)\S", re.I)),
    ("no-verify", re.compile(r"--no-verify\b")),
    ("sonar", re.compile(r"NOSONAR|sonar\.[\w.]*(exclusions|ignore)", re.I)),
]
QUARANTINE_FILES = ("tests/quarantine.toml", "scripts/quarantine.py", "tests/_quarantine.py")
# The required expiry check runs `make quarantine-check` from ci.yml: a quarantine line there is lane wiring too.
QUARANTINE_WIRING = ("Makefile", ".github/workflows/ci.yml", "tests/conftest.py")
TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)|^\s*(?:it|test)(?:\.each\([^)]*\))?\(\s*[\"'`]([^\"'`]+)[\"'`]|^\s*func\s+(Test\w+)\(")


def diff_text(repo: str, number: str) -> str | None:
    code, out = hzlib.gh("pr", "diff", number, "-R", repo)
    return out if code == 0 and out.strip() else None


def numbers(line: str, wanted: re.Pattern) -> list[tuple[str, float]]:
    return [(m.group("key").lower(), float(m.group("n"))) for m in NUMBER_KEY.finditer(line) if wanted.search(m.group("key"))]


def hunks(diff: str) -> list[tuple[str, list]]:
    """(path, hunk) for every hunk; a hunk is a list of (sign, line number, text), old numbers for `-`, new for `+`."""
    found: list[tuple[str, list]] = []
    path, current, old, new = "", None, 0, 0
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            path, current = raw.rsplit(" b/", 1)[-1], None
            continue
        if raw.startswith("--- ") and current is None:
            if raw[4:] != "/dev/null":
                path = raw[6:] if raw.startswith("--- a/") else raw[4:]
            continue
        if raw.startswith("+++ ") and current is None:
            if raw[4:] != "/dev/null":
                path = raw[6:] if raw.startswith("+++ b/") else raw[4:]
            continue
        match = HUNK.match(raw)
        if match:
            old, new = int(match.group(1)), int(match.group(2))
            current = []
            found.append((path, current))
            continue
        if current is None or not raw:
            continue
        sign, text = raw[0], raw[1:]
        if sign == "+":
            current.append(("+", new, text))
            new += 1
        elif sign == "-":
            current.append(("-", old, text))
            old += 1
        elif sign == " ":
            old, new = old + 1, new + 1
    return found


def compare(path: str, hunk: list, wanted: re.Pattern, rule: str, worse) -> list[tuple]:
    """Pair removed and added numbers by key within a hunk, in order; flag a pair that got `worse`."""
    removed: dict[str, list[float]] = {}
    for sign, _, text in hunk:
        if sign == "-":
            for key, n in numbers(text, wanted):
                removed.setdefault(key, []).append(n)
    found = []
    for sign, line, text in hunk:
        if sign != "+":
            continue
        for key, n in numbers(text, wanted):
            if removed.get(key):
                before = removed[key].pop(0)
                if worse(n, before):
                    found.append((path, line, rule, f"{key} {before:g} -> {n:g}: {text.strip()[:90]}"))
    return found


def scan(diff: str) -> list[tuple]:
    """Every finding as (path, line, rule, text)."""
    found, removed_tests, added_names = [], [], set()
    for path, hunk in hunks(diff):
        if path.endswith(DOC_FILES):
            continue
        if path in QUARANTINE_FILES or path in QUARANTINE_WIRING:
            changed = [(line, text) for sign, line, text in hunk if sign in "+-" and text.strip()
                       and (path in QUARANTINE_FILES or "quarantine" in text.lower())]
            if changed:
                line, text = changed[0]
                found.append((path, line, "quarantine", f"quarantine lane changed ({len(changed)} line(s)): {text.strip()[:70]}"))
        found += compare(path, hunk, TIMEOUT_KEY, "timeout", lambda new, old: new > old)
        found += compare(path, hunk, THRESHOLD_KEY, "threshold", lambda new, old: new < old)
        for sign, line, text in hunk:
            test = TEST_DEF.match(text)
            name = test and next(g for g in test.groups() if g)
            if sign == "-" and name:
                removed_tests.append((path, line, name, text.strip()))
            elif sign == "+" and name:
                added_names.add(name)
            if sign == "+":
                for rule, pattern in ADDED_RULES:
                    if pattern.search(text):
                        found.append((path, line, rule, text.strip()[:100]))
                        break
    found += [(p, l, "deleted", f"test removed: {t[:90]}") for p, l, name, t in removed_tests if name not in added_names]
    return sorted(set(found), key=lambda f: (f[0], f[1], f[2]))


def check(repo: str, number: str) -> list[tuple] | None:
    """Findings for a PR, or None when the diff can't be read."""
    diff = diff_text(repo, number)
    return None if diff is None else scan(diff)


def render(label: str, found: list[tuple] | None) -> str:
    if found is None:
        return f"UNREADABLE {label}: gh pr diff gave nothing (too large, or no access); review it by hand"
    if not found:
        return f"PASS {label}"
    return "\n".join([f"FLAG {label}: {len(found)} change(s) that weaken a check"] + [f"  {p}:{l}  {rule}  {t}" for p, l, rule, t in found])


def main(argv: list[str]) -> int:
    try:
        repo, number, key = hzlib.split_key(argv)
    except ValueError:
        sys.exit("usage: sm head guard <pr> | plugins <pr> | acme <pr> | sf <pr>")
    found = check(repo, number)
    print(render(key, found))
    return 2 if found is None else 1 if found else 0

