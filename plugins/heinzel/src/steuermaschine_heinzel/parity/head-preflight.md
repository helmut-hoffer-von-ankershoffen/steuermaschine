# Parity: `sm head preflight` against the former head preflight

Old: `tools/hz_preflight_head.py` (816 lines; `hz-preflight-head` was a two-line launcher). New:
`native/preflight_lib/head.py` (the checks, moved), `native/preflight.py` (the edge) and the core
`steuermaschine/preflight.py` (step record, ordered run, `--skip`, verdict, report).

## What was compared (2026-11-12)

The old module was imported by path (read only) and its functions were called in one process next to the new ones,
on the same inputs. Nothing old ran as a process. Inputs: five merged commits of the core repository, each against
its first parent (`23127697a` 41 files with a dependency change, `38676849f` 20 files, `f19a37822`, `6443f4b8c`,
`029715c23`), read with read-only git; the tree of each head extracted with `git archive`. The tools a check
starts (run lock, typos, commit-meta, scoped coverage, the GitHub read) were answered identically for both sides.

| Compared | Result |
|---|---|
| every step of `run()`: name, status, summary line, failures with fixes, data (tree, lint, typos, meta, coverage, dup, smells, deps, e2e, validation) | identical for all 5 heads after mapping the five texts listed below |
| skipped and opt-in-not-run lists, the verdict line | identical (`READY/NOT READY ... (opt-in not run: consumers, hosted)`) |
| the rendered report (old `render`, new `PreflightReport.render`) | identical apart from the first word of the title |
| `added_lines`, `hosted_lanes`, `dependency_changes`, `consumer_ref`, `cpd_exclusions` on the real diff, pyproject, ci.yml and Sonar properties of each head | identical |
| `driver_importers`, `e2e_owners` on all drivers (62 to 77) and journeys (44 to 47) of each head, as if 40 drivers had changed | identical |

Statuses seen: dup 0 of 2086 / 393 / 13 / 138 / 195 new lines, smells 2 / 1 / 0 / 0 / 0 findings, e2e owners
2 / 11 / 10 / 6 / 0, deps FAIL on the head that changed dependencies, coverage WARN at a 1-point margin.

Line fidelity (difflib, line by line): 583 of the 664 lines of `head.py` are lines of the old file; 43 further
old lines (the step record, the verdict) are in the core module. Not moved: the hosted step (35 lines), the
argument parser and `main` (40), `default_sh` and `tool` (14), `parse_run` and `validation_branch` (17).

## What differs on purpose

1. **The hosted step is gone from this command.** `--hosted` (throwaway commit, hosted lanes, branch deletion) is
   `sm head validate WORKTREE --lanes ...`, which holds the validation window and has every guard of the
   single-lane library. `head preflight` never pushes and never dispatches. When the head is READY and touches
   `web/` or `tests/e2e/`, the output ends with `next: sm head validate WORKTREE --lanes <lanes>`; the lanes come
   from the old `hosted_lanes()` (web-1, web-2; plus cli and supervised when the CLI head or the system module
   changed). Otherwise it ends with `next: sm head admit WORKTREE`. The verdict still says `hosted` under
   "opt-in not run". The summary no longer has a `hosted` step row.
2. Texts that named an old tool or switch: the title (`sm head preflight <sha> ...`), "the run lock gave no slot",
   "the commit-meta check failed", "scoped coverage gave no result", "run the full lanes with `sm head validate`",
   "throwaway commits (`sm head validate`)", "ask the coordinator to run this step with --allow-full".
3. `--json` is the root option `--format json`. The document is the result model `HeadPreflight`; the summary file
   next to SEL is the same document. Renamed keys: `tool` -> `command`, `argv` -> `arguments`, `sel` -> `selection`,
   `gh_calls` -> `github_calls`, failure `n` -> `number`. New keys: `title`, `hosted_lanes`, `next`, `summary_file`.
   `generated_at` has a colon in its offset (ISO 8601).
4. The full plugins `make check` of `--consumers` was allowed by an environment switch of the old lock; it is the
   option `--allow-full` now (logged by the run lock). Without it the lock refuses and the fix says so.
5. A mistyped `--skip` or `--consumers-target` refuses with exit 2 before anything runs (was: the argument parser).

