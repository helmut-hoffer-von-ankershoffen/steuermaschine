# Parity: `sm head coverage` against the former scoped-coverage script

Old: the launcher `tools/hz-scoped-cov` (6 lines of shell) and `tools/hz_scoped_cov.py` (25 lines, run inside the
worktree's interpreter under the old lock). New: `native/preflight_lib/scoped_cov.py` and the command
`sm head coverage WORKTREE SEL`. The admission area has no scoped coverage of its own, so the logic moved here.

## What was compared

The old script runs at import and cannot be called as a function, so nothing was run side by side. Compared by
reading, and pinned in `tests/test_native_preflight.py`:

| Behaviour | Result |
|---|---|
| production files: `git diff --name-only <base> HEAD -- src`, `.py` only | the same command |
| the test run: `<worktree>/.venv/bin/python -m coverage run --rcfile=<worktree>/pyproject.toml -m pytest -n 0 -q -p no:cacheprovider <SEL python>` with `COVERAGE_FILE` in a temporary directory | the same argv and environment, through the run lock |
| the analysis: the core's `scripts.focused_coverage._analyse(pyproject, files, out, datafile=...)` in the worktree's interpreter | the same call |
| per file `covered = executed lines + executed branches`, `total = covered + missing lines + missing branches`; the lines `FILE: p% (c/n) missing lines [...] missing branches [...]` and `SCOPED p% (c/n) threshold 90` | the same arithmetic and text (the loop is moved line by line) |

## What differs on purpose

1. One process became three calls (changed files, test run, analysis); only the test run holds a slot of the run
   lock. The old launcher held the lock for all of it.
2. Exit code: 1 below 90 % and 1 without a result (was: 0 whenever the tests passed; the threshold was only printed).
3. No Python file under `src/` changed: says so and exits 0 (was: a division by zero). A file without executable
   lines counts as 100 % (was: a division by zero).
4. The test output is shown only when there is no result (its last 30 lines); `--format json` prints `ScopedCoverage`.

## Not verified

No run in a real worktree with a real environment: the test run and the core's analysis were answered by fakes.
