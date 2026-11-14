"""Scoped coverage of a head: the hosted 'PR scoped coverage' check, reproduced locally (scripts/focused_coverage.py
semantics of the core).

Production = files under src/ changed between the base and HEAD; the denominator is every executable line plus every
branch arc of those WHOLE files; the selected tests run serially (-n 0) under `coverage run`, in the worktree's own
environment and through the run lock.

Moved from the former scoped-coverage script and its launcher. The arithmetic (`report`) is unchanged. The script ran
as one process inside the worktree's interpreter; here the three things it did are three calls: the changed files
(git), the test run under coverage (the worktree's python, through the run lock) and the analysis (the core's own
`_analyse`, in the worktree's python).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

THRESHOLD = 90
ANALYSE = ("import json, sys\nfrom pathlib import Path\nwt = Path(sys.argv[1])\nsys.path.insert(0, str(wt))\n"
           "from scripts.focused_coverage import _analyse\n"
           "_analyse(wt / 'pyproject.toml', json.loads(sys.argv[2]), Path(sys.argv[3]), datafile=sys.argv[4])\n")


def report(rep: dict[str, Any]) -> tuple[list[str], int, int]:
    """-> (one line per file, covered, total) of a coverage JSON report: lines plus branch arcs."""
    lines = []
    cov = tot = 0
    for f, r in rep["files"].items():
        ml, mb = r["missing_lines"], r["missing_branches"]
        c = len(r["executed_lines"]) + len(r["executed_branches"])
        n = c + len(ml) + len(mb)
        cov += c
        tot += n
        lines.append(f"{f}: {100 * c / n if n else 100.0:.2f}% ({c}/{n}) missing lines {ml} missing branches {mb}")
    return lines, cov, tot


def scoped_line(cov: int, tot: int) -> str:
    return f"SCOPED {100 * cov / tot:.3f}% ({cov}/{tot}) threshold {THRESHOLD}"


def production(sh: Any, wt: Path, base: str = "origin/main") -> list[str]:
    """The Python files under src/ that the head changes and keeps (a deleted file has nothing to measure)."""
    return [p for p in sh(["git", "diff", "--name-only", "--diff-filter=d", base, "HEAD", "--", "src"], cwd=wt)[1].split() if p.endswith(".py")]


def run(wt: Path, sel: Path, sh: Any, gate: Any, base: str = "origin/main", env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run the selection's python tests under coverage and measure the changed files. -> (exit code, output); the
    output ends with the `SCOPED n% (covered/total) threshold 90` line when there is a result.
    `sh(argv, cwd=None)` runs one process, `gate(argv, cwd=None, env=None)` one heavy command through the run lock;
    `env` is added to the environment of the test run (the coverage core, for one)."""
    try:
        selection = json.loads(Path(sel).read_text())["python"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return 2, f"unreadable selection {sel}: {e}"
    wt = Path(wt).resolve()
    python = str(wt / ".venv" / "bin" / "python")
    prod = production(sh, wt, base)
    with tempfile.TemporaryDirectory() as t:
        data = str(Path(t) / ".coverage")
        rc, out = gate([python, "-m", "coverage", "run", f"--rcfile={wt / 'pyproject.toml'}", "-m", "pytest", "-n", "0", "-q", "-p", "no:cacheprovider", *selection],
                       cwd=wt, env={**(env or {}), "COVERAGE_FILE": data})
        if rc != 0:
            return rc, out
        target = Path(t) / "cov.json"
        rc, said = sh([python, "-c", ANALYSE, str(wt), json.dumps(prod), str(target), data], cwd=wt)
        if rc != 0 or not target.is_file():
            return rc or 1, out + said
        rep = json.loads(target.read_text())
    lines, cov, tot = report(rep)
    if not tot:
        return 1, out + "\n".join(lines) + "\nno executable line in the changed files under src/: nothing to measure\n"
    return 0, out + "\n".join([*lines, scoped_line(cov, tot)]) + "\n"
