# ci flakes (read-only)

COVERS: ci flakes

Old: `tools/hz-flakes` (276 lines; `--days --bar --repo`, `--group --runs --short`). New: `native/flakes.py`,
`native/flakes_lib/count.py` (the decisions, moved), `native/flakes_lib/reads.py` (gh reads and cache), the test-name
parser shared with `ci why` (`ci_lib/flakes.py`, the same regular expressions as the old `TEST` and `STAMP`).

## Function by function
| Old | New | Change |
|---|---|---|
| `attempts_to_read`, `is_main_run`, `streak`, `macos_suite` | `count.py` | none |
| `day_rows(runs, details)` | `count.day_rows(runs)` | the unused `details` parameter dropped |
| `tally`, `group` | `count.tally`, `count.group` | `group` takes the readers as arguments; the thread pool and the choice of attempts (`wanted_attempts`) moved to the edge, same set |
| `read_attempt`, `run_jobs`, `gh` | `reads.Reads.attempt`, `.jobs`, `.gh` | gh through the Commands provider (120 s bound); cache directory is a setting |
| `report`, `group_report` | `FlakeDays.render`, `FlakeGroups.render` | the same format strings; top 12 / top 3 (`--short`) |
| `group_compact` (three lines for the old hz-next) | not ported | its only caller, the old `hz-next`, is replaced by `sm go`; `sm doc hour` calls `ci flakes` |
| `hzlib.resolve_main_runs` for the core repository | the merge queue's own (`queue_lib/hzlib.py`, moved), loaded as `ci main` loads it | none |

## Compared
- Expected texts in `test_native_ci_flakes.py` (`test_the_daily_count_in_the_former_format`,
  `test_group_in_the_former_format`) were derived from the old format strings for the same rows.
- Live, read-only, 2026-11-13: `sm ci flakes --group --short` against the core repository (10 main runs): 3 failing
  jobs, 1 of 10 each, streak 0 of 3 with 4 runs without a macOS suite skipped. The old tool was not run (the hook
  refuses the retired tools directory).
- The cache files have the old names and format (`<owner>_<repo>-<run>-<attempt>.json|.log`, `-<run>-jobs.json`); a copy
  of the old cache directory was read by the new command unchanged.

## What differs on purpose
1. Default `--repo` is the setting `repo` (the core repository), not a literal.
2. Cache: setting `ci_flakes_cache_dir`, default `cache/flakes` in the project's state directory (no old tool name in a
   state path). The old cache is not read; a new cache fills on first use (the first daily run reads every failed attempt
   of the window: minutes).
3. "Now" for `--days` comes from the clock provider.
4. `--group` with no runs: exit 1 with the message on stderr (old: `sys.exit(message)`, also exit 1).
5. `--format json`: one document (`FlakeDays` or `FlakeGroups`).
