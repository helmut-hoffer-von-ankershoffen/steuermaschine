"""Record real CI answers and the old tools' output once into tests/fixtures/ci (run by hand; the tests need no network).

    .venv/bin/python plugins/heinzel/tests/record_ci_fixtures.py PR RUN

Reads, read-only: the run (`gh run view RUN --json status,conclusion,jobs,workflowName,attempt,headSha`), its
failed-step log and the check rollup of the pull request. The log is cut down to the first lines of each job, every
error place with its context and the last lines; the job list to the failed jobs and two others. Then the old cause
module's analyze() and the old log tool's clean() and condense() run on exactly these recorded answers (their `gh`
reads are replaced by the recorded text; nothing else of them runs, no cache file is written).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent / "fixtures" / "ci"
TOOLS = Path("~/Code/heinzel-management/tools").expanduser()
REPO = "helmut-hoffer-von-ankershoffen/heinzel"


def load(name: str):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(TOOLS / name))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=False, timeout=120).stdout


def trim(log: str, err) -> str:
    lines = log.splitlines()
    keep: set[int] = set(range(3)) | set(range(len(lines) - 14, len(lines)))
    job = None
    for i, line in enumerate(lines):
        if line.split("\t")[0] != job:
            job = line.split("\t")[0]
            keep |= {i, i + 1}
        if err.search(line):
            keep |= set(range(i - 2, i + 3))
    kept = [lines[i] for i in sorted(keep) if 0 <= i < len(lines)]
    return "\n".join(re.sub(r"(RESULTS: (?:\w+ ){3}).*", lambda m: m[1].rstrip(), line) for line in kept) + "\n"  # the job result list, cut to three


def main(pr: str, run: str) -> None:
    sys.path.insert(0, str(TOOLS))
    import hz_ci_cause as old_cause

    old_log = load("hz-faillog")
    info = json.loads(gh("run", "view", run, "-R", REPO, "--json", "status,conclusion,jobs,workflowName,attempt,headSha"))
    failed = [j for j in info["jobs"] if j.get("conclusion") == "failure"]
    info["jobs"] = failed + [j for j in info["jobs"] if j.get("conclusion") != "failure"][:2]
    log = trim(gh("run", "view", run, "-R", REPO, "--log-failed"), old_log.ERR)
    rollup = json.loads(gh("pr", "view", pr, "-R", REPO, "--json", "statusCheckRollup"))
    rollup["statusCheckRollup"] = [c for c in rollup["statusCheckRollup"] if c.get("conclusion") != "SUCCESS" or c.get("name") in ("merge-gate", "Lint")]
    (HERE / f"run-{run}.json").write_text(json.dumps(info, indent=1) + "\n")
    (HERE / f"run-{run}.log").write_text(log)
    (HERE / f"pr-{pr}-rollup.json").write_text(json.dumps(rollup, indent=1) + "\n")
    answers = {"--json": json.dumps(info), "--log-failed": log}
    old_cause.sh = lambda args: next(text for flag, text in answers.items() if flag in args)
    line, rerun = old_cause.analyze(run, REPO)
    (HERE / f"run-{run}-old-cause.txt").write_text(line + ("\nrerun: " + rerun if rerun else "") + "\n")
    (HERE / f"run-{run}-old-faillog.txt").write_text("\n".join(old_log.condense(old_log.clean(log), 6, 12)) + "\n")
    print(f"recorded run {run} ({len(log)} bytes of log, {len(failed)} failed jobs) and PR {pr}: {line}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
