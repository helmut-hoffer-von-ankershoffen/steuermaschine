"""Classify failed CI jobs and distinguish tool fetches from browser setup timeouts."""

from __future__ import annotations

import re

TOOLS = ("gitleaks", "actionlint", "pnpm", "electron", "uv", "gh", "vhs", "ttyd", "jq", "tools wheel")
NET = re.compile(r"download failed|HTTP (?:error )?5\d\d|\b5\d\d (?:Server|Bad|Service|Gateway)|read timed out|timed out|Connection reset|curl: \(\d+\)", re.I)
STEP = re.compile(r"Install the locked Electron package", re.I)
ELECTRON_PACKAGE_STEP = "Build and verify unsigned native package"
ELECTRON_CHECKSUM_GATEWAY = re.compile(
    r"HTTPError: Response code (?:502|503|504) \([^\r\n]*\) for "
    r"https://github\.com/electron/electron/releases/download/v\d+\.\d+\.\d+/SHASUMS256\.txt(?:\s|$)"
)
TESTY = re.compile(r"(?:^E\s{2,})|\bFAILED\b.*::|AssertionError|error TS\d+|\d+ failed", re.M)
PLAYWRIGHT_DEPS_TIMEOUT = re.compile(
    r"subprocess\.TimeoutExpired: Command '[^\r\n]*, '-m', 'playwright', 'install', "
    r"'chromium', 'webkit', '--with-deps'\]' timed out after \d+ seconds"
)
APT_FETCH_STALL = re.compile(r"(?:^|[ \t])(?:Ign|Err):\d+ https?://", re.M)
HARD = ("failure", "timed_out", "startup_failure")


def tool_of(text: str) -> str | None:
    low = text.lower()
    return next((t for t in TOOLS if re.search(r"\b" + re.escape(t) + r"\b", low)), None)


def classify(step: str, log: str) -> tuple[str, str | None]:
    """Return the failure kind and, when identified, the tool or subsystem."""
    if STEP.search(step):
        return "DOWNLOAD", "electron"
    if step == ELECTRON_PACKAGE_STEP and ELECTRON_CHECKSUM_GATEWAY.search(log) and not TESTY.search(log):
        return "DOWNLOAD", "electron"
    if timeout := PLAYWRIGHT_DEPS_TIMEOUT.search(log):
        _, marker, dependency_log = log[:timeout.start()].rpartition("Installing dependencies...")
        source = "apt" if marker and APT_FETCH_STALL.search(dependency_log) else "playwright"
        return "BROWSER_SETUP_TIMEOUT", source
    if NET.search(log):
        t = tool_of(step) or tool_of(log)
        if t and not (TESTY.search(log) and not tool_of(step)):
            return "DOWNLOAD", t
    if TESTY.search(log):
        return "TEST", None
    return "UNKNOWN", None


def verdict(jobs: dict[str, tuple[str, str | None]], run_id: str | int) -> tuple[str, bool]:
    """jobs: {job: (kind, tool)} -> (one-line verdict, whether a rerun of the failed jobs is the next step: only when
    every failure is a download)."""
    if not jobs:
        return f"run {run_id}: no failed jobs", False
    jobs = {j: kt for j, kt in jobs.items() if kt[0] != "AGGREGATE"} or jobs
    parts = [f"{j[:40]}={k}" + (f"({t})" if t else "") for j, (k, t) in jobs.items()]
    if all(k == "DOWNLOAD" for k, _ in jobs.values()):
        return f"run {run_id}: all DOWNLOAD -> " + ", ".join(parts), True
    return f"run {run_id}: not all download, no rerun -> " + ", ".join(parts), False


def split_log(text: str) -> dict[tuple[str, str], list[str]]:
    """The failed-step log of a run (job<TAB>step<TAB>line) by (job, step)."""
    logs: dict[tuple[str, str], list[str]] = {}
    for line in text.splitlines():
        p = line.split("\t", 2)
        if len(p) == 3:
            logs.setdefault((p[0], p[1]), []).append(p[2])
    return logs


UNNAMED_STEP = "UNKNOWN STEP"  # what the step column of a failed-step log says when the log does not name the steps
AGGREGATE = "CI OK"  # the aggregate job of the CI run: red whenever another job is, it repeats the real failures
HOSTED_ONLY = (
    (re.compile(r"scoped coverage below the \d+% report threshold"), "scoped coverage under the floor (a hosted-only gate): cover the changed lines; sm head preflight shows it before admission"),
)


def classify_jobs(jobs: list[dict], logs: dict[tuple[str, str], list[str]]) -> dict[str, tuple[str, str | None]]:
    """{job name: (kind, tool)} for the failed jobs of a run. A job without log lines is UNKNOWN; a job with several
    failed steps is a download only when every one is. When the log does not name the steps (the step column says
    UNKNOWN STEP), those lines of the job are judged against each failed step name. The aggregate job is AGGREGATE
    when another job failed too."""
    failed = [j for j in jobs if j.get("conclusion") == "failure"]
    out: dict[str, tuple[str, str | None]] = {}
    for j in failed:
        if j["name"] == AGGREGATE and len(failed) > 1:
            out[j["name"]] = ("AGGREGATE", None)
            continue
        fsteps = [s["name"] for s in j.get("steps", []) if s.get("conclusion") == "failure"] or [""]
        whole = logs.get((j["name"], UNNAMED_STEP), [])
        by_step = {s: logs.get((j["name"], s)) or whole for s in fsteps}
        res = [classify(s, "\n".join(by_step[s])) for s in fsteps]
        if not any(by_step.values()):
            res = [("UNKNOWN", None)]
        out[j["name"]] = next((r for r in res if r[0] != "DOWNLOAD"), res[0])
    return out


def hosted_only_hint(log: str) -> str:
    """What a log line says about a gate that only hosted CI judges, or ""."""
    return next((text for pattern, text in HOSTED_ONLY if pattern.search(log)), "")
