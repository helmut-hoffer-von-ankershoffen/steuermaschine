"""Reading CI logs without printing them whole: the failing part of a failed-step log, the runs behind a pull request's
failing checks, and the failing test names. Pure functions on text and on the check rollup `gh` returns; reading the
log itself is the caller's (`gh run view RUN --log-failed`).

    lines = condense(clean(text), k=6, tail=12)     per failing step the first error lines with context, and the last lines
    failing_runs(rollup)                            {run id: names of its failing checks}
    attached_checks(rollup)                         failing checks without an Actions run (no log to read)
    failing_tests(text)                             pytest FAILED lines, vitest FAIL lines, Playwright failures
"""

from __future__ import annotations

import re
from typing import Any

ERR = re.compile(r"(^E\s{2,}|\bFAILED\b|^FAIL\b|Traceback \(|AssertionError|\berror\b[:\[]|error TS\d+|\bError:|##\[error\]|\bpanic\b|timed out|Timeout|ELIFECYCLE|Quality Gate|below the (floor|threshold)|✗|✖|\bFAILURE\b|\[error\])")  # noqa: E501
NOISE = re.compile(r"All checks passed|0 errors|no errors|--fail-fast|continue-on-error|exit code 0|Downloaded|Downloading", re.I)
ANSI = re.compile(r"\^\[\[[0-9;]*[A-Za-z]|\x1b\[[0-9;]*[A-Za-z]|\[(?:\d+;)*\d+m")  # first form: the escape as `gh` prints it into a pipe (caret notation)
TS = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?")
FAILING = ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED")
RUN_URL = re.compile(r"/actions/runs/(\d+)")  # a check without an Actions run (posted through the checks API) has no log to read
TEST = re.compile(r"FAILED (\S+::\S+)|^\s*FAIL\s+(\S+ > .+)$|^\s*\d+\) (\[[^\]]+\] › .+?)(?: \(\d|$)|✘\s+\d+ (\[[^\]]+\] › .+?)(?: \(\d|$)")  # noqa: RUF001
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s?")


def clean(text: str) -> list[tuple[str, str]]:
    """-> list of (job/step tag, body) from failed-step log lines (job<TAB>step<TAB>timestamp body) or plain logs."""
    out = []
    for x in text.splitlines():
        parts = x.split("\t", 2)
        tag, body = (f"{parts[0][:40]} / {parts[1][:40]}", parts[2]) if len(parts) == 3 else ("", x)
        out.append((tag, TS.sub("", ANSI.sub("", body)).rstrip()))
    return out


def condense(lines: list[Any], k: int = 6, tail: int = 12, width: int = 200) -> list[str]:
    pairs: list[tuple[str, str]] = [(line if isinstance(line, tuple) else ("", line)) for line in lines]
    idx = [i for i, (_, x) in enumerate(pairs) if ERR.search(x) and not NOISE.search(x) and not x.lstrip().startswith("##[group]")]
    out: list[str] = []
    last, tag = -9, None
    for i in idx[:k * 3]:
        if i <= last + 1:
            continue
        if pairs[i][0] != tag:
            tag = pairs[i][0]
            out.append(f"[{tag}]" if tag else "[log]")
        for j in range(max(0, i - 1), min(len(pairs), i + 2)):
            out.append("  " + pairs[j][1][:width])
        last = i + 1
        if len([x for x in out if x.startswith("[")]) > k or len(out) > k * 4:
            break
    if not out:
        out = ["(no error-looking line found)"]
    return out + [f"--- last {tail} lines ---"] + [x[1][:width] for x in pairs[-tail:]]


def failing_runs(rollup: list[dict[str, Any]]) -> dict[str, list[str]]:
    """{run id: names of its failing checks} from a pull request's check rollup."""
    ids: dict[str, list[str]] = {}
    for c in rollup or []:
        if c.get("conclusion") in FAILING:
            m = RUN_URL.search(c.get("detailsUrl") or "")
            if m:
                ids.setdefault(m.group(1), []).append(c.get("name") or "")
    return ids


def attached_checks(rollup: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Failing checks that are not jobs of an Actions run: posted to the head by another workflow's check suite."""
    return [c for c in rollup or [] if c.get("conclusion") in FAILING and not RUN_URL.search(c.get("detailsUrl") or "")]


def failing_tests(log: str) -> list[str]:
    """Failing test names out of a failed-step log (pytest FAILED lines, vitest FAIL lines, Playwright failures)."""
    names = []
    for raw in log.splitlines():
        line = STAMP.sub("", raw.split("\t")[-1]).strip()
        match = TEST.search(line)
        if match:
            names.append(next(g for g in match.groups() if g).strip())
    return names