## Edges replaced

| Old | New |
|---|---|
| `default_sh` (`subprocess.run`) | `providers.commands.run`; stdin (commit messages for typos) through a temporary file and a shell redirect, because the provider has no stdin |
| `hz-gate make lint`, `make build`, `make <target>` | the run lock (`lock_of().run(Request(...))`) with a runner that keeps the output for the report and runs in the step's directory |
| `hz-preflight-ci-meta BRANCH` (a process) | `preflight_lib/ci_meta.check` in process: see [head-meta.md](head-meta.md) |
| `hz-scoped-cov WORKTREE SEL` (a process) | `preflight_lib/scoped_cov.run`: see [head-coverage.md](head-coverage.md) |
| `gh api repos/R/commits/SHA/check-suites` | `steuermaschine.github.Gh(commands, repo).run([...])`: the same single read |
| `REPO`, `PLUGINS`, `BASE`, `~/Code/tmp-preflight-head` | settings `repo`, `preflight_plugins_repo`, `preflight_base`, `preflight_output_dir` |
| `time.strftime` | `providers.clock.now()` |

## Tests

All 23 tests of `tools/tests/test_preflight_head.py` moved to `tests/test_native_preflight_head.py` and run through
`sm --format json head preflight` (358 of 422 lines unchanged). Changed assertions, each marked `# was:` in the file:
the names of the fake's keys for the gate, meta and coverage; `gh_calls` -> `github_calls`; `HZ_ALLOW_FULL=1` ->
`--allow-full`; `m.REPO` -> the test's repository; `validation_branch` -> the core's `throwaway_branch` (same
result); the two `parse_run` assertions are gone with the function; the two tests of the hosted step became the two
tests of the `next:` line; the help test reads `sm head preflight --help`.
New: `tests/test_native_preflight.py` (the real edges on a scripted machine, text and json),
`tests/test_core_preflight.py` (the framework).

## Parity with the pre-push hook (2026-11-14, bead heinzel-management-98bw)

Added beyond the old tool, three checks so that no approved head fails the Core pre-push hook (`make check-fast`,
driven by `scripts/scoped_checks.py`) or the first CI lane for something preflight skipped. The code is in
`native/preflight_lib/head.py` (not a module of its own: a new file would widen the review digest of `head admit` past
its locality bound).
- `types`: the hook's own commands, read from `TYPES` in the hook script (mypy and pyright, strict; a WARN when the line
  cannot be parsed), over the whole project, for every change that is not web, desktop, plugins or documents only. The
  hook skips them for the root config files (it treats those as web inputs); this step runs them there too: a setting
  in pyproject.toml can break every file and the run is cheap.
- `standards`: the repository-wide standards tests, always. Core has no list file today, so the setting
  `preflight_standards` applies (six globs); the hook does not run them, this tool's rule does. A Core file
  `scripts/preflight_standards.txt` replaces the setting; then every pattern must match a file. An empty set fails.
- `related`: Core's `scripts/related_tests.py` run on the changed files, exactly its selection, uncapped (the hook has
  no cap); The script is read on standard output only (`2>/dev/null`; a uv notice on standard error once hid `ALL`); `ALL` runs
  the unit suite or fails, any line that is no test path fails, an empty clean answer runs the hook's own pin test. A
  head that touches web/ or desktop/ makes the hook run its whole gate: the step runs the selection of the other files
  and warns "the push hook runs the whole web gate; run `sm head validate --lanes web-1,web-2`" (coordinator waiver
  2026-11-14: such a head goes through hosted validation anyway). Extras (at most 30): integration tests naming a changed CLI
  or access module, `e2e_browser_output_test` for a changed `web/e2e/*.mjs`, `documentation_test` for changed CLI help.
  A Core without `related_tests.py` gets an `ast` import scan (relative imports, re-exports, transitive) and a warning.
Tests: `tests/test_native_preflight_parity.py`.

## Not verified

No live run against a real worktree: `make lint`, typos and the consumer build were answered by fakes in every test.
The three new checks are also only tested on fakes; the import scan is not the Core's own graph and may select more or fewer tests than it.
Checked against a real Core worktree (origin/main, 2026-11-14) with a driver script, not through the command.
